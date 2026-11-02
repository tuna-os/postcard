"""Map decrypted SDK data into Postcard's headers and MIME reader input."""

import base64
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import HeaderParser
from email.utils import format_datetime, formataddr

from ..models.message_header import MessageHeader
from ..threader import NO_SUBJECT
from . import MoveResult
from .imap_session import FLAG_SEEN
from .tuta_session import TutaError, TutaSession


def _address(value: dict | None) -> tuple[str, str]:
    value = value or {}
    return str(value.get("name") or ""), str(value.get("address") or "")


def message_header(item: dict) -> MessageHeader:
    sender_name, sender_address = _address(item.get("sender"))
    recipient_name, recipient_address = _address(item.get("recipient"))
    return MessageHeader(
        uid=item["id"],
        sender=sender_name or sender_address,
        sender_address=sender_address,
        recipient=recipient_name or recipient_address,
        recipient_address=recipient_address,
        subject=item.get("subject") or NO_SUBJECT,
        date=datetime.fromtimestamp(item["date_ms"] / 1000, UTC).isoformat(),
        is_unread=item["is_unread"],
        addresses=[(sender_name, sender_address), (recipient_name, recipient_address)],
    )


def mime_message(item: dict) -> bytes:
    """Build reader input locally; this does not use an email wire protocol.

    Preserve actual threading headers when available. No synthetic Message-ID:
    inventing one would sever the relationship to the official client.
    """
    header = item["header"]
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = header.get("subject") or NO_SUBJECT
    message["From"] = formataddr(_address(header.get("sender")))
    message["Date"] = format_datetime(
        datetime.fromtimestamp(header["date_ms"] / 1000, UTC)
    )
    for name, key in (("To", "to"), ("Cc", "cc"), ("Reply-To", "reply_to")):
        addresses = item.get(key) or []
        if addresses:
            message[name] = ", ".join(formataddr(_address(a)) for a in addresses)
    original = HeaderParser(policy=policy.default).parsestr(item.get("headers") or "")
    for name in (
        "Message-ID",
        "In-Reply-To",
        "References",
        "List-Unsubscribe",
        "List-Unsubscribe-Post",
    ):
        if original[name]:
            message[name] = str(original[name])
    message.set_content(item["html"], subtype="html")
    for attachment in item.get("attachments") or []:
        mime_type = attachment.get("mime_type") or "application/octet-stream"
        main_type, separator, sub_type = mime_type.partition("/")
        if not separator:
            main_type, sub_type = "application", "octet-stream"
        message.add_attachment(
            base64.b64decode(attachment["data"], validate=True),
            maintype=main_type,
            subtype=sub_type,
            filename=attachment["name"],
            cid=attachment.get("cid"),
        )
    return message.as_bytes()


def fetch_mime(session: TutaSession, message_id: str) -> bytes:
    return mime_message(session.request("message", id=message_id))


def set_flags(
    session: TutaSession, message_ids: list[str], flag: str, should_add: bool
) -> None:
    if flag != FLAG_SEEN:
        raise TutaError("unsupported_operation")
    session.request("mark_read", ids=message_ids, is_read=should_add)


def move(session: TutaSession, message_ids: list[str], destination: str) -> MoveResult:
    result = MoveResult()
    for index, message_id in enumerate(message_ids):
        try:
            session.request("move", ids=[message_id], destination=destination)
        except TutaError as error:
            result.failed_index, result.error = index, error
            break
        # Tuta may relocate a message between underlying lists. Reconcile the
        # destination on sync rather than asserting an unchanged ID.
        result.destination_uids.append(None)
    return result
