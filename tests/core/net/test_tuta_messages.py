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


def test_an_empty_flag_change_sends_no_request():
    class Session:
        def request(self, *args, **kwargs):
            pytest.fail("an empty flag change reached the helper")

    tuta_messages.set_flags(Session(), [], FLAG_SEEN, True)


def test_folder_rows_keep_their_server_stated_identity():
    (mailbox,) = tuta_messages.mailbox_infos(
        [{"id": "lists/inbox", "label": "Posteingang", "role": "inbox", "parent": ""}]
    )
    assert mailbox.name == "lists/inbox"
    assert mailbox.label == "Posteingang"
    assert mailbox.role == "inbox"
    assert mailbox.parent == ""


def test_an_unknown_folder_role_does_not_fail_the_sync():
    (mailbox,) = tuta_messages.mailbox_infos(
        [{"id": "lists/x", "label": "X", "role": "someday", "parent": ""}]
    )
    assert mailbox.role == "other"


@pytest.mark.parametrize(
    "rows",
    [
        {"id": "lists/inbox"},
        ["lists/inbox"],
        [{"label": "Inbox", "role": "inbox", "parent": ""}],
        [{"id": "", "label": "Inbox", "role": "inbox", "parent": ""}],
        [{"id": 42, "label": "Inbox", "role": "inbox", "parent": ""}],
    ],
)
def test_a_folder_row_without_an_id_is_not_a_folder(rows):
    with pytest.raises(TutaError) as caught:
        tuta_messages.mailbox_infos(rows)
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "item",
    [
        "lists/message",
        {**HEADER, "id": ""},
        {**HEADER, "id": 42},
        {**HEADER, "date_ms": "yesterday"},
        {**HEADER, "date_ms": True},
        {**HEADER, "date_ms": 10**20},
    ],
)
def test_a_header_without_identity_or_date_is_rejected(item):
    with pytest.raises(TutaError) as caught:
        tuta_messages.message_header(item)
    assert caught.value.code == "invalid_response"


def test_a_missing_unread_flag_fails_visible():
    assert tuta_messages.message_header({**HEADER, "is_unread": True}).is_unread is True
    header = dict(HEADER)
    del header["is_unread"]
    assert tuta_messages.message_header(header).is_unread is True


@pytest.mark.parametrize(
    "item",
    [
        "body",
        {"html": "body"},
        {"header": "lists/message", "html": "body"},
        {"header": HEADER, "html": 42},
        {"header": {**HEADER, "date_ms": "yesterday"}, "html": "body"},
        {"header": HEADER, "html": "body", "to": "ada@tuta.com"},
        {"header": HEADER, "html": "body", "attachments": "file.bin"},
    ],
)
def test_a_body_without_shape_is_rejected(item):
    with pytest.raises(TutaError) as caught:
        tuta_messages.mime_message(item)
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    "attachment",
    [
        "file.bin",
        {"name": "file.bin"},
        {"name": "file.bin", "data": "!!! not base64 !!!"},
        {"name": "file.bin", "data": 42},
    ],
)
def test_a_broken_attachment_fails_as_one_not_as_a_body(attachment):
    with pytest.raises(TutaError) as caught:
        tuta_messages.mime_message(
            {"header": HEADER, "html": "body", "attachments": [attachment]}
        )
    assert caught.value.code == "attachment_failed"


def test_a_mistyped_attachment_label_falls_back_to_a_default():
    raw = tuta_messages.mime_message(
        {
            "header": HEADER,
            "html": "body",
            "attachments": [{"name": 42, "mime_type": 42, "data": "aGk="}],
        }
    )
    attachment = next(
        BytesParser(policy=policy.default).parsebytes(raw).iter_attachments()
    )
    assert attachment.get_filename() == "attachment"
    assert attachment.get_content_type() == "application/octet-stream"
