"""Session lock: blank the window and drop state, restore on activity.

KeePassXC model, ADR 0001 section 7: on org.gnome.ScreenSaver ActiveChanged
and org.freedesktop.login1 PrepareForSleep the window shows the locked page,
stops its watchers, drops its pooled sessions, and closes the mail cache;
the matching return signal (or any focus) re-opens the cache through the
normal keyring path and restores the mail view. Unlock is automatic -- no
extra password -- and explicitly not a cryptographic boundary.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display. The database
lives under tmp, settings use the memory backend, and sync/watcher/keyring
entry points are stubbed, so nothing touches live accounts or the keyring.
"""

import os

os.environ.setdefault("GSETTINGS_BACKEND", "memory")

import time

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

from postcard import mail_sync  # noqa: E402
from postcard.core import APP_ID  # noqa: E402
from postcard.core.models.mailbox import MailboxInfo  # noqa: E402
from postcard.core.models.message_header import MessageHeader  # noqa: E402
from postcard.core.store.database import CacheError, Database  # noqa: E402
from postcard.mail_sync import SyncResult  # noqa: E402
from postcard.window import PAGE_LOCKED, PostcardMainWindow  # noqa: E402
from postcard.window_types import (  # noqa: E402
    PAGE_MAIL,
    PAGE_NO_ACCOUNT,
    OutboxResult,
)

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


def _make_window(tmp_path, monkeypatch, *, with_mail=True):
    """A window whose sync/watcher/keyring seams record, not dial.

    Database.__init__ is redirected to the tmp cache so the unlock's re-open
    exercises the real open path against the test database; its arguments are
    recorded to prove the unlock went through the keyring for the key.
    """
    db_path = str(tmp_path / "postcard.db")
    real_init = Database.__init__
    init_calls = []
    key_calls = []
    sync_calls = []
    watch_calls = []
    close_calls = []

    def fake_init(self, path=None, *, key=None):
        init_calls.append((path, key))
        real_init(self, db_path, key=key)

    real_close = mail_sync.close_sessions

    def record_close(account_id=None):
        close_calls.append(account_id)
        return real_close(account_id)

    monkeypatch.setattr(Database, "__init__", fake_init)

    def fake_database_key():
        key_calls.append(_KEY)
        return _KEY

    monkeypatch.setattr("postcard.core.secrets.database_key", fake_database_key)
    monkeypatch.setattr("postcard.core.secrets.credential_for", lambda account: None)
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
    monkeypatch.setattr(mail_sync, "close_sessions", record_close)

    db = Database(db_path, key=_KEY)
    account = None
    if with_mail:
        account = db.save_account(
            "ada@example.com", "Ada", "imap.example.com", 993, "s", 587
        )
        inbox = db.get_or_create_folder(account.id, "INBOX")
        db.save_email(inbox.id, "Bob", "hi", "hi", "now", False)
        db.save_email(inbox.id, "Bob", "hello again", "hello again", "now", False)
    settings = Gio.Settings(schema_id=APP_ID)
    app = _GuiTestApp()
    window = PostcardMainWindow(app, db, settings)
    # The sandbox may or may not report a network; the tests drive online.
    window._is_online = True
    calls = {
        "init": init_calls,
        "key": key_calls,
        "sync": sync_calls,
        "watch": watch_calls,
        "close": close_calls,
    }
    for recorded in calls.values():
        recorded.clear()
    return app, db, window, account, settings, calls


def _close_window(window, settings, db):
    settings.set_boolean("run-in-background", False)
    assert window._on_close_request(window) is False
    window.destroy()
    # Safe on an already-closed cache: close() swallows the checkpoint and a
    # second close is a no-op.
    db.close()


@pytest.fixture(autouse=True)
def drain_main_loop():
    yield
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 60
    while context.pending() and time.monotonic() < deadline:
        context.iteration(False)


def _screensaver(window, active):
    """A fake org.gnome.ScreenSaver ActiveChanged signal."""
    window._on_screensaver_active_changed(
        None, None, "", "", "", GLib.Variant("(b)", (active,)), None
    )


def _sleep(window, starting):
    """A fake org.freedesktop.login1 PrepareForSleep signal."""
    window._on_prepare_for_sleep(
        None, None, "", "", "", GLib.Variant("(b)", (starting,)), None
    )


def _message(uid, subject):
    return MessageHeader(
        uid=uid,
        sender="Bob",
        sender_address="bob@example.com",
        recipient="",
        recipient_address="",
        subject=subject,
        date="",
        is_unread=False,
    )


def _sync_result(*subjects, folder="INBOX"):
    return SyncResult(
        folders=[MailboxInfo(folder, ".", "")],
        messages=[
            _message(str(101 + index), subject)
            for index, subject in enumerate(subjects)
        ],
        folder=folder,
        exists=len(subjects),
        offset=0,
        all_uids={str(101 + index) for index in range(len(subjects))},
        unread_counts={},
        uidvalidity=1,
    )


def test_lock_signal_blanks_mail_and_drops_state(tmp_path, monkeypatch):
    _app, db, window, _account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL

        _screensaver(window, True)

        assert window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_LOCKED
        assert calls["close"] == [None]
        with pytest.raises(Exception, match="closed"):
            db.accounts()
        assert not window.connection_banner.get_revealed()

        # A second lock while locked changes nothing.
        _screensaver(window, True)
        assert calls["close"] == [None]
        assert window.main_stack.get_visible_child_name() == PAGE_LOCKED
    finally:
        _close_window(window, settings, db)


def test_unlock_signal_restores_mail(tmp_path, monkeypatch):
    _app, db, window, account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        _screensaver(window, True)
        _screensaver(window, False)

        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        # The cache re-opened through the keyring path: the key came from the
        # keyring, and the path is the default one, like the first open.
        assert calls["key"] == [_KEY]
        assert calls["init"] == [(None, _KEY)]
        assert [item.email for item in db.accounts()] == ["ada@example.com"]
        [inbox] = db.folders_for_account(account.id)
        assert sorted(mail.subject for mail in db.emails_in_folder(inbox.id)) == [
            "hello again",
            "hi",
        ]
        # Watchers restart and the unlock syncs what arrived meanwhile.
        assert calls["watch"] == [[account.id]]
        assert calls["sync"] == [account.id]
    finally:
        _close_window(window, settings, db)


def test_prepare_for_sleep_locks_and_resume_unlocks(tmp_path, monkeypatch):
    _app, db, window, account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        _sleep(window, True)

        assert window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_LOCKED
        assert calls["close"] == [None]
        with pytest.raises(Exception, match="closed"):
            db.accounts()

        _sleep(window, False)

        assert not window._screensaver_active
        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert db.accounts() != []
        assert calls["watch"] == [[account.id]]
        assert calls["sync"] == [account.id]
    finally:
        _close_window(window, settings, db)


def test_resume_while_screen_locked_stays_locked(tmp_path, monkeypatch):
    _app, db, window, _account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        _screensaver(window, True)
        _sleep(window, True)
        assert calls["close"] == [None]

        # The lock screen is still up: the resume must not bring the mail back.
        _sleep(window, False)
        assert window._screensaver_active
        assert window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_LOCKED
        with pytest.raises(Exception, match="closed"):
            db.accounts()

        _screensaver(window, False)
        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert db.accounts() != []
    finally:
        _close_window(window, settings, db)


def test_unlock_signal_when_unlocked_is_a_noop(tmp_path, monkeypatch):
    _app, db, window, _account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        _screensaver(window, False)

        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert calls["key"] == []
        assert calls["init"] == []
    finally:
        _close_window(window, settings, db)


def test_lock_and_unlock_with_no_accounts(tmp_path, monkeypatch):
    _app, db, window, _account, settings, calls = _make_window(
        tmp_path, monkeypatch, with_mail=False
    )
    try:
        assert window.main_stack.get_visible_child_name() == PAGE_NO_ACCOUNT

        _screensaver(window, True)
        assert window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_LOCKED
        with pytest.raises(Exception, match="closed"):
            db.accounts()

        _screensaver(window, False)
        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_NO_ACCOUNT
        assert db.accounts() == []
        assert calls["sync"] == []
    finally:
        _close_window(window, settings, db)


def test_sync_completion_waits_for_unlock(tmp_path, monkeypatch):
    _app, db, window, account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        token = window._lifecycle.mint(account.id)
        _screensaver(window, True)

        assert window._on_sync_done(account, token, _sync_result("new mail")) is False
        assert len(window._deferred_completions) == 1
        assert account.id not in window._syncing_account_ids

        _screensaver(window, False)

        assert window._deferred_completions == []
        [inbox] = db.folders_for_account(account.id)
        assert "new mail" in [mail.subject for mail in db.emails_in_folder(inbox.id)]
        assert calls["sync"] == [account.id]
    finally:
        _close_window(window, settings, db)


def test_outbox_result_waits_for_unlock(tmp_path, monkeypatch):
    _app, db, window, account, settings, _calls = _make_window(tmp_path, monkeypatch)
    try:
        outbox = db.get_or_create_folder(account.id, mail_sync.OUTBOX_FOLDER)
        queued = db.save_email(outbox.id, "Ada", "queued", "queued", "now", False)
        db.save_raw_message(queued.id, b"raw-bytes")
        db.queue_outbox_email(queued.id, ["you@example.com"])
        token = window._lifecycle.mint(account.id)
        _screensaver(window, True)

        # The worker finished the send; dropping it here would queue the mail
        # to go out a second time.
        assert (
            window._on_outbox_drained(
                account,
                token,
                [OutboxResult(queued.id, "queued", b"raw-bytes", None)],
            )
            is False
        )
        assert len(window._deferred_completions) == 1

        _screensaver(window, False)

        sent = db.get_folder_by_name(account.id, mail_sync.SENT_FOLDER)
        assert sent is not None
        assert [mail.subject for mail in db.emails_in_folder(sent.id)] == ["queued"]
        assert db.emails_in_folder(outbox.id) == []
    finally:
        _close_window(window, settings, db)


def test_background_entries_skip_while_locked(tmp_path, monkeypatch):
    _app, db, window, account, settings, calls = _make_window(tmp_path, monkeypatch)
    try:
        _screensaver(window, True)

        assert window._on_sync_tick() is True
        window._on_network_changed(window._network, False)
        assert not window._is_online
        assert not window.connection_banner.get_revealed()
        window._on_network_changed(window._network, True)
        assert window._is_online
        assert window._on_inbox_changed(account.id) is False
        assert window._selected_conversations() == []
        # Each of these reads the cache or starts work; all stay quiet.
        window._refresh_conversations()
        window._update_reader()
        window._reload_folders()
        window.reload_accounts()
        window.open_email(123, "456")
        window.open_mailto("mailto:ada@example.invalid")
        window._drain_outbox(account)
        window._sync_all()
        window._on_composer_finished(None, "")
        window._on_undo_move(None)
        window._on_account_added(None)
        assert calls["sync"] == []
        assert calls["watch"] == []

        _screensaver(window, False)

        # The deferred inbox ping syncs, and then the unlock syncs.
        assert calls["sync"] == [account.id, account.id]
        assert calls["watch"] == [[account.id]]
    finally:
        _close_window(window, settings, db)


def test_unlock_button_unlocks(tmp_path, monkeypatch):
    _app, db, window, _account, settings, _calls = _make_window(tmp_path, monkeypatch)
    try:
        _screensaver(window, True)
        window.unlock_button.emit("clicked")

        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert db.accounts() != []
    finally:
        _close_window(window, settings, db)


def test_failed_reopen_stays_locked_and_retries(tmp_path, monkeypatch):
    _app, db, window, _account, settings, _calls = _make_window(tmp_path, monkeypatch)
    real_reopen = PostcardMainWindow._reopen_cache
    attempts = []

    def flaky_reopen(self):
        attempts.append(1)
        if len(attempts) == 1:
            raise CacheError("key_unavailable")
        return real_reopen(self)

    monkeypatch.setattr(PostcardMainWindow, "_reopen_cache", flaky_reopen)
    try:
        _screensaver(window, True)
        _screensaver(window, False)

        assert window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_LOCKED
        with pytest.raises(Exception, match="closed"):
            db.accounts()

        _screensaver(window, False)
        assert not window._is_locked
        assert window.main_stack.get_visible_child_name() == PAGE_MAIL
        assert db.accounts() != []
    finally:
        _close_window(window, settings, db)


def test_move_timer_pauses_across_lock(tmp_path, monkeypatch):
    _app, db, window, _account, settings, _calls = _make_window(tmp_path, monkeypatch)
    try:
        window._pending_move_timeout = GLib.timeout_add(5000, lambda: False)
        window._move_deadline = time.monotonic() + 5

        _screensaver(window, True)
        assert window._pending_move_timeout == 0
        assert 0 < window._move_resume_ms <= 5000

        _screensaver(window, False)
        assert window._pending_move_timeout != 0
        assert window._move_resume_ms == 0

        GLib.source_remove(window._pending_move_timeout)
        window._pending_move_timeout = 0
    finally:
        _close_window(window, settings, db)
