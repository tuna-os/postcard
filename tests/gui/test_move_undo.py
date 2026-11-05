"""Archive/trash/move through the real window paths: the undo window, the
server result, and the partial failure in between.

A move lands in the database first and reaches the server only after the
undo window elapses; Undo must put every mail back at its source UID, a
completed move must file the UIDs the server reported, and a move that
fails part-way must file what crossed and restore the tail. The
completions are driven directly, the way the lifecycle suite drives
sync and drain completions, so no test touches a live server.

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
    from gi.repository import Adw, Gio, GLib
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

import time

from postcard.core.models.conversation import Conversation  # noqa: E402
from postcard.core.net import MoveResult  # noqa: E402
from postcard.core.store.database import Database  # noqa: E402
from postcard.window import PostcardMainWindow  # noqa: E402

_KEY = bytes(range(32))


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


def _mailbox(db, account):
    """Two mails with server UIDs in the INBOX beside an empty Archive."""
    inbox = db.get_or_create_folder(account.id, "INBOX")
    archive = db.get_or_create_folder(account.id, "Archive")
    first = db.save_email(inbox.id, "Bob", "one", "one", "now", False, server_id="101")
    second = db.save_email(inbox.id, "Bob", "two", "two", "now", False, server_id="102")
    return inbox, archive, Conversation([first, second])


def _close(window, settings):
    """Run the real-quit path: bare destroy() skips close-request, leaving the
    network handler connected and queued completions live past the db close."""
    settings.set_boolean("run-in-background", False)
    assert window._on_close_request(window) is False
    window.destroy()


@pytest.fixture(autouse=True)
def drain_main_loop():
    yield
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 60
    while context.pending() and time.monotonic() < deadline:
        context.iteration(False)


def _uids(db, folder):
    # Order is the listing's business, not the move's: compare sorted.
    return sorted(
        (mail.subject, mail.server_id) for mail in db.emails_in_folder(folder.id)
    )


def _start_move(window, account, archive, conversation):
    window._start_move(
        [conversation], {account.id: archive}, lambda moved: f"moved {moved}"
    )
    # The move is now pending behind its undo timer; the optimistic filing
    # already landed.
    [(pending, token)] = window._pending_moves
    return pending, token


def test_undo_returns_mail_to_its_source_uids(db, settings, offline):
    account = _account(db)
    inbox, archive, conversation = _mailbox(db, account)
    window = PostcardMainWindow(None, db, settings)
    _start_move(window, account, archive, conversation)

    # Optimistic first: the mail sits in the destination as a UID-less
    # placeholder until the server confirms the move.
    assert db.emails_in_folder(inbox.id) == []
    assert _uids(db, archive) == [("one", None), ("two", None)]

    toast = window._pending_toast
    assert toast is not None
    window._on_undo_move(toast)

    assert window._pending_moves == []
    assert db.emails_in_folder(archive.id) == []
    assert _uids(db, inbox) == [("one", "101"), ("two", "102")]
    _close(window, settings)


def test_a_completed_move_files_the_server_uids(db, settings, offline):
    account = _account(db)
    inbox, archive, conversation = _mailbox(db, account)
    window = PostcardMainWindow(None, db, settings)
    pending, token = _start_move(window, account, archive, conversation)
    # Production's commit disarms the undo timer before the worker runs; the
    # completion under test never sees it armed.
    window._clear_pending_moves()

    assert (
        window._on_move_result(
            pending, token, MoveResult(destination_uids=["201", "202"])
        )
        is False
    )

    assert db.emails_in_folder(inbox.id) == []
    assert _uids(db, archive) == [("one", "201"), ("two", "202")]
    _close(window, settings)


def test_a_partial_move_files_what_crossed_and_restores_the_tail(db, settings, offline):
    account = _account(db)
    inbox, archive, conversation = _mailbox(db, account)
    window = PostcardMainWindow(None, db, settings)
    pending, token = _start_move(window, account, archive, conversation)
    window._clear_pending_moves()

    assert (
        window._on_move_result(
            pending,
            token,
            MoveResult(
                destination_uids=["201"],
                failed_index=1,
                error=Exception("mid-move failure"),
            ),
        )
        is False
    )

    assert _uids(db, archive) == [("one", "201")]
    assert _uids(db, inbox) == [("two", "102")]
    _close(window, settings)


def test_a_failed_move_restores_everything(db, settings, offline):
    account = _account(db)
    inbox, archive, conversation = _mailbox(db, account)
    window = PostcardMainWindow(None, db, settings)
    pending, token = _start_move(window, account, archive, conversation)
    window._clear_pending_moves()

    assert (
        window._on_move_failed(
            pending, account.imap_host, Exception("no route to host"), token
        )
        is False
    )

    assert db.emails_in_folder(archive.id) == []
    assert _uids(db, inbox) == [("one", "101"), ("two", "102")]
    _close(window, settings)


def test_a_move_past_a_sign_in_failure_restores_everything(db, settings, offline):
    account = _account(db)
    inbox, archive, conversation = _mailbox(db, account)
    window = PostcardMainWindow(None, db, settings)
    pending, token = _start_move(window, account, archive, conversation)
    window._clear_pending_moves()

    assert window._on_move_sign_in_failed(pending, token) is False

    assert db.emails_in_folder(archive.id) == []
    assert _uids(db, inbox) == [("one", "101"), ("two", "102")]
    _close(window, settings)
