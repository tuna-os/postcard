"""Where an account's sign-in comes from: the system keyring for an account
typed in by hand, GNOME Online Accounts for one imported from Settings."""

import logging

import gi

gi.require_version("Secret", "1")

from gi.repository import Secret

from . import goa
from .models.account import Account
from .net import ms_oauth
from .net.auth import Credential
from .net.proton_session import MECHANISM_PROTON
from .net.tuta_session import MECHANISM_TUTA

logger = logging.getLogger(__name__)

_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postcard.Account",
    Secret.SchemaFlags.NONE,
    {"account-id": Secret.SchemaAttributeType.INTEGER},
)


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
