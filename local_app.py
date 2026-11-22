from http.server import BaseHTTPRequestHandler, HTTPServer
import ctypes
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from threading import Thread
from ctypes import wintypes

from selenium.common.exceptions import WebDriverException

from csrs_selenium import (
    LoginError,
    PortalSession,
    RegistrationPageError,
    run_selected_enlistments,
    start_portal_session,
)


HOST = "127.0.0.1"
PORT = 8765
SERVER_URL = f"http://{HOST}:{PORT}"
APP_WINDOW_TITLE = "PE/NSTP Enlistment App"
ROOT = Path(__file__).parent
INDEX_PATH = ROOT / "index.html"
SCRIPT_PATH = ROOT / "app.js"
MAX_REQUEST_SIZE = 32768
CSRF_TOKEN = secrets.token_urlsafe(32)
_active_session: PortalSession | None = None


class LocalAppHandler(BaseHTTPRequestHandler):
    def _valid_host(self) -> bool:
        return self.headers.get("Host") in {
            f"{HOST}:{PORT}",
            f"localhost:{PORT}",
        }

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        html: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if html:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json(self) -> dict | None:
        if self.headers.get_content_type() != "application/json":
            self._send_error_json(415, "Expected a JSON request.")
            return None

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_error_json(400, "Invalid request length.")
            return None
        if content_length <= 0 or content_length > MAX_REQUEST_SIZE:
            self._send_error_json(413, "Request is empty or too large.")
            return None

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "Invalid JSON request.")
            return None
        if not isinstance(payload, dict):
            self._send_error_json(400, "The request must be a JSON object.")
            return None
        return payload

    def _valid_csrf(self) -> bool:
        token = self.headers.get("X-CSRF-Token", "")
        return hmac.compare_digest(token, CSRF_TOKEN)

    def do_GET(self) -> None:
        if not self._valid_host():
            self.send_error(403, "Use the local app address shown in the terminal.")
            return

        if self.path == "/":
            try:
                page = INDEX_PATH.read_text(encoding="utf-8")
            except OSError:
                self._send_error_json(500, "Could not read index.html.")
                return
            page = page.replace("{{CSRF_TOKEN}}", CSRF_TOKEN)
            self._send_bytes(
                200,
                "text/html; charset=utf-8",
                page.encode("utf-8"),
                html=True,
            )
            return

        if self.path == "/app.js":
            try:
                script = SCRIPT_PATH.read_bytes()
            except OSError:
                self._send_error_json(500, "Could not read app.js.")
                return
            self._send_bytes(
                200,
                "text/javascript; charset=utf-8",
                script,
            )
            return

        if self.path == "/api/subjects":
            if _active_session is None:
                self._send_error_json(401, "Log in to load current subjects.")
                return
            self._send_json(200, {"tables": _active_session.public_tables()})
            return

        self.send_error(404)

    def do_POST(self) -> None:
        global _active_session

        if not self._valid_host():
            self.send_error(403, "Use the local app address shown in the terminal.")
            return
        if not self._valid_csrf():
            self._send_error_json(403, "Refresh the local app and try again.")
            return

        payload = self._read_json()
        if payload is None:
            return

        if self.path == "/api/login":
            username = payload.get("username")
            password = payload.get("password")
            if (
                not isinstance(username, str)
                or not username.strip()
                or not isinstance(password, str)
                or not password
            ):
                self._send_error_json(400, "Username and password are required.")
                return

            if _active_session is not None:
                _active_session.close()
                _active_session = None

            try:
                _active_session = start_portal_session(
                    username.strip(),
                    password,
                )
            except LoginError as exc:
                self._send_error_json(401, str(exc))
                return
            except RegistrationPageError as exc:
                self._send_error_json(502, str(exc))
                return
            except WebDriverException:
                self._send_error_json(
                    502,
                    "Selenium could not open or control Chrome.",
                )
                return

            self._send_json(
                200,
                {"tables": _active_session.public_tables()},
            )
            return

        if self.path == "/api/enlist":
            if _active_session is None:
                self._send_error_json(401, "Log in before enlisting subjects.")
                return

            keys = payload.get("keys")
            if (
                not isinstance(keys, list)
                or not keys
                or any(not isinstance(key, str) for key in keys)
            ):
                self._send_error_json(400, "Select at least one subject.")
                return
            if payload.get("confirmed") is not True:
                self._send_error_json(
                    400,
                    "Review and confirm the selected subjects before enlistment.",
                )
                return

            try:
                results = run_selected_enlistments(_active_session, keys)
            except ValueError as exc:
                self._send_error_json(400, str(exc))
                return
            except WebDriverException:
                self._send_error_json(
                    502,
                    "The portal session became unavailable before completion.",
                )
                return

            self._send_json(200, {"results": results})
            return

        if self.path == "/api/logout":
            if _active_session is not None:
                _active_session.close()
                _active_session = None
            self._send_json(200, {"ok": True})
            return

        self.send_error(404)


def _chrome_executable() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not locate the Google Chrome executable.")


def _local_app_window_rect() -> tuple[int, int, int, int]:
    if os.name == "nt":
        user32 = ctypes.windll.user32
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
    else:
        screen_width, screen_height = 1920, 1080

    half_width = screen_width // 2
    return half_width, 0, half_width, screen_height


def _open_local_app_browser() -> None:
    x_position, y_position, width, height = _local_app_window_rect()
    subprocess.Popen(
        [
            str(_chrome_executable()),
            "--new-window",
            "--profile-directory=Default",
            f"--window-position={x_position},{y_position}",
            f"--window-size={width},{height}",
            SERVER_URL,
        ]
    )
    _position_local_app_window()


def _window_process_id(hwnd: wintypes.HWND) -> int | None:
    user32 = ctypes.windll.user32
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)) == 0:
        return None
    return process_id.value


def _is_chrome_window(
    hwnd: wintypes.HWND,
) -> bool:
    process_id = _window_process_id(hwnd)
    if process_id is None:
        return False

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process = kernel32.OpenProcess(0x1000, False, process_id)
    if not process:
        return False

    try:
        image_path = ctypes.create_unicode_buffer(32768)
        path_length = wintypes.DWORD(len(image_path))
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if not kernel32.QueryFullProcessImageNameW(
            process,
            0,
            image_path,
            ctypes.byref(path_length),
        ):
            return False
        return Path(image_path.value).name.casefold() == "chrome.exe"
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(process)


def _find_local_app_window() -> wintypes.HWND | None:
    if os.name != "nt":
        return None

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    matches: list[wintypes.HWND] = []

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    def visit(hwnd: wintypes.HWND, _parameter: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        text_length = user32.GetWindowTextLengthW(hwnd)
        if text_length == 0:
            return True
        title = ctypes.create_unicode_buffer(text_length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if (
            APP_WINDOW_TITLE.casefold() in title.value.casefold()
            and _is_chrome_window(hwnd)
        ):
            matches.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return matches[0] if matches else None


def _position_local_app_window() -> None:
    deadline = time.monotonic() + 20
    window = None
    while window is None and time.monotonic() < deadline:
        window = _find_local_app_window()
        if window is None:
            time.sleep(0.2)
    if window is None:
        raise TimeoutError(
            f"Could not find the local Chrome window titled {APP_WINDOW_TITLE!r}."
        )

    x_position, y_position, width, height = _local_app_window_rect()
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow(window, 9)
    if not user32.SetWindowPos(
        window,
        None,
        x_position,
        y_position,
        width,
        height,
        0x0040,
    ):
        raise ctypes.WinError()


def main() -> None:
    server = HTTPServer((HOST, PORT), LocalAppHandler)
    browser_thread = Thread(
        target=_open_local_app_browser,
        name="local-app-browser-launcher",
    )
    print(f"Local subject-enlistment app: {SERVER_URL}")
    print("The local app is resized on the right; Selenium opens CSRS normally after login.")
    print("Press Ctrl+C to stop the app and close the Selenium session.")
    browser_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        browser_thread.join()
        try:
            if _active_session is not None:
                _active_session.close()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
