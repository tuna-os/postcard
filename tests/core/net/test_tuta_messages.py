import base64
from email import policy
from email.parser import BytesParser

import pytest

from postcard.core.net import tuta_messages
from postcard.core.net.imap_session import FLAG_FLAGGED, FLAG_SEEN
from postcard.core.net.tuta_session import TutaError

HEADER = {
    "id": "list/message",
    "subject": "नमस्ते",
    "date_ms": 0,
    "sender": {"name": "Ada", "address": "ada@tuta.com"},
    "recipient": {"name": "James", "address": "james@example.com"},
    "is_unread": True,
    "confidential": True,
}


def test_header_keeps_compound_id_unicode_and_utc_date():
    result = tuta_messages.message_header(HEADER)
    assert result.uid == "list/message"
    assert result.date == "1970-01-01T00:00:00+00:00"
    assert result.subject == "नमस्ते"
    assert result.is_unread
    assert result.sender_address == "ada@tuta.com"


def test_body_conversion_keeps_thread_headers_and_attachment_bytes():
    data = b"\x00attachment\xff"
    raw = tuta_messages.mime_message(
        {
            "header": HEADER,
            "html": "<p>नमस्ते</p>",
            "headers": (
                "Message-ID: <original@example.com>\r\n"
                "References: <parent@example.com>\r\n"
                "In-Reply-To: <parent@example.com>\r\n"
            ),
            "to": [HEADER["recipient"]],
            "cc": [],
            "reply_to": [{"name": "Support", "address": "support@example.com"}],
            "attachments": [
                {
                    "name": "file.bin",
                    "mime_type": "application/octet-stream",
                    "cid": "part1",
                    "data": base64.b64encode(data).decode(),
                }
            ],
        }
    )
    result = BytesParser(policy=policy.default).parsebytes(raw)
    assert result["Subject"] == "नमस्ते"
    assert result["Message-ID"] == "<original@example.com>"
    assert result["In-Reply-To"] == "<parent@example.com>"
    assert result["Reply-To"].addresses[0].addr_spec == "support@example.com"
    attachment = next(result.iter_attachments())
    assert attachment.get_payload(decode=True) == data
    assert attachment.get_filename() == "file.bin"
    assert (
        result.get_body(preferencelist=("html",)).get_content().strip() == "<p>नमस्ते</p>"
    )


def test_unknown_message_id_is_not_invented():
    raw = tuta_messages.mime_message({"header": HEADER, "html": "body"})
    assert BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"] is None


def test_flag_mapping_does_not_claim_unsupported_starring():
    class Session:
        def __init__(self):
            self.requests = []

        def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))

    session = Session()
    tuta_messages.set_flags(session, ["list/message"], FLAG_SEEN, True)
    assert session.requests == [
        (("mark_read",), {"ids": ["list/message"], "is_read": True})
    ]
    with pytest.raises(TutaError):
        tuta_messages.set_flags(session, ["list/message"], FLAG_FLAGGED, True)
    assert len(session.requests) == 1


def test_move_reports_successful_prefix_without_guessing_new_ids():
    class Session:
        def request(self, operation, **kwargs):
            if kwargs["ids"] == ["list/b"]:
                raise TutaError("request_failed")

    result = tuta_messages.move(
        Session(), ["list/a", "list/b", "list/c"], "folders/trash"
    )
    assert result.destination_uids == [None]
    assert result.failed_index == 1
    assert isinstance(result.error, TutaError)
