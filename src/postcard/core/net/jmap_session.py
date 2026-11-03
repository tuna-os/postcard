"""Session discovery and read-only requests for JMAP (RFC 8620 + RFC 8621).

Part 1 only fetches: Mailbox/get, Email/query, Email/queryChanges and
Email/get. Anything else is refused before it reaches the network, so a
read-only account is enough and no call can change the mailbox. Like the
Graph session it only speaks the protocol: no database, no GTK, and it runs
on a worker thread.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from . import NET_TIMEOUT_SECONDS
from .auth import MECHANISM_LOGIN, MECHANISM_XOAUTH2, Credential

logger = logging.getLogger(__name__)

CORE_URN = "urn:ietf:params:jmap:core"
MAIL_URN = "urn:ietf:params:jmap:mail"

# The only calls part 1 ever sends. Everything else (Email/set, Email/import,
# submission, calendars, contacts, ...) is refused in JmapSession.request.
READ_METHODS = frozenset(
    {"Mailbox/get", "Email/query", "Email/queryChanges", "Email/get"}
)

# Client-side error codes. A method the server itself refuses instead carries
# the JMAP "type" it answered with, which is RFC 8620's own fixed vocabulary
# (accountNotFound, unknownMethod, invalidArguments, cannotCalculateChanges).
CODE_HTTP = "httpError"
CODE_BAD_SESSION = "badSession"
CODE_READ_ONLY = "readOnly"
CODE_CANCELLED = "cancelled"
CODE_INVALID_ARGUMENTS = "invalidArguments"
CODE_UNSUPPORTED_CREDENTIAL = "unsupportedCredential"

# Every part 1 call is a read, so a refused request is always safe to send
# again -- unlike Graph's POST /me/sendMail, which may already have acted.
_RETRYABLE_STATUSES = frozenset({429, 503, 504})
_MAX_ATTEMPTS = 4
_MAX_RETRY_SECONDS = 30

# Used when the session resource omits the core capability's limits.
_DEFAULT_MAX_CALLS = 16
_DEFAULT_MAX_OBJECTS_IN_GET = 500

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
        with urllib.request.urlopen(request, timeout=NET_TIMEOUT_SECONDS) as reply:
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
    request = urllib.request.Request(
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
    max_calls, max_objects = _limits(payload)
    return Session(
        account_id=_mail_account(payload),
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


class JmapSession:
    """Read-only JMAP requests against one discovered Session."""

    def __init__(
        self,
        session: Session,
        credential: Credential,
        transport: Transport = urlopen_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._authorization = _auth_header(credential)
        self._transport = transport
        self._sleep = sleep

    @property
    def account_id(self) -> str:
        return self._session.account_id

    @property
    def max_objects_in_get(self) -> int:
        return self._session.max_objects_in_get

    def request(
        self,
        calls: list[JmapCall],
        cancelled: threading.Event | None = None,
    ) -> list[JmapAnswer]:
        """POST method calls, retried while the server says it is only busy."""
        raise_if_cancelled(cancelled)
        for call in calls:
            if call.method not in READ_METHODS:
                raise JmapError(0, CODE_READ_ONLY, f"{call.method} is not a read call")
        body = json.dumps(
            {
                "using": [CORE_URN, MAIL_URN],
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
        for attempt in range(_MAX_ATTEMPTS):
            raise_if_cancelled(cancelled)
            request = urllib.request.Request(
                self._session.api_url, data=body, headers=headers, method="POST"
            )
            response = self._transport(request)
            if (
                response.status not in _RETRYABLE_STATUSES
                or attempt == _MAX_ATTEMPTS - 1
            ):
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
            answers = response.json().get("methodResponses", [])
        except ValueError:
            raise JmapError(0, CODE_HTTP, "the JMAP answer was not JSON") from None
        if len(answers) != len(calls) or any(
            not isinstance(answer, list) or len(answer) != 3 for answer in answers
        ):
            raise JmapError(0, CODE_HTTP, "the JMAP answer was malformed")
        return [
            JmapAnswer(
                str(method),
                payload if isinstance(payload, dict) else {},
                str(call_id),
            )
            for method, payload, call_id in answers
        ]


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


def _limits(payload: dict) -> tuple[int, int]:
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
    )


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
