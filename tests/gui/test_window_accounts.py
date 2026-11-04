"""Adding, removing and re-adding accounts through the real window paths.

Accounts are added to a tmp database and removed through the remove buttons
of a real accounts dialog; the window learns about both through the same
`account-added` signal and `reload_accounts` the production dialogs use.
Sync and inbox-watcher entry points are stubbed to record rather than dial,
so no test touches a live server or the keyring.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display.
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
from postcard.account_dialog import PostcardAccountDialog
from postcard.accounts_dialog import PostcardAccountsDialog
from postcard.core import APP_ID, secrets
from postcard.core.store.database import Database
from postcard.window import PostcardMainWindow
from postcard.window_types import PAGE_EMPTY, PAGE_MAIL, PAGE_NO_ACCOUNT

_KEY = bytes(range(32))


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


def _add_account(db, email="ada@example.invalid"):
    # .invalid never resolves, so even a stub that leaked could dial nothing.
    account = db.save_account(
        email, "Ada", "mail.example.invalid", 993, "mail.example.invalid", 587
    )
    db.get_or_create_folder(account.id, "INBOX")
    return account


def _go_online(window):
    # Deterministic whatever the sandbox network is doing: a no-op when
    # already online, a real flip (banner and all) when not.
    window._on_network_changed(window._network, True)
    assert window._is_online


def _first_button(widget):
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            return child
        found = _first_button(child)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _remove_account_via_dialog(db, monkeypatch, email):
    """Click the account's remove button in a real accounts dialog."""
    cleared = []
    monkeypatch.setattr(
        secrets, "clear_password", lambda account_id: cleared.append(account_id)
    )
    dialog = PostcardAccountsDialog(db)
    for row in dialog._rows:
        if row.get_title() == email:
            button = _first_button(row)
            assert button is not None
            button.emit("clicked")
            return cleared
    raise AssertionError(f"no dialog row for {email}")


def test_first_account_loads_the_mail_view_and_syncs(tmp_path, monkeypatch):
    app, db, window, sync_calls, watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        assert window.main_stack.get_visible_child_name() == PAGE_NO_ACCOUNT
        _go_online(window)

        account = _add_account(db)
        window.reload_accounts()

        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert window._account is not None
        assert window._account.email == account.email
        assert window._current_folder is not None
        assert window._current_folder.name == "INBOX"
        assert window.conversation_stack.get_visible_child_name() == PAGE_EMPTY
        # A sync was requested for the new account, and watchers reconciled.
        assert sync_calls
        assert set(sync_calls) == {account.id}
        assert watch_calls and watch_calls[-1] == [account.id]
        # One account branch, and no All Inboxes row for a single account.
        assert window._folder_root_store.get_n_items() == 1
        assert app.tray.unread_pushes and app.tray.unread_pushes[-1] == 0
    finally:
        _close_window(window, db)


def test_adding_an_account_offline_skips_sync_until_reconnect(tmp_path, monkeypatch):
    _app, db, window, sync_calls, watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        window._on_network_changed(window._network, False)
        assert not window._is_online

        account = _add_account(db)
        window.reload_accounts()

        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert sync_calls == []
        assert all(call == [] for call in watch_calls)

        window._on_network_changed(window._network, True)
        assert not window.connection_banner.get_revealed()
        assert set(sync_calls) == {account.id}
    finally:
        _close_window(window, db)


def test_second_account_appears_beside_the_first(tmp_path, monkeypatch):
    _app, db, window, _sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        _go_online(window)
        first = _add_account(db, "ada@example.invalid")
        window.reload_accounts()
        assert window._current_folder.account_id == first.id

        second = _add_account(db, "bob@example.invalid")
        window.reload_accounts()

        assert set(window._accounts) == {first.id, second.id}
        # All Inboxes plus one branch per account.
        assert window._folder_root_store.get_n_items() == 3
        # The later account only adds a branch; the open folder stays put.
        assert window._current_folder.account_id == first.id
        assert window._current_folder.name == "INBOX"
    finally:
        _close_window(window, db)


def test_first_account_added_signal_builds_the_mail_view(tmp_path, monkeypatch):
    _app, db, window, sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        _go_online(window)
        dialog = PostcardAccountDialog(db)
        dialog.connect("account-added", window._on_account_added)

        account = _add_account(db)
        dialog.emit("account-added")

        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert window._account is not None
        assert window._account.email == account.email
        assert sync_calls
        assert set(sync_calls) == {account.id}
    finally:
        _close_window(window, db)


def test_later_account_added_signal_syncs_the_newest(tmp_path, monkeypatch):
    _app, db, window, sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        _go_online(window)
        first = _add_account(db, "ada@example.invalid")
        window.reload_accounts()
        assert window._current_folder is not None
        dialog = PostcardAccountDialog(db)
        dialog.connect("account-added", window._on_account_added)
        sync_calls.clear()

        newest = _add_account(db, "bob@example.invalid")
        dialog.emit("account-added")

        assert set(window._accounts) == {first.id, newest.id}
        assert sync_calls == [newest.id]
        assert window._folder_root_store.get_n_items() == 3
    finally:
        _close_window(window, db)


def test_remove_account_returns_to_the_empty_state(tmp_path, monkeypatch):
    app, db, window, _sync_calls, watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        _go_online(window)
        account = _add_account(db)
        window.reload_accounts()
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL

        cleared = _remove_account_via_dialog(db, monkeypatch, account.email)

        assert db.accounts() == []
        assert cleared == [account.id]
        window.reload_accounts()
        assert window.main_stack.get_visible_child_name() == PAGE_NO_ACCOUNT
        assert window._account is None
        assert window._accounts == {}
        assert window._current_folder is None
        assert app.tray.unread_pushes and app.tray.unread_pushes[-1] == 0
        assert watch_calls and watch_calls[-1] == []
    finally:
        _close_window(window, db)


def test_readded_account_rebuilds_a_single_branch(tmp_path, monkeypatch):
    _app, db, window, _sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        _go_online(window)
        account = _add_account(db)
        window.reload_accounts()
        cleared = _remove_account_via_dialog(db, monkeypatch, account.email)
        assert cleared == [account.id]
        window.reload_accounts()
        assert window.main_stack.get_visible_child_name() == PAGE_NO_ACCOUNT

        # Stale per-folder state must not survive the account that owned it.
        for cache in (
            window._loaded_counts,
            window._folders_with_more_mail,
            window._folder_sync_times,
            window._remote_unread_counts,
        ):
            cache[999] = 1

        reread = _add_account(db)
        window.reload_accounts()

        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert window._folder_root_store.get_n_items() == 1
        assert window._current_folder.name == "INBOX"
        assert window._current_folder.account_id == reread.id
        for cache in (
            window._loaded_counts,
            window._folders_with_more_mail,
            window._folder_sync_times,
            window._remote_unread_counts,
        ):
            assert 999 not in cache
    finally:
        _close_window(window, db)


def test_removing_one_of_two_keeps_the_other_open(tmp_path, monkeypatch):
    _app, db, window, _sync_calls, _watch_calls = _make_window(tmp_path, monkeypatch)
    try:
        _go_online(window)
        first = _add_account(db, "ada@example.invalid")
        second = _add_account(db, "bob@example.invalid")
        window.reload_accounts()
        assert window._current_folder is window._all_inboxes_row

        _remove_account_via_dialog(db, monkeypatch, first.email)
        window.reload_accounts()

        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert set(window._accounts) == {second.id}
        # Back to a single branch: All Inboxes leaves with the second account.
        assert window._folder_root_store.get_n_items() == 1
        assert window._current_folder.account_id == second.id
        assert window._account.id == second.id
    finally:
        _close_window(window, db)
