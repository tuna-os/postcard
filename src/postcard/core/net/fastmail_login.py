"""One-call native Fastmail sign-in, without GTK.

Ties fastmail_oauth and the shared loopback catcher together: verify the
authorization-server metadata, register a client for this login, build a
PKCE authorization URL for a fresh loopback listener, hand it to the
caller's browser opener, wait for the code, and exchange it. Runs on a
worker thread like every network operation; open_browser must return
promptly, so a GTK caller hops to the main loop inside it (idle_add)
rather than showing the URI inline. Returns the tokens plus the stored
form of the refresh token; persisting it stays with the caller, which
owns the keyring.

The callback's ``iss`` is verified against the metadata issuer before
the code is exchanged: a code from any other issuer is a mix-up, and it
is never sent anywhere. The token answer's ``username`` is returned as
the identity the grant belongs to, which may differ from the address the
user typed.
"""

import secrets
from collections.abc import Callable
from typing import NamedTuple

from .fastmail_oauth import (
    FastmailOAuthError,
    Tokens,
    authorization_url,
    code_challenge,
    code_verifier,
    encode_stored,
    exchange_code,
    fetch_metadata,
    register_client,
    urlopen_noredirect_transport,
)
from .jmap_session import Transport, urlopen_transport
from .ms_loopback import DEFAULT_TIMEOUT_SECONDS, LoopbackRedirect
from .ms_oauth import MsOAuthError


class NativeLogin(NamedTuple):
    tokens: Tokens
    stored: str
    # The account the grant belongs to, when the token answer named it;
    # None falls back to the address the user typed.
    username: str | None = None


def log_in(
    *,
    open_browser: Callable[[str], None],
    transport: Transport = urlopen_transport,
    metadata_transport: Transport = urlopen_noredirect_transport,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> NativeLogin:
    """Sign in natively and return what the caller must persist.

    Raises invalid_request before the browser opens when the server
    stops conforming to the profile or refuses the registration,
    invalid_grant when the provider refused, and invalid_response when
    the callback or the token answer is not what the flow issued --
    including a missing refresh token, without which there is nothing
    to keep the sign-in alive, so a storeless login fails closed.
    """
    server = fetch_metadata(metadata_transport)
    registration = register_client(server, transport=transport)
    verifier = code_verifier()
    state = secrets.token_urlsafe(32)
    try:
        listener = LoopbackRedirect()
    except MsOAuthError as error:
        raise FastmailOAuthError(error.code) from error
    with listener:
        url = authorization_url(
            client_id=registration.client_id,
            redirect_uri=listener.redirect_uri,
            state=state,
            code_challenge=code_challenge(verifier),
        )
        open_browser(url)
        try:
            code = listener.wait_for_code(state, timeout=timeout)
        except MsOAuthError as error:
            # The catcher is shared with the Microsoft flow and raises its
            # error; the codes are the same fixed vocabulary, so only the
            # type is translated.
            raise FastmailOAuthError(error.code) from error
        _require_issuer(listener, code, server.issuer)
        tokens = exchange_code(
            code,
            verifier,
            redirect_uri=listener.redirect_uri,
            client_id=registration.client_id,
            transport=transport,
        )
    if not tokens.refresh_token:
        raise FastmailOAuthError("invalid_response")
    stored = encode_stored(tokens.refresh_token, client_id=registration.client_id)
    return NativeLogin(tokens, stored, tokens.username)


def _require_issuer(listener: LoopbackRedirect, code: str, issuer: str) -> None:
    """The callback's iss matches the metadata issuer, or the code dies.

    Runs before the exchange, so a mixed-up code is never sent anywhere.
    """
    # The shared catcher returns only the code; its recorded requests are
    # the only way to see the iss it arrived with. ms_loopback is the
    # Microsoft lane's file, so this reads the recording rather than
    # changing what the catcher returns.
    recorded = listener._server.requests  # noqa: SLF001
    for _path, query in reversed(recorded):
        if query.get("code") != code:
            continue
        if query.get("iss") == issuer:
            return
        break
    raise FastmailOAuthError("invalid_response")
