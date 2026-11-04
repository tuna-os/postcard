"""Tuta send: explicit ordinary delivery, atomic submit, no silent downgrade.

Ordinary delivery is explicit end to end: the driver always marks it, the
helper refuses without the mark, and secure delivery stays refused rather
than downgraded. Submits are atomic, so a failure either sent nothing
(definitive: safe to retry) or may have sent (`send_uncertain`: a human
checks Sent, never an automatic retry).
"""

import base64
import json

import pytest

import postcard.mail_sync as mail_sync
from postcard.core import compose
from postcard.core.models.account import (
    PROTOCOL_TUTA,
    TUTA_HOST,
    TUTA_PORT,
    Account,
)
from postcard.core.models.attachment import Attachment
from postcard.core.net import tuta_messages
from postcard.core.net.auth import Credential
from postcard.core.net.imap_session import FLAG_FLAGGED, FLAG_SEEN
from postcard.core.net.tuta_session import MECHANISM_TUTA, TutaError

SEND_REPLY = {
    "message_id": "server-message-1",
    "sent_date_ms": 0,
    "sent_mail": "lists/sent",
    "internal_recipients": 1,
    "external_recipients": 2,
}


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


def tuta_credential(account: Account) -> Credential:
    return Credential(
        account.email, json.dumps({"login": account.email}), MECHANISM_TUTA
    )


@pytest.fixture
def sent(monkeypatch):
    """A Tuta account whose helper records its send and reports success."""
    account = tuta_account()
    credential = tuta_credential(account)
    requests = []

    class Session:
        def __init__(self, credential):
            pass

        def request(self, operation, **kwargs):
            requests.append((operation, kwargs))
            if operation in ("mark_read", "move"):
                return {
                    "outcomes": [{"id": uid, "code": None} for uid in kwargs["ids"]]
                }
            return dict(SEND_REPLY)

    monkeypatch.setattr(mail_sync, "TutaSession", Session)
    return account, credential, requests


def composer_raw(  # noqa: PLR0913 -- the six parts of a message under test.
    to, cc, bcc, subject, body_html, attachments=()
):
    """A message exactly as the composer builds it, plus its envelope."""
    message = compose.build_mime_message(
        "ada@tuta.com", to, cc, subject, body_html, list(attachments)
    )
    envelope = compose.flatten_envelope(
        compose.build_envelope(", ".join(to), ", ".join(cc), ", ".join(bcc))
    )
    return message.as_bytes(), envelope


def test_send_submits_headers_envelope_body_and_files(sent):
    account, credential, requests = sent
    attachment = Attachment(
        filename="résumé.bin", mime_type="application/octet-stream", content=b"\x00\xff"
    )
    raw, envelope = composer_raw(
        ["Bo <bo@tuta.com>"],
        ["cc@example.com"],
        ["hidden@example.com"],
        "नमस्ते",
        "<p>नमस्ते</p>",
        [attachment],
    )
    outcome = mail_sync.send_message(account, credential, account.email, envelope, raw)
    assert outcome.filed_uid == "lists/sent"
    assert outcome.uidvalidity is None
    assert outcome.file_error is None
    ((operation, params),) = requests
    assert operation == "send"
    assert params["sender"] == "ada@tuta.com"
    assert params["to"] == [{"name": "Bo", "address": "bo@tuta.com"}]
    assert params["cc"] == [{"name": "", "address": "cc@example.com"}]
    assert params["bcc"] == [{"name": "", "address": "hidden@example.com"}]
    assert params["subject"] == "नमस्ते"
    assert params["body_html"].strip() == "<p>नमस्ते</p>"
    assert params["delivery"] == "ordinary"
    assert params["ordinary_confirmed"] is True
    (sent_attachment,) = params["attachments"]
    assert sent_attachment["name"] == "résumé.bin"
    assert sent_attachment["mime_type"] == "application/octet-stream"
    assert base64.b64decode(sent_attachment["data"]) == b"\x00\xff"


def test_secure_delivery_is_never_requested(sent):
    account, credential, requests = sent
    raw, envelope = composer_raw(["bo@tuta.com"], [], [], "subject", "<p>body</p>")
    mail_sync.send_message(account, credential, account.email, envelope, raw)
    ((_, params),) = requests
    assert params["delivery"] == "ordinary"
    assert "secure" not in json.dumps(params)


def test_bcc_only_mail_keeps_empty_to_and_cc(sent):
    account, credential, requests = sent
    raw, envelope = composer_raw([], [], ["hidden@example.com"], "s", "<p>b</p>")
    mail_sync.send_message(account, credential, account.email, envelope, raw)
    ((_, params),) = requests
    assert params["to"] == []
    assert params["cc"] == []
    assert params["bcc"] == [{"name": "", "address": "hidden@example.com"}]


def test_envelope_addresses_missing_from_headers_become_bcc(sent):
    account, credential, requests = sent
    raw, _ = composer_raw(["bo@tuta.com"], [], [], "s", "<p>b</p>")
    mail_sync.send_message(
        account, credential, account.email, ["bo@tuta.com", "bcc@example.com"], raw
    )
    ((_, params),) = requests
    assert params["bcc"] == [{"name": "", "address": "bcc@example.com"}]


def test_a_text_only_body_is_escaped_not_dropped(sent):
    account, credential, requests = sent
    raw = (
        b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nSubject: s\r\n"
        b"Content-Type: text/plain\r\n\r\n<script>alert(1)</script>"
    )
    mail_sync.send_message(account, credential, account.email, ["bo@tuta.com"], raw)
    ((_, params),) = requests
    assert params["body_html"] == "<pre>&lt;script&gt;alert(1)&lt;/script&gt;</pre>"


def test_reply_to_and_threading_headers():
    captured = {}

    class Session:
        def request(self, operation, **kwargs):
            captured.update(kwargs)
            return dict(SEND_REPLY)

    raw = (
        b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nSubject: Re: s\r\n"
        b"Message-ID: <ours@example.com>\r\nIn-Reply-To: <parent@example.com>\r\n"
        b"References: <parent@example.com>\r\n"
        b"Reply-To: Support <support@example.com>\r\n"
        b"Content-Type: text/plain\r\n\r\nbody"
    )
    tuta_messages.send_mime(
        Session(),
        "ada@tuta.com",
        "Ada",
        raw,
        ["bo@tuta.com"],
        ordinary_confirmed=True,
    )
    assert captured["reply_tos"] == [
        {"name": "Support", "address": "support@example.com"}
    ]
    # Threading stays local: the server links conversations by its own ids,
    # so no Message-ID, In-Reply-To or References is submitted.
    assert "ours@example.com" not in json.dumps(captured)
    assert "parent@example.com" not in json.dumps(captured)


def test_an_empty_subject_falls_back_to_no_subject(sent):
    account, credential, requests = sent
    raw = (
        b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nContent-Type: text/plain\r\n\r\nbody"
    )
    mail_sync.send_message(account, credential, account.email, ["bo@tuta.com"], raw)
    ((_, params),) = requests
    assert params["subject"] == "(no subject)"


def test_send_counts_come_back_on_the_result(sent):
    account, credential, _ = sent
    raw, envelope = composer_raw(["bo@tuta.com"], [], [], "s", "<p>b</p>")
    session_requests = []

    class Session:
        def request(self, operation, **kwargs):
            session_requests.append(kwargs)
            return dict(SEND_REPLY)

    result = tuta_messages.send_mime(
        Session(),
        account.email,
        account.display_name,
        raw,
        envelope,
        ordinary_confirmed=True,
    )
    assert result.message_id == "server-message-1"
    assert result.sent_mail == "lists/sent"
    assert result.sent_at == "1970-01-01T00:00:00+00:00"
    assert (result.internal_recipients, result.external_recipients) == (1, 2)


def test_unknown_lookup_counts_pass_through_as_none():
    class Session:
        def request(self, operation, **kwargs):
            return {
                **SEND_REPLY,
                "internal_recipients": None,
                "external_recipients": None,
            }

    raw = (
        b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nSubject: s\r\n"
        b"Content-Type: text/plain\r\n\r\nbody"
    )
    result = tuta_messages.send_mime(
        Session(), "ada@tuta.com", "Ada", raw, ["bo@tuta.com"], ordinary_confirmed=True
    )
    assert result.internal_recipients is None
    assert result.external_recipients is None


@pytest.mark.parametrize(
    "code",
    [
        "invalid_recipient",
        "sender_unknown",
        "body_too_large",
        "attachment_failed",
        "confidential_send_unsupported",
        "ordinary_delivery_requires_consent",
        "send_failed",
    ],
)
def test_definitive_failures_sent_nothing_and_propagate(sent, monkeypatch, code):
    account, credential, _ = sent

    class Failing:
        def __init__(self, credential):
            pass

        def request(self, operation, **kwargs):
            raise TutaError(code)

    monkeypatch.setattr(mail_sync, "TutaSession", Failing)
    raw, envelope = composer_raw(["bo@tuta.com"], [], [], "s", "<p>b</p>")
    with pytest.raises(TutaError) as caught:
        mail_sync.send_message(account, credential, account.email, envelope, raw)
    assert caught.value.code == code


def test_an_uncertain_send_never_comes_back_as_success(sent, monkeypatch):
    account, credential, _ = sent

    class Uncertain:
        def __init__(self, credential):
            pass

        def request(self, operation, **kwargs):
            raise TutaError("send_uncertain")

    monkeypatch.setattr(mail_sync, "TutaSession", Uncertain)
    raw, envelope = composer_raw(["bo@tuta.com"], [], [], "s", "<p>b</p>")
    with pytest.raises(TutaError) as caught:
        mail_sync.send_message(account, credential, account.email, envelope, raw)
    assert caught.value.code == "send_uncertain"
    assert "Check Sent" in str(caught.value)


def test_unconfirmed_ordinary_delivery_is_not_forced_client_side():
    captured = {}

    class Session:
        def request(self, operation, **kwargs):
            captured.update(kwargs)
            return dict(SEND_REPLY)

    raw = (
        b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nSubject: s\r\n"
        b"Content-Type: text/plain\r\n\r\nbody"
    )
    tuta_messages.send_mime(
        Session(), "ada@tuta.com", "Ada", raw, ["bo@tuta.com"], ordinary_confirmed=False
    )
    assert captured["ordinary_confirmed"] is False


def test_sent_copy_is_absent_while_the_filed_uid_links_sent(sent):
    account, credential, _ = sent
    assert (
        mail_sync.driver_for(PROTOCOL_TUTA).sent_copy(
            account, credential, "<ours@example.com>"
        )
        is None
    )
    raw, envelope = composer_raw(["bo@tuta.com"], [], [], "s", "<p>b</p>")
    outcome = mail_sync.send_message(account, credential, account.email, envelope, raw)
    assert outcome.filed_uid == "lists/sent"


def test_starring_is_refused_without_a_request(sent):
    account, credential, requests = sent
    with pytest.raises(TutaError) as caught:
        mail_sync.set_flag(
            account,
            credential,
            "folders/inbox",
            ["list/mail"],
            FLAG_FLAGGED,
            True,
            uidvalidity=None,
        )
    assert caught.value.code == "unsupported_operation"
    assert requests == []


def test_seen_flag_reports_per_id_through_the_driver(sent):
    account, credential, _ = sent
    (outcome,) = mail_sync.set_flag(
        account,
        credential,
        "folders/inbox",
        ["list/mail"],
        FLAG_SEEN,
        True,
        uidvalidity=None,
    )
    assert (outcome.server_id, outcome.succeeded) == ("list/mail", True)


@pytest.mark.parametrize(
    "reply",
    [
        {**SEND_REPLY, "message_id": ""},
        {**SEND_REPLY, "sent_mail": 42},
        {**SEND_REPLY, "sent_date_ms": "now"},
        {**SEND_REPLY, "internal_recipients": True},
        {**SEND_REPLY, "external_recipients": "many"},
        {"message_id": "server-1"},
    ],
)
def test_a_misshapen_send_reply_is_rejected(reply):
    class Session:
        def request(self, *args, **kwargs):
            return reply

    raw = (
        b"From: ada@tuta.com\r\nTo: bo@tuta.com\r\nSubject: s\r\n"
        b"Content-Type: text/plain\r\n\r\nbody"
    )
    with pytest.raises(TutaError) as caught:
        tuta_messages.send_mime(
            Session(),
            "ada@tuta.com",
            "Ada",
            raw,
            ["bo@tuta.com"],
            ordinary_confirmed=True,
        )
    assert caught.value.code == "invalid_response"


def test_a_text_attachment_travels_as_bytes():
    captured = {}

    class Session:
        def request(self, operation, **kwargs):
            captured.update(kwargs)
            return dict(SEND_REPLY)

    note = Attachment(
        filename="note.txt", mime_type="text/plain", content="héllo".encode()
    )
    raw, _ = composer_raw(["bo@tuta.com"], [], [], "s", "<p>b</p>", [note])
    tuta_messages.send_mime(
        Session(), "ada@tuta.com", "Ada", raw, ["bo@tuta.com"], ordinary_confirmed=True
    )
    (attachment,) = captured["attachments"]
    assert attachment["mime_type"] == "text/plain"
    assert base64.b64decode(attachment["data"]) == "héllo".encode()


def test_a_body_with_a_bogus_charset_fails_definitively():
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "ada@tuta.com"
    message["To"] = "bo@tuta.com"
    message["Subject"] = "s"
    message.set_content("body")
    message.set_param("charset", "bogus-charset-xyz")

    class Session:
        def request(self, *args, **kwargs):
            pytest.fail("an undecodable body reached the helper")

    with pytest.raises(TutaError) as caught:
        tuta_messages.send_mime(
            Session(),
            "ada@tuta.com",
            "Ada",
            message.as_bytes(),
            ["bo@tuta.com"],
            ordinary_confirmed=True,
        )
    assert caught.value.code == "send_failed"


def test_a_plain_forwarded_message_keeps_its_bytes():
    from email.message import EmailMessage
    from email.mime.message import MIMEMessage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    nested = EmailMessage()
    nested["From"] = "zed@example.com"
    nested["To"] = "ada@tuta.com"
    nested["Subject"] = "original"
    nested.set_content("nested body")
    forwarded = MIMEMessage(nested)
    forwarded.add_header("Content-Disposition", "attachment", filename="original.eml")
    message = MIMEMultipart()
    message["From"] = "ada@tuta.com"
    message["To"] = "bo@tuta.com"
    message["Subject"] = "Fwd: original"
    message.attach(MIMEText("see attached"))
    message.attach(forwarded)
    captured = {}

    class Session:
        def request(self, operation, **kwargs):
            captured.update(kwargs)
            return dict(SEND_REPLY)

    tuta_messages.send_mime(
        Session(),
        "ada@tuta.com",
        "Ada",
        message.as_bytes(),
        ["bo@tuta.com"],
        ordinary_confirmed=True,
    )
    (attachment,) = captured["attachments"]
    assert b"nested body" in base64.b64decode(attachment["data"])


def test_a_forwarded_message_attachment_travels_as_bytes():
    from email.message import EmailMessage

    forwarded = EmailMessage()
    forwarded["From"] = "zed@example.com"
    forwarded["To"] = "ada@tuta.com"
    forwarded["Subject"] = "original"
    forwarded.set_content("original body")
    message = EmailMessage()
    message["From"] = "ada@tuta.com"
    message["To"] = "bo@tuta.com"
    message["Subject"] = "Fwd: original"
    message.set_content("see attached")
    message.add_attachment(
        forwarded.as_bytes(),
        maintype="message",
        subtype="rfc822",
        filename="original.eml",
    )
    captured = {}

    class Session:
        def request(self, operation, **kwargs):
            captured.update(kwargs)
            return dict(SEND_REPLY)

    tuta_messages.send_mime(
        Session(),
        "ada@tuta.com",
        "Ada",
        message.as_bytes(),
        ["bo@tuta.com"],
        ordinary_confirmed=True,
    )
    (attachment,) = captured["attachments"]
    assert attachment["mime_type"] == "message/rfc822"
    assert b"original body" in base64.b64decode(attachment["data"])
