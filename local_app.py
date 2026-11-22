from http.server import BaseHTTPRequestHandler, HTTPServer
import ctypes
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import time
from threading import Barrier, Event, Lock, Thread
from ctypes import wintypes

from selenium import webdriver
from selenium.common.exceptions import WebDriverException

from csrs_selenium import (
    LoginError,
    PortalAttemptCancelled,
    PortalAttemptError,
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
MAX_SELENIUM_INSTANCES = 50
CSRF_TOKEN = secrets.token_urlsafe(32)
_active_session: PortalSession | None = None


def _close_portal_driver(driver: webdriver.Chrome | None) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except WebDriverException as exc:
        print(f"Could not close a losing Selenium browser: {exc}")


def _start_first_successful_session(
    username: str,
    password: str,
    instance_count: int,
) -> PortalSession:
    cancellation_events = [Event() for _ in range(instance_count)]
    launch_barrier = Barrier(instance_count)
    credentials_barrier = Barrier(instance_count)
    failures: list[Exception] = []
    selection_lock = Lock()
    winning_index: int | None = None
    winner_future: Future[PortalSession] | None = None
    winner: PortalSession | None = None

    def select_first_registration(index: int) -> None:
        nonlocal winning_index
        with selection_lock:
            if winning_index is not None:
                return
            winning_index = index
        for other_index, cancellation_event in enumerate(cancellation_events):
            if other_index != index:
                cancellation_event.set()

    with ThreadPoolExecutor(max_workers=instance_count) as executor:
        futures: dict[Future[PortalSession], tuple[int, Event]] = {}
        for index, cancellation_event in enumerate(cancellation_events):
            def start_instance(
                attempt_index: int = index,
                attempt_cancel_event: Event = cancellation_event,
            ) -> PortalSession:
                try:
                    columns = math.ceil(math.sqrt(instance_count))
                    return start_portal_session(
                        username,
                        password,
                        attempt_cancel_event,
                        lambda: select_first_registration(attempt_index),
                        launch_barrier,
                        credentials_barrier,
                        _selenium_window_rect(attempt_index, instance_count),
                        (attempt_index % columns, attempt_index // columns),
                    )
                except Exception:
                    launch_barrier.abort()
                    credentials_barrier.abort()
                    raise

            future = executor.submit(start_instance)
            futures[future] = (index, cancellation_event)
        try:
            for future in as_completed(futures):
                try:
                    session = future.result()
                except PortalAttemptCancelled:
                    continue
                except (LoginError, RegistrationPageError, WebDriverException) as exc:
                    failures.append(exc)
                else:
                    index = futures[future][0]
                    with selection_lock:
                        selected_index = winning_index
                    if selected_index is None:
                        select_first_registration(index)
                        selected_index = index
                    if selected_index == index:
                        winner_future = future
                        winner = session
                        break
        finally:
            for future, (index, cancellation_event) in futures.items():
                if winner_future is None or index != winning_index:
                    cancellation_event.set()

    if winner is not None:
        for future in futures:
            if future is winner_future:
                continue
            try:
                other_session = future.result()
            except (
                LoginError,
                PortalAttemptCancelled,
                PortalAttemptError,
                RegistrationPageError,
            ) as exc:
                _close_portal_driver(exc.driver)
            except WebDriverException:
                continue
            else:
                _close_portal_driver(other_session.driver)
        return winner

    login_errors = [error for error in failures if isinstance(error, LoginError)]
    if login_errors:
        raise login_errors[0]
    registration_errors = [
        error for error in failures if isinstance(error, RegistrationPageError)
    ]
    if registration_errors:
        raise registration_errors[0]
    webdriver_errors = [
        error for error in failures if isinstance(error, WebDriverException)
    ]
    if webdriver_errors:
        raise webdriver_errors[0]
    raise WebDriverException("No Selenium instance reached the Registration page.")


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
            instance_count = payload.get("instance_count", 1)
            if (
                not isinstance(username, str)
                or not username.strip()
                or not isinstance(password, str)
                or not password
            ):
                self._send_error_json(400, "Username and password are required.")
                return
            if (
                not isinstance(instance_count, int)
                or isinstance(instance_count, bool)
                or not 1 <= instance_count <= MAX_SELENIUM_INSTANCES
            ):
                self._send_error_json(
                    400,
                    f"Choose between 1 and {MAX_SELENIUM_INSTANCES} Selenium instances.",
                )
                return

            _active_session = None

            try:
                _active_session = _start_first_successful_session(
                    username.strip(),
                    password,
                    instance_count,
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
            if (
                payload.get("confirmed") is not True
                and payload.get("auto_enlist") is not True
            ):
                self._send_error_json(
                    400,
                    "Review and confirm the selected subjects or enable auto-enlist.",
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


def _selenium_window_rect(index: int, instance_count: int) -> tuple[int, int, int, int]:
    _, _, area_width, area_height = _local_app_window_rect()
    columns = math.ceil(math.sqrt(instance_count))
    rows = math.ceil(instance_count / columns)
    column = index % columns
    row = index // columns
    x = column * (area_width // columns)
    y = row * (area_height // rows)
    width = (
        area_width - x
        if column == columns - 1
        else area_width // columns
    )
    height = (
        area_height - y
        if row == rows - 1
        else area_height // rows
    )
    return x, y, width, height


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
    print("Press Ctrl+C to stop the app; Selenium windows will remain open.")
    browser_thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        browser_thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
