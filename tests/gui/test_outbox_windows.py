"""Two windows, one Outbox: the second drain files no second Sent copy.

Each main window drains the shared Outbox on its own completions, so a
send one window already filed must be a no-op in the other -- never a
duplicate Sent copy and never a crash on the forgotten queue row.

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

from postcard import mail_sync  # noqa: E402
from postcard.core.store.database import Database  # noqa: E402
from postcard.window import PostcardMainWindow  # noqa: E402
from postcard.window_types import OutboxResult  # noqa: E402

_KEY = bytes(range(32))

_RAW = (
    b"From: ada@example.com\r\nTo: bo@example.com\r\nSubject: queued\r\n"
    b"Message-ID: <queued-1@example.com>\r\n"
    b"Content-Type: text/plain\r\n\r\nbody"
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


def test_a_second_windows_drain_files_no_second_sent_copy(db, settings, offline):
    account = db.save_account(
        "ada@example.com", "Ada", "imap.example.com", 993, "s", 587
    )
    outbox = db.get_or_create_folder(account.id, mail_sync.OUTBOX_FOLDER)
    queued = db.save_email(outbox.id, "Ada", "queued", "queued", "now", False)
    db.save_raw_message(queued.id, _RAW)
    db.queue_outbox_email(queued.id, ["bo@example.com"])
    first = PostcardMainWindow(None, db, settings)
    second = PostcardMainWindow(None, db, settings)
    token_a = first._lifecycle.mint(account.id)
    token_b = second._lifecycle.mint(account.id)

    result = OutboxResult(queued.id, "queued", _RAW, None)
    assert first._on_outbox_drained(account, token_a, [result]) is False
    assert second._on_outbox_drained(account, token_b, [result]) is False

    sent = db.get_folder_by_name(account.id, mail_sync.SENT_FOLDER)
    assert sent is not None
    assert [mail.subject for mail in db.emails_in_folder(sent.id)] == ["queued"]
    assert db.emails_in_folder(outbox.id) == []
    assert db.queued_send(queued.id) is None
    _close(first, settings)
    _close(second, settings)
