"""Fastmail sign-in over OAuth2, without an app password.

A Fastmail account typed in by hand signs in with an app password the user
mints in Settings. This module is the protocol half of signing in inside
Postcard instead: a public-client OAuth2 authorization-code flow with PKCE,
as RFC 8252 prescribes for native apps.

What lives here: the authorize URL, PKCE helpers, the code exchange and token
refresh, all over the stdlib. The loopback listener that catches the code is
reused from ms_loopback. What does not, yet: the GTK half -- opening the
system browser and the onboarding UI around those two -- and anything that
syncs mail with the token. Those are tracked with the app registration below.

The registration is the one gate that is not code. Fastmail issues tokens to
a registered OAuth client, identified by a client id that is public (a
shipped app cannot keep a secret, which is why there is no client secret
here -- PKCE replaces it). Native clients may register dynamically or by mail
(see the dev docs), but until one id is chosen, FASTMAIL_CLIENT_ID stays
empty and every function here refuses with ``invalid_request``.

Refresh tokens are stored with secrets.store_password, encoded by
encode_stored; access tokens are minted per operation and never persisted.
Fastmail rotates the refresh token on every refresh, and reusing an old one
revokes the whole grant -- so a caller that refreshes more than once must
persist the new token each time, never keep the old one.

Endpoints and scopes below are Fastmail's documented ones:
https://www.fastmail.com/dev/ -- "OAuth at Fastmail".
"""

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import NamedTuple

from .auth import MECHANISM_XOAUTH2, Credential
from .jmap_session import Response, Transport, urlopen_transport

FASTMAIL_AUTHORIZE_URL = "https://api.fastmail.com/oauth/authorize"

# The one token endpoint serves both the code exchange and the refresh; the
# grant_type tells them apart.
FASTMAIL_TOKEN_URL = "https://api.fastmail.com/oauth/refresh"

# Where a Bearer [REDACTED] lands: the JMAP session resource. Sent as the RFC 8707
# resource indicator on the authorize URL -- without it the endpoint answers
# invalid_target before any consent screen -- and used to discover the API.
FASTMAIL_JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"

# core is mandatory with any other JMAP scope; mail reads and manages mail,
# submission sends it. Vacation, contacts and masked email stay out: a mail
# client requests the minimal set that lets it function.
FASTMAIL_SCOPES = (
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:mail",
    "urn:ietf:params:jmap:submission",
)

# Fill in once an OAuth client is registered for Postcard; see above.
FASTMAIL_CLIENT_ID = ""


class FastmailOAuthError(Exception):
    def __init__(self, code: str) -> None:
        # Error codes come from a fixed vocabulary, never server text: the
        # token endpoint's error_description names users and lockouts.
        self.code = code
        super().__init__("The Fastmail sign-in could not be completed.")


ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_grant",
        "invalid_response",
        "server_error",
    }
)


class Tokens(NamedTuple):
    access_token: str
    # None when the server sent no new one: the caller keeps the old refresh
    # token. Fastmail always rotates, so this is a shape allowance, not a
    # case the server exercises -- see the module docstring.
    refresh_token: str | None
    expires_in: int


def code_verifier() -> str:
    """A random PKCE verifier: 86 characters, inside RFC 7636's 43-128."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    """The S256 challenge for a verifier, base64url without padding."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(
    *,
    client_id: str = FASTMAIL_CLIENT_ID,
    redirect_uri: str,
    scopes: tuple[str, ...] = FASTMAIL_SCOPES,
    state: str,
    code_challenge: str,
) -> str:
    """The URL the system browser opens to start a native sign-in."""
    if not client_id:
        raise FastmailOAuthError("invalid_request")
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # The RFC 8707 resource indicator: without it the endpoint answers
            # invalid_target before any consent screen.
            "resource": FASTMAIL_JMAP_SESSION_URL,
        }
    )
    return f"{FASTMAIL_AUTHORIZE_URL}?{query}"


def exchange_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str,
    client_id: str = FASTMAIL_CLIENT_ID,
    transport: Transport = urlopen_transport,
) -> Tokens:
    """Trade the code the loopback redirect caught for the first tokens."""
    return _token_request(
        transport,
        client_id,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = FASTMAIL_CLIENT_ID,
    transport: Transport = urlopen_transport,
) -> Tokens:
    """Mint a fresh access token. Called on the worker thread, like every
    credential lookup -- it is one HTTPS round trip.

    The answer carries a new refresh token that replaces the one sent: the
    caller must persist it, since reusing the old one revokes the grant.
    """
    return _token_request(
        transport,
        client_id,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


def _token_request(
    transport: Transport, client_id: str, fields: dict[str, str]
) -> Tokens:
    if not client_id:
        raise FastmailOAuthError("invalid_request")
    body = urllib.parse.urlencode({"client_id": client_id} | fields).encode()
    request = urllib.request.Request(
        FASTMAIL_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _tokens_from(transport(request))


def _tokens_from(response: Response) -> Tokens:
    try:
        body = response.json()
    except ValueError:
        raise FastmailOAuthError("invalid_response") from None
    if not isinstance(body, dict):
        raise FastmailOAuthError("invalid_response")
    if response.status >= 400:
        code = body.get("error", "")
        # A temporary invalid_grant (a locked user, unlocked later) still
        # reads as invalid_grant: reconnecting is harmless either way, and
        # the fixed vocabulary has no "try again much later".
        raise FastmailOAuthError(
            "invalid_grant" if code == "invalid_grant" else "server_error"
        )
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise FastmailOAuthError("invalid_response")
    refresh = body.get("refresh_token")
    try:
        lifetime = int(body.get("expires_in", 0))
    except (TypeError, ValueError):
        lifetime = 0
    return Tokens(access, refresh if isinstance(refresh, str) else None, lifetime)


def encode_stored(refresh_token: str) -> str:
    """What secrets.store_password holds for a native Fastmail account."""
    return json.dumps({"refresh_token": refresh_token})


def native_credential(
    email: str,
    stored: str,
    *,
    client_id: str = FASTMAIL_CLIENT_ID,
    transport: Transport = urlopen_transport,
) -> Credential:
    """An xoauth2 Credential from a stored refresh token, refreshing it.

    Raises FastmailOAuthError when the stored value is not ours or the refresh
    is refused; the caller turns that into "could not sign in", never a retry
    of the password the user no longer has.

    One-shot only: the refresh rotates the stored token and this return
    cannot carry the new one. A caller that signs in repeatedly must call
    refresh_access_token itself and persist each rotation.
    """
    try:
        refresh = json.loads(stored).get("refresh_token")
    except (ValueError, AttributeError):
        raise FastmailOAuthError("invalid_request") from None
    if not isinstance(refresh, str) or not refresh:
        raise FastmailOAuthError("invalid_request")
    tokens = refresh_access_token(refresh, client_id=client_id, transport=transport)
    return Credential(email, tokens.access_token, MECHANISM_XOAUTH2)
