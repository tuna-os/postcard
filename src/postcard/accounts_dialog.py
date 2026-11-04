import logging
import threading
from gettext import gettext as _

from gi.repository import Adw, Gtk

from . import mail_sync
from .account_dialog import PostcardAccountDialog
from .core import secrets
from .core.models.account import Account
from .core.net import fastmail_oauth, ms_oauth
from .core.net.auth import Credential
from .core.net.tuta_session import MECHANISM_TUTA, TutaSession
from .core.store.database import Database
from .fastmail_dialog import PostcardFastmailDialog
from .microsoft_dialog import PostcardMicrosoftDialog
from .online_accounts_dialog import PostcardOnlineAccountsDialog
from .tuta_dialog import PostcardTutaDialog

logger = logging.getLogger(__name__)


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/accounts-dialog.ui")
class PostcardAccountsDialog(Adw.Dialog):
    __gtype_name__ = "PostcardAccountsDialog"

    accounts_group: Adw.PreferencesGroup = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()
    online_accounts_button: Gtk.Button = Gtk.Template.Child()
    microsoft_button: Gtk.Button = Gtk.Template.Child()
    fastmail_button: Gtk.Button = Gtk.Template.Child()
    tuta_button: Gtk.Button = Gtk.Template.Child()

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self._rows: list[Adw.ActionRow] = []

        self.add_button.connect("clicked", self._on_add_clicked)
        self.online_accounts_button.connect("clicked", self._on_online_accounts_clicked)
        self.microsoft_button.connect("clicked", self._on_microsoft_clicked)
        self.fastmail_button.connect("clicked", self._on_fastmail_clicked)
        self.tuta_button.connect("clicked", self._on_tuta_clicked)
        # No Entra registration, no button: the flow cannot complete yet.
        self.microsoft_button.set_visible(bool(ms_oauth.MS_CLIENT_ID))
        # No Fastmail client, no button: same gate.
        self.fastmail_button.set_visible(bool(fastmail_oauth.FASTMAIL_CLIENT_ID))
        self._reload()

    def _reload(self) -> None:
        for row in self._rows:
            self.accounts_group.remove(row)
        self._rows.clear()

        for account in self._db.accounts():
            row = Adw.ActionRow(title=account.email, subtitle=account.display_name)

            remove_button = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Remove Account"),
            )
            remove_button.add_css_class("flat")
            remove_button.connect("clicked", self._on_remove_clicked, account.id)
            row.add_suffix(remove_button)

            self.accounts_group.add(row)
            self._rows.append(row)

    def _on_remove_clicked(self, _button: Gtk.Button, account_id: int) -> None:
        # First: a re-added account reuses the rowid, so a completion landing
        # after the rows go would otherwise file the old one's mail as its own.
        mail_sync.invalidate_account(account_id)
        account = next(
            (known for known in self._db.accounts() if known.id == account_id), None
        )
        if account is not None and account.is_tuta:
            # Before the local secret is cleared: revoking needs it, and runs
            # on a worker because it dials the server.
            self._revoke_tuta_session(account)
        self._db.delete_account(account_id)
        secrets.clear_password(account_id)
        mail_sync.close_sessions(account_id)
        self._reload()

    def _revoke_tuta_session(self, account: Account) -> None:
        stored = secrets.lookup_password(account.id)
        if not stored:
            return
        credential = Credential(account.login_name, stored, MECHANISM_TUTA)
        threading.Thread(
            target=self._revoke_worker, args=(account.email, credential), daemon=True
        ).start()

    @staticmethod
    def _revoke_worker(email: str, credential: Credential) -> None:
        # Best-effort: the local credential is cleared regardless, and an
        # already-gone session counts as revoked.
        try:
            TutaSession(credential).request("revoke")
        except Exception:
            logger.warning("could not revoke the Tuta session for %s", email)

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostcardAccountDialog(self._db)
        dialog.connect("account-added", lambda _d: self._reload())
        dialog.present(self)

    def _on_online_accounts_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostcardOnlineAccountsDialog(self._db)
        dialog.connect("account-added", lambda _d: self._reload())
        dialog.present(self)

    def _on_microsoft_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostcardMicrosoftDialog(self._db)
        dialog.connect("account-added", lambda _d: self._reload())
        dialog.present(self)

    def _on_fastmail_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostcardFastmailDialog(self._db)
        dialog.connect("account-added", lambda _d: self._reload())
        dialog.present(self)

    def _on_tuta_clicked(self, _button: Gtk.Button) -> None:
        dialog = PostcardTutaDialog(self._db)
        dialog.connect("account-added", lambda _d: self._reload())
        dialog.present(self)
