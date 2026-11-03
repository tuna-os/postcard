"""Catch a native Microsoft authorization response on loopback.

The GTK-free half of native onboarding: bind 127.0.0.1 on an ephemeral port,
hand its URL to ms_oauth.authorization_url, then wait for the browser to
deliver the code. Stray local requests are answered and ignored; only a code
whose state matches is returned. The browser, the keyring and the token
exchange stay with the caller.
"""

import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from secrets import compare_digest
from time import monotonic
from types import TracebackType
from typing import cast

from .ms_oauth import MsOAuthError

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 300.0

# A local process can knock; after this many requests the wait is not going to
# succeed, so stop rather than serve a scanner forever.
_MAX_REQUESTS = 20

# Fixed pages: a reply must never reflect the code, state or provider error.
_SUCCESS_PAGE = (
    b"<!doctype html><html><body>"
    b"<p>You can close this window and return to Postcard.</p>"
    b"</body></html>"
)
_FAILURE_PAGE = (
    b"<!doctype html><html><body>"
    b"<p>Sign-in failed. You can close this window and try again in Postcard.</p>"
    b"</body></html>"
)


def _is_callback(query: dict[str, str], expected_state: str) -> bool:
    code = query.get("code", "")
    state = query.get("state", "")
    if not code or not state or not expected_state:
        return False
    return compare_digest(expected_state.encode(), state.encode())


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "PostcardM365/1"

    def log_message(self, *_args: object) -> None:
        """The request URL carries the code, so it is never logged."""

    def do_GET(self) -> None:  # noqa: N802 -- required by BaseHTTPRequestHandler.
        server = cast("_LoopbackServer", self.server)
        path = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(path.query, keep_blank_values=True))
        server.requests.append((path.path, query))
        if path.path != "/":
            self._send_page(404, _FAILURE_PAGE)
        elif "error" in query:
            server.outcome = ("provider_error", "")
            self._send_page(400, _FAILURE_PAGE)
        elif _is_callback(query, server.expected_state):
            server.outcome = ("code", query["code"])
            self._send_page(200, _SUCCESS_PAGE)
        else:
            self._send_page(400, _FAILURE_PAGE)

    def _send_page(self, status: int, page: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


class _LoopbackServer(HTTPServer):
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.outcome: tuple[str, str] | None = None
        self.expected_state = ""
        try:
            super().__init__((LOOPBACK_HOST, 0), _CallbackHandler)
        except OSError as error:
            raise MsOAuthError("invalid_request") from error


class LoopbackRedirect:
    """One native sign-in's loopback listener. Use as a context manager."""

    def __init__(self) -> None:
        self._server = _LoopbackServer()
        self.redirect_uri = f"http://{LOOPBACK_HOST}:{self._server.server_address[1]}/"

    def __enter__(self) -> "LoopbackRedirect":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._server.server_close()

    def wait_for_code(
        self, expected_state: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> str:
        """The authorization code delivered for this state.

        Raises invalid_grant when the provider refused, invalid_request when
        nothing usable arrived in time. Never logs or returns the state.
        """
        if not expected_state or timeout <= 0:
            raise MsOAuthError("invalid_request")
        server = self._server
        server.expected_state = expected_state
        server.outcome = None
        deadline = monotonic() + timeout
        while server.outcome is None and len(server.requests) < _MAX_REQUESTS:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            try:
                server.handle_request()
            except OSError:
                # A failed accept counts as a knock; the request limit stops a
                # broken socket from spinning here forever.
                server.requests.append(("", {}))
        return _code_from(server)


def _code_from(server: _LoopbackServer) -> str:
    outcome = server.outcome
    server.expected_state = ""
    server.outcome = None
    if outcome is None:
        raise MsOAuthError("invalid_request")
    kind, code = outcome
    if kind == "provider_error":
        raise MsOAuthError("invalid_grant")
    return code
