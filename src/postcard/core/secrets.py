"""Where an account's sign-in comes from: the system keyring for an account
typed in by hand, GNOME Online Accounts for one imported from Settings."""

import json
import logging
import secrets as stdlib_secrets

import gi

gi.require_version("Secret", "1")

from gi.repository import GLib, Secret

from . import goa
from .models.account import Account
from .net import ms_oauth
from .net.auth import Credential
from .net.proton_session import MECHANISM_PROTON, ProtonError
from .net.tuta_session import MECHANISM_TUTA, TutaError
from .store.database import CacheError

logger = logging.getLogger(__name__)

_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postcard.Account",
    Secret.SchemaFlags.NONE,
    {"account-id": Secret.SchemaAttributeType.INTEGER},
)

_DB_KEY_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postcard.CacheKey",
    Secret.SchemaFlags.NONE,
    {"purpose": Secret.SchemaAttributeType.STRING},
)
_DB_KEY_BYTES = 32
_DB_KEY_ATTRIBUTES = {"purpose": "mail-cache"}


def store_password(account_id: int, password: str) -> None:
    Secret.password_store_sync(
        _SCHEMA,
        {"account-id": str(account_id)},
        Secret.COLLECTION_DEFAULT,
        f"Postcard account {account_id}",
        password,
        None,
    )


def lookup_password(account_id: int) -> str | None:
    return Secret.password_lookup_sync(_SCHEMA, {"account-id": str(account_id)}, None)


def clear_password(account_id: int) -> bool:
    return Secret.password_clear_sync(_SCHEMA, {"account-id": str(account_id)}, None)


def store_tuta_session(account_id: int, credential: Credential) -> None:
    """Store a resumable Tuta session in the keyring.

    The credential must be a session the helper returned, never the account
    password: refusing anything else here is what keeps passwords out of
    long-term storage even if a caller mixes them up. The account database
    holds no credentials at all.
    """
    if credential.mechanism != MECHANISM_TUTA:
        raise TutaError("invalid_request")
    try:
        session = json.loads(credential.secret)
    except ValueError:
        raise TutaError("invalid_request") from None
    if not isinstance(session, dict) or not isinstance(session.get("login"), str):
        raise TutaError("invalid_request")
    store_password(account_id, credential.secret)


def store_proton_session(account_id: int, credential: Credential) -> None:
    """Store a resumable Proton session in the keyring.

    Same contract as the Tuta side: only a session the helper returned,
    never the account password. The login check mirrors
    ``proton_session.login`` so a session issued for another address cannot
    be stored under this one.
    """
    if credential.mechanism != MECHANISM_PROTON:
        raise ProtonError("invalid_request")
    try:
        session = json.loads(credential.secret)
    except ValueError:
        raise ProtonError("invalid_request") from None
    if not isinstance(session, dict) or not isinstance(session.get("login"), str):
        raise ProtonError("invalid_request")
    store_password(account_id, credential.secret)


def database_key() -> bytes:
    """The SQLCipher key for the mail cache, generating it once if needed.

    Called on the main thread at startup; a locked keyring prompts here rather
    than failing the launch outright. Raises CacheError when the keyring gives
    nothing usable -- the caller waits for the user, never falls back.
    """
    try:
        stored = Secret.password_lookup_sync(_DB_KEY_SCHEMA, _DB_KEY_ATTRIBUTES, None)
    except GLib.Error as error:
        raise CacheError("key_unavailable") from error
    if stored:
        try:
            key = bytes.fromhex(stored)
        except ValueError:
            key = b""
        if len(key) == _DB_KEY_BYTES:
            return key
        logger.warning("the stored cache key is malformed; keeping it untouched")
        raise CacheError("key_unavailable")
    key = stdlib_secrets.token_bytes(_DB_KEY_BYTES)
    try:
        Secret.password_store_sync(
            _DB_KEY_SCHEMA,
            _DB_KEY_ATTRIBUTES,
            Secret.COLLECTION_DEFAULT,
            "Postcard mail cache key",
            key.hex(),
            None,
        )
    except GLib.Error as error:
        raise CacheError("key_unavailable") from error
    return key


def credential_for(account: Account) -> Credential | None:
    """How to sign this account in, or None when we cannot.

    Called from the worker thread: both branches block on IPC, and the Online
    Accounts one can spend a network round trip refreshing an expired token.
    """
    if account.goa_id:
        return goa.credential(account.goa_id)

    stored = lookup_password(account.id)
    if not stored:
        return None
    if account.is_graph:
        # A Microsoft account added natively holds a refresh token, not a
        # password. Never raise: workers call this outside their try block, so
        # a failure here must read as "could not sign in", like every other.
        try:
            return ms_oauth.native_credential(account.email, stored)
        except ms_oauth.MsOAuthError:
            logger.warning(
                "could not refresh the Microsoft sign-in for %s", account.email
            )
            return None
    if account.is_tuta:
        mechanism = MECHANISM_TUTA
    elif account.is_proton:
        mechanism = MECHANISM_PROTON
    else:
        mechanism = "login"
    return Credential(account.login_name, stored, mechanism)
