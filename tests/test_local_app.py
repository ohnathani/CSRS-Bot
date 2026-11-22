import json
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import local_app


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

    def test_main_closes_the_server_and_active_portal_on_exit(self) -> None:
        fake_server = Mock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        local_app._active_session = self.session

        with (
            patch.object(local_app, "HTTPServer", return_value=fake_server),
            patch.object(local_app, "_open_local_app_browser"),
            patch("builtins.print"),
        ):
            local_app.main()

        self.assertTrue(self.session.closed)
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
        self.assertIn("function persistSelectedKeys()", script)
        self.assertNotIn('fetch("/api/subjects"', script)

    def test_login_keeps_password_and_enlist_arguments_out_of_response(self) -> None:
        args = ("student-id", "subject-id", "PE II")
        with patch.object(
            local_app,
            "start_portal_session",
            return_value=self.session,
        ) as start, patch.object(
            local_app,
            "_position_local_app_window",
        ) as position_window:
            status, response = self.request_json(
                "/api/login",
                {"username": "student", "password": "dummy-password"},
            )

        start.assert_called_once_with(
            "student",
            "dummy-password",
        )
        position_window.assert_not_called()
        self.assertEqual(status, 200)
        response_text = json.dumps(response)
        self.assertNotIn("dummy-password", response_text)
        self.assertNotIn("enlist_args", response_text)
        self.assertNotIn(args[0], response_text)
        self.assertEqual(local_app._active_session, self.session)

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
