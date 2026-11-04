"""Proton onboarding, exercised where the Adw typelib exists.

Covers the form validation, the password login that saves a resumable
session, and the inline two-factor retry -- all against a stubbed
`proton_session.login`, so no test touches a live server or the keyring.

Runs in CI's gui-tests.yml and locally via `just test-gui`: inside the
installed Flatpak under Xvfb. Skips anywhere without a display.
"""

import json
import os

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

import time

from postcard.core.models.account import PROTOCOL_PROTON, PROTON_HOST
from postcard.core.net.auth import Credential
from postcard.core.net.proton_session import MECHANISM_PROTON, ProtonError
from postcard.core.store.database import Database
from postcard.proton_dialog import PostcardProtonDialog

_KEY = bytes(range(32))

SESSION = {
    "login": "ada@proton.me",
    "uid": "user",
    "access_token": "secret-token",
    "refresh_token": "refresh-token",
}
CREDENTIAL = Credential(SESSION["login"], json.dumps(SESSION), MECHANISM_PROTON)


def pump_main_loop_until(predicate, timeout=10.0):
    """Run main-loop iterations until predicate passes or time runs out."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        context.iteration(True)
    assert predicate()


def fill_form(dialog):
    dialog.email_row.set_text("ada@proton.me")
    dialog.display_name_row.set_text("Ada")
    dialog.password_row.set_text("fixture-password")


def test_sign_in_saves_a_proton_account(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    stored = {}
    monkeypatch.setattr(
        "postcard.proton_dialog.proton_session.login",
        lambda email, password, totp=None: CREDENTIAL,
    )
    monkeypatch.setattr(
        "postcard.proton_dialog.secrets.store_proton_session",
        lambda account_id, credential: stored.setdefault(account_id, credential.secret),
    )
    dialog = PostcardProtonDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    fill_form(dialog)
    dialog.totp_row.set_text("001234")
    assert dialog.sign_in_button.get_sensitive()

    dialog.sign_in_button.emit("clicked")
    assert dialog.dialog_stack.get_visible_child_name() == "progress"
    pump_main_loop_until(lambda: added)

    [account] = db.accounts()
    assert (account.email, account.protocol, account.imap_host, account.goa_id) == (
        "ada@proton.me",
        PROTOCOL_PROTON,
        PROTON_HOST,
        "",
    )
    assert stored[account.id] == CREDENTIAL.secret
    assert "fixture-password" not in stored[account.id]
    db.close()


def test_an_expired_code_clears_the_row_for_retry(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fake_login(email, password, totp=None):
        raise ProtonError("invalid_totp")

    monkeypatch.setattr("postcard.proton_dialog.proton_session.login", fake_login)
    dialog = PostcardProtonDialog(db)
    fill_form(dialog)
    dialog.totp_row.set_text("001234")

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.totp_row.get_text() == "")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert dialog.password_row.get_text() == "fixture-password"
    assert db.accounts() == []
    db.close()


def test_a_refused_login_returns_to_the_form(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fail(email, password, totp=None):
        raise ProtonError("login_failed")

    monkeypatch.setattr("postcard.proton_dialog.proton_session.login", fail)
    dialog = PostcardProtonDialog(db)
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "form")

    assert db.accounts() == []
    db.close()


def test_sign_in_without_a_password_never_starts_login(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fail(email, password, totp=None):
        pytest.fail("login started with no password")

    monkeypatch.setattr("postcard.proton_dialog.proton_session.login", fail)
    dialog = PostcardProtonDialog(db)
    dialog.email_row.set_text("ada@proton.me")

    assert not dialog.sign_in_button.get_sensitive()
    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert db.accounts() == []
    db.close()


def test_an_existing_address_is_not_added_twice(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    db.save_account("ada@proton.me", "Ada", PROTON_HOST, 443, PROTON_HOST, 443)

    def fail(email, password, totp=None):
        pytest.fail("login started for an existing account")

    monkeypatch.setattr("postcard.proton_dialog.proton_session.login", fail)
    dialog = PostcardProtonDialog(db)
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert len(db.accounts()) == 1
    db.close()


def test_dialog_template_ids_match(monkeypatch, tmp_path):
    """Constructing the dialog proves every Template.Child id resolves."""
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    dialog = PostcardProtonDialog(db)

    assert isinstance(dialog.toast_overlay, Gtk.Widget)
    assert isinstance(dialog.totp_row, Gtk.Widget)
    db.close()
