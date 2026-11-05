"""Cancelling a login, and the keyring failing under one.

Progress-cancel orphans the worker still running: its late success must
save nothing, and the form keeps what was typed. A keyring failure under
a successful login returns to the form without emitting -- all four
onboarding dialogs save the database row before the secret, so the Tuta
dialog stands in for the shared pattern.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display.
"""

import json
import os
import threading

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

from postcard.core.net.auth import Credential  # noqa: E402
from postcard.core.net.tuta_session import MECHANISM_TUTA  # noqa: E402
from postcard.core.store.database import Database  # noqa: E402
from postcard.tuta_dialog import PostcardTutaDialog  # noqa: E402

_KEY = bytes(range(32))

SESSION = {
    "login": "ada@tuta.com",
    "user_id": "user",
    "access_token": "secret-token",
    "encrypted_passphrase_key": [1, 2, 3],
}
CREDENTIAL = Credential(SESSION["login"], json.dumps(SESSION), MECHANISM_TUTA)


def pump_main_loop_until(predicate, timeout=10.0):
    """Run main-loop iterations until predicate passes or time runs out."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        context.iteration(True)
    assert predicate()


def churn_main_loop_until(predicate, timeout=60.0):
    """Dispatch pending sources without blocking until predicate passes.

    This module runs right after the composer tests, whose leaked backlog
    a blocking pump would sit inside (webkit's one-off settle; see the
    lifecycle suite's drain). Non-blocking iterations churn through it
    instead. The 60s budget matches the suite's drain precedent; the sleep
    keeps the race window from hot-spinning when nothing is pending yet.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            time.sleep(0.005)
    assert predicate()


def fill_form(dialog, email="ada@tuta.com"):
    dialog.email_row.set_text(email)
    dialog.display_name_row.set_text("Ada")
    dialog.password_row.set_text("fixture-password")


def test_cancelling_a_login_saves_no_account(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    entered = threading.Event()
    gate = threading.Event()
    stored = {}

    def gated_login(email, password, totp=None):
        entered.set()
        assert gate.wait(timeout=10)
        return CREDENTIAL

    completions = []
    original_done = PostcardTutaDialog._on_login_done

    def record_done(self, *args):
        completions.append(True)
        return original_done(self, *args)

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", gated_login)
    monkeypatch.setattr(
        "postcard.tuta_dialog.secrets.store_tuta_session",
        lambda account_id, credential: stored.setdefault(account_id, credential.secret),
    )
    monkeypatch.setattr(PostcardTutaDialog, "_on_login_done", record_done)
    dialog = PostcardTutaDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    assert entered.wait(timeout=10)
    assert dialog.dialog_stack.get_visible_child_name() == "progress"

    dialog.progress_cancel_button.emit("clicked")
    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert dialog.email_row.get_text() == "ada@tuta.com"

    gate.set()
    churn_main_loop_until(lambda: completions)

    assert added == []
    assert stored == {}
    assert db.accounts() == []
    db.close()


def test_a_keyring_failure_returns_to_the_form_without_emitting(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    monkeypatch.setattr(
        "postcard.tuta_dialog.tuta_session.login",
        lambda email, password, totp=None: CREDENTIAL,
    )

    def locked_keyring(account_id, credential):
        raise OSError("keyring locked")

    monkeypatch.setattr(
        "postcard.tuta_dialog.secrets.store_tuta_session", locked_keyring
    )
    dialog = PostcardTutaDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    assert dialog.dialog_stack.get_visible_child_name() == "progress"
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "form")

    assert added == []
    db.close()


@pytest.mark.xfail(
    strict=True,
    reason="keyring failure leaves the saved account row without its "
    "secret, so a retry refuses the address as a duplicate (tuta, proton, "
    "microsoft and fastmail share the save-then-store shape)",
)
def test_a_keyring_failure_leaves_no_orphan_account(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    monkeypatch.setattr(
        "postcard.tuta_dialog.tuta_session.login",
        lambda email, password, totp=None: CREDENTIAL,
    )

    def locked_keyring(account_id, credential):
        raise OSError("keyring locked")

    monkeypatch.setattr(
        "postcard.tuta_dialog.secrets.store_tuta_session", locked_keyring
    )
    dialog = PostcardTutaDialog(db)
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "form")

    assert db.accounts() == []
    db.close()
