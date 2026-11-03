"""One-call native Microsoft sign-in, without GTK.

Ties ms_oauth and ms_loopback together: build a PKCE authorization URL for a
fresh loopback listener, hand it to the caller's browser opener, wait for the
code, and exchange it. Runs on a worker thread like every network operation;
open_browser must return promptly, so a GTK caller hops to the main loop
inside it (idle_add) rather than showing the URI inline. Returns the tokens
plus the stored form of the refresh token; persisting it stays with the
caller, which owns the keyring.
"""

import secrets
from collections.abc import Callable
from typing import NamedTuple

from .graph_session import Transport, urlopen_transport
from .ms_loopback import DEFAULT_TIMEOUT_SECONDS, LoopbackRedirect
from .ms_oauth import (
    MS_CLIENT_ID,
    MS_SCOPES,
    MsOAuthError,
    Tokens,
    authorization_url,
    code_challenge,
    code_verifier,
    encode_stored,
    exchange_code,
)


class NativeLogin(NamedTuple):
    tokens: Tokens
    stored: str


def log_in(
    *,
    client_id: str = MS_CLIENT_ID,
    scopes: tuple[str, ...] = MS_SCOPES,
    open_browser: Callable[[str], None],
    transport: Transport = urlopen_transport,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> NativeLogin:
    """Sign in natively and return what the caller must persist.

    Raises invalid_request before the browser opens when no app is registered,
    invalid_grant when the provider refused, and invalid_response when the
    answer carried no refresh token -- without one there is nothing to keep
    the sign-in alive, so a storeless login fails closed.
    """
    verifier = code_verifier()
    state = secrets.token_urlsafe(32)
    with LoopbackRedirect() as listener:
        url = authorization_url(
            client_id=client_id,
            redirect_uri=listener.redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=code_challenge(verifier),
        )
        open_browser(url)
        code = listener.wait_for_code(state, timeout=timeout)
        tokens = exchange_code(
            code,
            verifier,
            redirect_uri=listener.redirect_uri,
            client_id=client_id,
            transport=transport,
        )
    if not tokens.refresh_token:
        raise MsOAuthError("invalid_response")
    return NativeLogin(tokens, encode_stored(tokens.refresh_token))
