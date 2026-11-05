"""Fastmail sign-in over OAuth2, without an app password.

A Fastmail account typed in by hand signs in with an app password the user
mints in Settings. This module is the protocol half of signing in inside
Postcard instead: a public-client OAuth2 authorization-code flow with PKCE,
as RFC 8252 prescribes for native apps, following the OAuth Profile for
Open Public Clients (draft-ietf-mailmaint-oauth-public).

What lives here: the authorization-server metadata check, the dynamic
client registration that replaces any manual step, the authorize URL, PKCE
helpers, the code exchange and token refresh, all over the stdlib. The
loopback listener that catches the code is reused from ms_loopback. What
does not: the GTK half -- opening the system browser and the onboarding UI
around those two -- and anything that syncs mail with the token.

There is deliberately no client secret here: a shipped app cannot keep
one, and Fastmail's metadata advertises ``token_endpoint_auth_methods``
``none``. PKCE replaces the secret, and a ``client_secret`` in a
registration answer is ignored rather than used.

Refresh tokens are stored with secrets.store_password, encoded by
encode_stored together with the client id they were issued to: the id
stays valid only while a refresh token lives under it, and on
``invalid_client`` the client re-registers rather than retrying. Access
tokens are minted per operation and never persisted. Fastmail rotates the
refresh token on every refresh, and reusing an old one revokes the whole
grant -- so a caller that refreshes more than once must persist the new
token each time, never keep the old one.

Endpoints below are Fastmail's documented ones, verified live against the
authorization-server metadata at https://api.fastmail.com/.well-known/
oauth-authorization-server: the metadata is fetched on every login and the
flow aborts when it drifts from these pins or stops conforming to the
profile. Scopes are the profile's registered ones, not the per-protocol
JMAP URNs: the registration endpoint answers ``invalid_client_metadata``
for ``urn:ietf:params:jmap:mail``, while ``urn:ietf:params:oauth:scope:
mail`` covers the JMAP core, mail and submission capabilities the client
needs (profile section 5.4.1) and ``offline_access`` buys the refresh
token.
"""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from . import NET_TIMEOUT_SECONDS, https_context
from .auth import MECHANISM_XOAUTH2, Credential
from .jmap_session import Response, Transport, urlopen_transport

FASTMAIL_ISSUER = "https://api.fastmail.com"

FASTMAIL_METADATA_URL = (
    "https://api.fastmail.com/.well-known/oauth-authorization-server"
)

FASTMAIL_AUTHORIZE_URL = "https://api.fastmail.com/oauth/authorize"

# The one token endpoint serves both the code exchange and the refresh; the
# grant_type tells them apart.
# A URL constant, not a credential: the name only trips S105's token match.
FASTMAIL_TOKEN_URL = "https://api.fastmail.com/oauth/refresh"  # noqa: S105

# Where a Bearer [REDACTED] lands: the JMAP session resource. Sent as the RFC 8707
# resource indicator on the authorize URL -- without it the endpoint answers
# invalid_target before any consent screen -- and used to discover the API.
FASTMAIL_JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"

# The profile's registered mail scope, not the per-protocol JMAP URNs (see
# the module docstring): one scope for reading, managing and sending mail,
# plus offline_access for the refresh token that keeps the sign-in alive.
FASTMAIL_SCOPES = (
    "urn:ietf:params:oauth:scope:mail",
    "offline_access",
)

# The redirect every login registers: loopback without a port, which the
# profile's port exception then lets the authorize request vary. The
# listener binds 127.0.0.1 on an ephemeral port, never this bare URI.
FASTMAIL_LOOPBACK_REDIRECT = "http://127.0.0.1/"

# Who the consent screen names. Logo, terms and policy URLs stay unsent:
# the registration endpoint issues the client id without them, and a link
# the project does not host would be worse than none.
FASTMAIL_CLIENT_NAME = "Postcard"
FASTMAIL_CLIENT_URI = "https://postcard.gxanshu.in/"
FASTMAIL_SOFTWARE_ID = "in.gxanshu.postcard"

# The picker row's gate flag, and nothing else. Dynamic registration
# replaced the manual step this once stood for, so no function here reads
# it anymore; provider_dialog keeps gating the row on it until the ungate
# follow-up, which also updates the gui tests that pin both states.
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


class AuthorizationServer(NamedTuple):
    issuer: str
    registration_endpoint: str
    authorization_endpoint: str
    token_endpoint: str
    scopes_supported: tuple[str, ...]
    protected_resources: tuple[str, ...]


class Registration(NamedTuple):
    client_id: str
    scope: str


class Tokens(NamedTuple):
    access_token: str
    # None when the server sent no new one: the caller keeps the old refresh
    # token. Fastmail always rotates, so this is a shape allowance, not a
    # case the server exercises -- see the module docstring.
    refresh_token: str | None
    expires_in: int
    # The account the authorization was granted for, when the token answer
    # named it. The profile requires it, but older deployments predate the
    # requirement, so a missing username falls back to the typed address.
    username: str | None = None
    # The granted scopes, when the token answer named them. Checked against
    # FASTMAIL_SCOPES on the code exchange; empty when unnamed.
    scope: str = ""


def code_verifier() -> str:
    """A random PKCE verifier: 86 characters, inside RFC 7636's 43-128."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    """The S256 challenge for a verifier, base64url without padding."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def urlopen_noredirect_transport(request: urllib.request.Request) -> Response:
    """Send a request for real, refusing redirects instead of following.

    The metadata fetch uses this: a redirect there is treated the same as
    an error response, so the caller can tell a moved metadata URL from a
    genuine one. Every other call uses urlopen_transport.
    """
    opener = urllib.request.build_opener(
        _RefuseRedirect, urllib.request.HTTPSHandler(context=https_context())
    )
    try:
        # S310: the metadata URL is a module constant, and the opener below
        # refuses to be led anywhere else.
        with opener.open(request, timeout=NET_TIMEOUT_SECONDS) as reply:  # noqa: S310
            return Response(reply.status, reply.read(), dict(reply.headers))
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read(), dict(error.headers))
    except urllib.error.URLError as error:
        if isinstance(error.reason, Exception):
            raise error.reason from error
        raise


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """An opener handler that fails a redirect instead of following it."""

    # PLR0913: the arity is dictated by HTTPRedirectHandler.
    def redirect_request(  # noqa: PLR0913
        self, _req, fp, code, msg, headers, newurl
    ):
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


def fetch_metadata(
    transport: Transport = urlopen_noredirect_transport,
) -> AuthorizationServer:
    """The authorization server's profile conformance, verified or refused.

    Fetched on every login: the issuer must match exactly, every endpoint
    the flow touches must be https, and the metadata must advertise the
    public-client shape (code response, both grants, auth method none, S256
    challenges, the iss response parameter) and the scopes the client
    needs. The authorize and token endpoints must also equal the pinned
    constants -- a single-issuer client follows no moved endpoint, it
    fails closed until the pins are re-verified.
    """
    response = transport(urllib.request.Request(FASTMAIL_METADATA_URL))
    if response.status != 200:
        raise FastmailOAuthError("server_error")
    try:
        body = response.json()
    except ValueError:
        raise FastmailOAuthError("invalid_response") from None
    if not isinstance(body, dict):
        raise FastmailOAuthError("invalid_response")
    _require_json_content(response)
    if body.get("issuer") != FASTMAIL_ISSUER:
        raise FastmailOAuthError("invalid_response")
    registration = _https_url(body, "registration_endpoint")
    authorization = _https_url(body, "authorization_endpoint")
    token = _https_url(body, "token_endpoint")
    if authorization != FASTMAIL_AUTHORIZE_URL or token != FASTMAIL_TOKEN_URL:
        raise FastmailOAuthError("invalid_response")
    _require_lists(body)
    if body.get("authorization_response_iss_parameter_supported") is not True:
        raise FastmailOAuthError("invalid_response")
    scopes = _string_list(body, "scopes_supported")
    if any(scope not in scopes for scope in FASTMAIL_SCOPES):
        raise FastmailOAuthError("invalid_response")
    protected = _optional_string_list(body, "protected_resources")
    if protected is not None and FASTMAIL_JMAP_SESSION_URL not in protected:
        # The server cannot serve the one endpoint the client needs.
        raise FastmailOAuthError("invalid_response")
    return AuthorizationServer(
        FASTMAIL_ISSUER,
        registration,
        authorization,
        token,
        scopes,
        protected if protected is not None else (),
    )


def _require_json_content(response: Response) -> None:
    """The metadata answers JSON; anything else is not the metadata."""
    content_type = response.headers.get("Content-Type", "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise FastmailOAuthError("invalid_response")


def _https_url(body: dict, name: str) -> str:
    """A metadata endpoint that is usable: present, https, with a host."""
    value = body.get(name)
    if not isinstance(value, str):
        raise FastmailOAuthError("invalid_response")
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        raise FastmailOAuthError("invalid_response") from None
    if parts.scheme != "https" or not parts.netloc:
        raise FastmailOAuthError("invalid_response")
    return value


def _string_list(body: dict, name: str) -> tuple[str, ...]:
    """A metadata string array, refused when missing or misshapen."""
    value = body.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FastmailOAuthError("invalid_response")
    return tuple(value)


def _optional_string_list(body: dict, name: str) -> tuple[str, ...] | None:
    """A metadata string array, or None when absent and allowed to be."""
    if body.get(name) is None:
        return None
    return _string_list(body, name)


def _require_lists(body: dict) -> None:
    """The public-client shape the profile mandates in the metadata."""
    required = (
        ("response_types_supported", ("code",)),
        ("grant_types_supported", ("authorization_code", "refresh_token")),
        ("token_endpoint_auth_methods_supported", ("none",)),
        ("code_challenge_methods_supported", ("S256",)),
    )
    for name, members in required:
        advertised = _string_list(body, name)
        if any(member not in advertised for member in members):
            raise FastmailOAuthError("invalid_response")


def register_client(
    server: AuthorizationServer,
    *,
    transport: Transport = urlopen_transport,
) -> Registration:
    """An open dynamic registration: the client id for one login.

    No authentication, no pre-existing relationship -- the loopback
    redirect is what keeps this a native-only flow. A ``client_secret``
    in the answer is ignored: a public client proves possession of the
    code with PKCE instead. Aborts when the granted scope or the echoed
    redirects omit what the flow needs.
    """
    payload = json.dumps(
        {
            "redirect_uris": [FASTMAIL_LOOPBACK_REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": " ".join(FASTMAIL_SCOPES),
            "client_name": FASTMAIL_CLIENT_NAME,
            "client_uri": FASTMAIL_CLIENT_URI,
            "software_id": FASTMAIL_SOFTWARE_ID,
        }
    ).encode()
    # S310: the endpoint came from verified metadata (https with a host),
    # never from caller input.
    request = urllib.request.Request(  # noqa: S310
        server.registration_endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    response = transport(request)
    try:
        body = response.json()
    except ValueError:
        raise FastmailOAuthError("invalid_response") from None
    if response.status != 201:
        raise FastmailOAuthError(_registration_error(body))
    if not isinstance(body, dict):
        raise FastmailOAuthError("invalid_response")
    client_id = body.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise FastmailOAuthError("invalid_response")
    scope = body.get("scope")
    if scope is not None and (
        not isinstance(scope, str)
        or any(wanted not in scope.split() for wanted in FASTMAIL_SCOPES)
    ):
        raise FastmailOAuthError("invalid_response")
    redirects = body.get("redirect_uris")
    if redirects is not None and (
        not isinstance(redirects, list) or FASTMAIL_LOOPBACK_REDIRECT not in redirects
    ):
        raise FastmailOAuthError("invalid_response")
    return Registration(client_id, scope if isinstance(scope, str) else "")


def _registration_error(body: object) -> str:
    """The fixed code for a refused registration, never server text."""
    if not isinstance(body, dict):
        return "invalid_response"
    code = body.get("error", "")
    if code in ("invalid_redirect_uri", "invalid_client_metadata"):
        # Our registration is malformed: a client bug, not a server one.
        return "invalid_request"
    return "server_error"


def authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...] = FASTMAIL_SCOPES,
    state: str,
    code_challenge: str,
) -> str:
    """The URL the system browser opens to start a native sign-in.

    The client id is the one register_client just issued, and the
    redirect is the loopback listener's with-port URI. ``prompt`` stays
    ``consent``: offline_access is always requested, and some deployments
    issue no refresh token without it.
    """
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
            "prompt": "consent",
        }
    )
    return f"{FASTMAIL_AUTHORIZE_URL}?{query}"


def exchange_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str,
    client_id: str,
    transport: Transport = urlopen_transport,
) -> Tokens:
    """Trade the code the loopback redirect caught for the first tokens.

    The granted scopes are checked when the answer names them: the user
    may have authorized a subset, and the client cannot function without
    the mail scope.
    """
    tokens = _token_request(
        transport,
        client_id,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    if tokens.scope and any(
        wanted not in tokens.scope.split() for wanted in FASTMAIL_SCOPES
    ):
        raise FastmailOAuthError("invalid_response")
    return tokens


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str,
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
        raise FastmailOAuthError(_token_error(body))
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise FastmailOAuthError("invalid_response")
    refresh = body.get("refresh_token")
    try:
        lifetime = int(body.get("expires_in", 0))
    except (TypeError, ValueError):
        lifetime = 0
    username = body.get("username")
    scope = body.get("scope")
    return Tokens(
        access,
        refresh if isinstance(refresh, str) else None,
        lifetime,
        username if isinstance(username, str) and username else None,
        scope if isinstance(scope, str) else "",
    )


def _token_error(body: dict) -> str:
    """The fixed code for a refused token request, never server text."""
    code = body.get("error", "")
    # A temporary invalid_grant (a locked user, unlocked later) still
    # reads as invalid_grant: reconnecting is harmless either way, and
    # the fixed vocabulary has no "try again much later".
    if code == "invalid_grant":
        return "invalid_grant"
    # The registration died under the grant: the next login re-registers
    # before authorizing again, which is a reconnect by another name.
    if code == "invalid_client":
        return "invalid_grant"
    return "server_error"


def encode_stored(refresh_token: str, *, client_id: str) -> str:
    """What secrets.store_password holds for a native Fastmail account."""
    return json.dumps({"refresh_token": refresh_token, "client_id": client_id})


def native_credential(
    email: str,
    stored: str,
    *,
    transport: Transport = urlopen_transport,
) -> Credential:
    """An xoauth2 Credential from a stored refresh token, refreshing it.

    The stored value carries the client id the token was issued to, and
    the refresh goes out under it. Raises FastmailOAuthError when the
    stored value is not ours or the refresh is refused; the caller turns
    that into "could not sign in", never a retry of the password the
    user no longer has.

    One-shot only: the refresh rotates the stored token and this return
    cannot carry the new one. A caller that signs in repeatedly must call
    refresh_access_token itself and persist each rotation.
    """
    try:
        saved = json.loads(stored)
        refresh = saved.get("refresh_token")
        client_id = saved.get("client_id")
    except (ValueError, AttributeError):
        raise FastmailOAuthError("invalid_request") from None
    if (
        not isinstance(refresh, str)
        or not refresh
        or not isinstance(client_id, str)
        or not client_id
    ):
        raise FastmailOAuthError("invalid_request")
    tokens = refresh_access_token(refresh, client_id=client_id, transport=transport)
    return Credential(email, tokens.access_token, MECHANISM_XOAUTH2)
