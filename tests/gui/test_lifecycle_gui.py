"""Lifecycle generations: stale completions must mutate nothing.

A completion that lands after its account was removed (possibly re-added under
the same reused rowid), its folder pruned, or its window closed must drop its
result -- while releasing the busy state it held.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display.
"""

import os
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk
except (ImportError, ValueError):
    pytest.skip("GTK/Adw typelibs unavailable", allow_module_level=True)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    pytest.skip("no display", allow_module_level=True)

os.environ.setdefault("GTK_A11Y", "none")
Adw.init()


def _register_resources():
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
from postcard.accounts_dialog import PostcardAccountsDialog  # noqa: E402
from postcard.composer_window import PostcardComposerWindow  # noqa: E402
from postcard.core.models.mailbox import MailboxInfo  # noqa: E402
from postcard.core.models.message_header import MessageHeader  # noqa: E402
from postcard.core.store.database import Database  # noqa: E402
from postcard.mail_sync import SyncResult  # noqa: E402
from postcard.window import PostcardMainWindow  # noqa: E402
from postcard.window_types import OutboxResult  # noqa: E402

_KEY = bytes(range(32))


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


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "postcard.db"), key=_KEY)
    yield database
    database.close()


@pytest.fixture
def settings():
    return Gio.Settings(schema_id="in.gxanshu.postcard")


@pytest.fixture
def offline(monkeypatch):
    """Construction syncs against example.com; neuter what they can reach."""
    monkeypatch.setattr("postcard.mail_sync.watch_inboxes", lambda *args: None)
    monkeypatch.setattr("postcard.core.secrets.credential_for", lambda account: None)
    monkeypatch.setattr("postcard.core.secrets.clear_password", lambda account_id: None)


def _account(db, email="ada@example.com"):
    return db.save_account(email, "Ada", "imap.example.com", 993, "s", 587)


def _remove(db, account_id):
    PostcardAccountsDialog(db)._on_remove_clicked(Gtk.Button(), account_id)


def _close(window, settings):
    """Run the real-quit path: bare destroy() skips close-request, leaving the
    network handler connected and queued completions live past the db close."""
    settings.set_boolean("run-in-background", False)
    assert window._on_close_request(window) is False
    window.destroy()


@pytest.fixture(autouse=True)
def drain_main_loop():
    yield
    # These tests never pump, so each leaves its completions queued -- and the
    # first pump after a composer additionally eats webkit's one-off ~25s
    # settle. Drain both here: whoever pumps next (here or in another module)
    # must not pay for this test's backlog.
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 60
    while context.pending() and time.monotonic() < deadline:
        context.iteration(False)


def _all_subjects(db):
    subjects = []
    for account in db.accounts():
        for folder in db.folders_for_account(account.id):
            subjects.extend(mail.subject for mail in db.emails_in_folder(folder.id))
    return subjects


def test_a_sync_past_remove_and_readd_files_nothing(db, settings, offline):
    old = _account(db)
    window = PostcardMainWindow(None, db, settings)
    token = window._lifecycle.mint(old.id)
    window._syncing_account_ids.add(old.id)

    _remove(db, old.id)
    new = _account(db, "new@example.com")
    assert new.id == old.id
    window.reload_accounts()

    assert window._on_sync_done(old, token, _sync_result("old mail")) is False

    assert db.folders_for_account(new.id) == []
    assert old.id not in window._syncing_account_ids
    assert old.id not in window._sync_failures
    _close(window, settings)


def test_a_live_sync_still_files(db, settings, offline):
    account = _account(db)
    window = PostcardMainWindow(None, db, settings)
    token = window._lifecycle.mint(account.id)

    assert window._on_sync_done(account, token, _sync_result("new mail")) is False

    [folder] = db.folders_for_account(account.id)
    assert folder.name == "INBOX"
    assert [mail.subject for mail in db.emails_in_folder(folder.id)] == ["new mail"]
    _close(window, settings)


def test_a_queued_send_past_removal_recreates_no_sent_folder(db, settings, offline):
    account = _account(db)
    outbox = db.get_or_create_folder(account.id, mail_sync.OUTBOX_FOLDER)
    queued = db.save_email(outbox.id, "Ada", "queued", "queued", "now", False)
    db.save_raw_message(queued.id, b"raw-bytes")
    window = PostcardMainWindow(None, db, settings)
    token = window._lifecycle.mint(account.id)
    window._draining_account_ids.add(account.id)

    _remove(db, account.id)
    new = _account(db, "new@example.com")
    assert new.id == account.id

    drained = window._on_outbox_drained(
        account, token, [OutboxResult(queued.id, "queued", b"raw-bytes", None)]
    )

    assert drained is False
    assert db.folders_for_account(new.id) == []
    assert account.id not in window._draining_account_ids
    _close(window, settings)


def test_a_body_past_a_folder_replacement_lands_nowhere(db, settings, offline):
    account = _account(db)
    inbox = db.get_or_create_folder(account.id, "INBOX")
    old = db.save_email(inbox.id, "Bob", "old", "old", "now", False)
    window = PostcardMainWindow(None, db, settings)
    token = window._lifecycle.mint(account.id, "INBOX")

    # A sync whose listing no longer has the INBOX prunes it, which ends the
    # folder's jobs the way a UIDVALIDITY change does.
    window._on_sync_done(
        account, window._lifecycle.mint(account.id), _sync_result(folder="Archive")
    )
    assert db.get_folder_by_name(account.id, "INBOX") is None
    fresh_inbox = db.get_or_create_folder(account.id, "INBOX")
    fresh = db.save_email(fresh_inbox.id, "Bob", "new", "new", "now", False)

    delivered = []
    result = window._deliver_body(
        lambda raw, error: delivered.append((raw, error)),
        old.id,
        b"old raw bytes",
        None,
        token,
    )

    assert result is False
    assert delivered == []
    assert db.get_raw_message(fresh.id) is None
    _close(window, settings)


def test_a_live_body_still_delivers(db, settings, offline):
    account = _account(db)
    inbox = db.get_or_create_folder(account.id, "INBOX")
    mail = db.save_email(inbox.id, "Bob", "hi", "hi", "now", False)
    window = PostcardMainWindow(None, db, settings)
    token = window._lifecycle.mint(account.id, "INBOX")

    delivered = []
    result = window._deliver_body(
        lambda raw, error: delivered.append((raw, error)),
        mail.id,
        b"raw-bytes",
        None,
        token,
    )

    assert result is False
    assert delivered == [(b"raw-bytes", None)]
    assert db.get_raw_message(mail.id) == b"raw-bytes"
    _close(window, settings)


def test_completions_past_window_close_touch_no_widgets(db, settings, offline):
    account = _account(db)
    window = PostcardMainWindow(None, db, settings)
    token = window._lifecycle.mint(account.id)
    window._syncing_account_ids.add(account.id)
    settings.set_boolean("run-in-background", False)
    window.present()
    window.close()

    assert window._lifecycle.is_shutdown
    assert window._on_sync_done(account, token, _sync_result("old")) is False
    assert window._on_sync_error(account, token, False, "nope") is False
    assert window._on_outbox_drained(account, token, []) is False
    assert window._on_inbox_changed(account.id) is False
    assert db.folders_for_account(account.id) == []
    assert account.id not in window._syncing_account_ids


def test_removing_one_account_spares_the_others_sync(db, settings, offline):
    old_a = _account(db, "a@example.com")
    account_b = _account(db, "b@example.com")
    window = PostcardMainWindow(None, db, settings)
    token_a = window._lifecycle.mint(old_a.id)
    token_b = window._lifecycle.mint(account_b.id)

    _remove(db, old_a.id)
    window.reload_accounts()

    assert not window._lifecycle.is_live(token_a)
    assert window._lifecycle.is_live(token_b)
    window._on_sync_done(old_a, token_a, _sync_result("a old"))
    window._on_sync_done(account_b, token_b, _sync_result("b new"))

    assert db.folders_for_account(old_a.id) == []
    assert _all_subjects(db) == ["b new"]
    _close(window, settings)


def test_a_composer_send_past_removal_files_no_sent_copy(db, settings, offline):
    account = _account(db)
    outbox = db.get_or_create_folder(account.id, mail_sync.OUTBOX_FOLDER)
    queued = db.save_email(outbox.id, "Ada", "queued", "queued", "now", False)
    db.save_raw_message(queued.id, b"raw-bytes")
    composer = PostcardComposerWindow(None, db, account)
    token = composer._lifecycle.mint(account.id)

    _remove(db, account.id)
    new = _account(db, "new@example.com")
    assert new.id == account.id

    assert (
        composer._on_send_done(account, queued.id, "queued", b"raw-bytes", token)
        is False
    )

    assert db.folders_for_account(new.id) == []
    assert _all_subjects(db) == []


def test_a_live_composer_send_files_its_sent_copy(db, settings, offline):
    account = _account(db)
    outbox = db.get_or_create_folder(account.id, mail_sync.OUTBOX_FOLDER)
    queued = db.save_email(outbox.id, "Ada", "queued", "queued", "now", False)
    db.save_raw_message(queued.id, b"raw-bytes")
    composer = PostcardComposerWindow(None, db, account)
    finished = []
    composer.connect("finished", lambda _c, failure: finished.append(failure))
    token = composer._lifecycle.mint(account.id)

    assert (
        composer._on_send_done(account, queued.id, "queued", b"raw-bytes", token)
        is False
    )

    assert finished == [""]
    assert _all_subjects(db) == ["queued"]
    sent = db.get_folder_by_name(account.id, mail_sync.SENT_FOLDER)
    assert sent is not None
    assert db.emails_in_folder(outbox.id) == []
