from dataclasses import dataclass
import getpass

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from registration_parser import parse_enlist_arguments, parse_registration_tables


LOGIN_URL = "https://student.upmin.edu.ph/index.php?go=dp_student&task=index"
REGISTRATION_URL = "index.php?go=studentaccess&task=index"
LOGIN_TIMEOUT = 30
RESULT_TIMEOUT = 8


class LoginError(Exception):
    pass


class RegistrationPageError(Exception):
    pass


@dataclass
class PortalSession:
    driver: webdriver.Chrome
    tables: list[dict]
    subjects_by_key: dict[str, dict]

    def public_tables(self) -> list[dict]:
        return [
            {
                "category": table["category"],
                "headers": table["headers"],
                "subjects": [
                    {
                        "key": subject["key"],
                        "cells": subject["cells"],
                        "selectable": subject["selectable"],
                        "reason": subject["reason"],
                        "action_label": subject["action_label"],
                    }
                    for subject in table["subjects"]
                ],
            }
            for table in self.tables
        ]

    def close(self) -> None:
        self.driver.quit()


def start_portal_session(username: str, password: str) -> PortalSession:
    if not username.strip() or not password:
        raise ValueError("Username and password are required.")

    options = webdriver.ChromeOptions()
    options.add_argument("--remote-debugging-port=0")
    driver = webdriver.Chrome(options=options)
    keep_driver = False
    try:
        driver.get(LOGIN_URL)
        wait = WebDriverWait(driver, LOGIN_TIMEOUT)

        try:
            username_field = wait.until(
                EC.element_to_be_clickable((By.ID, "username"))
            )
            password_field = wait.until(
                EC.element_to_be_clickable((By.NAME, "password"))
            )
        except TimeoutException as exc:
            raise LoginError("The student login form did not appear.") from exc

        username_field.send_keys(username)
        password_field.send_keys(password)
        wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="submit"]'))
        ).click()

        try:
            registration_menu = wait.until(
                EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, 'a[rel="registration"]')
                )
            )
        except TimeoutException as exc:
            raise LoginError(
                "Login failed or the Registration menu did not appear."
            ) from exc

        ActionChains(driver).move_to_element(registration_menu).perform()
        try:
            registration_link = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, f'a[href="{REGISTRATION_URL}"]')
                )
            )
        except TimeoutException as exc:
            raise RegistrationPageError(
                "The Registration page link was not available after login."
            ) from exc

        registration_link.click()
        try:
            wait.until(
                lambda browser: "go=studentaccess" in browser.current_url
                and "task=index" in browser.current_url
            )
            wait.until(
                lambda browser: bool(
                    parse_registration_tables(browser.page_source)
                )
            )
        except TimeoutException as exc:
            raise RegistrationPageError(
                "The Registration page did not load its subject tables."
            ) from exc

        tables = parse_registration_tables(driver.page_source)
        categories = {table["category"] for table in tables}
        missing = {"PE", "NSTP"} - categories
        if missing:
            names = ", ".join(sorted(missing))
            raise RegistrationPageError(
                f"The live Registration page is missing the {names} subject table(s)."
            )
        subjects_by_key: dict[str, dict] = {}
        for table in tables:
            for subject in table["subjects"]:
                key = subject["key"]
                if key in subjects_by_key:
                    raise RegistrationPageError(
                        "The live subject table contains duplicate row keys."
                    )
                subjects_by_key[key] = {
                    **subject,
                    "category": table["category"],
                    "headers": table["headers"],
                }

        session = PortalSession(driver, tables, subjects_by_key)
        keep_driver = True
        return session
    finally:
        if not keep_driver:
            driver.quit()


def _classify_feedback(message: str) -> str | None:
    text = " ".join(message.casefold().split())
    if not text:
        return None
    if "already" in text and any(
        word in text for word in ("enlist", "add", "register", "enroll")
    ):
        return "already_enlisted"
    if any(
        word in text
        for word in ("not allowed", "not eligible", "full", "closed", "error", "failed", "denied")
    ):
        return "failed"
    if any(
        phrase in text
        for phrase in (
            "successfully enlisted",
            "enlistment successful",
            "successfully added",
            "added successfully",
            "successfully registered",
            "registration successful",
            "successfully enrolled",
            "enrollment successful",
        )
    ):
        return "success"
    return None


def _visible_feedback(driver: webdriver.Chrome) -> list[str]:
    messages = []
    for element in driver.find_elements(
        By.CSS_SELECTOR,
        "[role='alert'], .alert, .message, .notification",
    ):
        if element.is_displayed():
            text = element.text.strip()
            if text:
                messages.append(text)
    return messages


def _is_enlist_confirmation_prompt(message: str) -> bool:
    text = message.casefold()
    asks_to_confirm = (
        "are you sure" in text
        or "confirm" in text
        or "proceed" in text
        or "?" in text
    )
    names_action = any(word in text for word in ("enlist", "add", "register", "enroll"))
    return asks_to_confirm and names_action


def _classify_portal_response(
    driver: webdriver.Chrome,
    previous_messages: list[str],
) -> tuple[str, str]:
    try:
        alert = WebDriverWait(driver, 2).until(EC.alert_is_present())
    except TimeoutException:
        alert = None

    if alert is not None:
        alert_text = alert.text.strip()
        status = _classify_feedback(alert_text)
        if status is not None:
            alert.accept()
            return status, alert_text[:300]
        if not _is_enlist_confirmation_prompt(alert_text):
            alert.dismiss()
            return "unconfirmed", (
                alert_text[:300] or "The portal displayed an unrecognized dialog."
            )

        alert.accept()
        try:
            alert = WebDriverWait(driver, RESULT_TIMEOUT).until(
                EC.alert_is_present()
            )
        except TimeoutException:
            alert = None
        if alert is not None:
            alert_text = alert.text.strip()
            status = _classify_feedback(alert_text)
            alert.accept()
            if status is not None:
                return status, alert_text[:300]
            return "unconfirmed", (
                alert_text[:300] or "The portal dialog did not confirm the result."
            )

    def new_feedback(browser: webdriver.Chrome) -> str | bool:
        current_messages = _visible_feedback(browser)
        for message in current_messages:
            if message not in previous_messages:
                return message
        return False

    try:
        message = WebDriverWait(driver, RESULT_TIMEOUT).until(new_feedback)
    except TimeoutException:
        return (
            "unconfirmed",
            "No explicit result confirmation appeared; remaining subjects were not attempted.",
        )

    status = _classify_feedback(message)
    if status is None:
        return "unconfirmed", message[:300]
    return status, message[:300]


def _find_live_add_control(
    driver: webdriver.Chrome,
    subject: dict,
) -> webdriver.remote.webelement.WebElement | None:
    matches = []
    for element in driver.find_elements(By.CSS_SELECTOR, "[onclick]"):
        args = parse_enlist_arguments(element.get_attribute("onclick") or "")
        if args != subject["enlist_args"]:
            continue
        try:
            row = element.find_element(By.XPATH, "./ancestor::tr[1]")
            if (
                element.is_displayed()
                and element.is_enabled()
                and "not allowed" not in row.text.casefold()
            ):
                matches.append(element)
        except StaleElementReferenceException:
            continue
    if len(matches) != 1:
        return None
    return matches[0]


def _attempt_subject(session: PortalSession, subject: dict) -> tuple[str, str]:
    driver = session.driver
    try:
        if "go=login" in driver.current_url or driver.find_elements(
            By.ID, "username"
        ):
            return (
                "session_lost",
                "The portal session ended before this subject was attempted.",
            )

        current_subjects = {
            row["key"]: row
            for table in parse_registration_tables(driver.page_source)
            for row in table["subjects"]
        }
        current = current_subjects.get(subject["key"])
        if current is None:
            return "unconfirmed", "The subject row disappeared from the live page."
        if not current["selectable"]:
            return "failed", "This subject is no longer available on the live page."
        if current["enlist_args"] != subject["enlist_args"]:
            return "unconfirmed", "The live enlist action changed; no action was taken."

        control = _find_live_add_control(driver, current)
        if control is None:
            return "unconfirmed", "The live Add control was missing or ambiguous."

        previous_messages = _visible_feedback(driver)
        try:
            control.click()
        except UnexpectedAlertPresentException:
            pass
        except WebDriverException:
            return "unconfirmed", "The live Add control could not be activated safely."
        return _classify_portal_response(driver, previous_messages)
    except WebDriverException:
        return "unconfirmed", "The portal state could not be verified safely."


def run_selected_enlistments(
    session: PortalSession,
    selected_keys: list[str],
) -> list[dict]:
    if not selected_keys:
        raise ValueError("Select at least one subject.")
    if len(set(selected_keys)) != len(selected_keys):
        raise ValueError("Duplicate subject selections are not allowed.")

    selected_subjects = []
    for key in selected_keys:
        subject = session.subjects_by_key.get(key)
        if subject is None or not subject["selectable"]:
            raise ValueError("A selected subject is not available in this session.")
        selected_subjects.append(subject)

    results: list[dict] = []
    for index, subject in enumerate(selected_subjects):
        result_status, message = _attempt_subject(session, subject)

        results.append(
            {
                "key": subject["key"],
                "category": subject["category"],
                "cells": subject["cells"],
                "status": result_status,
                "message": message,
            }
        )

        if result_status in {"unconfirmed", "session_lost"}:
            for not_attempted in selected_subjects[index + 1 :]:
                results.append(
                    {
                        "key": not_attempted["key"],
                        "category": not_attempted["category"],
                        "cells": not_attempted["cells"],
                        "status": "not_attempted",
                        "message": "Stopped because the previous result was uncertain.",
                    }
                )
            break

    return results


def main() -> None:
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    session = start_portal_session(username, password)
    try:
        counts = {
            category: sum(
                len(table["subjects"])
                for table in session.tables
                if table["category"] == category
            )
            for category in ("PE", "NSTP")
        }
        print(
            "Registration page loaded: "
            f"{counts['PE']} PE rows, {counts['NSTP']} NSTP rows."
        )
        input("Press Enter to close the browser...")
    finally:
        session.close()


if __name__ == "__main__":
    main()
