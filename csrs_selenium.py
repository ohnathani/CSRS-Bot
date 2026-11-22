from dataclasses import dataclass
from collections.abc import Iterable
import getpass
from threading import Barrier, BrokenBarrierError, Event
from typing import Callable, TypeVar

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from registration_parser import parse_enlist_arguments, parse_registration_tables


LOGIN_URL = "https://student.upmin.edu.ph/index.php?go=dp_student&task=index"
REGISTRATION_URL = "index.php?go=studentaccess&task=index"
LOGIN_RETRY_INTERVAL = 5
RESULT_TIMEOUT = 8
LOGIN_REJECTION_MARKERS = (
    "invalid username",
    "invalid password",
    "incorrect username",
    "incorrect password",
    "wrong username",
    "wrong password",
    "invalid credentials",
    "incorrect credentials",
    "authentication failed",
    "login failed",
    "account not found",
    "user not found",
)
WaitResult = TypeVar("WaitResult")


class LoginError(Exception):
    def __init__(
        self,
        message: str,
        driver: webdriver.Chrome | None = None,
    ) -> None:
        super().__init__(message)
        self.driver = driver


class RegistrationPageError(Exception):
    def __init__(
        self,
        message: str,
        driver: webdriver.Chrome | None = None,
    ) -> None:
        super().__init__(message)
        self.driver = driver


class PortalAttemptCancelled(Exception):
    def __init__(self, driver: webdriver.Chrome | None) -> None:
        super().__init__("This Selenium attempt was cancelled.")
        self.driver = driver


class PortalAttemptError(WebDriverException):
    def __init__(self, message: str, driver: webdriver.Chrome) -> None:
        super().__init__(message)
        self.driver = driver


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


def _wait_until_available(
    driver: webdriver.Chrome,
    condition: Callable[[webdriver.Chrome], WaitResult],
    cancel_event: Event | None = None,
    on_timeout: Callable[[], None] | None = None,
) -> WaitResult:
    def cancellable_condition(browser: webdriver.Chrome) -> WaitResult:
        if cancel_event is not None and cancel_event.is_set():
            raise PortalAttemptCancelled(driver)
        return condition(browser)

    while True:
        try:
            return WebDriverWait(driver, LOGIN_RETRY_INTERVAL).until(
                cancellable_condition
            )
        except TimeoutException:
            if on_timeout is not None:
                on_timeout()


def _has_explicit_login_rejection(driver: webdriver.Chrome) -> bool:
    body_elements = driver.find_elements(By.TAG_NAME, "body")
    if not body_elements:
        return False
    try:
        body_text = " ".join(body_elements[0].text.casefold().split())
    except StaleElementReferenceException:
        return False
    return any(marker in body_text for marker in LOGIN_REJECTION_MARKERS)


def _first_visible_enabled(elements: Iterable[WebElement]) -> WebElement | None:
    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except StaleElementReferenceException:
            continue
    return None


def _load_portal_session(
    driver: webdriver.Chrome,
    username: str,
    password: str,
    cancel_event: Event | None,
    on_registration_ready: Callable[[], None] | None,
    credentials_barrier: Barrier | None,
) -> PortalSession:
    username_field = _wait_until_available(
        driver,
        EC.element_to_be_clickable((By.ID, "username")),
        cancel_event,
    )
    password_field = _wait_until_available(
        driver,
        EC.element_to_be_clickable((By.NAME, "password")),
        cancel_event,
    )
    if credentials_barrier is not None:
        try:
            credentials_barrier.wait()
        except BrokenBarrierError as exc:
            raise PortalAttemptError(
                "A Selenium window could not join the credential queue.",
                driver,
            ) from exc

    username_field.send_keys(username)
    password_field.send_keys(password)
    _wait_until_available(
        driver,
        EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="submit"]')),
        cancel_event,
    ).click()

    login_prompt_cleared = False

    def reject_explicit_login_failure() -> None:
        nonlocal username_field, login_prompt_cleared

        if cancel_event is not None and cancel_event.is_set():
            raise PortalAttemptCancelled(driver)
        if _has_explicit_login_rejection(driver):
            raise LoginError(
                "The portal rejected the login credentials.",
                driver,
            )
        if not login_prompt_cleared:
            try:
                login_prompt_cleared = not username_field.is_displayed()
            except StaleElementReferenceException:
                login_prompt_cleared = True
        if not login_prompt_cleared:
            return

        username_prompt = _first_visible_enabled(
            driver.find_elements(By.ID, "username")
        )
        password_prompt = _first_visible_enabled(
            driver.find_elements(By.NAME, "password")
        )
        submit_prompt = _first_visible_enabled(
            driver.find_elements(By.CSS_SELECTOR, 'input[type="submit"]')
        )
        if (
            username_prompt is None
            or password_prompt is None
            or submit_prompt is None
        ):
            return
        if cancel_event is not None and cancel_event.is_set():
            raise PortalAttemptCancelled(driver)

        username_prompt.send_keys(username)
        password_prompt.send_keys(password)
        submit_prompt.click()
        username_field = username_prompt
        login_prompt_cleared = False

    registration_menu = _wait_until_available(
        driver,
        EC.visibility_of_element_located(
            (By.CSS_SELECTOR, 'a[rel="registration"]')
        ),
        cancel_event,
        reject_explicit_login_failure,
    )

    ActionChains(driver).move_to_element(registration_menu).perform()
    registration_link = _wait_until_available(
        driver,
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, f'a[href="{REGISTRATION_URL}"]')
        ),
        cancel_event,
    )

    registration_link.click()

    def registration_tables_loaded(browser: webdriver.Chrome) -> bool:
        if (
            "go=studentaccess" not in browser.current_url
            or "task=index" not in browser.current_url
        ):
            return False
        categories = {
            table["category"]
            for table in parse_registration_tables(browser.page_source)
        }
        return {"PE", "NSTP"}.issubset(categories)

    _wait_until_available(
        driver,
        registration_tables_loaded,
        cancel_event,
    )

    tables = parse_registration_tables(driver.page_source)
    categories = {table["category"] for table in tables}
    missing = {"PE", "NSTP"} - categories
    if missing:
        names = ", ".join(sorted(missing))
        raise RegistrationPageError(
            f"The live Registration page is missing the {names} subject table(s).",
            driver,
        )
    subjects_by_key: dict[str, dict] = {}
    for table in tables:
        for subject in table["subjects"]:
            key = subject["key"]
            if key in subjects_by_key:
                raise RegistrationPageError(
                    "The live subject table contains duplicate row keys.",
                    driver,
                )
            subjects_by_key[key] = {
                **subject,
                "category": table["category"],
                "headers": table["headers"],
            }

    session = PortalSession(driver, tables, subjects_by_key)
    if on_registration_ready is not None:
        on_registration_ready()
    return session


def start_portal_session(
    username: str,
    password: str,
    cancel_event: Event | None = None,
    on_registration_ready: Callable[[], None] | None = None,
    launch_barrier: Barrier | None = None,
    credentials_barrier: Barrier | None = None,
    window_rect: tuple[int, int, int, int] | None = None,
    window_grid_position: tuple[int, int] | None = None,
) -> PortalSession:
    if not username.strip() or not password:
        raise ValueError("Username and password are required.")
    if cancel_event is not None and cancel_event.is_set():
        raise PortalAttemptCancelled(None)

    options = webdriver.ChromeOptions()
    options.add_argument("--remote-debugging-port=0")
    options.add_experimental_option("detach", True)
    try:
        driver = webdriver.Chrome(options=options)
    except Exception:
        if launch_barrier is not None:
            launch_barrier.abort()
        raise
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise PortalAttemptCancelled(driver)
        if window_rect is not None:
            x, y, width, height = window_rect
            driver.set_window_rect(x=x, y=y, width=width, height=height)
            if window_grid_position is not None:
                actual_rect = driver.get_window_rect()
                column, row = window_grid_position
                x += column * max(0, actual_rect["width"] - width)
                y += row * max(0, actual_rect["height"] - height)
                if x != window_rect[0] or y != window_rect[1]:
                    driver.set_window_position(x=x, y=y)
        if launch_barrier is not None:
            launch_barrier.wait()
        driver.set_page_load_timeout(30)
        try:
            driver.get(LOGIN_URL)
        except TimeoutException:
            pass
        return _load_portal_session(
            driver,
            username,
            password,
            cancel_event,
            on_registration_ready,
            credentials_barrier,
        )
    except (
        LoginError,
        PortalAttemptCancelled,
        PortalAttemptError,
        RegistrationPageError,
    ):
        raise
    except BrokenBarrierError as exc:
        raise PortalAttemptError(
            "A Selenium window could not join the launch group.",
            driver,
        ) from exc
    except WebDriverException as exc:
        raise PortalAttemptError(str(exc), driver) from exc


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
    input("Press Enter to exit; the Selenium browser will remain open...")


if __name__ == "__main__":
    main()
