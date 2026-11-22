import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException
import csrs_selenium
from csrs_selenium import (
    LoginError,
    PortalSession,
    _classify_feedback,
    run_selected_enlistments,
    start_portal_session,
)


def _subject(key: str) -> dict:
    return {
        "key": key,
        "cells": [f"PE {key}", "A", "MWF", "8:00 AM"],
        "selectable": True,
        "reason": None,
        "enlist_args": ("student-id", key, f"PE {key}"),
        "category": "PE",
        "action_label": "Click to add",
    }


class SelectedEnlistmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subjects = [_subject("one"), _subject("two"), _subject("three")]
        self.session = PortalSession(
            driver=None,
            tables=[],
            subjects_by_key={subject["key"]: subject for subject in self.subjects},
        )

    def test_explicit_login_rejection_stops_retry_and_keeps_browser_open(self) -> None:
        driver = Mock()
        body = Mock()
        body.text = "Invalid username or password."
        driver.find_elements.return_value = [body]
        username_field = Mock()
        password_field = Mock()
        submit_button = Mock()
        with (
            patch("csrs_selenium.webdriver.Chrome", return_value=driver),
            patch("csrs_selenium.WebDriverWait") as wait,
        ):
            wait.return_value.until.side_effect = [
                username_field,
                password_field,
                submit_button,
                TimeoutException(),
            ]
            with self.assertRaises(LoginError) as error:
                start_portal_session("student", "password")

        self.assertEqual(
            str(error.exception),
            "The portal rejected the login credentials.",
        )
        self.assertIs(error.exception.driver, driver)
        driver.quit.assert_not_called()

    def test_driver_is_detached_so_browser_stays_open_after_app_exit(self) -> None:
        driver = Mock()
        options = Mock()
        with (
            patch("csrs_selenium.webdriver.ChromeOptions", return_value=options),
            patch("csrs_selenium.webdriver.Chrome", return_value=driver),
            patch("csrs_selenium.WebDriverWait") as wait,
        ):
            body = Mock()
            body.text = "Invalid username or password."
            driver.find_elements.return_value = [body]
            wait.return_value.until.side_effect = [
                Mock(),
                Mock(),
                Mock(),
                TimeoutException(),
            ]
            with self.assertRaises(LoginError):
                start_portal_session("student", "password")

        options.add_experimental_option.assert_called_once_with("detach", True)
        driver.quit.assert_not_called()

    def test_driver_waits_at_launch_barrier_before_navigating(self) -> None:
        driver = Mock()
        body = Mock()
        body.text = "Invalid username or password."
        driver.find_elements.return_value = [body]
        events = []
        launch_barrier = Mock()
        launch_barrier.wait.side_effect = lambda: events.append("launch barrier")
        credentials_barrier = Mock()
        credentials_barrier.wait.side_effect = lambda: events.append(
            "credentials barrier"
        )
        username_field = Mock()
        username_field.send_keys.side_effect = lambda _value: events.append(
            "username"
        )
        password_field = Mock()
        password_field.send_keys.side_effect = lambda _value: events.append(
            "password"
        )
        submit_button = Mock()
        with (
            patch(
                "csrs_selenium.webdriver.Chrome",
                side_effect=lambda **_kwargs: (
                    events.append("created"),
                    driver,
                )[1],
            ),
            patch("csrs_selenium.WebDriverWait") as wait,
        ):
            driver.get_window_rect.return_value = {"width": 516, "height": 540}
            driver.set_window_rect.side_effect = lambda **_rect: events.append(
                "position"
            )
            driver.set_window_position.side_effect = lambda **_position: events.append(
                "reposition"
            )
            driver.get.side_effect = lambda _url: events.append("navigate")
            wait.return_value.until.side_effect = [
                username_field,
                password_field,
                submit_button,
                TimeoutException(),
            ]
            with self.assertRaises(LoginError):
                start_portal_session(
                    "student",
                    "password",
                    launch_barrier=launch_barrier,
                    credentials_barrier=credentials_barrier,
                    window_rect=(480, 0, 480, 540),
                    window_grid_position=(1, 0),
                )

        self.assertEqual(
            events[:8],
            [
                "created",
                "position",
                "reposition",
                "launch barrier",
                "navigate",
                "credentials barrier",
                "username",
                "password",
            ],
        )
        driver.set_window_rect.assert_called_once_with(
            x=480,
            y=0,
            width=480,
            height=540,
        )
        driver.set_window_position.assert_called_once_with(x=516, y=0)
        launch_barrier.wait.assert_called_once()
        credentials_barrier.wait.assert_called_once()
        driver.quit.assert_not_called()

    def test_transient_timeouts_retry_until_registration_loads(self) -> None:
        driver = Mock()
        username_field = Mock()
        password_field = Mock()
        submit_button = Mock()
        registration_menu = Mock()
        registration_link = Mock()
        driver.find_elements.return_value = []
        tables = [
            {"category": "PE", "headers": [], "subjects": []},
            {"category": "NSTP", "headers": [], "subjects": []},
        ]
        with (
            patch("csrs_selenium.webdriver.Chrome", return_value=driver),
            patch("csrs_selenium.WebDriverWait") as wait,
            patch("csrs_selenium.ActionChains"),
            patch("csrs_selenium.parse_registration_tables", return_value=tables),
        ):
            wait.return_value.until.side_effect = [
                TimeoutException(),
                username_field,
                password_field,
                submit_button,
                TimeoutException(),
                registration_menu,
                registration_link,
                TimeoutException(),
                True,
            ]
            session = start_portal_session("student", "password")

        self.assertIs(session.driver, driver)
        self.assertEqual(session.tables, tables)
        self.assertTrue(
            all(call.args == (driver, 5) for call in wait.call_args_list)
        )
        username_field.send_keys.assert_called_once_with("student")
        password_field.send_keys.assert_called_once_with("password")
        driver.quit.assert_not_called()

    def test_credentials_are_reentered_after_a_new_login_form_appears(self) -> None:
        driver = Mock()
        new_username_field = Mock()
        new_password_field = Mock()
        new_submit = Mock()
        driver.find_elements.side_effect = [
            [],
            [new_username_field],
            [new_password_field],
            [new_submit],
        ]
        username_field = Mock()
        username_field.is_displayed.return_value = False
        password_field = Mock()
        initial_submit = Mock()
        registration_menu = Mock()
        registration_link = Mock()
        tables = [
            {"category": "PE", "headers": [], "subjects": []},
            {"category": "NSTP", "headers": [], "subjects": []},
        ]
        clickable_conditions = [
            lambda _driver: username_field,
            lambda _driver: password_field,
            lambda _driver: initial_submit,
            lambda _driver: registration_link,
        ]
        with (
            patch("csrs_selenium.webdriver.Chrome", return_value=driver),
            patch("csrs_selenium.WebDriverWait") as wait,
            patch(
                "csrs_selenium.EC.element_to_be_clickable",
                side_effect=clickable_conditions,
            ),
            patch("csrs_selenium.ActionChains"),
            patch("csrs_selenium.parse_registration_tables", return_value=tables),
        ):
            wait.return_value.until.side_effect = [
                username_field,
                password_field,
                initial_submit,
                TimeoutException(),
                registration_menu,
                registration_link,
                True,
            ]
            session = start_portal_session("student", "password")

        self.assertIs(session.driver, driver)
        username_field.send_keys.assert_called_once_with("student")
        password_field.send_keys.assert_called_once_with("password")
        new_username_field.send_keys.assert_called_once_with("student")
        new_password_field.send_keys.assert_called_once_with("password")
        initial_submit.click.assert_called_once()
        new_submit.click.assert_called_once()
        driver.quit.assert_not_called()

    def test_rejects_empty_unknown_and_duplicate_selections(self) -> None:
        with patch("csrs_selenium._attempt_subject") as attempt:
            for keys in ([], ["unknown"], ["one", "one"]):
                with self.subTest(keys=keys):
                    with self.assertRaises(ValueError):
                        run_selected_enlistments(self.session, keys)
            attempt.assert_not_called()

    def test_stops_after_unconfirmed_result_but_reports_remaining_keys(self) -> None:
        with patch(
            "csrs_selenium._attempt_subject",
            side_effect=[
                ("failed", "Course full."),
                ("unconfirmed", "No portal confirmation."),
            ],
        ) as attempt:
            results = run_selected_enlistments(
                self.session,
                ["one", "two", "three"],
            )

        self.assertEqual(
            [result["status"] for result in results],
            ["failed", "unconfirmed", "not_attempted"],
        )
        self.assertEqual(attempt.call_count, 2)

    def test_success_requires_known_explicit_success_feedback(self) -> None:
        self.assertEqual(
            _classify_feedback("Enlistment successful."),
            "success",
        )
        self.assertIsNone(_classify_feedback("Request received."))

    def test_public_subject_data_does_not_include_enlist_arguments(self) -> None:
        self.session.tables = [
            {
                "category": "PE",
                "headers": ["Course Number"],
                "subjects": [self.subjects[0]],
            }
        ]

        public = self.session.public_tables()

        self.assertNotIn("enlist_args", public[0]["subjects"][0])


if __name__ == "__main__":
    unittest.main()
