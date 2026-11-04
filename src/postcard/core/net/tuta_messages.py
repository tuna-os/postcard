"""Map decrypted SDK data into Postcard's headers and MIME reader input.

Every function here treats the helper reply as untrusted input: a missing key
or a mistyped value raises TutaError, never KeyError or TypeError, so a broken
helper surfaces as a friendly message instead of leaking internals into the UI.
"""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser, HeaderParser
from email.utils import format_datetime, formataddr, getaddresses
from html import escape

from ..models.folder import FolderRole
from ..models.mailbox import MailboxInfo
from ..models.message_header import MessageHeader
from ..providers.api import PerIdOutcome
from ..threader import NO_SUBJECT
from . import MoveResult
from .imap_session import FLAG_SEEN
from .tuta_session import TutaError, TutaSession

# The helper applies at most this many ids per mutation call; larger batches
# are rejected rather than half-applied, so callers chunk first.
BATCH_LIMIT = 50


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


def _check_outcomes(result: object, message_ids: list[str]) -> list[str | None]:
    """Per-id error codes from a mutation reply, in input order.

    The helper reports every id it applied, the first failure's code, and
    `not_attempted` for the rest. A reply that does not line up with the
    request is corruption, not a partial success.
    """
    if not isinstance(result, dict):
        raise TutaError("invalid_response")
    rows = result.get("outcomes")
    if not isinstance(rows, list) or len(rows) != len(message_ids):
        raise TutaError("invalid_response")
    codes = []
    for row, message_id in zip(rows, message_ids, strict=True):
        if not isinstance(row, dict) or row.get("id") != message_id:
            raise TutaError("invalid_response")
        code = row.get("code")
        if code is not None and not isinstance(code, str):
            raise TutaError("invalid_response")
        codes.append(code)
    return codes


def set_flags(
    session: TutaSession, message_ids: list[str], flag: str, should_add: bool
) -> tuple[PerIdOutcome, ...]:
    """Report each id's outcome; only the seen flag exists server-side.

    Tuta mails have no starred/flagged state, so anything but FLAG_SEEN is
    refused rather than faked. A failed chunk stops the batch: later ids
    report `not_attempted`, never a guess.
    """
    if flag != FLAG_SEEN:
        raise TutaError("unsupported_operation")
    outcomes: list[PerIdOutcome] = []
    for offset in range(0, len(message_ids), BATCH_LIMIT):
        chunk = message_ids[offset : offset + BATCH_LIMIT]
        try:
            codes = _check_outcomes(
                session.request("mark_read", ids=chunk, is_read=should_add), chunk
            )
        except TutaError as error:
            outcomes.extend(PerIdOutcome(uid, False, error.code) for uid in chunk)
            outcomes.extend(
                PerIdOutcome(uid, False, "not_attempted")
                for uid in message_ids[offset + len(chunk) :]
            )
            break
        for uid, code in zip(chunk, codes, strict=True):
            outcomes.append(PerIdOutcome(uid, code is None, code))
        if any(codes):
            outcomes.extend(
                PerIdOutcome(uid, False, "not_attempted")
                for uid in message_ids[offset + len(chunk) :]
            )
            break
    return tuple(outcomes)


def move(session: TutaSession, message_ids: list[str], destination: str) -> MoveResult:
    result = MoveResult()
    for offset in range(0, len(message_ids), BATCH_LIMIT):
        chunk = message_ids[offset : offset + BATCH_LIMIT]
        try:
            codes = _check_outcomes(
                session.request("move", ids=chunk, destination=destination), chunk
            )
        except TutaError as error:
            result.failed_index, result.error = offset, error
            break
        for position, code in enumerate(codes):
            if code is not None:
                result.failed_index, result.error = offset + position, TutaError(code)
                break
            # Tuta may relocate a message between underlying lists. Reconcile
            # the destination on sync rather than asserting an unchanged ID.
            result.destination_uids.append(None)
        if result.failed_index is not None:
            break
    return result


@dataclass(frozen=True, slots=True)
class SendResult:
    """What the server filed for a send: its own ids, not ours.

    message_id is the id the server assigned, and sent_mail the Sent copy's
    compound id -- the link the next sync merges on. The recipient counts come
    from best-effort public-key lookups and are None when a lookup failed;
    ordinary delivery needs no keys, so unknown counts never fail a send.
    """

    message_id: str
    sent_mail: str
    sent_at: str
    internal_recipients: int | None
    external_recipients: int | None


def _send_body(message: EmailMessage) -> str:
    """The HTML the server stores: the html part, else the text part escaped."""
    try:
        body = message.get_body(preferencelist=("html", "plain"))
        if body is None:
            return ""
        content = body.get_content()
    except (LookupError, ValueError, UnicodeError):
        raise TutaError("send_failed") from None
    if not isinstance(content, str):
        raise TutaError("send_failed")
    if body.get_content_type() == "text/html":
        return content
    return f"<pre>{escape(content)}</pre>"


def _nested_message_bytes(part: EmailMessage) -> bytes:
    """The bytes of a forwarded message attachment.

    A 7bit message/rfc822 part parses into its nested message; a base64 one
    (which is what the composer emits for an attached .eml file) parses into
    the still-encoded text, which is decoded back strictly. Anything else is
    refused rather than transmitted as garbage.
    """
    nested = part.get_payload()
    if not isinstance(nested, list) or len(nested) != 1:
        raise TutaError("attachment_failed")
    (inner,) = nested
    if not isinstance(inner, Message) or inner.is_multipart():
        raise TutaError("attachment_failed")
    encoding = str(part.get("Content-Transfer-Encoding") or "7bit").lower()
    if encoding in ("7bit", "8bit", "binary"):
        return inner.as_bytes()
    if encoding == "base64":
        text = inner.get_payload()
        if not isinstance(text, str):
            raise TutaError("attachment_failed")
        try:
            return base64.b64decode("".join(text.split()), validate=True)
        except ValueError:
            raise TutaError("attachment_failed") from None
    raise TutaError("attachment_failed")


def _send_attachments(message: EmailMessage) -> list[dict]:
    rows = []
    for part in message.iter_attachments():
        # A parsed message/rfc822 part is a container holding the nested
        # message, not bytes of its own.
        if part.is_multipart():
            if part.get_content_type() != "message/rfc822":
                raise TutaError("attachment_failed")
            data = _nested_message_bytes(part)
        else:
            data = part.get_payload(decode=True)
            if isinstance(data, Message):
                data = data.as_bytes()
            elif isinstance(data, str):
                data = data.encode()
            elif data is None:
                data = b""
            if not isinstance(data, bytes):
                raise TutaError("attachment_failed")
        cid = part["Content-ID"]
        rows.append(
            {
                "name": part.get_filename() or "attachment",
                "mime_type": part.get_content_type(),
                "cid": cid.strip().strip("<>")
                if isinstance(cid, str) and cid.strip()
                else None,
                "data": base64.b64encode(data).decode(),
            }
        )
    return rows


def _address_rows(addrs: list[tuple[str, str]]) -> list[dict]:
    return [{"name": name, "address": addr} for name, addr in addrs]


def send_mime(  # noqa: PLR0913 -- the six parts of a send, like build_mime_message.
    session: TutaSession,
    sender: str,
    sender_name: str,
    raw: bytes,
    envelope: list[str],
    *,
    ordinary_confirmed: bool,
) -> SendResult:
    """Send one message as ordinary (unencrypted) Tuta mail.

    To/Cc display names come from the headers; Bcc survives only in the
    envelope, so those addresses are derived as envelope minus To/Cc -- a retry
    without the envelope would silently drop Bcc. Threading headers are not
    submitted: the server links conversations by its own ids, so every send
    starts a new conversation, while the filed raw keeps In-Reply-To and
    References for local threading.

    `ordinary_confirmed` is the caller's explicit delivery choice, given when
    the message was queued; without it the helper refuses. Failures split two
    ways: definitive ones (`send_failed` and the validation codes) sent
    nothing and are safe to retry, while `send_uncertain` means the mail may
    have gone out -- the caller must terminal-fail it for a human to check
    Sent, never auto-retry it.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw)
    to_addrs = [
        (name, addr) for name, addr in getaddresses(message.get_all("To", [])) if addr
    ]
    cc_addrs = [
        (name, addr) for name, addr in getaddresses(message.get_all("Cc", [])) if addr
    ]
    named = {addr.casefold() for _, addr in to_addrs + cc_addrs}
    bcc_addrs = [("", addr) for addr in envelope if addr.casefold() not in named]
    reply_tos = [
        (name, addr)
        for name, addr in getaddresses(message.get_all("Reply-To", []))
        if addr
    ]
    result = session.request(
        "send",
        sender=sender,
        sender_name=sender_name,
        to=_address_rows(to_addrs),
        cc=_address_rows(cc_addrs),
        bcc=_address_rows(bcc_addrs),
        reply_tos=_address_rows(reply_tos),
        subject=str(message["Subject"] or "") or NO_SUBJECT,
        body_html=_send_body(message),
        delivery="ordinary",
        ordinary_confirmed=ordinary_confirmed,
        attachments=_send_attachments(message),
    )
    if not isinstance(result, dict):
        raise TutaError("invalid_response")
    message_id, sent_mail = result.get("message_id"), result.get("sent_mail")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(sent_mail, str)
        or not sent_mail
    ):
        raise TutaError("invalid_response")
    internal, external = (
        result.get("internal_recipients"),
        result.get("external_recipients"),
    )
    for count in (internal, external):
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int)
        ):
            raise TutaError("invalid_response")
    return SendResult(
        message_id=message_id,
        sent_mail=sent_mail,
        sent_at=_iso_date(result.get("sent_date_ms")),
        internal_recipients=internal,
        external_recipients=external,
    )
