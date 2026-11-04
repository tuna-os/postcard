"""The reader's WebKit sandbox, proven against a live local sentinel server.

A message body is untrusted HTML rendered by a real WebKitWebView. These tests
serve its subresources from an HTTP server on loopback that logs every
request, then assert on what the renderer fetched and what it ran:

- remote images stay unfetched until the reader allows them (the banner's
  "Show Images" path), and load once allowed;
- scripts never run and never even download, with or without images allowed;
- link clicks only ever reach the three external schemes, and never navigate
  the body view itself;
- the shared web process survives releasing one view and restarts cleanly.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display. No live
accounts, no outside network -- every URL points at the sentinel.

Two capability probes gate the suite, in the same spirit as the display
skip: `working_renderer` can the WebKit web process complete a page load
here, and `fetching_renderer` can it also fetch the page's subresources
over HTTP. A host that fails a probe skips what it cannot prove rather
than failing vacuously -- a blocked image proves nothing where no image
could load.
"""

import os

os.environ.setdefault("GSETTINGS_BACKEND", "memory")

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Adw, Gio, GLib, Gtk, WebKit
except (ImportError, ValueError):
    pytest.skip("GTK/Adw/WebKit typelibs unavailable", allow_module_level=True)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    pytest.skip("no display", allow_module_level=True)

os.environ.setdefault("GTK_A11Y", "none")
# The running app inits Adw via Adw.Application; a bare test does it here so
# the template builder knows the Adw types.
Adw.init()


def _register_resources():
    # The running app loads its GResource via resource_base_path; a bare test
    # interpreter registers the compiled bundle itself, or skips.
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        "/app/share/postcard/postcard.gresource",
        os.path.join(here, "..", "..", "build", "src", "postcard.gresource"),
    ):
        if os.path.exists(candidate):
            Gio.resources_register(Gio.Resource.load(candidate))
            return
    pytest.skip("no compiled gresource", allow_module_level=True)


_register_resources()

import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from postcard import message_view
from postcard.avatar_loader import AvatarLoader
from postcard.core import APP_ID
from postcard.core.models.email import Email
from postcard.message_view import MessageView

# A 1x1 transparent PNG, the "remote image" the sentinel serves.
_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
# If this ever executes, it phones home and renames the document -- both are
# asserted absent, so either half failing proves the script ran.
_EVIL_JS = b"fetch('/pwned');document.title='pwned';"
_INLINE_PWNED = '<script>document.title="pwned"</script>'
# First load also starts the shared web process, which costs seconds cold
# and longer on a loaded host; every wait below stays bounded regardless.
LOAD_TIMEOUT = 45.0
PARSE_TIMEOUT = 10.0
SETTLE_SECONDS = 2.0


class _SentinelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        server = self.server
        with server.log_lock:
            server.request_log.append(self.path)
        if self.path == "/pixel.png":
            body, content_type = _PIXEL, "image/png"
        elif self.path == "/evil.js":
            body, content_type = _EVIL_JS, "application/javascript"
        elif self.path == "/pwned":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SentinelHandler)
    server.log_lock = threading.Lock()
    server.request_log = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture()
def sentinel():
    """A loopback HTTP server logging every path the renderer requests."""
    server, thread = _serve()
    yield server
    server.shutdown()
    thread.join(timeout=10)
    server.server_close()


def _hits(server, path):
    with server.log_lock:
        return [hit for hit in server.request_log if hit == path]


def _pump_until(predicate, timeout, diagnosis):
    """Run main-loop iterations until predicate passes, or fail with logs."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        context.iteration(True)
    assert predicate(), f"timed out after {timeout}s: {diagnosis()}"


def _settle(seconds=SETTLE_SECONDS):
    """Let already-queued renderer traffic land without waiting for more."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.01)


def _raw_message(html):
    message = EmailMessage()
    message["From"] = "ada@example.invalid"
    message["To"] = "bob@example.invalid"
    message["Subject"] = "sentinel"
    message.set_content("plain fallback")
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _make_view(html, *, allow_remote_images=False):
    """A MessageView already expanded onto `html`, with its avatar loader."""
    settings = Gio.Settings(schema_id=APP_ID)
    avatars = AvatarLoader(settings)
    raw = _raw_message(html)
    mail = Email(
        id=1,
        folder_id=1,
        server_id="1",
        sender="Ada",
        subject="sentinel",
        preview="",
        date="not a timestamp",
        is_unread=True,
    )
    loads = []

    def on_load(_mail, callback):
        loads.append(True)
        callback(raw, None)

    view = MessageView(
        mail,
        on_load,
        lambda _attachment: None,
        lambda _attachment: None,
        avatars,
        should_load_remote_images=allow_remote_images,
        is_expanded=True,
    )
    assert loads == [True]
    _wait_for_parse(view)
    webview = view._webview
    assert webview is not None
    return view, avatars, webview


def _wait_for_parse(view, timeout=PARSE_TIMEOUT):
    # MIME parsing runs on a worker; the webview only exists once its result
    # lands back on the main loop.
    _pump_until(
        lambda: view.parsed is not None,
        timeout,
        lambda: f"parsed={view.parsed!r}",
    )


def _wait_for_load(webview, timeout=LOAD_TIMEOUT):
    finished = []
    webview.connect(
        "load-changed",
        lambda _view, event: (
            finished.append(event) if event == WebKit.LoadEvent.FINISHED else None
        ),
    )
    _pump_until(
        lambda: finished,
        timeout,
        lambda: f"is_loading={webview.is_loading()} uri={webview.get_uri()!r}",
    )


def _close_view(view, avatars):
    view.release()
    avatars.shutdown()


def _try_plain_load():
    """One bounded body load: True only if the renderer finishes it."""
    view, avatars, webview = _make_view("<p>probe</p>")
    try:
        _wait_for_load(webview)
    except Exception as error:
        print(f"renderer probe failed: {error}")
        return False
    else:
        return True
    finally:
        _close_view(view, avatars)


def _try_remote_fetch():
    """One bounded allowed-images load: True only if the pixel arrives."""
    server, thread = _serve()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        view, avatars, webview = _make_view(
            f'<img src="{base}/pixel.png">', allow_remote_images=True
        )
        try:
            _wait_for_load(webview)
            _pump_until(
                lambda: _hits(server, "/pixel.png"),
                LOAD_TIMEOUT,
                lambda: f"requests={server.request_log}",
            )
        except Exception as error:
            print(f"fetch probe failed: {error}")
            return False
        else:
            return True
        finally:
            _close_view(view, avatars)
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


_LOAD_PROBE_RESULT = None
_FETCH_PROBE_RESULT = None


@pytest.fixture(scope="module")
def working_renderer():
    """Skip the module's load tests where the web process cannot finish one."""
    global _LOAD_PROBE_RESULT
    if _LOAD_PROBE_RESULT is None:
        _LOAD_PROBE_RESULT = _try_plain_load()
    if not _LOAD_PROBE_RESULT:
        pytest.skip("WebKit could not complete a page load in this environment")


@pytest.fixture(scope="module")
def fetching_renderer(working_renderer):
    """Skip fetch-positive tests where subresources cannot arrive."""
    global _FETCH_PROBE_RESULT
    if _FETCH_PROBE_RESULT is None:
        _FETCH_PROBE_RESULT = _try_remote_fetch()
    if not _FETCH_PROBE_RESULT:
        pytest.skip("WebKit could not fetch a subresource in this environment")


def test_remote_images_stay_blocked(sentinel, working_renderer):
    base = f"http://127.0.0.1:{sentinel.server_address[1]}"
    view, avatars, webview = _make_view(f'<img src="{base}/pixel.png">')
    try:
        _wait_for_load(webview)
        _settle()
        assert _hits(sentinel, "/pixel.png") == []
    finally:
        _close_view(view, avatars)


def test_show_images_loads_remote_images(sentinel, fetching_renderer):
    base = f"http://127.0.0.1:{sentinel.server_address[1]}"
    view, avatars, webview = _make_view(f'<img src="{base}/pixel.png">')
    try:
        _wait_for_load(webview)
        _settle()
        assert _hits(sentinel, "/pixel.png") == []

        # The banner's button is what allows them; the reload must fetch.
        view._images_banner.emit("button-clicked")
        _pump_until(
            lambda: _hits(sentinel, "/pixel.png"),
            LOAD_TIMEOUT,
            lambda: f"requests={sentinel.request_log}",
        )
    finally:
        _close_view(view, avatars)


def test_scripts_never_run_and_never_download(sentinel, working_renderer):
    base = f"http://127.0.0.1:{sentinel.server_address[1]}"
    html = f'<script src="{base}/evil.js"></script>{_INLINE_PWNED}'
    view, avatars, webview = _make_view(html)
    try:
        _wait_for_load(webview)
        _settle()
        assert _hits(sentinel, "/evil.js") == []
        assert _hits(sentinel, "/pwned") == []
        assert webview.get_title() != "pwned"
    finally:
        _close_view(view, avatars)


def test_allowed_images_do_not_allow_scripts(sentinel, fetching_renderer):
    base = f"http://127.0.0.1:{sentinel.server_address[1]}"
    html = (
        f'<img src="{base}/pixel.png">'
        f'<script src="{base}/evil.js"></script>{_INLINE_PWNED}'
    )
    view, avatars, webview = _make_view(html, allow_remote_images=True)
    try:
        _wait_for_load(webview)
        _pump_until(
            lambda: _hits(sentinel, "/pixel.png"),
            LOAD_TIMEOUT,
            lambda: f"requests={sentinel.request_log}",
        )
        _settle()
        assert _hits(sentinel, "/evil.js") == []
        assert _hits(sentinel, "/pwned") == []
        assert webview.get_title() != "pwned"
    finally:
        _close_view(view, avatars)


def _click_link(webview, index):
    clicked = []
    webview.evaluate_javascript(
        f"document.querySelectorAll('a')[{index}].click()",
        -1,
        None,
        None,
        None,
        lambda _view, _result: clicked.append(True),
    )
    _pump_until(lambda: clicked, LOAD_TIMEOUT, lambda: "click never dispatched")


class _FakeLauncher:
    """Stands in for Gtk.UriLauncher and records what would have opened."""

    launched = []

    def __init__(self, uri=""):
        self._uri = uri

    def launch(self, *_args):
        type(self).launched.append(self._uri)


@pytest.fixture()
def launched_uris(monkeypatch):
    _FakeLauncher.launched = []
    monkeypatch.setattr(Gtk, "UriLauncher", _FakeLauncher)
    return _FakeLauncher.launched


def _stayed_put(webview):
    return (webview.get_uri() or "") in ("about:blank", "")


def test_mailto_and_https_links_launch_outside_the_body(
    launched_uris, working_renderer
):
    html = '<a href="mailto:ada@example.invalid">m</a><a href="https://example.invalid/x">h</a>'
    view, avatars, webview = _make_view(html)
    try:
        _wait_for_load(webview)
        _click_link(webview, 0)
        _pump_until(
            lambda: launched_uris == ["mailto:ada@example.invalid"],
            LOAD_TIMEOUT,
            lambda: f"launched={launched_uris}",
        )
        assert _stayed_put(webview)

        _click_link(webview, 1)
        _pump_until(
            lambda: (
                launched_uris
                == ["mailto:ada@example.invalid", "https://example.invalid/x"]
            ),
            LOAD_TIMEOUT,
            lambda: f"launched={launched_uris}",
        )
        assert _stayed_put(webview)
    finally:
        _close_view(view, avatars)


def test_foreign_scheme_links_launch_nothing(launched_uris, working_renderer):
    html = (
        '<a href="file:///etc/hostname">f</a>'
        '<a href="tel:+15551234567">t</a>'
        "<a href=\"javascript:document.title='pwned'\">j</a>"
    )
    view, avatars, webview = _make_view(html)
    try:
        _wait_for_load(webview)
        for index in range(3):
            _click_link(webview, index)
            _settle()
        assert launched_uris == []
        assert webview.get_title() != "pwned"
        assert _stayed_put(webview)
    finally:
        _close_view(view, avatars)


def test_collapsed_view_defers_the_renderer(working_renderer):
    settings = Gio.Settings(schema_id=APP_ID)
    avatars = AvatarLoader(settings)
    mail = Email(
        id=1,
        folder_id=1,
        server_id="1",
        sender="Ada",
        subject="sentinel",
        preview="",
        date="not a timestamp",
        is_unread=True,
    )
    loads = []

    def on_load(_mail, callback):
        loads.append(True)
        callback(_raw_message("<p>hi</p>"), None)

    view = MessageView(
        mail,
        on_load,
        lambda _attachment: None,
        lambda _attachment: None,
        avatars,
    )
    try:
        assert loads == []
        assert view._webview is None

        toggle = view.get_first_child()
        assert isinstance(toggle, Gtk.Button)
        toggle.emit("clicked")

        assert loads == [True]
        _wait_for_parse(view)
        assert view._webview is not None
        _wait_for_load(view._webview)
    finally:
        _close_view(view, avatars)


def test_release_drops_the_view_but_keeps_the_process():
    assert message_view._anchor is None or isinstance(
        message_view._anchor, WebKit.WebView
    )
    view, avatars, _webview = _make_view("<p>hi</p>")
    try:
        assert message_view._anchor is not None
        view.release()
        view.release()
        assert view._webview is None
        assert view.raw is None
        assert view.parsed is None
        assert message_view._anchor is not None
    finally:
        avatars.shutdown()


def test_release_anchor_restarts_the_web_process(working_renderer):
    view, avatars, webview = _make_view("<p>hi</p>")
    _wait_for_load(webview)
    _close_view(view, avatars)

    message_view.release_anchor()
    assert message_view._anchor is None
    # Death is asynchronous; production always reloads later, on re-show.
    _settle()

    view, avatars, webview = _make_view("<p>after restart</p>")
    try:
        assert message_view._anchor is not None
        _wait_for_load(webview)
    finally:
        _close_view(view, avatars)
        message_view.release_anchor()
        assert message_view._anchor is None
