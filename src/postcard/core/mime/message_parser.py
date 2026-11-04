import contextlib
import email
import email.parser
import email.utils
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

from ..models.attachment import Attachment
from . import html_colors

# RFC 2369 wraps each unsubscribe target in angle brackets and separates them
# with commas, which may also appear inside a target -- so match the brackets.
_TARGET = re.compile(r"<([^>]+)>")

# Bounds on MIME work. email.message_from_bytes copies the input and each
# base64 part decodes to a second copy, so the raw byte count is the budget
# everything else is measured against. A refusal raises rather than returning
# a half-parsed message: truncated mail that looks complete is worse than mail
# that says it is too large.
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024
MAX_MIME_PARTS = 1000
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class MimeLimitError(ValueError):
    """A message this parser refuses to decode: too large, not truncated."""


class MessageTooLargeError(MimeLimitError):
    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"message of {size_bytes} bytes exceeds the {limit_bytes} byte limit"
        )


class AttachmentTooLargeError(MessageTooLargeError):
    def __init__(self, filename: str, size_bytes: int, limit_bytes: int) -> None:
        self.filename = filename
        super().__init__(size_bytes, limit_bytes)


class TooManyPartsError(MimeLimitError):
    def __init__(self, part_count: int, limit: int) -> None:
        self.part_count = part_count
        self.limit = limit
        super().__init__(
            f"message has more than {limit} MIME parts ({part_count} seen)"
        )


class ParseCancelledError(Exception):
    """A parse stopped because its view went away mid-decode. Expected."""


@dataclass(frozen=True, slots=True)
class Unsubscribe:
    """Where a mailing list says it will accept an unsubscribe request."""

    url: str = ""
    mailto: str = ""
    is_one_click: bool = False


@dataclass(frozen=True)
class ParsedMessage:
    """A fully decoded message, built on a worker and read on the main loop.

    Frozen so a result crossing threads cannot be half-rewritten; the lists it
    holds are filled once at construction and never mutated afterwards.
    """

    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    subject: str = ""
    from_display: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    date: str = ""


def clock_format(is_12_hour_clock: bool) -> str:
    """The strftime pattern for a time of day: "9:05 PM" or "21:05"."""
    # %-I drops the leading zero (glibc); a locale with no AM/PM leaves a
    # trailing space for the caller to strip.
    return "%-I:%M %p" if is_12_hour_clock else "%H:%M"


def parse_message(
    raw: bytes,
    *,
    is_12_hour_clock: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
) -> ParsedMessage:
    """Decode a message's headers, bodies and attachments, within the bounds.

    Refusals raise MimeLimitError; malformed parts are kept on a best-effort
    basis, never dropped. is_cancelled is polled once per part so a view torn
    down mid-decode stops paying for a result nobody will read.
    """
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageTooLargeError(len(raw), MAX_MESSAGE_BYTES)
    if is_cancelled is not None and is_cancelled():
        raise ParseCancelledError

    msg = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(msg, EmailMessage)

    subject = str(msg.get("Subject", ""))
    from_display = ", ".join(_addresses(msg, "From"))
    to = _addresses(msg, "To")
    cc = _addresses(msg, "Cc")
    bcc = _addresses(msg, "Bcc")
    date = _format_date(msg.get("Date"), is_12_hour_clock)

    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = []
    part_count = 0
    for part in msg.walk():
        part_count += 1
        if part_count > MAX_MIME_PARTS:
            raise TooManyPartsError(part_count, MAX_MIME_PARTS)
        if is_cancelled is not None and is_cancelled():
            raise ParseCancelledError
        if part.is_multipart():
            continue  # a container part -- its children are visited on their own

        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        if disposition == "attachment":
            attachments.append(_as_attachment(part))
        elif content_type == "text/plain" and text_body is None:
            text_body = _decoded_text(part)
        elif content_type == "text/html" and html_body is None:
            html_body = _decoded_text(part)
        else:
            # An inline image or unrecognised type is kept, not dropped.
            attachments.append(_as_attachment(part))

    return ParsedMessage(
        text_body=text_body,
        html_body=html_body,
        attachments=attachments,
        subject=subject,
        from_display=from_display,
        to=to,
        cc=cc,
        bcc=bcc,
        date=date,
    )


def parse_headers(raw: bytes, *, is_12_hour_clock: bool = False) -> ParsedMessage:
    """Read a message's headers without touching its body.

    The refusal UX shows these for a message whose body is over the limit, so
    the headers get their own bound: a header block past MAX_HEADER_BYTES
    raises rather than parsing megabytes of headers on the main loop.
    """
    header_end = _header_end(raw)
    if header_end is None:
        header_end = len(raw)
    if header_end > MAX_HEADER_BYTES:
        raise MessageTooLargeError(header_end, MAX_HEADER_BYTES)
    msg = email.parser.BytesParser(policy=default_policy).parsebytes(
        raw, headersonly=True
    )
    assert isinstance(msg, EmailMessage)
    return ParsedMessage(
        subject=str(msg.get("Subject", "")),
        from_display=", ".join(_addresses(msg, "From")),
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        bcc=_addresses(msg, "Bcc"),
        date=_format_date(msg.get("Date"), is_12_hour_clock),
    )


def _header_end(raw: bytes) -> int | None:
    """Offset where the header block ends, tolerating either line ending."""
    lf_blank = raw.find(b"\n\n")
    crlf_blank = raw.find(b"\r\n\r\n")
    candidates = [offset for offset in (lf_blank, crlf_blank) if offset != -1]
    return min(candidates) if candidates else None


def write_file_atomically(path: Path, content: bytes) -> None:
    """Write bytes to path via a same-directory temp file plus rename.

    The temp file inherits 0600 from mkstemp, is fsynced before the rename,
    and is removed again if anything fails. A failure raises: there is no
    fallback location, so no copy of message bytes is ever left where the
    caller did not ask for one.
    """
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".postcard-", delete=False
        ) as tmp:
            temp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        assert temp_name is not None
        os.replace(temp_name, path)
    except BaseException:
        if temp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
        raise


def parse_unsubscribe(raw: bytes) -> Unsubscribe | None:
    """Read a message's List-Unsubscribe target, parsing its headers only."""
    msg = email.parser.BytesParser(policy=default_policy).parsebytes(
        raw, headersonly=True
    )
    assert isinstance(msg, EmailMessage)
    url = ""
    mailto = ""
    for bracketed in _TARGET.findall(str(msg.get("List-Unsubscribe", ""))):
        # These URLs carry a long opaque token, so senders fold the header --
        # and unfolding keeps the continuation whitespace inside the URL, where
        # it makes the request unsendable. Strip every space, not just the ends.
        target = "".join(bracketed.split())
        scheme = target.partition(":")[0].lower()
        # A stranger's header may name any scheme, and a registered handler
        # would happily take file: or smb: from one. http is honoured only as a
        # link, never as a request this app makes itself -- so an https target
        # wins even when an http one was published first.
        if scheme in ("https", "http") and not url.lower().startswith("https:"):
            url = target
        elif scheme == "mailto" and not mailto:
            mailto = target

    if not url and not mailto:
        return None

    post = str(msg.get("List-Unsubscribe-Post", "")).lower()
    is_one_click = url.lower().startswith("https:") and "one-click" in post
    return Unsubscribe(url=url, mailto=mailto, is_one_click=is_one_click)


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    raw = [str(value) for value in msg.get_all(header, [])]
    out = []
    for name, addr in email.utils.getaddresses(raw):
        if name and addr:
            out.append(f"{name} <{addr}>")
        elif addr:
            out.append(addr)
        elif name:
            out.append(name)
    return out


def _format_date(raw: object, is_12_hour_clock: bool = False) -> str:
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return str(raw)
    return parsed.strftime(f"%b %d, %Y {clock_format(is_12_hour_clock)}").strip()


def _decoded_text(part: EmailMessage) -> str:
    """A text part's content, surviving charsets this machine never heard of.

    get_content raises LookupError on an unknown charset; decoding the raw
    payload as UTF-8 with replacements keeps the readable bytes instead of
    failing the whole message.
    """
    try:
        content = part.get_content()
    except (LookupError, ValueError, UnicodeError):
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)
    return content if isinstance(content, str) else str(content)


def _as_attachment(part: EmailMessage) -> Attachment:
    filename = part.get_filename() or "attachment"
    # One decode, not get_content's decode plus a transcode: an attachment is
    # bytes on the way out, so the charset the sender declared is irrelevant.
    # The estimate refuses first, so a huge part never pays for its own decode.
    encoded = part.get_payload()
    if isinstance(encoded, str):
        estimated = len(encoded) * 3 // 4
        if estimated > MAX_ATTACHMENT_BYTES:
            raise AttachmentTooLargeError(filename, estimated, MAX_ATTACHMENT_BYTES)
    try:
        payload = part.get_payload(decode=True)
    except (ValueError, TypeError):
        payload = None
    if payload is None:
        content = b""
    elif isinstance(payload, EmailMessage):
        content = payload.as_bytes()
    elif isinstance(payload, bytes):
        content = payload
    else:
        content = str(payload).encode("utf-8", errors="replace")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise AttachmentTooLargeError(filename, len(content), MAX_ATTACHMENT_BYTES)
    return Attachment(
        filename=filename,
        mime_type=part.get_content_type(),
        content=content,
    )


# WebKit's auto-load-images setting only gates <img>; a remote stylesheet,
# @import, @font-face or <iframe> loads regardless and leaks the read just the
# same. Only img-src is toggled: remote CSS is never needed to read mail, so it
# stays blocked even after the user asks for images.
_CSP = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:"
_CSP_WITH_IMAGES = _CSP + " https: http:"


def sandbox_html(html: str, *, are_remote_images_allowed: bool, is_dark: bool) -> str:
    """Wrap a message body in a document whose CSP blocks remote subresources.

    The body is recolored onto the reader's surface (see `html_colors`), and
    `color-scheme` gives unstyled text and links the matching defaults.
    """
    policy = _CSP_WITH_IMAGES if are_remote_images_allowed else _CSP
    scheme = "dark" if is_dark else "light"
    return (
        '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" '
        f'content="{policy}"><style>:root{{color-scheme:{scheme}}}'
        "html,body{background:transparent}</style></head>"
        f"<body>{html_colors.recolor(html, is_dark=is_dark)}</body></html>"
    )
