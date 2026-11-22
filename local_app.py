from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from pathlib import Path
import secrets
from threading import Lock, Thread
from urllib.parse import parse_qs
import webbrowser

from csrs_selenium import run_bot


HOST = "127.0.0.1"
PORT = 8765
SERVER_URL = f"http://{HOST}:{PORT}"
INDEX_PATH = Path(__file__).with_name("index.html")
MAX_REQUEST_SIZE = 4096

_job_lock = Lock()
_job_active = False
_csrf_lock = Lock()
_csrf_token = None


def _run_bot_and_release(username: str, password: str) -> None:
    global _job_active
    try:
        run_bot(username, password)
    finally:
        with _job_lock:
            _job_active = False


class LocalAppHandler(BaseHTTPRequestHandler):
    def _send_html(self, status: int, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_message(self, status: int, title: str, message: str) -> None:
        self._send_html(
            status,
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            f"<title>{title}</title><h1>{title}</h1><p>{message}</p>"
            "<p><a href='/'>Return to login form</a></p></html>",
        )

    def _valid_host(self) -> bool:
        return self.headers.get("Host") in {
            f"{HOST}:{PORT}",
            f"localhost:{PORT}",
        }

    def do_GET(self) -> None:
        global _csrf_token

        if not self._valid_host():
            self.send_error(403, "Use the local app address shown in the terminal.")
            return
        if self.path != "/":
            self.send_error(404)
            return

        try:
            page = INDEX_PATH.read_text(encoding="utf-8")
        except OSError:
            self.send_error(500, "Could not read index.html.")
            return

        token = secrets.token_urlsafe(32)
        with _csrf_lock:
            _csrf_token = token
        page = page.replace("{{CSRF_TOKEN}}", token)
        self._send_html(200, page)

    def do_POST(self) -> None:
        global _job_active
        global _csrf_token

        if not self._valid_host():
            self.send_error(403, "Use the local app address shown in the terminal.")
            return
        if self.path != "/run":
            self.send_error(404)
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.send_error(415, "Expected a form submission.")
            return

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(400, "Invalid request length.")
            return
        if content_length <= 0 or content_length > MAX_REQUEST_SIZE:
            self.send_error(413, "Request is empty or too large.")
            return

        try:
            fields = parse_qs(
                self.rfile.read(content_length).decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
            )
        except (UnicodeDecodeError, ValueError):
            self.send_error(400, "Invalid form data.")
            return

        usernames = fields.get("username", [])
        passwords = fields.get("password", [])
        csrf_tokens = fields.get("csrf_token", [])
        if len(usernames) != 1 or len(passwords) != 1:
            self.send_error(400, "Refresh the form and enter one username and password.")
            return
        if len(csrf_tokens) != 1:
            self.send_error(403, "Refresh the local login form and try again.")
            return

        with _csrf_lock:
            valid_token = (
                _csrf_token is not None
                and hmac.compare_digest(csrf_tokens[0], _csrf_token)
            )
            if valid_token:
                _csrf_token = None
        if not valid_token:
            self.send_error(403, "Refresh the local login form and try again.")
            return

        username = usernames[0].strip()
        password = passwords[0]
        if not username or not password:
            self.send_error(400, "Username and password are required.")
            return

        with _job_lock:
            if _job_active:
                self._send_message(
                    409,
                    "Automation is already running",
                    "Wait for the current browser session to finish before starting another.",
                )
                return
            _job_active = True

        try:
            Thread(
                target=_run_bot_and_release,
                args=(username, password),
                daemon=False,
            ).start()
        except RuntimeError:
            with _job_lock:
                _job_active = False
            self.send_error(500, "Could not start the Selenium worker.")
            return

        self._send_message(
            202,
            "Automation started",
            "Check the Selenium Chrome window. When the Registration page is open, "
            "press Enter in the terminal running this app to close it.",
        )


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), LocalAppHandler)
    print(f"Local login form: {SERVER_URL}")
    print("Press Ctrl+C to stop the local app.")
    webbrowser.open(SERVER_URL)
    server.serve_forever()


if __name__ == "__main__":
    main()
