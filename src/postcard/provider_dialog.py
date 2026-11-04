from gettext import gettext as _

from gi.repository import Adw, GObject, Gtk

from .account_dialog import PostcardAccountDialog
from .core.net import fastmail_oauth, ms_oauth, proton_session
from .core.providers.catalog import (
    PROVIDERS,
    AuthMethod,
    ProviderInfo,
    availability,
)
from .core.store.database import Database
from .fastmail_dialog import PostcardFastmailDialog
from .microsoft_dialog import PostcardMicrosoftDialog
from .online_accounts_dialog import PostcardOnlineAccountsDialog
from .proton_dialog import PostcardProtonDialog
from .tuta_dialog import PostcardTutaDialog


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/provider-dialog.ui")
class PostcardProviderDialog(Adw.Dialog):
    __gtype_name__ = "PostcardProviderDialog"

    providers_group: Adw.PreferencesGroup = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # Google has no native client, so its row opens the Online Accounts
    # import, which signs in with Google through Settings instead.
    _DIALOGS = {
        "google": PostcardOnlineAccountsDialog,
        "microsoft": PostcardMicrosoftDialog,
        "fastmail": PostcardFastmailDialog,
        "proton": PostcardProtonDialog,
        "tuta": PostcardTutaDialog,
        "other": PostcardAccountDialog,
    }

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        enabled = availability(
            {
                "microsoft": bool(ms_oauth.MS_CLIENT_ID),
                "fastmail": bool(fastmail_oauth.FASTMAIL_CLIENT_ID),
                "proton-helper": proton_session.helper_available(),
            }
        )
        for provider in PROVIDERS:
            self.providers_group.add(self._row(provider, enabled[provider.id]))

    def _row(self, provider: ProviderInfo, is_enabled: bool) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=provider.name,
            subtitle=provider.subtitle if is_enabled else provider.unavailable_subtitle,
        )
        row.add_prefix(
            Gtk.Image(
                icon_name=provider.icon_name,
                pixel_size=32,
                valign=Gtk.Align.CENTER,
            )
        )
        if provider.auth is AuthMethod.OAUTH:
            row.add_suffix(
                Gtk.Image(
                    icon_name="web-browser-symbolic",
                    valign=Gtk.Align.CENTER,
                    tooltip_text=_("Signs in through your browser"),
                )
            )
        if is_enabled:
            row.set_activatable(True)
            row.connect("activated", self._on_row_activated, provider.id)
        else:
            row.set_sensitive(False)
        return row

    def _on_row_activated(self, _row: Adw.ActionRow, provider_id: str) -> None:
        dialog = self._DIALOGS[provider_id](self._db)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_account_added(self, _dialog: Adw.Dialog) -> None:
        self.emit("account-added")
        self.close()
