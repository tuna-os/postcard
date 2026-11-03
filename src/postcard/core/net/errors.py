import html
import re
import socket
import ssl
from gettext import gettext as _

from ..store.database import CacheError
from .graph_session import GraphError
from .imap_session import ImapError, UidValidityError
from .ms_oauth import MsOAuthError
from .proton_session import ProtonError
from .smtp_session import SmtpError
from .tuta_session import TutaError

# Substrings (lowercased) that mean the server rejected the credentials.
_AUTH_HINTS = (
    "authenticationfailed",
    "authentication failed",
    "invalid credentials",
    "username and password not accepted",
    "login failed",
    "5.7.8",
)


# Turn a raw exception into an (is it the password?, friendly message) pair.
# Only that one distinction is acted on -- a rejected password is the failure
# no Retry button can fix. Order matters: most socket errors subclass OSError,
# so the specific cases are checked first.
def classify(  # noqa: PLR0911, PLR0912, C901
    exc: Exception, host: str
) -> tuple[bool, str]:
    # noqa PLR0911/PLR0912/C901: a dispatch table. One return per exception
    # category is the clearest shape here; collapsing them into a dict costs
    # readability.
    if isinstance(exc, ssl.SSLError):
        return False, _("Couldn't establish a secure connection to {host}.").format(
            host=host
        )
    if isinstance(exc, socket.gaierror):
        return False, _(
            "Can't find {host}. Check the server address or your connection."
        ).format(host=host)
    if isinstance(exc, ConnectionRefusedError):
        return False, _("{host} refused the connection. Check the port.").format(
            host=host
        )
    if isinstance(exc, TimeoutError):
        return False, _("Connecting to {host} timed out.").format(host=host)
    if isinstance(exc, TutaError):
        return _classify_tuta(exc)
    if isinstance(exc, ProtonError):
        return _classify_proton(exc)
    if isinstance(exc, MsOAuthError):
        return _classify_ms_oauth(exc)
    if isinstance(exc, CacheError):
        return _classify_cache(exc)
    if isinstance(exc, GraphError):
        return _classify_graph(exc)
    if isinstance(exc, UidValidityError):
        return False, _("This folder changed on the server. Refresh and try again.")
    if isinstance(exc, (ImapError, SmtpError)):
        text = str(exc).lower()
        if any(hint in text for hint in _AUTH_HINTS):
            return True, _("Sign-in failed. Check the account password.")
        return False, str(exc)
    if isinstance(exc, OSError):
        return False, _("Couldn't reach the mail server. Check your connection.")
    return False, str(exc)


# Graph's HTTP statuses, grouped by what the user can do about them.
_GRAPH_UNAUTHORIZED = 401
_GRAPH_FORBIDDEN = 403
_GRAPH_BUSY = frozenset({429, 503, 504})


def _classify_cache(exc: CacheError) -> tuple[bool, str]:
    if exc.code in ("key_missing", "key_unavailable"):
        return False, _("Could not get the mail cache key from the keyring.")
    if exc.code == "crypto_unavailable":
        return False, _("This build cannot decrypt the mail cache.")
    if exc.code == "unlock_failed":
        return False, _("The stored key does not unlock the mail cache.")
    if exc.code == "migration_failed":
        return False, _("The old mail cache could not be encrypted.")
    if exc.code == "schema_too_new":
        return False, _(
            "This mail cache needs a newer Postcard. Update the app to open it."
        )
    return False, _("The mail cache could not be upgraded.")


def _classify_graph(exc: GraphError) -> tuple[bool, str]:
    # The token comes from GNOME Online Accounts, so a rejected one is fixed
    # there, not with a password field in Postcard.
    if exc.status == _GRAPH_UNAUTHORIZED:
        return True, _(
            "Microsoft rejected the sign-in. Sign in again under Online Accounts "
            "in Settings."
        )
    if exc.status == _GRAPH_FORBIDDEN:
        return True, _(
            "Microsoft refused access to this mailbox. Your organization may "
            "need to allow GNOME Online Accounts to read mail."
        )
    if exc.status in _GRAPH_BUSY:
        return False, _("Microsoft is busy right now. Try again in a moment.")
    return False, exc.message


# Trailing punctuation is sentence, not URL: "see https://x/y." keeps the dot out.
_URL_PATTERN = re.compile(r"https?://[^\s<>]*[^\s<>.,;:)\]]")


# Server messages often carry a help URL ("Application-specific password
# required: https://support.google.com/..."). The banner renders Pango markup,
# so escape the text first, then turn bare URLs into clickable links. Quotes are
# escaped too: an unescaped one would break out of the href="..." attribute.
def linkify(text: str) -> str:
    escaped = html.escape(text)
    return _URL_PATTERN.sub(lambda m: f'<a href="{m.group()}">{m.group()}</a>', escaped)


def _classify_ms_oauth(exc: MsOAuthError) -> tuple[bool, str]:
    if exc.code == "invalid_grant":
        return True, _(
            "The Microsoft sign-in expired. Reconnect the account to continue."
        )
    return False, _("The Microsoft sign-in could not be completed. Try again later.")


def _classify_proton(exc: ProtonError) -> tuple[bool, str]:
    if exc.code == "helper_missing":
        return False, _("This build does not include the experimental Proton helper.")
    if exc.code == "login_failed":
        return True, _("Proton sign-in failed. Check the account password.")
    if exc.code in ("second_factor_required", "invalid_totp", "second_factor_failed"):
        return True, _("Proton requires a valid second-factor code.")
    if exc.code == "unsupported_second_factor":
        return True, _("This Proton second-factor method is not supported yet.")
    if exc.code == "unsupported_operation":
        return False, _(
            "This operation is not supported by the experimental Proton backend yet."
        )
    return False, _("The experimental Proton operation failed. Try again later.")


def _classify_tuta(exc: TutaError) -> tuple[bool, str]:
    if exc.code == "helper_missing":
        return False, _("This build does not include the experimental Tuta helper.")
    if exc.code in ("login_failed", "resume_failed"):
        return False, _(
            "Tuta sign-in could not be completed. Check the session and client version."
        )
    if exc.code in ("second_factor_required", "invalid_totp", "second_factor_failed"):
        return True, _("Tuta requires a valid second-factor code.")
    if exc.code == "unsupported_second_factor":
        return True, _("This Tuta second-factor method is not supported yet.")
    if exc.code == "unsupported_operation":
        return False, _(
            "This operation is not supported by the experimental Tuta backend yet."
        )
    return False, _("The experimental Tuta operation failed. Try again later.")
