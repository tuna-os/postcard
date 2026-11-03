"""Map decrypted SDK data into Postcard's headers and MIME reader input.

Every function here treats the helper reply as untrusted input: a missing key
or a mistyped value raises TutaError, never KeyError or TypeError, so a broken
helper surfaces as a friendly message instead of leaking internals into the UI.
"""

import base64
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import HeaderParser
from email.utils import format_datetime, formataddr

from ..models.folder import FolderRole
from ..models.mailbox import MailboxInfo
from ..models.message_header import MessageHeader
from ..threader import NO_SUBJECT
from . import MoveResult
from .imap_session import FLAG_SEEN
from .tuta_session import TutaError, TutaSession


def _address(value: object) -> tuple[str, str]:
    if not isinstance(value, dict):
        return "", ""
    return str(value.get("name") or ""), str(value.get("address") or "")


def _moment(value: object) -> datetime:
    """Milliseconds since the epoch as an aware datetime."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TutaError("invalid_response")
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        raise TutaError("invalid_response") from None


def _iso_date(value: object) -> str:
    """Milliseconds since the epoch as a stored timestamp."""
    return _moment(value).isoformat()


def mailbox_infos(rows: object) -> list[MailboxInfo]:
    """Folder rows from a sync reply, as the server lists them.

    An unknown role becomes OTHER rather than failing the sync: the folder is
    still browsable, and a newer helper may name roles this client predates.
    A row without an id is not a folder at all.
    """
    if not isinstance(rows, list):
        raise TutaError("invalid_response")
    mailboxes = []
    for row in rows:
        if not isinstance(row, dict):
            raise TutaError("invalid_response")
        folder_id = row.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise TutaError("invalid_response")
        try:
            role = FolderRole(row.get("role"))
        except ValueError:
            role = FolderRole.OTHER
        label = row.get("label")
        parent = row.get("parent")
        mailboxes.append(
            MailboxInfo(
                folder_id,
                "/",
                "",
                label=label if isinstance(label, str) else "",
                role=role,
                parent=parent if isinstance(parent, str) else "",
            )
        )
    return mailboxes


def message_header(item: dict) -> MessageHeader:
    if not isinstance(item, dict):
        raise TutaError("invalid_response")
    uid = item.get("id")
    if not isinstance(uid, str) or not uid:
        raise TutaError("invalid_response")
    sender_name, sender_address = _address(item.get("sender"))
    recipient_name, recipient_address = _address(item.get("recipient"))
    return MessageHeader(
        uid=uid,
        sender=sender_name or sender_address,
        sender_address=sender_address,
        recipient=recipient_name or recipient_address,
        recipient_address=recipient_address,
        subject=item.get("subject") or NO_SUBJECT,
        date=_iso_date(item.get("date_ms")),
        # Fail visible: an unread badge that should not be there heals on the
        # next sync, while a missing one hides mail that arrived.
        is_unread=bool(item.get("is_unread", True)),
        addresses=[(sender_name, sender_address), (recipient_name, recipient_address)],
    )


def mime_message(item: dict) -> bytes:
    """Build reader input locally; this does not use an email wire protocol.

    Preserve actual threading headers when available. No synthetic Message-ID:
    inventing one would sever the relationship to the official client.
    """
    if not isinstance(item, dict):
        raise TutaError("invalid_response")
    header = item.get("header")
    if not isinstance(header, dict):
        raise TutaError("invalid_response")
    html = item.get("html")
    if not isinstance(html, str):
        raise TutaError("invalid_response")
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = header.get("subject") or NO_SUBJECT
    message["From"] = formataddr(_address(header.get("sender")))
    message["Date"] = format_datetime(_moment(header.get("date_ms")))
    for name, key in (("To", "to"), ("Cc", "cc"), ("Reply-To", "reply_to")):
        addresses = item.get(key) or []
        if not isinstance(addresses, list):
            raise TutaError("invalid_response")
        if addresses:
            message[name] = ", ".join(formataddr(_address(a)) for a in addresses)
    _copy_thread_headers(message, item)
    message.set_content(html, subtype="html")
    attachments = item.get("attachments") or []
    if not isinstance(attachments, list):
        raise TutaError("invalid_response")
    for attachment in attachments:
        _add_attachment(message, attachment)
    return message.as_bytes()


def _copy_thread_headers(message: EmailMessage, item: dict) -> None:
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


def _add_attachment(message: EmailMessage, attachment: object) -> None:
    if not isinstance(attachment, dict):
        raise TutaError("attachment_failed")
    try:
        data = base64.b64decode(attachment["data"], validate=True)
    except (KeyError, TypeError, ValueError):
        raise TutaError("attachment_failed") from None
    mime_type = attachment.get("mime_type")
    if not isinstance(mime_type, str):
        mime_type = "application/octet-stream"
    main_type, separator, sub_type = mime_type.partition("/")
    if not separator:
        main_type, sub_type = "application", "octet-stream"
    cid = attachment.get("cid")
    filename = attachment.get("name")
    message.add_attachment(
        data,
        maintype=main_type,
        subtype=sub_type,
        filename=filename if isinstance(filename, str) and filename else "attachment",
        cid=cid if isinstance(cid, str) else None,
    )


def fetch_mime(session: TutaSession, message_id: str) -> bytes:
    return mime_message(session.request("message", id=message_id))


def set_flags(
    session: TutaSession, message_ids: list[str], flag: str, should_add: bool
) -> None:
    if flag != FLAG_SEEN:
        raise TutaError("unsupported_operation")
    if not message_ids:
        return
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
