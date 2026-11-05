"""Session discovery and mail-only requests for JMAP (RFC 8620 + RFC 8621).

Reads (Mailbox/get and /changes, Email/query and /queryChanges and /get and
/changes, EmailSubmission/get) go over a default session. Writes (Email/set,
EmailSubmission/set) need a session built with writable=True, and are still
refused on an account the server marked read-only. Anything outside mail --
calendars, contacts, quotas -- is refused before it reaches the network. Like
the Graph session it only speaks the protocol: no database, no GTK, and it
runs on a worker thread.
"""

import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from . import NET_TIMEOUT_SECONDS, https_context
from .auth import MECHANISM_LOGIN, MECHANISM_XOAUTH2, Credential

logger = logging.getLogger(__name__)

CORE_URN = "urn:ietf:params:jmap:core"
MAIL_URN = "urn:ietf:params:jmap:mail"
SUBMISSION_URN = "urn:ietf:params:jmap:submission"

# The calls a default session sends: reads only, so a read-only account is
# enough and no call can change the mailbox.
READ_METHODS = frozenset(
    {
        "Mailbox/get",
        "Mailbox/changes",
        "Email/query",
        "Email/queryChanges",
        "Email/get",
        "Email/changes",
        "EmailSubmission/get",
    }
)

# The calls a writable session additionally sends. Email/import is
# deliberately absent: archiving server-side state the client never saw would
# silently resurrect mail the user deleted elsewhere.
WRITE_METHODS = frozenset({"Email/set", "EmailSubmission/set"})

# Everything JmapSession.request ever sends. Calendars, contacts, vacation
# responses and the rest are refused before they reach the network.
MAIL_METHODS = READ_METHODS | WRITE_METHODS

# Client-side error codes. A method the server itself refuses instead carries
# the JMAP "type" it answered with, which is RFC 8620's own fixed vocabulary
# (accountNotFound, unknownMethod, invalidArguments, cannotCalculateChanges,
# stateMismatch). Per-object /set failures keep their own "type" in SetError.
CODE_HTTP = "httpError"
CODE_BAD_SESSION = "badSession"
CODE_READ_ONLY = "readOnly"
CODE_CANCELLED = "cancelled"
CODE_INVALID_ARGUMENTS = "invalidArguments"
CODE_UNSUPPORTED_CREDENTIAL = "unsupportedCredential"
CODE_NO_SUBMISSION = "submissionUnsupported"
CODE_PUSH_FAILED = "pushUnavailable"
CODE_NOT_FOUND = "notFound"
CODE_UNCERTAIN_SUBMISSION = "uncertainSubmission"

# A stored state token the server no longer honours: the only way on is a
# full walk, never applying the failure as an empty result.
EXPIRED_STATE_CODES = frozenset({"cannotCalculateChanges"})

# Reads are always safe to send again. Writes retry only on an explicit rate
# limit (429 means "not processed, try later"); a 503/504 answer to a /set may
# already have acted, so it is raised for the caller to reconcile instead.
_RETRYABLE_STATUSES = frozenset({429, 503, 504})
_WRITE_RETRYABLE_STATUSES = frozenset({429})
_MAX_ATTEMPTS = 4
_MAX_RETRY_SECONDS = 30

# Used when the session resource omits the core capability's limits.
_DEFAULT_MAX_CALLS = 16
_DEFAULT_MAX_OBJECTS_IN_GET = 500
_DEFAULT_MAX_OBJECTS_IN_SET = 100

# The variables each URI template must provide to be usable.
_DOWNLOAD_VARS = frozenset({"accountId", "blobId"})
_UPLOAD_VARS = frozenset({"accountId"})
_EVENT_SOURCE_VARS = frozenset({"types", "closeafter", "ping"})

_TEMPLATE_VAR = re.compile(r"\{([A-Za-z0-9_]+)\}")


class JmapError(Exception):
    """A JMAP request that failed, or that must not be sent at all."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> dict:
        return json.loads(self.body) if self.body else {}


Transport = Callable[[urllib.request.Request], Response]


def urlopen_transport(request: urllib.request.Request) -> Response:
    """Send a request for real. An HTTP error status is returned, not raised,
    so retrying and error parsing live in one place."""
    try:
        # S310: every URL is https-only (discovery pins them to the session's
        # https origin, and each caller rechecks), and the context verifies
        # rather than trusting the ambient default.
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=NET_TIMEOUT_SECONDS, context=https_context()
        ) as reply:
            return Response(reply.status, reply.read(), dict(reply.headers))
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read(), dict(error.headers))
    except urllib.error.URLError as error:
        # Same shape as graph_session.urlopen_transport: the reason is the
        # socket or TLS error itself, which errors.classify already knows how
        # to word; the URLError wrapper would hide it.
        if isinstance(error.reason, Exception):
            raise error.reason from error
        raise


def raise_if_cancelled(cancelled: threading.Event | None) -> None:
    """Stop a multi-request sync the moment the caller asks it to."""
    if cancelled is not None and cancelled.is_set():
        raise JmapError(0, CODE_CANCELLED, "the JMAP sync was cancelled")


def expand_template(template: str, variables: Mapping[str, str]) -> str:
    """Fill an RFC 6570 level-1 URI template, e.g. downloadUrl with its ids."""
    missing: list[str] = []

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            missing.append(name)
            return ""
        return urllib.parse.quote(variables[name], safe="")

    expanded = _TEMPLATE_VAR.sub(fill, template)
    if missing or "{" in expanded or "}" in expanded:
        raise JmapError(0, CODE_INVALID_ARGUMENTS, f"could not expand {template}")
    return expanded


@dataclass(frozen=True)
class Session:
    """Where a JMAP account lives: its id plus the session resource's URLs."""

    account_id: str
    api_url: str
    download_template: str
    upload_template: str
    event_source_template: str
    capabilities: frozenset[str]
    max_calls: int
    max_objects_in_get: int
    max_objects_in_set: int
    is_read_only: bool
    submission_supported: bool

    def download_url(self, blob_id: str, name: str) -> str:
        return expand_template(
            self.download_template,
            {"accountId": self.account_id, "blobId": blob_id, "name": name},
        )

    def upload_url(self) -> str:
        return expand_template(self.upload_template, {"accountId": self.account_id})

    def event_source_url(self, types: str, close_after: int, ping: int) -> str:
        return expand_template(
            self.event_source_template,
            {
                "types": types,
                "closeafter": str(close_after),
                "ping": str(ping),
            },
        )


def discover(
    session_url: str,
    credential: Credential,
    transport: Transport = urlopen_transport,
) -> Session:
    """Fetch the session resource and validate what it claims: the mail
    capability, the account to use, and URLs that stay on the session's own
    origin, since they are all fetched with the same credentials."""
    try:
        session_origin = _origin(session_url)
    except ValueError:
        raise JmapError(
            0, CODE_BAD_SESSION, "the JMAP session URL is not a URL"
        ) from None
    if session_origin[0] != "https":
        raise JmapError(0, CODE_BAD_SESSION, "the JMAP session URL must use https")
    # S310: https-only, refused unless https above.
    request = urllib.request.Request(  # noqa: S310
        session_url,
        headers={
            "Authorization": _auth_header(credential),
            "Accept": "application/json",
        },
    )
    response = transport(request)
    if response.status >= 400:
        raise _error_from(response)
    try:
        payload = response.json()
    except ValueError:
        raise JmapError(
            0, CODE_BAD_SESSION, "the JMAP session answer was not JSON"
        ) from None
    if not isinstance(payload, dict):
        raise JmapError(0, CODE_BAD_SESSION, "the JMAP session answer was not JSON")
    capabilities = _capabilities(payload)
    max_calls, max_objects, max_set = _limits(payload)
    account_id = _mail_account(payload)
    return Session(
        account_id=account_id,
        api_url=_api_url(payload, session_origin),
        download_template=_checked_template(
            payload, "downloadUrl", session_origin, _DOWNLOAD_VARS
        ),
        upload_template=_checked_template(
            payload, "uploadUrl", session_origin, _UPLOAD_VARS
        ),
        event_source_template=_checked_template(
            payload, "eventSourceUrl", session_origin, _EVENT_SOURCE_VARS
        ),
        capabilities=capabilities,
        max_calls=max_calls,
        max_objects_in_get=max_objects,
        max_objects_in_set=max_set,
        is_read_only=_is_read_only(payload, account_id),
        submission_supported=SUBMISSION_URN in capabilities,
    )


@dataclass(frozen=True)
class JmapCall:
    """One method call inside a JMAP request, answered in order."""

    method: str
    arguments: dict
    call_id: str = "0"


@dataclass(frozen=True)
class JmapAnswer:
    method: str
    payload: dict
    call_id: str

    @property
    def is_error(self) -> bool:
        return self.method == "error"


def result_or_raise(answer: JmapAnswer) -> dict:
    """The call's result, or the server's typed refusal as a JmapError."""
    if answer.is_error:
        code = str(answer.payload.get("type") or "serverFail")
        detail = str(answer.payload.get("description") or code)
        raise JmapError(0, code, detail)
    return answer.payload


def needs_full_sync(error: JmapError) -> bool:
    """Whether a stored state token has expired: the caller must walk the
    mailbox over instead of asking what changed. Any other failure is raised
    as-is -- it says nothing about what the mailbox holds."""
    return error.code in EXPIRED_STATE_CODES


def is_state_mismatch(error: JmapError) -> bool:
    """Whether a /set was refused because its ifInState no longer matches:
    the caller must re-read the state and rebuild the change, not resend it."""
    return error.code == "stateMismatch"


@dataclass(frozen=True, slots=True)
class SetError:
    """One object a /set call refused: the id (or creation id) plus the
    server's typed reason. Its siblings in the same call may still have
    applied, so this is returned, never raised."""

    id: str
    code: str
    message: str


def parse_set_errors(payload: dict, key: str) -> dict[str, SetError]:
    """The per-object failures under a notCreated/notUpdated/notDestroyed key,
    each keeping the server's "type" as its code."""
    entries = payload.get(key, {})
    if not isinstance(entries, dict):
        raise JmapError(0, CODE_HTTP, f"the JMAP answer has no {key} map")
    errors = {}
    for failed_id, detail in entries.items():
        detail = detail if isinstance(detail, dict) else {}
        code = str(detail.get("type") or "serverFail")
        errors[str(failed_id)] = SetError(
            str(failed_id), code, str(detail.get("description") or code)
        )
    return errors


def batch_calls(calls: list[JmapCall], max_calls: int) -> list[list[JmapCall]]:
    """Split calls into requests of at most max_calls, honouring the server's
    maxCallsInRequest. Single-call requests always fit."""
    if max_calls < 1:
        raise JmapError(0, CODE_INVALID_ARGUMENTS, "maxCallsInRequest is not usable")
    return [
        calls[start : start + max_calls] for start in range(0, len(calls), max_calls)
    ]


@dataclass(frozen=True, slots=True)
class StateChange:
    """What an event-source event announced: per account, the type names
    (Email, Mailbox, ...) whose state moved. A hint to sync, never the sync."""

    changed: dict[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class PushConfig:
    """How watch_push holds the event source: what to listen for, when the
    server should hang up, and how hard to redial."""

    types: str = "Email,Mailbox,EmailSubmission"
    close_after: int = 300
    ping: int = 30
    max_reconnects: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    sleep: Callable[[float], None] = time.sleep


# Opens the event source and yields its text chunks until the server hangs up.
StreamFactory = Callable[
    [urllib.request.Request, "threading.Event | None"], Iterable[str]
]


class JmapSession:
    """Mail-only JMAP requests against one discovered Session.

    A default session only reads. Pass writable=True for one that may also
    send Email/set and EmailSubmission/set -- which is still refused when the
    server marked the account read-only.
    """

    def __init__(
        self,
        session: Session,
        credential: Credential,
        transport: Transport = urlopen_transport,
        sleep: Callable[[float], None] = time.sleep,
        writable: bool = False,
    ) -> None:
        self._session = session
        self._authorization = _auth_header(credential)
        self._transport = transport
        self._sleep = sleep
        self._writable = writable

    @property
    def account_id(self) -> str:
        return self._session.account_id

    @property
    def is_writable(self) -> bool:
        """Whether /set calls go out: asked for and not forbidden."""
        return self._writable and not self._session.is_read_only

    @property
    def submission_supported(self) -> bool:
        return self._session.submission_supported

    @property
    def max_calls(self) -> int:
        return self._session.max_calls

    @property
    def max_objects_in_get(self) -> int:
        return self._session.max_objects_in_get

    @property
    def max_objects_in_set(self) -> int:
        return self._session.max_objects_in_set

    def request(
        self,
        calls: list[JmapCall],
        cancelled: threading.Event | None = None,
    ) -> list[JmapAnswer]:
        """POST method calls, retried while the server says it is only busy.
        Answers are matched to calls by call id, so each call needs its own."""
        raise_if_cancelled(cancelled)
        _require_https(self._session.api_url)
        _check_calls(calls, self.is_writable)
        using = [CORE_URN, MAIL_URN]
        if any(call.method.startswith("EmailSubmission/") for call in calls):
            using.append(SUBMISSION_URN)
        body = json.dumps(
            {
                "using": using,
                "methodCalls": [
                    [call.method, call.arguments, call.call_id] for call in calls
                ],
            }
        ).encode()
        headers = {
            "Authorization": self._authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        retryable = (
            _WRITE_RETRYABLE_STATUSES
            if any(call.method in WRITE_METHODS for call in calls)
            else _RETRYABLE_STATUSES
        )
        for attempt in range(_MAX_ATTEMPTS):
            raise_if_cancelled(cancelled)
            # S310: https-only, required above before anything goes out.
            request = urllib.request.Request(  # noqa: S310
                self._session.api_url, data=body, headers=headers, method="POST"
            )
            response = self._transport(request)
            if response.status not in retryable or attempt == _MAX_ATTEMPTS - 1:
                break
            delay = _retry_delay(response.headers, attempt)
            logger.debug(
                "JMAP %s answered %d; retrying in %.0fs",
                ", ".join(call.method for call in calls),
                response.status,
                delay,
            )
            self._sleep(delay)

        if response.status >= 400:
            raise _error_from(response)
        try:
            payload = response.json()
        except ValueError:
            raise JmapError(0, CODE_HTTP, "the JMAP answer was not JSON") from None
        if not isinstance(payload, dict):
            raise JmapError(0, CODE_HTTP, "the JMAP answer was not JSON")
        answers = payload.get("methodResponses", [])
        if len(answers) != len(calls) or any(
            not isinstance(answer, list) or len(answer) != 3 for answer in answers
        ):
            raise JmapError(0, CODE_HTTP, "the JMAP answer was malformed")
        parsed = [
            JmapAnswer(
                str(method),
                payload if isinstance(payload, dict) else {},
                str(call_id),
            )
            for method, payload, call_id in answers
        ]
        return _ordered_by_call_id(parsed, calls)

    def download_blob(
        self,
        blob_id: str,
        name: str,
        cancelled: threading.Event | None = None,
    ) -> bytes:
        """The bytes of one blob: a body part or attachment fetched by id."""
        raise_if_cancelled(cancelled)
        url = self._session.download_url(blob_id, name)
        _require_https(url)
        _assert_same_origin(url, self._session.api_url)
        # S310: https-only, required above before anything goes out.
        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"Authorization": self._authorization, "Accept": "*/*"},
        )
        response = self._transport(request)
        if response.status >= 400:
            raise _error_from(response)
        return response.body

    def upload_blob(
        self,
        data: bytes,
        content_type: str = "application/octet-stream",
        cancelled: threading.Event | None = None,
    ) -> str:
        """Upload raw bytes (a composed message) and return their blob id."""
        raise_if_cancelled(cancelled)
        if not self.is_writable:
            raise JmapError(0, CODE_READ_ONLY, "upload needs a writable session")
        url = self._session.upload_url()
        _require_https(url)
        _assert_same_origin(url, self._session.api_url)
        # S310: https-only, required above before anything goes out.
        request = urllib.request.Request(  # noqa: S310
            url,
            data=data,
            headers={
                "Authorization": self._authorization,
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )
        response = self._transport(request)
        if response.status >= 400:
            raise _error_from(response)
        try:
            payload = response.json()
        except ValueError:
            raise JmapError(
                0, CODE_HTTP, "the JMAP upload answer was not JSON"
            ) from None
        blob_id = payload.get("blobId") if isinstance(payload, dict) else None
        if not isinstance(blob_id, str) or not blob_id:
            raise JmapError(0, CODE_HTTP, "the JMAP upload answered without a blob id")
        return blob_id

    def watch_push(
        self,
        stream: StreamFactory,
        on_change: Callable[[StateChange], None],
        cancelled: threading.Event | None = None,
        config: PushConfig | None = None,
    ) -> None:
        """Hold the server's event source open, calling on_change per state
        change, until cancelled. Dropped connections reconnect with backoff;
        when the budget runs out pushUnavailable is raised and the caller
        falls back to polling."""
        active = config or PushConfig()
        url = self._session.event_source_url(
            active.types, active.close_after, active.ping
        )
        _require_https(url)
        _assert_same_origin(url, self._session.api_url)
        # S310: https-only, required above before anything goes out.
        request = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "Authorization": self._authorization,
                "Accept": "text/event-stream",
            },
        )
        failures = 0
        while True:
            raise_if_cancelled(cancelled)
            try:
                for chunk in stream(request, cancelled):
                    raise_if_cancelled(cancelled)
                    for change in parse_state_changes(chunk):
                        on_change(change)
            except JmapError:
                raise
            except Exception as error:
                failures += 1
                if failures > active.max_reconnects:
                    raise JmapError(
                        0,
                        CODE_PUSH_FAILED,
                        "push reconnects ran out; poll instead",
                    ) from error
                delay = reconnect_delay(failures, active.base_delay, active.max_delay)
                logger.debug("JMAP push dropped; reconnecting in %.0fs", delay)
                active.sleep(delay)
                continue
            # The server closed the stream (closeafter): resume with a fresh
            # connection and a reset failure budget, pausing first so a server
            # that closes every stream at once cannot spin the loop hot.
            failures = 0
            active.sleep(active.base_delay)


def _check_calls(calls: list[JmapCall], writable: bool) -> None:
    """Refuse before the network what must never go out: non-mail methods,
    writes on a read-only session, and reused call ids."""
    seen_ids: set[str] = set()
    for call in calls:
        if call.method not in MAIL_METHODS:
            raise JmapError(0, CODE_READ_ONLY, f"{call.method} is not a JMAP mail call")
        if call.method in WRITE_METHODS and not writable:
            raise JmapError(
                0, CODE_READ_ONLY, f"{call.method} needs a writable session"
            )
        if call.call_id in seen_ids:
            raise JmapError(
                0, CODE_INVALID_ARGUMENTS, f"call id {call.call_id} is used twice"
            )
        seen_ids.add(call.call_id)


def _ordered_by_call_id(
    answers: list[JmapAnswer], calls: list[JmapCall]
) -> list[JmapAnswer]:
    """The answers in call order, matched by call id rather than position: a
    response the server reordered still lands on its own call."""
    by_id = {answer.call_id: answer for answer in answers}
    if len(by_id) != len(answers):
        raise JmapError(0, CODE_HTTP, "the JMAP answer repeated a call id")
    try:
        return [by_id[call.call_id] for call in calls]
    except KeyError:
        raise JmapError(0, CODE_HTTP, "the JMAP answer missed a call id") from None


def _auth_header(credential: Credential) -> str:
    if credential.mechanism == MECHANISM_LOGIN:
        raw = f"{credential.user}:{credential.secret}".encode()
        return f"Basic {base64.b64encode(raw).decode()}"
    if credential.mechanism == MECHANISM_XOAUTH2:
        return f"Bearer {credential.secret}"
    raise JmapError(
        0,
        CODE_UNSUPPORTED_CREDENTIAL,
        f"{credential.mechanism} cannot sign in",
    )


def _error_from(response: Response) -> JmapError:
    return JmapError(
        response.status, CODE_HTTP, f"the JMAP request failed: HTTP {response.status}"
    )


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    """Seconds to wait before retrying: Retry-After when given, else backoff."""
    lowered = {name.lower(): value for name, value in headers.items()}
    try:
        seconds = float(lowered.get("retry-after", ""))
    except ValueError:
        seconds = 2.0**attempt
    return max(0.0, min(seconds, _MAX_RETRY_SECONDS))


def _origin(url: str) -> tuple[str, str, int]:
    """The origin credentials may be sent to: scheme, host and port."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


def _capabilities(payload: dict) -> frozenset[str]:
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict) or MAIL_URN not in capabilities:
        raise JmapError(0, CODE_BAD_SESSION, "the server does not speak JMAP mail")
    return frozenset(capabilities)


def _mail_account(payload: dict) -> str:
    """The account to read: the mail primary, or the only mail-capable one."""
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise JmapError(0, CODE_BAD_SESSION, "the session resource lists no accounts")
    primary = payload.get("primaryAccounts")
    primary = primary if isinstance(primary, dict) else {}
    mail_primary = primary.get(MAIL_URN)
    if isinstance(mail_primary, str) and mail_primary in accounts:
        return mail_primary
    capable = [
        account_id
        for account_id, detail in accounts.items()
        if isinstance(detail, dict)
        and MAIL_URN in (detail.get("accountCapabilities") or {})
    ]
    if len(capable) == 1:
        return capable[0]
    raise JmapError(0, CODE_BAD_SESSION, "the session resource names no mail account")


def _limits(payload: dict) -> tuple[int, int, int]:
    """The core capability's paging bounds, defaulted when the server omits
    them."""
    capabilities = payload.get("capabilities")
    core = capabilities.get(CORE_URN) if isinstance(capabilities, dict) else None
    core = core if isinstance(core, dict) else {}

    def bound(name: str, default: int) -> int:
        value = core.get(name)
        return value if isinstance(value, int) and value > 0 else default

    return (
        bound("maxCallsInRequest", _DEFAULT_MAX_CALLS),
        bound("maxObjectsInGet", _DEFAULT_MAX_OBJECTS_IN_GET),
        bound("maxObjectsInSet", _DEFAULT_MAX_OBJECTS_IN_SET),
    )


def _is_read_only(payload: dict, account_id: str) -> bool:
    """Whether the server marked the chosen account read-only: writes to it
    are refused before they reach the network."""
    accounts = payload.get("accounts")
    detail = accounts.get(account_id) if isinstance(accounts, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    return detail.get("isReadOnly") is True


def _api_url(payload: dict, session_origin: tuple[str, str, int]) -> str:
    url = _checked_url(payload, "apiUrl", session_origin)
    if "{" in url:
        raise JmapError(0, CODE_BAD_SESSION, "apiUrl is not a plain URL")
    return url


def _checked_template(
    payload: dict,
    key: str,
    session_origin: tuple[str, str, int],
    required: frozenset[str],
) -> str:
    template = _checked_url(payload, key, session_origin)
    provided = set(_TEMPLATE_VAR.findall(template))
    if missing := required - provided:
        name = sorted(missing)[0]
        raise JmapError(0, CODE_BAD_SESSION, f"{key} provides no {{{name}}}")
    return template


def _checked_url(payload: dict, key: str, session_origin: tuple[str, str, int]) -> str:
    """One session URL, staying on the session's own https origin: anything
    else would have the credentials sent to a host the user never approved."""
    url = payload.get(key)
    if not isinstance(url, str) or not url:
        raise JmapError(0, CODE_BAD_SESSION, f"the session resource has no {key}")
    try:
        origin = _origin(url)
    except ValueError:
        raise JmapError(0, CODE_BAD_SESSION, f"{key} is not a URL") from None
    if origin[0] != "https":
        raise JmapError(0, CODE_BAD_SESSION, f"{key} must use https")
    if origin != session_origin:
        raise JmapError(
            0, CODE_BAD_SESSION, f"{key} is outside the session at {origin[1]}"
        )
    return url


def _require_https(url: str) -> None:
    """Refuse a URL that would carry the credentials in the clear.

    Discovery pins every URL to the session's https origin, so this only
    trips on a tampered Session -- the same threat _assert_same_origin
    guards against.
    """
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise JmapError(0, CODE_BAD_SESSION, "the JMAP URL must use https")


def _assert_same_origin(url: str, trusted: str) -> None:
    """Refuse a blob or push URL that left the api origin: discovery already
    checked the templates, so this only trips on a tampered Session."""
    if _origin(url) != _origin(trusted):
        raise JmapError(
            0, CODE_BAD_SESSION, f"the URL is outside the session at {_origin(url)[1]}"
        )


def urlopen_event_stream(
    request: urllib.request.Request, cancelled: threading.Event | None = None
) -> Iterator[str]:
    """The event source for real, one line at a time. A blocking read cannot
    notice cancellation until the next line (or ping) arrives, so this runs on
    a thread the caller owns."""
    # S310: https-only (required by the caller), and the context verifies
    # rather than trusting the ambient default.
    with urllib.request.urlopen(  # noqa: S310
        request, timeout=NET_TIMEOUT_SECONDS, context=https_context()
    ) as reply:
        for raw in reply:
            raise_if_cancelled(cancelled)
            yield raw.decode("utf-8", "replace")


def parse_state_changes(text: str) -> list[StateChange]:
    """The state events in one event-source chunk. Pings, comments and other
    event types are ignored; a malformed state event is logged and skipped,
    since the next sync reconciles whatever it announced."""
    changes = []
    for block in text.split("\n\n"):
        event = "message"
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith(":") or not line.strip():
                continue
            name, _, value = line.partition(":")
            if name.strip() == "event":
                event = value.strip()
            elif name.strip() == "data":
                data.append(value.lstrip(" "))
        if event != "state" or not data:
            continue
        try:
            payload = json.loads("\n".join(data))
            changed = payload["changed"]
        except (ValueError, KeyError, TypeError):
            logger.warning("could not parse a JMAP push event")
            continue
        if not isinstance(changed, dict):
            logger.warning("could not parse a JMAP push event")
            continue
        changes.append(
            StateChange(
                {
                    str(account): frozenset(
                        name for name in detail if isinstance(name, str)
                    )
                    for account, detail in changed.items()
                    if isinstance(detail, dict)
                }
            )
        )
    return changes


def reconnect_delay(attempt: int, base: float, cap: float) -> float:
    """Backoff before redialling push: exponential from base, capped."""
    return max(0.0, min(base * 2.0 ** max(0, attempt - 1), cap))
