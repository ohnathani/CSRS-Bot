import json
from http.server import HTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import local_app
from csrs_selenium import (
    LoginError,
    PortalAttemptCancelled,
    RegistrationPageError,
)


class FakePortalSession:
    def __init__(self) -> None:
        self.closed = False
        self.driver = Mock()
        self.driver.title = "CSRS Portal"
        self.driver.capabilities = {"goog:processID": 1234}

    def public_tables(self) -> list[dict]:
        return [
            {
                "category": "PE",
                "headers": ["Course Number"],
                "subjects": [
                    {
                        "key": "opaque-row-key",
                        "cells": ["PE II"],
                        "selectable": True,
                        "reason": None,
                        "action_label": "Click to add",
                    }
                ],
            }
        ]

    def close(self) -> None:
        self.closed = True


class LocalAppApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_port = local_app.PORT
        self.previous_token = local_app.CSRF_TOKEN
        self.previous_session = local_app._active_session
        self.session = FakePortalSession()
        self.server = HTTPServer((local_app.HOST, 0), local_app.LocalAppHandler)
        local_app.PORT = self.server.server_port
        local_app.CSRF_TOKEN = "test-csrf-token"
        local_app._active_session = None
        self.base_url = f"http://{local_app.HOST}:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        local_app._active_session = self.previous_session
        local_app.PORT = self.previous_port
        local_app.CSRF_TOKEN = self.previous_token

    def request_json(
        self,
        path: str,
        payload: dict | None = None,
        include_token: bool = True,
    ) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if include_token:
            headers["X-CSRF-Token"] = local_app.CSRF_TOKEN
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method="GET" if payload is None else "POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_local_app_browser_uses_default_profile_on_the_right(self) -> None:
        chrome = Path("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
        window_rect = (960, 0, 960, 1080)
        with (
            patch.object(local_app, "_chrome_executable", return_value=chrome),
            patch.object(local_app, "_local_app_window_rect", return_value=window_rect),
            patch.object(local_app, "_position_local_app_window") as position_window,
            patch.object(local_app.subprocess, "Popen") as popen,
        ):
            local_app._open_local_app_browser()

        popen.assert_called_once_with(
            [
                str(chrome),
                "--new-window",
                "--profile-directory=Default",
                "--window-position=960,0",
                "--window-size=960,1080",
                local_app.SERVER_URL,
            ]
        )
        position_window.assert_called_once_with()

    def test_selenium_instances_are_tiled_on_the_left_half(self) -> None:
        with patch.object(
            local_app,
            "_local_app_window_rect",
            return_value=(960, 0, 960, 1080),
        ):
            rects = [
                local_app._selenium_window_rect(index, 4)
                for index in range(4)
            ]

        self.assertEqual(
            rects,
            [
                (0, 0, 480, 540),
                (480, 0, 480, 540),
                (0, 540, 480, 540),
                (480, 540, 480, 540),
            ],
        )

    def test_main_leaves_selenium_open_and_closes_the_server_on_exit(self) -> None:
        fake_server = Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        local_app._active_session = self.session

        with (
            patch.object(local_app, "HTTPServer", return_value=fake_server),
            patch.object(local_app, "_open_local_app_browser"),
            patch("builtins.print"),
        ):
            local_app.main()

        self.assertFalse(self.session.closed)
        self.session.driver.quit.assert_not_called()
        fake_server.server_close.assert_called_once()

    def test_home_page_injects_csrf_token_and_serves_client_script(self) -> None:
        with urlopen(self.base_url + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        with urlopen(self.base_url + "/app.js", timeout=3) as response:
            script = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)

        self.assertIn('name="csrf-token" content="test-csrf-token"', page)
        self.assertNotIn("{{CSRF_TOKEN}}", page)
        self.assertIn('id="login-panel"', page)
        self.assertIn('id="selenium-instance-count"', page)
        self.assertIn('id="auto-enlist"', page)
        self.assertIn('type="text" inputmode="numeric"', page)
        self.assertIn("function persistSelectedKeys()", script)
        self.assertNotIn('fetch("/api/subjects"', script)

    def test_login_keeps_password_and_enlist_arguments_out_of_response(self) -> None:
        args = ("student-id", "subject-id", "PE II")
        with patch.object(
            local_app,
            "_start_first_successful_session",
            return_value=self.session,
        ) as start_session, patch.object(
            local_app,
            "_position_local_app_window",
        ) as position_window:
            status, response = self.request_json(
                "/api/login",
                {
                    "username": "student",
                    "password": "dummy-password",
                    "instance_count": 3,
                },
            )

        start_session.assert_called_once_with(
            "student",
            "dummy-password",
            3,
        )
        position_window.assert_not_called()
        self.assertEqual(status, 200)
        response_text = json.dumps(response)
        self.assertNotIn("dummy-password", response_text)
        self.assertNotIn("enlist_args", response_text)
        self.assertNotIn(args[0], response_text)
        self.assertEqual(local_app._active_session, self.session)

    def test_failed_login_keeps_browser_open_until_retry(self) -> None:
        failed_driver = Mock()
        failed_login = LoginError("Login failed.", failed_driver)
        with patch.object(
            local_app,
            "_start_first_successful_session",
            side_effect=failed_login,
        ):
            status, response = self.request_json(
                "/api/login",
                {"username": "student", "password": "dummy-password"},
            )

        self.assertEqual(status, 401)
        self.assertEqual(response["error"], "Login failed.")
        failed_driver.quit.assert_not_called()

        with patch.object(
            local_app,
            "_start_first_successful_session",
            return_value=self.session,
        ):
            status, _ = self.request_json(
                "/api/login",
                {"username": "student", "password": "correct-password"},
            )

        self.assertEqual(status, 200)
        failed_driver.quit.assert_not_called()

    def test_registration_page_failure_keeps_browser_open(self) -> None:
        failed_driver = Mock()
        failure = RegistrationPageError(
            "The Registration page did not load its subject tables.",
            failed_driver,
        )
        with patch.object(
            local_app,
            "_start_first_successful_session",
            side_effect=failure,
        ):
            status, response = self.request_json(
                "/api/login",
                {"username": "student", "password": "dummy-password"},
            )

        self.assertEqual(status, 502)
        self.assertEqual(
            response["error"],
            "The Registration page did not load its subject tables.",
        )
        failed_driver.quit.assert_not_called()

    def test_first_successful_instance_is_kept_and_losers_are_closed(self) -> None:
        sessions = [FakePortalSession(), FakePortalSession()]
        first_registration_ready = Event()
        second_session_returning = Event()
        launch_barriers = [None, None]
        window_rects = [None, None]
        next_index = 0
        index_lock = Lock()

        def start_instance(
            _username,
            _password,
            _cancel_event,
            on_registration_ready,
            launch_barrier,
            credentials_barrier,
            window_rect,
            window_grid_position,
        ):
            nonlocal next_index
            with index_lock:
                index = next_index
                next_index += 1
            launch_barriers[index] = (
                launch_barrier,
                credentials_barrier,
            )
            window_rects[index] = window_rect
            self.assertEqual(window_grid_position, (index, 0))
            if index == 0:
                on_registration_ready()
                first_registration_ready.set()
                self.assertTrue(second_session_returning.wait(timeout=3))
                return sessions[index]
            self.assertTrue(first_registration_ready.wait(timeout=3))
            on_registration_ready()
            second_session_returning.set()
            return sessions[index]

        with patch.object(
            local_app,
            "start_portal_session",
            side_effect=start_instance,
        ) as start:
            winner = local_app._start_first_successful_session(
                "student",
                "password",
                2,
            )

        self.assertIs(winner, sessions[0])
        self.assertEqual(start.call_count, 2)
        self.assertIs(launch_barriers[0][0], launch_barriers[1][0])
        self.assertIs(launch_barriers[0][1], launch_barriers[1][1])
        self.assertEqual(launch_barriers[0][0].parties, 2)
        self.assertEqual(launch_barriers[0][1].parties, 2)
        self.assertNotEqual(window_rects[0], window_rects[1])
        winner.driver.quit.assert_not_called()
        sessions[1].driver.quit.assert_called_once()

    def test_losing_in_progress_instance_is_cancelled_and_closed(self) -> None:
        start_barrier = Barrier(2)
        winner = FakePortalSession()
        losing_driver = Mock()
        next_index = 0
        index_lock = Lock()

        def start_instance(
            _username,
            _password,
            cancel_event,
            on_registration_ready,
            _launch_barrier,
            _credentials_barrier,
            _window_rect,
            _window_grid_position,
        ):
            nonlocal next_index
            with index_lock:
                index = next_index
                next_index += 1
            start_barrier.wait(timeout=3)
            if index == 0:
                on_registration_ready()
                return winner
            if cancel_event.wait(timeout=3):
                raise PortalAttemptCancelled(losing_driver)
            raise AssertionError("The losing attempt was not cancelled.")

        with patch.object(
            local_app,
            "start_portal_session",
            side_effect=start_instance,
        ):
            result = local_app._start_first_successful_session(
                "student",
                "password",
                2,
            )

        self.assertIs(result, winner)
        winner.driver.quit.assert_not_called()
        losing_driver.quit.assert_called_once()

    def test_all_failed_instances_remain_open(self) -> None:
        drivers = [Mock(), Mock()]
        next_index = 0
        index_lock = Lock()

        def reject_login(
            _username,
            _password,
            _cancel_event,
            _on_registration_ready,
            _launch_barrier,
            _credentials_barrier,
            _window_rect,
            _window_grid_position,
        ):
            nonlocal next_index
            with index_lock:
                driver = drivers[next_index]
                next_index += 1
            raise LoginError("The portal rejected the login credentials.", driver)

        with (
            patch.object(
                local_app,
                "start_portal_session",
                side_effect=reject_login,
            ),
            self.assertRaises(LoginError),
        ):
            local_app._start_first_successful_session(
                "student",
                "password",
                2,
            )

        for driver in drivers:
            driver.quit.assert_not_called()

    def test_invalid_instance_counts_are_rejected(self) -> None:
        with patch.object(local_app, "_start_first_successful_session") as start:
            status, response = self.request_json(
                "/api/login",
                {
                    "username": "student",
                    "password": "dummy-password",
                    "instance_count": 51,
                },
            )

        self.assertEqual(status, 400)
        self.assertIn("1 and 50", response["error"])
        start.assert_not_called()

    def test_logout_leaves_selenium_browser_open(self) -> None:
        local_app._active_session = self.session

        status, response = self.request_json("/api/logout", {})

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertIsNone(local_app._active_session)
        self.assertFalse(self.session.closed)
        self.session.driver.quit.assert_not_called()

    def test_enlist_requires_confirmation_and_forwards_only_opaque_keys(self) -> None:
        local_app._active_session = self.session
        status, response = self.request_json(
            "/api/enlist",
            {"keys": ["opaque-row-key"], "confirmed": False},
        )
        self.assertEqual(status, 400)
        self.assertIn("confirm", response["error"].lower())

        with patch.object(
            local_app,
            "run_selected_enlistments",
            return_value=[{"key": "opaque-row-key", "status": "success"}],
        ) as run:
            status, response = self.request_json(
                "/api/enlist",
                {"keys": ["opaque-row-key"], "confirmed": True},
            )

        run.assert_called_once_with(self.session, ["opaque-row-key"])
        self.assertEqual(status, 200)
        self.assertEqual(response["results"][0]["status"], "success")

    def test_auto_enlist_request_forwards_only_saved_subject_keys(self) -> None:
        local_app._active_session = self.session
        with patch.object(
            local_app,
            "run_selected_enlistments",
            return_value=[{"key": "opaque-row-key", "status": "success"}],
        ) as run:
            status, response = self.request_json(
                "/api/enlist",
                {"keys": ["opaque-row-key"], "auto_enlist": True},
            )

        run.assert_called_once_with(self.session, ["opaque-row-key"])
        self.assertEqual(status, 200)
        self.assertEqual(response["results"][0]["status"], "success")

    def test_post_rejects_missing_csrf_token(self) -> None:
        with patch.object(local_app, "start_portal_session") as start:
            status, _ = self.request_json(
                "/api/login",
                {"username": "student", "password": "dummy-password"},
                include_token=False,
            )

        self.assertEqual(status, 403)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
