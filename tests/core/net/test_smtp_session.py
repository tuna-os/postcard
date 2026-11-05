import smtplib
import ssl

import pytest

from postcard.core.net.auth import MECHANISM_XOAUTH2, Credential
from postcard.core.net.smtp_session import SmtpError, SmtpRefusedError, SmtpSession


class FakeSmtp:
    def __init__(self, refused=None):
        self.calls = []
        self.greeted = False
        self._refused = refused if refused is not None else {}

    def ehlo_or_helo_if_needed(self):
        self.greeted = True

    def sendmail(self, from_addr, recipients, raw):
        self.calls.append(("sendmail", from_addr, recipients, raw))
        return self._refused

    def auth(self, mechanism, authobject, initial_response_ok=True):
        self.calls.append(("auth", mechanism, authobject()))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def starttls(self, context=None):
        self.calls.append(("starttls", context))


def connect(monkeypatch, smtp: FakeSmtp) -> SmtpSession:
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP_SSL",
        lambda *args, **kwargs: smtp,
    )
    session = SmtpSession("smtp.example.com", 465)
    session.connect()
    return session


def test_send_raw_raises_when_never_connected() -> None:
    # Arrange: a session that was built but never connect()ed.
    session = SmtpSession("smtp.example.com", 465)

    # Act / Assert: it must fail loudly. Returning normally here would make
    # mail_sync.send_message report a message as sent that never left.
    with pytest.raises(SmtpError, match="not connected to smtp.example.com:465"):
        session.send_raw("me@example.com", ["you@example.com"], b"raw")


def test_sign_in_raises_when_never_connected() -> None:
    session = SmtpSession("smtp.example.com", 587, "starttls")

    with pytest.raises(SmtpError, match="not connected to smtp.example.com:587"):
        session.sign_in(Credential("me@example.com", "hunter2"))


def test_quit_is_safe_when_never_connected() -> None:
    # Teardown runs from a `finally:` on every send path, so it has to tolerate
    # a session that never opened rather than mask the real error.
    SmtpSession("smtp.example.com", 465).quit()


def test_send_raw_succeeds_when_every_recipient_accepts(monkeypatch) -> None:
    session = connect(monkeypatch, FakeSmtp())

    session.send_raw("me@example.com", ["you@example.com"], b"raw")


def test_send_raw_raises_on_a_partial_refusal(monkeypatch) -> None:
    # sendmail reports per-recipient refusals by returning them, not by
    # raising -- ignoring the return value reports the mail as sent. The
    # refusal names who was refused and who already got it; the server's
    # verbatim reason stays in the log, never in the message.
    refused = {"bob@example.com": (550, b"mailbox unavailable")}
    session = connect(monkeypatch, FakeSmtp(refused=refused))

    with pytest.raises(SmtpRefusedError) as caught:
        session.send_raw(
            "me@example.com", ["you@example.com", "bob@example.com"], b"raw"
        )

    assert caught.value.refused == ("bob@example.com",)
    assert caught.value.accepted == ("you@example.com",)
    assert "bob@example.com" in str(caught.value)
    assert "mailbox unavailable" not in str(caught.value)


def test_send_raw_names_the_recipients_in_the_error(monkeypatch) -> None:
    # Arrange: connect() succeeds against a server that then refuses the mail.
    class RefusingSmtp:
        def sendmail(self, from_addr: str, recipients: list[str], raw: bytes) -> None:
            raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: RefusingSmtp())
    session = SmtpSession("smtp.example.com", 465)
    session.connect()

    # Act / Assert: the message says which recipient it failed for.
    with pytest.raises(SmtpError, match="could not send to you@example.com"):
        session.send_raw("me@example.com", ["you@example.com"], b"raw")


def test_xoauth2_greets_the_server_before_authenticating(monkeypatch):
    # SMTP_SSL does not EHLO on connect and auth() -- unlike login() -- will not
    # do it either, so the AUTH would go out before the server said hello.
    smtp = FakeSmtp()
    session = connect(monkeypatch, smtp)

    session.sign_in(Credential("me@example.com", "token", MECHANISM_XOAUTH2))

    assert smtp.greeted
    assert smtp.calls == [
        ("auth", "XOAUTH2", "user=me@example.com\x01auth=Bearer token\x01\x01")
    ]


def test_an_unknown_mechanism_fails_rather_than_staying_unauthenticated(monkeypatch):
    smtp = FakeSmtp()
    session = connect(monkeypatch, smtp)

    with pytest.raises(SmtpError, match="unsupported mechanism none"):
        session.sign_in(Credential("me@example.com", "", "none"))

    assert smtp.calls == []


def test_a_password_account_still_uses_plain_login(monkeypatch):
    smtp = FakeSmtp()
    session = connect(monkeypatch, smtp)

    session.sign_in(Credential("me@example.com", "hunter2"))

    assert smtp.calls == [("login", "me@example.com", "hunter2")]


# --- TLS -------------------------------------------------------------------
# Same defect as imaplib: smtplib's implicit context checks neither the
# certificate nor the hostname, so a send would leak the password.


def _assert_verifies(context: ssl.SSLContext) -> None:
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_connect_gives_smtp_ssl_a_verifying_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP_SSL",
        lambda *args, **kwargs: captured.update(kwargs) or FakeSmtp(),
    )

    SmtpSession("smtp.example.com", 465).connect()

    _assert_verifies(captured["context"])


def test_connect_gives_starttls_a_verifying_context(monkeypatch):
    smtp = FakeSmtp()
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP",
        lambda *args, **kwargs: smtp,
    )

    SmtpSession("smtp.example.com", 587, "starttls").connect()

    assert smtp.calls[0][0] == "starttls"
    _assert_verifies(smtp.calls[0][1])


def test_a_wrong_host_certificate_fails_the_connection(monkeypatch):
    # The server presents another host's certificate: verification raises,
    # connect() lets it through, and the session stays unusable -- sign_in
    # fails rather than sending the password anywhere.
    def wrong_host(*args, **kwargs):
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr("postcard.core.net.smtp_session.smtplib.SMTP_SSL", wrong_host)

    session = SmtpSession("smtp.example.com", 465)
    with pytest.raises(ssl.SSLCertVerificationError):
        session.connect()
    with pytest.raises(SmtpError, match="not connected"):
        session.sign_in(Credential("me@example.com", "hunter2"))


@pytest.mark.parametrize(
    "failure",
    [
        smtplib.SMTPException("STARTTLS extension not supported by server"),
        ssl.SSLCertVerificationError("certificate verify failed"),
    ],
)
def test_a_failed_starttls_leaves_no_usable_session(monkeypatch, failure):
    # A server that refuses STARTTLS -- or whose handshake fails on a
    # wrong-host certificate -- must never receive the password over the
    # plaintext connection that remains. The original error propagates
    # untouched, so errors.classify still words it.
    smtp = FakeSmtp()

    def starttls(context=None):
        raise failure

    smtp.starttls = starttls
    closed = []
    smtp.close = lambda: closed.append(True)
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP",
        lambda *args, **kwargs: smtp,
    )

    session = SmtpSession("smtp.example.com", 587, "starttls")
    with pytest.raises(type(failure)):
        session.connect()
    with pytest.raises(SmtpError, match="not connected"):
        session.sign_in(Credential("me@example.com", "hunter2"))
    assert smtp.calls == []
    assert closed == [True]


def test_loopback_uses_the_unverified_bridge_context(monkeypatch):
    # The documented exception in ssl_context_for: a local bridge (ProtonMail
    # Bridge, hydroxide) terminates TLS with a self-signed certificate, and
    # there is no network path to attack between two local processes.
    captured = {}
    monkeypatch.setattr(
        "postcard.core.net.smtp_session.smtplib.SMTP_SSL",
        lambda *args, **kwargs: captured.update(kwargs) or FakeSmtp(),
    )

    SmtpSession("127.0.0.1", 465).connect()

    assert captured["context"].verify_mode == ssl.CERT_NONE
    assert not captured["context"].check_hostname
