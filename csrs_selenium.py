import json
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGIN_URL = "https://student.upmin.edu.ph/index.php?go=dp_student&task=index"
CREDENTIALS_PATH = Path(__file__).with_name("credentials.json")


def load_credentials() -> tuple[str, str]:
    try:
        with CREDENTIALS_PATH.open(encoding="utf-8") as file:
            credentials = json.load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_PATH}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{CREDENTIALS_PATH.name} must contain valid JSON "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(credentials, dict):
        raise ValueError("credentials.json must contain a JSON object.")

    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("credentials.json must have a non-empty 'username' string.")
    if not isinstance(password, str) or not password:
        raise ValueError("credentials.json must have a non-empty 'password' string.")

    return username, password


def run_bot(username: str, password: str) -> None:
    driver = webdriver.Chrome()
    try:
        driver.get(LOGIN_URL)
        wait = WebDriverWait(driver, 20)

        username_field = wait.until(
            EC.element_to_be_clickable((By.ID, "username"))
        )
        password_field = wait.until(
            EC.element_to_be_clickable((By.NAME, "password"))
        )
        username_field.send_keys(username)
        password_field.send_keys(password)

        submit_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[type="submit"]'))
        )
        submit_button.click()

        registration_link = wait.until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, 'a[rel="registration"]')
            )
        )
        ActionChains(driver).move_to_element(registration_link).perform()

        student_access_link = wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    'a[href="index.php?go=studentaccess&task=index"]',
                )
            )
        )
        student_access_link.click()
        wait.until(
            lambda browser: "go=studentaccess" in browser.current_url
            and "task=index" in browser.current_url
        )

        print("Opened the Registration page. Check the Chrome window.")
        input("Press Enter to close the browser...")
    finally:
        driver.quit()


def main() -> None:
    username, password = load_credentials()
    run_bot(username, password)


if __name__ == "__main__":
    main()
