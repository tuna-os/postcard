"""Tuta onboarding, exercised where the Adw typelib exists.

Covers the form validation, the password login that saves a resumable
session, and the TOTP challenge with expiry retry -- all against a stubbed
`tuta_session.login`, so no test touches a live server or the keyring.

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
    from gi.repository import Adw, Gio, GLib
except (ImportError, ValueError):
    pytest.skip("GTK/Adw typelibs unavailable", allow_module_level=True)

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

from postcard.core.models.account import PROTOCOL_TUTA, TUTA_HOST
from postcard.core.net.auth import Credential
from postcard.core.net.tuta_session import MECHANISM_TUTA, TutaError
from postcard.core.store.database import Database
from postcard.tuta_dialog import PostcardTutaDialog

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


def fill_form(dialog, email="ada@tuta.com"):
    dialog.email_row.set_text(email)
    dialog.display_name_row.set_text("Ada")
    dialog.password_row.set_text("fixture-password")


def test_sign_in_saves_a_tuta_account(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    stored = {}
    monkeypatch.setattr(
        "postcard.tuta_dialog.tuta_session.login",
        lambda email, password, totp=None: CREDENTIAL,
    )
    monkeypatch.setattr(
        "postcard.tuta_dialog.secrets.store_tuta_session",
        lambda account_id, credential: stored.setdefault(account_id, credential.secret),
    )
    dialog = PostcardTutaDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    fill_form(dialog)
    assert dialog.sign_in_button.get_sensitive()

    dialog.sign_in_button.emit("clicked")
    assert dialog.dialog_stack.get_visible_child_name() == "progress"
    pump_main_loop_until(lambda: added)

    [account] = db.accounts()
    assert (account.email, account.protocol, account.imap_host, account.goa_id) == (
        "ada@tuta.com",
        PROTOCOL_TUTA,
        TUTA_HOST,
        "",
    )
    assert stored[account.id] == CREDENTIAL.secret
    assert "fixture-password" not in stored[account.id]
    db.close()


def test_custom_domain_addresses_sign_in_too(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    seen = []
    monkeypatch.setattr(
        "postcard.tuta_dialog.tuta_session.login",
        lambda email, password, totp=None: seen.append(email) or CREDENTIAL,
    )
    monkeypatch.setattr(
        "postcard.tuta_dialog.secrets.store_tuta_session",
        lambda account_id, credential: None,
    )
    dialog = PostcardTutaDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    # The provider was chosen by opening this dialog; the domain is never
    # inspected, so a custom domain signs in like any Tuta address.
    fill_form(dialog, email="ada@example.com")
    assert dialog.sign_in_button.get_sensitive()

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: added)

    assert seen == ["ada@example.com"]
    assert db.accounts()[0].protocol == PROTOCOL_TUTA
    db.close()


def test_totp_challenge_retries_with_the_code(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    calls = []

    def fake_login(email, password, totp=None):
        calls.append(totp)
        if totp is None:
            raise TutaError("second_factor_required")
        return CREDENTIAL

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", fake_login)
    monkeypatch.setattr(
        "postcard.tuta_dialog.secrets.store_tuta_session",
        lambda account_id, credential: None,
    )
    dialog = PostcardTutaDialog(db)
    added = []
    dialog.connect("account-added", lambda *args: added.append(True))
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "totp")

    dialog.totp_row.set_text("001234")
    assert dialog.totp_verify_button.get_sensitive()
    dialog.totp_verify_button.emit("clicked")
    pump_main_loop_until(lambda: added)

    assert calls == [None, "001234"]
    assert len(db.accounts()) == 1
    db.close()


def test_an_expired_code_stays_on_the_totp_page(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fake_login(email, password, totp=None):
        if totp is None:
            raise TutaError("second_factor_required")
        raise TutaError("invalid_totp")

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", fake_login)
    dialog = PostcardTutaDialog(db)
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "totp")
    dialog.totp_row.set_text("001234")

    dialog.totp_verify_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.totp_row.get_text() == "")

    assert dialog.dialog_stack.get_visible_child_name() == "totp"
    assert db.accounts() == []
    db.close()


def test_a_security_key_account_reports_itself_honestly(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fake_login(email, password, totp=None):
        raise TutaError("unsupported_webauthn")

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", fake_login)
    dialog = PostcardTutaDialog(db)
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "form")

    assert db.accounts() == []
    db.close()


def test_a_refused_login_returns_to_the_form(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fail(email, password, totp=None):
        raise TutaError("login_failed")

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", fail)
    dialog = PostcardTutaDialog(db)
    fill_form(dialog)

    dialog.sign_in_button.emit("clicked")
    pump_main_loop_until(lambda: dialog.dialog_stack.get_visible_child_name() == "form")

    assert db.accounts() == []
    db.close()


def test_sign_in_without_a_password_never_starts_login(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)

    def fail(email, password, totp=None):
        pytest.fail("login started with no password")

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", fail)
    dialog = PostcardTutaDialog(db)
    dialog.email_row.set_text("ada@tuta.com")

    assert not dialog.sign_in_button.get_sensitive()
    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert db.accounts() == []
    db.close()


def test_an_existing_address_is_not_added_twice(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "postcard.db"), key=_KEY)
    db.save_account("ada@tuta.com", "Ada", TUTA_HOST, 443, TUTA_HOST, 443)

    def fail(email, password, totp=None):
        pytest.fail("login started for an existing account")

    monkeypatch.setattr("postcard.tuta_dialog.tuta_session.login", fail)
    dialog = PostcardTutaDialog(db)
    fill_form(dialog, email="ADA@tuta.com")

    dialog.sign_in_button.emit("clicked")

    assert dialog.dialog_stack.get_visible_child_name() == "form"
    assert len(db.accounts()) == 1
    db.close()
