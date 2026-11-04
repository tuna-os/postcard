"""One-call native Fastmail sign-in, without GTK.

Ties fastmail_oauth and the shared loopback catcher together: build a PKCE
authorization URL for a fresh loopback listener, hand it to the caller's
browser opener, wait for the code, and exchange it. Runs on a worker thread
like every network operation; open_browser must return promptly, so a GTK
caller hops to the main loop inside it (idle_add) rather than showing the URI
inline. Returns the tokens plus the stored form of the refresh token;
persisting it stays with the caller, which owns the keyring.
"""

import secrets
from collections.abc import Callable
from typing import NamedTuple

from .fastmail_oauth import (
    FASTMAIL_CLIENT_ID,
    FASTMAIL_SCOPES,
    FastmailOAuthError,
    Tokens,
    authorization_url,
    code_challenge,
    code_verifier,
    encode_stored,
    exchange_code,
)
from .jmap_session import Transport, urlopen_transport
from .ms_loopback import DEFAULT_TIMEOUT_SECONDS, LoopbackRedirect
from .ms_oauth import MsOAuthError


class NativeLogin(NamedTuple):
    tokens: Tokens
    stored: str


def log_in(
    *,
    client_id: str = FASTMAIL_CLIENT_ID,
    scopes: tuple[str, ...] = FASTMAIL_SCOPES,
    open_browser: Callable[[str], None],
    transport: Transport = urlopen_transport,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> NativeLogin:
    """Sign in natively and return what the caller must persist.

    Raises invalid_request before the browser opens when no client is
    registered, invalid_grant when the provider refused, and invalid_response
    when the answer carried no refresh token -- without one there is nothing
    to keep the sign-in alive, so a storeless login fails closed.
    """
    verifier = code_verifier()
    state = secrets.token_urlsafe(32)
    try:
        listener = LoopbackRedirect()
    except MsOAuthError as error:
        raise FastmailOAuthError(error.code) from error
    with listener:
        url = authorization_url(
            client_id=client_id,
            redirect_uri=listener.redirect_uri,
            scopes=scopes,
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
        tokens = exchange_code(
            code,
            verifier,
            redirect_uri=listener.redirect_uri,
            client_id=client_id,
            transport=transport,
        )
    if not tokens.refresh_token:
        raise FastmailOAuthError("invalid_response")
    return NativeLogin(tokens, encode_stored(tokens.refresh_token))
