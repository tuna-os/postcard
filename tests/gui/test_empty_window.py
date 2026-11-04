"""The main window on an empty database: startup, actions, offline flips.

"No account" is a real state -- the mail view is skipped entirely while
background callbacks can still fire -- so every window action, accelerator
and network flip here must be a safe no-op rather than a crash.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display. The database
lives under tmp, settings use the memory backend, and sync/watcher entry
points are stubbed, so nothing touches live accounts or the keyring.
"""

import os

os.environ.setdefault("GSETTINGS_BACKEND", "memory")

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Adw, Gio, GLib, Gtk
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

import time

from postcard import mail_sync
from postcard.core import APP_ID
from postcard.core.store.database import Database
from postcard.window import PostcardMainWindow
from postcard.window_types import MAIL_ACTIONS, PAGE_NO_ACCOUNT, REPLY_FORWARD_ACTIONS

_KEY = bytes(range(32))

# The win.* actions carrying accelerators, registered in _setup_actions.
_ACCEL_ACTIONS = (
    "toggle-read",
    "toggle-star",
    "archive",
    "trash",
    "compose",
    "reply",
    "reply-all",
    "forward",
    "refresh",
    "search",
)


class _TrayStub:
    """Stands in for the tray icon and records badge pushes."""

    def __init__(self):
        self.unread_pushes = []

    def set_unread(self, count):
        self.unread_pushes.append(count)


class _GuiTestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="in.gxanshu.postcard.guitest")
        self.tray = _TrayStub()


def _make_window(tmp_path, monkeypatch):
    """An empty-database window whose sync/watcher seams record, not dial."""
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    settings = Gio.Settings(schema_id=APP_ID)
    app = _GuiTestApp()
    sync_calls = []
    watch_calls = []
    monkeypatch.setattr(
        PostcardMainWindow,
        "_start_sync",
        lambda self, account, **kwargs: sync_calls.append(account.id),
    )
    monkeypatch.setattr(
        mail_sync,
        "watch_inboxes",
        lambda accounts, credential_for, on_change: watch_calls.append(
            [account.id for account in accounts]
        ),
    )
    window = PostcardMainWindow(app, db, settings)
    return app, db, window, sync_calls, watch_calls


def _close_window(window, db):
    window.close()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5.0
    while context.iteration(False) and time.monotonic() < deadline:
        pass
    db.close()


def test_starts_on_the_no_account_page(tmp_path, monkeypatch):
    _app, db, window, _sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        assert window.main_stack.get_visible_child_name() == PAGE_NO_ACCOUNT
        assert window._account is None
        assert window._sync_timer_id == 0
        assert window._syncing_account_ids == set()
        assert window._conversation_store.get_n_items() == 0

        # Mapping runs _on_map; closing runs the full cleanup path.
        window.present()
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5.0
        while not window.get_mapped() and time.monotonic() < deadline:
            context.iteration(True)
        assert window.get_mapped()
    finally:
        _close_window(window, db)


def test_mail_actions_start_disabled(tmp_path, monkeypatch):
    _app, db, window, _sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        for name in (*MAIL_ACTIONS, *REPLY_FORWARD_ACTIONS, "unsubscribe"):
            action = window.lookup_action(name)
            assert isinstance(action, Gio.SimpleAction)
            assert not action.get_enabled()
    finally:
        _close_window(window, db)


def test_window_actions_are_safe_without_accounts(tmp_path, monkeypatch):
    app, db, window, sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        window_count = len(app.get_windows())
        for name in _ACCEL_ACTIONS:
            # Detailed names, as the accelerators carry them.
            window.activate_action(f"win.{name}")
            assert db.accounts() == []
            assert len(app.get_windows()) == window_count
            assert window._account is None
        assert sync_calls == []

        # The one action carrying a parameter takes it quietly too.
        window.activate_action("win.move", GLib.Variant("s", "INBOX"))
        assert db.accounts() == []

        # Search is the only one with visible behaviour: it toggles the bar.
        assert window.search_bar.get_search_mode()
        window.activate_action("win.search")
        assert not window.search_bar.get_search_mode()
    finally:
        _close_window(window, db)


def test_accelerators_are_registered(tmp_path, monkeypatch):
    app, db, window, _sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        for name in _ACCEL_ACTIONS:
            assert app.get_accels_for_action(f"win.{name}"), name
    finally:
        _close_window(window, db)


def test_offline_flip_shows_and_hides_the_banner(tmp_path, monkeypatch):
    _app, db, window, sync_calls, watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        window._on_network_changed(window._network, False)
        assert not window._is_online
        assert window.connection_banner.get_revealed()
        # A duplicate signal changes nothing.
        window._on_network_changed(window._network, False)
        assert not window._is_online
        assert window.connection_banner.get_revealed()

        window._on_network_changed(window._network, True)
        assert window._is_online
        assert not window.connection_banner.get_revealed()
        # Nothing to sync and no watchers to start without accounts.
        assert sync_calls == []
        assert watch_calls and watch_calls[-1] == []

        watches = len(watch_calls)
        window._on_network_changed(window._network, True)
        assert len(watch_calls) == watches
    finally:
        _close_window(window, db)


def test_compose_and_notification_entries_guard_without_accounts(tmp_path, monkeypatch):
    app, db, window, sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        window_count = len(app.get_windows())
        window.open_email(123, "456")
        window.open_mailto("mailto:ada@example.invalid")
        window._on_compose_clicked()
        assert window._account is None
        assert len(app.get_windows()) == window_count
        assert sync_calls == []
    finally:
        _close_window(window, db)
