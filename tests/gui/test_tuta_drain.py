"""The Outbox drain terminal-fails an ambiguous Tuta send.

The composer path already does; the background drain must too, or its next
automatic run would resend a mail that may have gone out. A definitive
failure still retries. Runs via `just test-gui`: inside the installed
Flatpak under Xvfb. Skips anywhere without a display.
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
from datetime import datetime

from postcard import mail_sync
from postcard.core import APP_ID
from postcard.core.models.account import PROTOCOL_TUTA, TUTA_HOST, TUTA_PORT
from postcard.core.net.tuta_session import TutaError
from postcard.core.store.database import Database, OutboxState
from postcard.window import PostcardMainWindow

_KEY = bytes(range(32))

_RAW = (
    b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nSubject: hi\r\n"
    b"Message-ID: <drain-1@example.com>\r\n"
    b"Content-Type: text/plain\r\n\r\nbody"
)


class _TrayStub:
    def set_unread(self, count):
        pass


class _GuiTestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="in.gxanshu.postcard.guitest")
        self.tray = _TrayStub()


def _make_window(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    settings = Gio.Settings(schema_id=APP_ID)
    app = _GuiTestApp()
    monkeypatch.setattr(
        PostcardMainWindow,
        "_start_sync",
        lambda self, account, **kwargs: None,
    )
    window = PostcardMainWindow(app, db, settings)
    return window, db


def _close_window(window, db):
    window.close()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5.0
    while context.iteration(False) and time.monotonic() < deadline:
        pass
    db.close()


def _queued(db, account):
    outbox = db.get_or_create_folder(
        account.id,
        mail_sync.OUTBOX_FOLDER,
        mail_sync.icon_for_folder(mail_sync.OUTBOX_FOLDER),
    )
    row = db.save_email(
        outbox.id,
        sender=account.email,
        subject="hi",
        preview="hi",
        date=datetime.now().astimezone().isoformat(),
        is_unread=False,
    )
    db.save_raw_message(row.id, _RAW)
    db.queue_outbox_email(row.id, ["bo@tuta.com"])
    # The drain claims before sending and releases before recording, as
    # _on_outbox_drained does; the row stays SENDING for the record call.
    assert mail_sync.claim_queued_send(db, row.id) is not None
    mail_sync.release_queued_send(row.id)
    return outbox, row.id


def _tuta_account(db):
    return db.save_account(
        "ada@tuta.com",
        "Ada",
        TUTA_HOST,
        TUTA_PORT,
        TUTA_HOST,
        TUTA_PORT,
        protocol=PROTOCOL_TUTA,
    )


def test_an_uncertain_drained_send_is_terminal_failed(tmp_path, monkeypatch):
    window, db = _make_window(tmp_path, monkeypatch)
    try:
        outbox, email_id = _queued(db, _tuta_account(db))
        window._record_drained_error(email_id, TutaError("send_uncertain"))
        send = db.queued_send(email_id)
        assert send.state is OutboxState.FAILED
        assert "may have been sent" in send.last_error
        assert "Check Sent" in send.last_error
        pending = [s.email_id for s in db.pending_outbox_sends(outbox.id)]
        assert email_id not in pending
    finally:
        _close_window(window, db)


def test_a_definitively_failed_drained_send_still_retries(tmp_path, monkeypatch):
    window, db = _make_window(tmp_path, monkeypatch)
    try:
        outbox, email_id = _queued(db, _tuta_account(db))
        window._record_drained_error(email_id, TutaError("send_failed"))
        send = db.queued_send(email_id)
        assert send.state is OutboxState.RETRY
        pending = [s.email_id for s in db.pending_outbox_sends(outbox.id)]
        assert email_id in pending
    finally:
        _close_window(window, db)
