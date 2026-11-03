import logging
import threading
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .core import secrets
from .core.models.account import GRAPH_HOST, GRAPH_PORT, PROTOCOL_GRAPH
from .core.net import errors, ms_login, ms_oauth
from .core.store.database import Database

logger = logging.getLogger(__name__)

PAGE_FORM = "form"
PAGE_WAITING = "waiting"


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/microsoft-dialog.ui")
class PostcardMicrosoftDialog(Adw.Dialog):
    __gtype_name__ = "PostcardMicrosoftDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    dialog_stack: Gtk.Stack = Gtk.Template.Child()
    email_row: Adw.EntryRow = Gtk.Template.Child()
    display_name_row: Adw.EntryRow = Gtk.Template.Child()
    cancel_button: Gtk.Button = Gtk.Template.Child()
    sign_in_button: Gtk.Button = Gtk.Template.Child()
    waiting_cancel_button: Gtk.Button = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self._cancelled = False

        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.waiting_cancel_button.connect("clicked", lambda _b: self.close())
        self.connect("closed", self._on_closed)
        self.email_row.connect("changed", self._update_sign_in_sensitivity)
        self.sign_in_button.connect("clicked", self._on_sign_in_clicked)
        self._update_sign_in_sensitivity()

    def _on_closed(self, *_args: object) -> None:
        # A worker still waiting on the browser drops its result instead of
        # saving an account for a dialog the user dismissed.
        self._cancelled = True

    def _update_sign_in_sensitivity(self, *_args: object) -> None:
        self.sign_in_button.set_sensitive("@" in self.email_row.get_text().strip())

    def _on_sign_in_clicked(self, _button: Gtk.Button) -> None:
        email = self.email_row.get_text().strip()
        if "@" not in email:
            # Belt-and-braces; the insensitive button already guards this.
            return
        if any(
            account.email.casefold() == email.casefold()
            for account in self._db.accounts()
        ):
            self._toast(_("This account is already added."))
            return
        if not ms_oauth.MS_CLIENT_ID:
            self._toast(_("Microsoft sign-in is not available in this build yet."))
            return
        if Gio.AppInfo.get_default_for_uri_scheme("https") is None:
            self._toast(_("No web browser is available to complete the sign-in."))
            return
        self.dialog_stack.set_visible_child_name(PAGE_WAITING)
        display_name = (
            self.display_name_row.get_text().strip() or email.partition("@")[0]
        )
        threading.Thread(
            target=self._login_worker, args=(email, display_name), daemon=True
        ).start()

    # Runs on the worker thread: network only, no Gtk/database access.
    def _login_worker(self, email: str, display_name: str) -> None:
        try:
            result = ms_login.log_in(open_browser=self._open_browser)
        except Exception as error:
            logger.exception("could not complete the Microsoft sign-in for %s", email)
            GLib.idle_add(self._on_login_failed, error)
        else:
            GLib.idle_add(self._on_login_done, email, display_name, result)

    def _open_browser(self, url: str) -> None:
        # Called on the worker thread; launching is main-loop work, and this
        # must return at once so the worker can wait for the browser.
        GLib.idle_add(self._launch_browser, url)

    def _launch_browser(self, url: str) -> bool:
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error:
            logger.warning("could not open the browser for sign-in", exc_info=True)
            self._toast(_("Could not open the browser."))
        return False

    def _on_login_done(
        self, email: str, display_name: str, result: ms_login.NativeLogin
    ) -> bool:
        if self._cancelled:
            return False
        try:
            account = self._db.save_account(
                email=email,
                display_name=display_name,
                imap_host=GRAPH_HOST,
                imap_port=GRAPH_PORT,
                smtp_host=GRAPH_HOST,
                smtp_port=GRAPH_PORT,
                protocol=PROTOCOL_GRAPH,
            )
            secrets.store_password(account.id, result.stored)
        except Exception as error:
            logger.exception("could not save the Microsoft account %s", email)
            _is_auth_failure, message = errors.classify(error, GRAPH_HOST)
            self._toast(message)
            self.dialog_stack.set_visible_child_name(PAGE_FORM)
            return False
        self.emit("account-added")
        self.close()
        return False

    def _on_login_failed(self, error: Exception) -> bool:
        if self._cancelled:
            return False
        _is_auth_failure, message = errors.classify(error, GRAPH_HOST)
        self._toast(message)
        self.dialog_stack.set_visible_child_name(PAGE_FORM)
        return False

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))
