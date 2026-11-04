"""Keyboard access and accessible names/states across the mail view.

Issue #23: the sidebar resize handles were pointer-only, and the account
and error surfaces had never been exercised for keyboard/screen-reader
use. These tests drive the resize handles through their real key
controllers, walk folders/list/reader without a pointer, and assert the
names and states assistive technology reads -- without ever asserting
exact English copy or pixel layouts.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display. The
database lives under tmp, settings use the memory backend, and
sync/watcher entry points are stubbed, so nothing touches live accounts
or the keyring.
"""

import os

os.environ.setdefault("GSETTINGS_BACKEND", "memory")

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
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
from postcard.core import APP_ID
from postcard.core.models.folder import Folder
from postcard.core.store.database import Database
from postcard.window import (
    SETTING_CONVERSATION_WIDTH,
    SETTING_FOLDER_WIDTH,
    PostcardMainWindow,
)
from postcard.window_types import PAGE_EMPTY, PAGE_MESSAGE

_KEY = bytes(range(32))
_NO_MODIFIERS = Gdk.ModifierType(0)


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
    return app, db, settings, window, sync_calls, watch_calls


def _close_window(window, db):
    window.close()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5.0
    while context.iteration(False) and time.monotonic() < deadline:
        pass
    db.close()


def _pump(seconds=2.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while context.iteration(False) and time.monotonic() < deadline:
        pass


def _present(window):
    window.present()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5.0
    while not window.get_mapped() and time.monotonic() < deadline:
        context.iteration(True)
    assert window.get_mapped()


def _add_account(db, email="ada@example.invalid"):
    # .invalid never resolves, so even a stub that leaked could dial nothing.
    return db.save_account(
        email, "Ada", "mail.example.invalid", 993, "mail.example.invalid", 587
    )


def _seed_emails(db, folder_id, subjects):
    for subject in subjects:
        # Read and without a server id: selecting one renders it without
        # spawning the flag worker or the body fetcher.
        db.save_email(
            folder_id,
            sender="Bob",
            subject=subject,
            preview="preview",
            date="Tue, 04 Nov 2026 09:00:00 +0000",
            is_unread=False,
        )
    db.reassign_conversations(folder_id)


def _mail_window(tmp_path, monkeypatch, subjects=("First", "Second", "Third")):
    """A window with one account, INBOX/Sent folders, and seeded mail."""
    made = _make_window(tmp_path, monkeypatch)
    _app, db, settings, window, _sync_calls, _watch_calls = made
    window._on_network_changed(window._network, True)
    assert window._is_online
    _add_account(db)
    account = db.accounts()[0]
    inbox = db.get_or_create_folder(account.id, "INBOX")
    db.get_or_create_folder(account.id, "Sent")
    _seed_emails(db, inbox.id, subjects)
    window.reload_accounts()
    return made


def _folder_position(window, name):
    selection = window._folder_selection
    for position in range(selection.get_n_items()):
        row = selection.get_item(position)
        assert isinstance(row, Gtk.TreeListRow)
        entry = row.get_item()
        if isinstance(entry, Folder) and entry.name == name:
            return position
    raise AssertionError(f"no folder row for {name}")


def _has_focus_within(window, container):
    focus = window.get_focus()
    # is_ancestor is asked of the descendant: focus inside container.
    return focus is not None and (focus == container or focus.is_ancestor(container))


def _key_controller(handle):
    controllers = handle.observe_controllers()
    for index in range(controllers.get_n_items()):
        controller = controllers.get_item(index)
        if isinstance(controller, Gtk.EventControllerKey):
            return controller
    raise AssertionError("no key controller on the resize handle")


def _schema_range(settings, key):
    # Settings.get_range is deprecated; the schema key carries the same.
    schema = settings.get_property("settings-schema")
    kind, bounds = schema.get_key(key).get_range().unpack()
    assert kind == "range"
    if isinstance(bounds, GLib.Variant):
        bounds = bounds.unpack()
    lower, upper = bounds
    return int(lower), int(upper)


def _sidebar_width(split):
    return int(split.get_min_sidebar_width())


def test_resize_handles_are_focusable_named_and_key_driven(tmp_path, monkeypatch):
    _app, _db, _settings, window, _sync, _watch = _mail_window(tmp_path, monkeypatch)
    try:
        for handle, split in (
            (window.folder_resize_handle, window.outer_split),
            (window.conversation_resize_handle, window.inner_split),
        ):
            assert handle.get_focusable()
            # The hover text doubles as the name a screen reader falls back
            # to; the template's accessibility label is the primary one.
            assert handle.get_tooltip_text()
            keys = _key_controller(handle)

            start = _sidebar_width(split)
            assert keys.emit("key-pressed", Gdk.KEY_Right, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == start + 10
            assert keys.emit(
                "key-pressed", Gdk.KEY_Left, 0, Gdk.ModifierType.SHIFT_MASK
            )
            assert _sidebar_width(split) == start + 10 - 50

            # A key the handle does not speak leaves the width alone.
            assert not keys.emit("key-pressed", Gdk.KEY_a, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == start + 10 - 50

            # The handle rides the trailing edge, which mirrors in RTL.
            handle.set_direction(Gtk.TextDirection.RTL)
            try:
                before = _sidebar_width(split)
                assert keys.emit("key-pressed", Gdk.KEY_Right, 0, _NO_MODIFIERS)
                assert _sidebar_width(split) == before - 10
            finally:
                handle.set_direction(Gtk.TextDirection.LTR)
    finally:
        _close_window(window, _db)


def test_resize_limits_and_reset_follow_persisted_settings(tmp_path, monkeypatch):
    _app, _db, settings, window, _sync, _watch = _mail_window(tmp_path, monkeypatch)
    try:
        for key, handle, split in (
            (
                SETTING_FOLDER_WIDTH,
                window.folder_resize_handle,
                window.outer_split,
            ),
            (
                SETTING_CONVERSATION_WIDTH,
                window.conversation_resize_handle,
                window.inner_split,
            ),
        ):
            keys = _key_controller(handle)
            lower, upper = _schema_range(settings, key)

            # Home/End land exactly on the schema's range, however wide the
            # drag limits in the constructor claim to be.
            assert keys.emit("key-pressed", Gdk.KEY_Home, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == lower
            assert keys.emit("key-pressed", Gdk.KEY_Left, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == lower
            assert keys.emit("key-pressed", Gdk.KEY_End, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == upper
            assert keys.emit("key-pressed", Gdk.KEY_Right, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == upper

            # 0 restores the width GSettings still holds -- the value a
            # restart would show -- including after settings.reset().
            settings.set_int(key, lower + 20)
            assert keys.emit("key-pressed", Gdk.KEY_End, 0, _NO_MODIFIERS)
            assert keys.emit("key-pressed", Gdk.KEY_0, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == lower + 20
            settings.reset(key)
            assert keys.emit("key-pressed", Gdk.KEY_End, 0, _NO_MODIFIERS)
            assert keys.emit("key-pressed", Gdk.KEY_0, 0, _NO_MODIFIERS)
            assert _sidebar_width(split) == settings.get_default_value(key).unpack()
    finally:
        _close_window(window, _db)


def test_keyboard_folder_list_reader_navigation(tmp_path, monkeypatch):
    subjects = ("First", "Second", "Third")
    _app, _db, _settings, window, _sync, _watch = _mail_window(
        tmp_path, monkeypatch, subjects=subjects
    )
    try:
        _present(window)

        # Arrow keys move these selection models; Tab reaches the lists.
        assert window.folder_list.get_focusable()
        assert window.conversation_list.get_focusable()
        window.folder_list.grab_focus()
        assert _has_focus_within(window, window.folder_list)

        window._folder_selection.set_selected(_folder_position(window, "Sent"))
        assert window._current_folder is not None
        assert window._current_folder.name == "Sent"
        assert window._conversation_store.get_n_items() == 0
        window._folder_selection.set_selected(_folder_position(window, "INBOX"))
        assert window._current_folder.name == "INBOX"
        assert window._conversation_store.get_n_items() == len(subjects)

        window.conversation_list.grab_focus()
        assert _has_focus_within(window, window.conversation_list)
        window._selection.select_item(0, True)
        _pump()
        assert window.reader_stack.get_visible_child_name() == PAGE_MESSAGE
        assert window.reader_subject.get_label()
        assert window.thread_box.get_first_child() is not None
        assert window.lookup_action("toggle-read").get_enabled()

        # Search is keyboard-cancelled: toggling it off clears the query and
        # brings the full list back.
        window.activate_action("win.search")
        assert window.search_bar.get_search_mode()
        assert window.search_entry.get_placeholder_text()
        window.search_entry.grab_focus()
        assert _has_focus_within(window, window.search_entry)
        window.search_entry.set_text("zzz-no-such-subject")
        window._on_search_timeout()
        assert window._conversation_store.get_n_items() == 0
        assert window.conversation_stack.get_visible_child_name() == PAGE_EMPTY
        window.activate_action("win.search")
        assert not window.search_bar.get_search_mode()
        assert window.search_entry.get_text() == ""
        window._on_search_timeout()
        assert window._conversation_store.get_n_items() == len(subjects)
    finally:
        _close_window(window, _db)


def test_list_refresh_restores_row_focus(tmp_path, monkeypatch):
    _app, _db, _settings, window, _sync, _watch = _mail_window(tmp_path, monkeypatch)
    try:
        _present(window)
        window._folder_selection.set_selected(_folder_position(window, "INBOX"))
        assert window._conversation_store.get_n_items() > 0

        keep_id = window._conversation_store.get_item(0).id
        window.conversation_list.grab_focus()
        window.conversation_list.scroll_to(0, Gtk.ListScrollFlags.FOCUS, None)
        _pump()
        assert _has_focus_within(window, window.conversation_list)

        # A refresh splices the store, recycling the focused row widget; the
        # focus must come back to the row being read, not leave the list.
        window._refresh_conversations(keep_id=keep_id)
        _pump()
        assert _has_focus_within(window, window.conversation_list)
    finally:
        _close_window(window, _db)


def test_account_dialog_validation_and_cancel(tmp_path, monkeypatch):
    _app, db, _settings, window, _sync, _watch = _make_window(tmp_path, monkeypatch)
    try:
        dialog = PostcardAccountDialog(db)
        assert not dialog.add_button.get_sensitive()
        assert dialog.cancel_button.get_label()
        assert dialog.add_button.get_label()
        for row in (
            dialog.display_name_row,
            dialog.email_row,
            dialog.username_row,
            dialog.password_row,
            dialog.imap_host_row,
            dialog.imap_port_row,
            dialog.smtp_host_row,
            dialog.smtp_port_row,
            dialog.imap_security_row,
            dialog.smtp_security_row,
        ):
            assert row.get_title()

        dialog.display_name_row.set_text("Ada")
        dialog.email_row.set_text("ada@example.invalid")
        dialog.password_row.set_text("not-a-real-password")
        dialog.imap_host_row.set_text("mail.example.invalid")
        dialog.smtp_host_row.set_text("mail.example.invalid")
        assert dialog.add_button.get_sensitive()

        # Invalid input surfaces as a disabled commit, not a crash: the
        # ports are validated, not just non-empty.
        dialog.imap_port_row.set_text("not-a-port")
        dialog.smtp_port_row.set_text("")
        assert not dialog.add_button.get_sensitive()
        dialog.imap_port_row.set_text("993")
        dialog.smtp_port_row.set_text("587")
        assert dialog.add_button.get_sensitive()

        # Cancelling adds nothing and emits nothing.
        added = []
        dialog.connect("account-added", lambda _dialog: added.append(True))
        dialog.cancel_button.emit("clicked")
        _pump()
        assert added == []
        assert db.accounts() == []
    finally:
        _close_window(window, db)


def test_sync_error_banner_names_failure_and_releases_busy(tmp_path, monkeypatch):
    _app, db, _settings, window, _sync, _watch = _mail_window(tmp_path, monkeypatch)
    try:
        account = db.accounts()[0]

        # While a sync is in flight the Refresh button goes insensitive but
        # keeps its name.
        window._set_syncing(account.id, True)
        assert not window.refresh_button.get_sensitive()
        assert window.refresh_button.get_tooltip_text()

        # A failed sync releases the busy state and names the account and
        # the reason on the banner, with a retry for transient failures.
        token = window._lifecycle.mint(account.id)
        window._on_sync_error(account, token, False, "frobnicator unreachable")
        assert window.refresh_button.get_sensitive()
        assert window.connection_banner.get_revealed()
        title = window.connection_banner.get_title()
        assert title and account.email in title
        assert window.connection_banner.get_button_label()

        # An auth failure names itself too, but offers no retry that could
        # not help.
        token = window._lifecycle.mint(account.id)
        window._on_sync_error(account, token, True, "password rejected")
        assert window.connection_banner.get_revealed()
        assert window.connection_banner.get_title()
        assert window.connection_banner.get_button_label() == ""

        window._sync_failures.clear()
        window._show_sync_failures()
        assert not window.connection_banner.get_revealed()

        # The offline banner names its state on the mail view as well.
        window._on_network_changed(window._network, False)
        assert window.connection_banner.get_revealed()
        assert window.connection_banner.get_title()
        window._on_network_changed(window._network, True)
        assert not window.connection_banner.get_revealed()
    finally:
        _close_window(window, db)


def _walk_controls(widget):
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.MenuButton):
            # A leaf: its inner toggle is toolkit-private.
            yield child
        else:
            if isinstance(child, Gtk.Button):
                yield child
            yield from _walk_controls(child)
        child = child.get_next_sibling()


def test_header_controls_have_accessible_names(tmp_path, monkeypatch):
    _app, _db, _settings, window, _sync, _watch = _mail_window(tmp_path, monkeypatch)
    try:
        # Anchored from known buttons rather than template ids, so the
        # sweep covers the header buttons without one (search, the main
        # menu, unsubscribe) too.
        bars = {
            window.compose_button.get_ancestor(Adw.HeaderBar),
            window.refresh_button.get_ancestor(Adw.HeaderBar),
            window.reply_button.get_ancestor(Adw.HeaderBar),
        }
        assert len(bars) == 3
        names = 0
        for bar in bars:
            for control in _walk_controls(bar):
                # The close/minimize/maximize and navigation back buttons
                # are toolkit-owned, not app controls.
                if control.get_ancestor(Gtk.WindowControls) is not None:
                    continue
                if isinstance(control, Gtk.Button) and control.get_action_name() in (
                    "navigation.pop",
                    "navigation.push",
                ):
                    continue
                detail = (
                    control.get_child(),
                    control.get_css_classes(),
                )
                if isinstance(control, Gtk.MenuButton):
                    assert control.get_tooltip_text(), detail
                else:
                    assert control.get_tooltip_text() or control.get_label(), detail
                names += 1
        assert names >= 14

        assert window.add_account_button.get_label()
    finally:
        _close_window(window, _db)


def test_large_text_dark_scheme_and_long_strings(tmp_path, monkeypatch):
    long_text = "L" * 300
    made = _make_window(tmp_path, monkeypatch)
    _app, db, _settings, window, _sync, _watch = made
    window._on_network_changed(window._network, True)
    _add_account(db)
    account = db.accounts()[0]
    inbox = db.get_or_create_folder(account.id, "INBOX")
    db.get_or_create_folder(account.id, f"Sent-{long_text}")
    _seed_emails(db, inbox.id, [f"Subject {long_text}", f"Re: {long_text}"])
    window.reload_accounts()

    gtk_settings = Gtk.Settings.get_default()
    previous_dpi = gtk_settings.get_property("gtk-xft-dpi")
    style = Adw.StyleManager.get_default()
    previous_scheme = style.get_color_scheme()
    # High contrast itself follows the desktop theme, which a test cannot
    # flip; the app's own stylesheet only references theme variables, so
    # rendering under large text in both schemes is the meaningful check.
    gtk_settings.set_property("gtk-xft-dpi", int(previous_dpi * 1.5))
    style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    try:
        _present(window)
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        _pump()

        window._folder_selection.set_selected(_folder_position(window, "INBOX"))
        assert window._conversation_store.get_n_items() == 2
        window._selection.select_item(0, True)
        _pump()
        assert window.reader_stack.get_visible_child_name() == PAGE_MESSAGE
        assert long_text in window.reader_subject.get_label()

        window._folder_selection.set_selected(
            _folder_position(window, f"Sent-{long_text}")
        )
        assert window._current_folder is not None
        assert window._current_folder.name.startswith("Sent-")
    finally:
        gtk_settings.set_property("gtk-xft-dpi", previous_dpi)
        style.set_color_scheme(previous_scheme)
        _close_window(window, db)
