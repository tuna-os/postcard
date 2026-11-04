"""Stdio client for the future native Proton helper, contract-first.

No helper binary exists yet -- nothing in the app calls this module, and
mail_sync keeps failing Proton accounts closed until one lands. What this
module pins down now is the exact contract that helper must speak, mirroring
the Tuta adapter's proven shape so the two backends stay symmetric:

One UTF-8 JSON object on stdin, one on stdout, stderr discarded. Requests
carry ``version: 1`` and an ``op``; replies are ``{"version": 1,
"result": {...}}`` or ``{"version": 1, "error": {"code": "..."}}`` with a
code from the fixed vocabulary below, never server text. Passwords, second
factors and session material travel on stdin only, never argv.

The v1 operations a helper implements are ``login`` (SRP + mailbox password
+ optional TOTP, returns a resumable ``session`` echoing ``login``), ``sync``
(folder tree, headers, full ID snapshot on offset zero), ``message``
(decrypted body, headers, recipients, attachments), ``mark_read`` and
``move``. SRP, message crypto and key unlocking all happen inside the helper
(go-proton-api / gopenpgp / go-srp); this side only maps validated replies.

Tracked in `#7 <https://github.com/tuna-os/postcard/issues/7>`_.
"""

import json
import shutil
import subprocess
from collections.abc import Callable

from .auth import Credential
from .auth import is_valid_totp as is_valid_totp

MECHANISM_PROTON = "proton-session"
HELPER_NAME = "postcard-proton"
PROTOCOL_VERSION = 1
HELPER_TIMEOUT_SECONDS = 100


class ProtonError(Exception):
    def __init__(self, code: str) -> None:
        # Error codes come from a fixed vocabulary, never server text.
        self.code = code
        super().__init__("The Proton Mail operation could not be completed.")


ERROR_CODES = frozenset(
    {
        "helper_missing",
        "helper_failed",
        "invalid_response",
        "unsupported_version",
        "invalid_request",
        "login_failed",
        "second_factor_required",
        "invalid_totp",
        "unsupported_second_factor",
        "second_factor_failed",
        "request_failed",
        "timeout",
        "unsupported_operation",
    }
)

Transport = Callable[[dict], dict]


def helper_available() -> bool:
    """Whether the native helper is installed: the picker's Proton gate."""
    return shutil.which(HELPER_NAME) is not None


def helper_transport(request: dict) -> dict:
    executable = shutil.which(HELPER_NAME)
    if executable is None:
        raise ProtonError("helper_missing")
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
        raise ProtonError("helper_failed") from None
    if response.returncode != 0:
        raise ProtonError("helper_failed")
    try:
        reply = json.loads(response.stdout)
    except ValueError:
        raise ProtonError("invalid_response") from None
    if not isinstance(reply, dict):
        raise ProtonError("invalid_response")
    return reply


def _request(transport: Transport, operation: str, **parameters: object) -> dict:
    reply = transport({"version": PROTOCOL_VERSION, "op": operation, **parameters})
    if reply.get("version") != PROTOCOL_VERSION:
        raise ProtonError("unsupported_version")
    if "error" in reply:
        detail = reply["error"]
        code = (
            detail.get("code", "request_failed")
            if isinstance(detail, dict)
            else "request_failed"
        )
        raise ProtonError(code if code in ERROR_CODES else "request_failed")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise ProtonError("invalid_response")
    return result


def login(
    email: str,
    password: str,
    totp: str | None = None,
    *,
    transport: Transport = helper_transport,
) -> Credential:
    """Return a resumable session for libsecret; do not persist the password.

    The helper proves the SRP login (plus a TOTP challenge when the account
    sets one) and returns the tokens later operations resume from; mailbox
    keys stay locked until sync first needs to decrypt. This side only
    checks the session belongs to the address it was requested for.
    """
    result = _request(transport, "login", email=email, password=password, totp=totp)
    session = result.get("session")
    if (
        not isinstance(session, dict)
        or session.get("login", "").casefold() != email.casefold()
    ):
        raise ProtonError("invalid_response")
    return Credential(email, json.dumps(session), MECHANISM_PROTON)


class ProtonSession:
    def __init__(
        self, credential: Credential, transport: Transport = helper_transport
    ) -> None:
        if credential.mechanism != MECHANISM_PROTON:
            raise ProtonError("unsupported_operation")
        try:
            session = json.loads(credential.secret)
        except ValueError:
            raise ProtonError("invalid_request") from None
        if (
            not isinstance(session, dict)
            or session.get("login", "").casefold() != credential.user.casefold()
        ):
            raise ProtonError("invalid_request")
        self._session = session
        self._transport = transport

    def request(self, operation: str, **parameters: object) -> dict:
        return _request(self._transport, operation, session=self._session, **parameters)
