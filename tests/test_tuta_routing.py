"""Tuta credentials stay on the Tuta path: routing and redaction.

A stored Tuta session must never reach an IMAP, SMTP or Graph session, and
no secret material may leak through credentials, errors or log lines.
"""

import json

import pytest

from postcard.core import secrets
from postcard.core.models.account import (
    PROTOCOL_IMAP,
    PROTOCOL_TUTA,
    TUTA_HOST,
    TUTA_PORT,
    Account,
)
from postcard.core.net.auth import Credential
from postcard.core.net.errors import classify
from postcard.core.net.graph_session import GraphError, GraphSession
from postcard.core.net.imap_session import ImapError, ImapSession
from postcard.core.net.smtp_session import SmtpError, SmtpSession
from postcard.core.net.tuta_session import (
    ERROR_CODES,
    MECHANISM_TUTA,
    TutaError,
)
from postcard.core.providers import settings_for_email

SESSION = {
    "login": "ada@tuta.com",
    "user_id": "user",
    "access_token": "secret-token",
    "encrypted_passphrase_key": [1, 2, 3],
}
SECRET = json.dumps(SESSION)
TUTA_CREDENTIAL = Credential(SESSION["login"], SECRET, MECHANISM_TUTA)


def tuta_account() -> Account:
    return Account(
        id=9,
        email="ada@tuta.com",
        display_name="Ada",
        protocol=PROTOCOL_TUTA,
        imap_host=TUTA_HOST,
        imap_port=TUTA_PORT,
        smtp_host=TUTA_HOST,
        smtp_port=TUTA_PORT,
    )


class FakeImap:
    def __init__(self):
        self.calls = []

    def login(self, user, password):
        self.calls.append(("LOGIN", user, password))

    def authenticate(self, mechanism, authobject):
        self.calls.append(("AUTHENTICATE", mechanism, authobject(b"")))


class FakeSmtp:
    def __init__(self):
        self.calls = []

    def ehlo_or_helo_if_needed(self):
        pass

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def auth(self, mechanism, authobject, initial_response_ok=True):
        self.calls.append(("auth", mechanism, authobject()))


def test_tuta_session_cannot_sign_in_to_imap(monkeypatch):
    fake = FakeImap()
    monkeypatch.setattr(
        "postcard.core.net.imap_session.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: fake,
    )
    session = ImapSession("imap.example.com", 993)
    session.connect()
    with pytest.raises(ImapError):
        session.sign_in(TUTA_CREDENTIAL)
    assert fake.calls == []


def test_tuta_session_cannot_sign_in_to_smtp(monkeypatch):
    fake = FakeSmtp()
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP_SSL",
        lambda *args, **kwargs: fake,
    )
    session = SmtpSession("smtp.example.com", 465)
    session.connect()
    with pytest.raises(SmtpError):
        session.sign_in(TUTA_CREDENTIAL)
    assert fake.calls == []


def test_tuta_session_cannot_open_graph():
    def forbidden(*args, **kwargs):
        pytest.fail("A Tuta credential reached the Graph transport")

    with pytest.raises(GraphError):
        GraphSession(TUTA_CREDENTIAL, transport=forbidden)


def test_credential_for_routes_by_protocol(monkeypatch):
    monkeypatch.setattr(secrets, "lookup_password", lambda account_id: SECRET)
    tuta = secrets.credential_for(tuta_account())
    assert tuta is not None
    assert (tuta.user, tuta.secret, tuta.mechanism) == (
        "ada@tuta.com",
        SECRET,
        MECHANISM_TUTA,
    )
    plain = Account(
        id=10,
        email="ada@example.com",
        display_name="Ada",
        protocol=PROTOCOL_IMAP,
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=465,
    )
    assert secrets.credential_for(plain) == Credential(
        "ada@example.com", SECRET, "login"
    )


def test_store_tuta_session_refuses_anything_but_a_session(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        secrets,
        "store_password",
        lambda account_id, secret: stored.setdefault(account_id, secret),
    )
    secrets.store_tuta_session(9, TUTA_CREDENTIAL)
    assert stored == {9: SECRET}
    for credential in (
        Credential("ada@tuta.com", "fixture-password", "login"),
        Credential("ada@tuta.com", "not-json", MECHANISM_TUTA),
        Credential("ada@tuta.com", json.dumps(["not", "a", "session"]), MECHANISM_TUTA),
        Credential("ada@tuta.com", json.dumps({"user_id": "no-login"}), MECHANISM_TUTA),
    ):
        with pytest.raises(TutaError) as caught:
            secrets.store_tuta_session(9, credential)
        assert caught.value.code == "invalid_request"
    assert stored == {9: SECRET}


def test_error_codes_are_a_fixed_vocabulary():
    for code in ERROR_CODES:
        assert code.isascii() and code.replace("_", "").isalnum()


def test_no_tuta_error_carries_secret_material():
    for code in ERROR_CODES:
        error = TutaError(code)
        assert "secret-token" not in str(error)
        assert "secret-token" not in error.code
        assert SECRET not in repr(error)
    assert "secret-token" not in repr(TUTA_CREDENTIAL)


def test_classified_messages_name_no_secrets():
    for code in ERROR_CODES:
        _is_auth_failure, message = classify(TutaError(code), TUTA_HOST)
        assert "secret-token" not in message
        assert SECRET not in message


@pytest.mark.parametrize(
    ("code", "is_auth_failure"),
    [
        ("login_failed", True),
        ("session_revoked", True),
        ("session_invalid", True),
        ("invalid_totp", True),
        ("unsupported_u2f", True),
        ("unsupported_webauthn", True),
        ("unsupported_second_factor", True),
        ("client_outdated", False),
        ("transport_failed", False),
        ("timeout", False),
        ("helper_missing", False),
        ("request_failed", False),
    ],
)
def test_retry_guidance_follows_the_taxonomy(code, is_auth_failure):
    # Auth failures hide the Retry button: no retry fixes them. Outages and
    # refused versions keep it.
    assert classify(TutaError(code), TUTA_HOST)[0] is is_auth_failure


def test_tuta_is_an_explicit_choice_not_a_domain_preset():
    # Tuta onboarding opens its own dialog; no domain preset may route a
    # Tuta address at IMAP settings, whatever the domain.
    for address in (
        "ada@tuta.com",
        "ada@tutanota.com",
        "ada@tutanota.de",
        "ada@tuta.io",
        "ada@keemail.me",
        "ada@example.com",
    ):
        assert settings_for_email(address) is None


def test_revoked_outage_and_refused_version_read_differently():
    _revoked_auth, revoked = classify(TutaError("session_revoked"), TUTA_HOST)
    _outage_auth, outage = classify(TutaError("transport_failed"), TUTA_HOST)
    _outdated_auth, outdated = classify(TutaError("client_outdated"), TUTA_HOST)
    assert len({revoked, outage, outdated}) == 3
