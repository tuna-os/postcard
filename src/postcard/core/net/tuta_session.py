"""Private stdio client for the experimental SDK-backed Tuta helper.

The bundled helper handles HTTPS and cryptography. No credentials are passed
on the command line, and helper diagnostics never reach logs or the UI.
"""

import json
import shutil
import subprocess
from collections.abc import Callable

from .auth import Credential

MECHANISM_TUTA = "tuta-session"
HELPER_NAME = "postcard-tuta"
PROTOCOL_VERSION = 1
HELPER_TIMEOUT_SECONDS = 100
# A TOTP code is six ASCII digits; anything else is rejected without waking
# the helper, which applies the same check before spending a login attempt.
TOTP_LENGTH = 6
# A helper reply is JSON whose bulk is base64 bodies and attachments -- ASCII,
# so one character is one byte where the size actually matters. Checked before
# json.loads, so an unbounded reply never pays for the parse and the copies
# after it.
MAX_IPC_RESPONSE_BYTES = 64 * 1024 * 1024


class TutaError(Exception):
    def __init__(self, code: str) -> None:
        # Error codes come from a fixed vocabulary, never server text.
        self.code = code
        super().__init__("The experimental Tuta operation could not be completed.")


Transport = Callable[[dict], dict]


def helper_transport(request: dict) -> dict:
    executable = shutil.which(HELPER_NAME)
    if executable is None:
        raise TutaError("helper_missing")
    try:
        response = subprocess.run(
            [executable],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=HELPER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TutaError("helper_failed") from None
    if response.returncode != 0:
        raise TutaError("helper_failed")
    if len(response.stdout) > MAX_IPC_RESPONSE_BYTES:
        raise TutaError("response_too_large")
    try:
        reply = json.loads(response.stdout)
    except ValueError:
        raise TutaError("invalid_response") from None
    if not isinstance(reply, dict):
        raise TutaError("invalid_response")
    return reply


def _request(transport: Transport, operation: str, **parameters: object) -> dict:
    reply = transport({"version": PROTOCOL_VERSION, "op": operation, **parameters})
    if reply.get("version") != PROTOCOL_VERSION:
        raise TutaError("unsupported_version")
    if "error" in reply:
        detail = reply["error"]
        code = (
            detail.get("code", "request_failed")
            if isinstance(detail, dict)
            else "request_failed"
        )
        raise TutaError(code if code in ERROR_CODES else "request_failed")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise TutaError("invalid_response")
    return result


ERROR_CODES = frozenset(
    {
        "helper_missing",
        "helper_failed",
        "response_too_large",
        "invalid_response",
        "unsupported_version",
        "invalid_request",
        "invalid_id",
        "invalid_page",
        "invalid_move_batch",
        "login_failed",
        "resume_failed",
        "session_revoked",
        "session_invalid",
        "client_outdated",
        "revoke_failed",
        "second_factor_required",
        "invalid_totp",
        "unsupported_second_factor",
        "unsupported_u2f",
        "unsupported_webauthn",
        "second_factor_failed",
        "request_failed",
        "folder_not_found",
        "incomplete_response",
        "unsupported_legacy_body",
        "decryption_failed",
        "attachment_failed",
        "transport_failed",
        "timeout",
        "unsupported_operation",
    }
)


def is_valid_totp(code: str) -> bool:
    """Whether a code is worth a helper round trip: six ASCII digits.

    `str.isdigit` alone is not enough -- it accepts non-ASCII numerals the
    helper would reject after spending a login attempt.
    """
    return len(code) == TOTP_LENGTH and code.isascii() and code.isdigit()


def login(
    email: str,
    password: str,
    totp: str | None = None,
    *,
    transport: Transport = helper_transport,
) -> Credential:
    """Return a resumable session for libsecret; do not persist the password.

    The helper runs the KDF and unlocks the keys, which can take a while --
    call from a worker thread. When the account has TOTP enabled and no code
    was given, this raises `second_factor_required` and the caller retries
    with the code; an `invalid_totp` may just have expired, so that retry is
    the caller's to offer. U2F/WebAuthn-only accounts report which factor
    blocks them instead of a generic refusal.
    """
    result = _request(transport, "login", email=email, password=password, totp=totp)
    session = result.get("session")
    if (
        not isinstance(session, dict)
        or session.get("login", "").casefold() != email.casefold()
    ):
        raise TutaError("invalid_response")
    return Credential(email, json.dumps(session), MECHANISM_TUTA)


class TutaSession:
    """One helper operation on a resumed stored session.

    Every operation re-resumes, so after a restart a revoked session
    (`session_revoked`) reads differently from an outage (`transport_failed`)
    and a refused client version (`client_outdated`) with no dedicated
    check. Closing the session server-side is `request("revoke")`, which
    treats an already-gone session as closed.
    """

    def __init__(
        self, credential: Credential, transport: Transport = helper_transport
    ) -> None:
        if credential.mechanism != MECHANISM_TUTA:
            raise TutaError("unsupported_operation")
        try:
            session = json.loads(credential.secret)
        except ValueError:
            raise TutaError("invalid_request") from None
        if (
            not isinstance(session, dict)
            or session.get("login", "").casefold() != credential.user.casefold()
        ):
            raise TutaError("invalid_request")
        self._session = session
        self._transport = transport

    def request(self, operation: str, **parameters: object) -> dict:
        return _request(self._transport, operation, session=self._session, **parameters)
