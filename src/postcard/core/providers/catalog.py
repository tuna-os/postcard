"""The providers the add-account picker offers, and how each one signs in.

Pure metadata: no Gtk, no secrets, no network. The picker renders one row
per entry in ``PROVIDERS`` order; ``availability`` evaluates the gates
(shipped OAuth client ids, installed helpers) from the flags the caller
passes in, so tests pin the matrix without importing the OAuth modules
and the dialog stays a thin renderer.

OAuth first is deliberate: where a provider offers it, the browser flow
means Postcard never sees the password. Google has no native client, so
its row opens the Online Accounts import, which signs in with Google
through Settings instead.
"""

from dataclasses import dataclass
from enum import Enum
from gettext import gettext as _


class AuthMethod(Enum):
    """How the row's dialog signs in."""

    OAUTH = "oauth"
    ONLINE_ACCOUNTS = "online-accounts"
    PASSWORD = "password"
    MANUAL = "manual"


@dataclass(frozen=True)
class ProviderInfo:
    """One picker row: stable id, display strings, icon, and sign-in path."""

    id: str
    name: str
    subtitle: str
    icon_name: str
    auth: AuthMethod
    # Build gate: the flag this build must ship (an OAuth client id, an
    # installed helper), or "" when the row needs nothing. A gated row
    # without its flag renders insensitive with ``unavailable_subtitle``
    # instead of hiding, so the picker always shows the full list.
    client_flag: str = ""
    unavailable_subtitle: str = ""


PROVIDERS: tuple[ProviderInfo, ...] = (
    ProviderInfo(
        id="google",
        name=_("Google"),
        subtitle=_("Gmail, using GNOME Online Accounts"),
        icon_name="postcard-provider-google",
        auth=AuthMethod.ONLINE_ACCOUNTS,
    ),
    ProviderInfo(
        id="microsoft",
        name=_("Microsoft 365"),
        subtitle=_("Outlook, with a browser sign-in"),
        icon_name="postcard-provider-microsoft",
        auth=AuthMethod.OAUTH,
        client_flag="microsoft",
        unavailable_subtitle=_("Not available in this build yet"),
    ),
    ProviderInfo(
        id="fastmail",
        name=_("Fastmail"),
        subtitle=_("With a browser sign-in"),
        icon_name="postcard-provider-fastmail",
        auth=AuthMethod.OAUTH,
        client_flag="fastmail",
        unavailable_subtitle=_("Not available in this build yet"),
    ),
    ProviderInfo(
        id="proton",
        name=_("Proton"),
        subtitle=_("Proton Mail, with password and two-factor code"),
        icon_name="postcard-provider-proton",
        auth=AuthMethod.PASSWORD,
        client_flag="proton-helper",
        unavailable_subtitle=_("Needs its sign-in helper, which is not shipped yet"),
    ),
    ProviderInfo(
        id="tuta",
        name=_("Tuta"),
        subtitle=_("With password and two-factor code"),
        icon_name="postcard-provider-tuta",
        auth=AuthMethod.PASSWORD,
    ),
    ProviderInfo(
        id="other",
        name=_("Other"),
        subtitle=_("Any IMAP account, set up by hand"),
        icon_name="postcard-provider-other",
        auth=AuthMethod.MANUAL,
    ),
)


def availability(client_flags: dict[str, bool]) -> dict[str, bool]:
    """Which provider ids the picker enables, by advertised client id.

    A provider with no ``client_flag`` is always available. A gated one is
    available only when its flag reads true; an unknown flag reads false,
    so a misspelled gate disables its row instead of enabling it.
    """
    return {
        provider.id: (
            not provider.client_flag
            or bool(client_flags.get(provider.client_flag, False))
        )
        for provider in PROVIDERS
    }
