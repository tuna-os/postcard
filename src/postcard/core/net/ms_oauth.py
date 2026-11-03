"""Microsoft 365 sign-in without GNOME Online Accounts.

Today a Graph account's token comes from the desktop's Online Accounts panel,
which some organizations block and some desktops lack. This module is the
protocol half of signing in inside Postcard instead: a public-client OAuth2
authorization-code flow with PKCE, as RFC 8252 prescribes for native apps.

What lives here: the authorize URL, PKCE helpers, the code exchange and token
refresh, all over the stdlib. The loopback listener that catches the code
lives in ms_loopback. What does not, yet: the GTK half -- opening the system
browser and the onboarding UI around those two. That is tracked with the app
registration below.

The registration is the one gate that is not code. Microsoft issues tokens to
a registered Entra application, identified by a client id that is public (a
shipped app cannot keep a secret, which is why there is no client secret
here -- PKCE replaces it). Until one is registered, MS_CLIENT_ID stays empty
and every function here refuses with ``invalid_request``.

Refresh tokens are stored with secrets.store_password, encoded by
encode_stored; access tokens are minted per operation and never persisted.
"""

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import NamedTuple

from .auth import MECHANISM_XOAUTH2, Credential
from .graph_session import Response, Transport, urlopen_transport

# Personal Outlook.com accounts as well as work tenants.
MS_AUTHORITY = "https://login.microsoftonline.com/common"

# offline_access is what buys a refresh token; without it the sign-in lasts an
# hour and there is nothing to renew it with.
MS_SCOPES = ("Mail.ReadWrite", "Mail.Send", "offline_access")

# Fill in once an Entra application is registered for Postcard; see above.
MS_CLIENT_ID = ""


class MsOAuthError(Exception):
    def __init__(self, code: str) -> None:
        # Error codes come from a fixed vocabulary, never server text: the
        # token endpoint's error_description names tenants and policies.
        self.code = code
        super().__init__("The Microsoft sign-in could not be completed.")


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
    # token. Microsoft rotates it on most refreshes, but the shape allows it.
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
    client_id: str = MS_CLIENT_ID,
    redirect_uri: str,
    scopes: tuple[str, ...] = MS_SCOPES,
    state: str,
    code_challenge: str,
) -> str:
    """The URL the system browser opens to start a native sign-in."""
    if not client_id:
        raise MsOAuthError("invalid_request")
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{MS_AUTHORITY}/oauth2/v2.0/authorize?{query}"


def exchange_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str,
    client_id: str = MS_CLIENT_ID,
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
    scopes: tuple[str, ...] = MS_SCOPES,
    client_id: str = MS_CLIENT_ID,
    transport: Transport = urlopen_transport,
) -> Tokens:
    """Mint a fresh access token. Called on the worker thread, like every
    credential lookup -- it is one HTTPS round trip."""
    return _token_request(
        transport,
        client_id,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(scopes),
        },
    )


def _token_request(
    transport: Transport, client_id: str, fields: dict[str, str]
) -> Tokens:
    if not client_id:
        raise MsOAuthError("invalid_request")
    body = urllib.parse.urlencode({"client_id": client_id} | fields).encode()
    request = urllib.request.Request(
        f"{MS_AUTHORITY}/oauth2/v2.0/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _tokens_from(transport(request))


def _tokens_from(response: Response) -> Tokens:
    try:
        body = response.json()
    except ValueError:
        raise MsOAuthError("invalid_response") from None
    if not isinstance(body, dict):
        raise MsOAuthError("invalid_response")
    if response.status >= 400:
        code = body.get("error", "")
        raise MsOAuthError(
            "invalid_grant" if code == "invalid_grant" else "server_error"
        )
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise MsOAuthError("invalid_response")
    refresh = body.get("refresh_token")
    try:
        lifetime = int(body.get("expires_in", 0))
    except (TypeError, ValueError):
        lifetime = 0
    return Tokens(access, refresh if isinstance(refresh, str) else None, lifetime)


def encode_stored(refresh_token: str) -> str:
    """What secrets.store_password holds for a native Microsoft account."""
    return json.dumps({"refresh_token": refresh_token})


def native_credential(
    email: str,
    stored: str,
    *,
    client_id: str = MS_CLIENT_ID,
    transport: Transport = urlopen_transport,
) -> Credential:
    """An xoauth2 Credential from a stored refresh token, refreshing it.

    Raises MsOAuthError when the stored value is not ours or the refresh is
    refused; the caller turns that into "could not sign in", never a retry of
    the password the user no longer has.
    """
    try:
        refresh = json.loads(stored).get("refresh_token")
    except (ValueError, AttributeError):
        raise MsOAuthError("invalid_request") from None
    if not isinstance(refresh, str) or not refresh:
        raise MsOAuthError("invalid_request")
    tokens = refresh_access_token(refresh, client_id=client_id, transport=transport)
    return Credential(email, tokens.access_token, MECHANISM_XOAUTH2)
