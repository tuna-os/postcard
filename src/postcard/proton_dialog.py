import logging
import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, GObject, Gtk

from .core import secrets
from .core.models.account import PROTOCOL_PROTON, PROTON_HOST, PROTON_PORT
from .core.net import errors, proton_session
from .core.net.auth import Credential
from .core.net.proton_session import ProtonError
from .core.store.database import Database

logger = logging.getLogger(__name__)

PAGE_FORM = "form"
PAGE_PROGRESS = "progress"

# Second-factor failures that deserve another code, not the form: the code
# may simply have rotated in the thirty seconds it took to type it.
_RETRYABLE_TOTP = frozenset(
    {"invalid_totp", "second_factor_failed", "second_factor_required"}
)


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/proton-dialog.ui")
class PostcardProtonDialog(Adw.Dialog):
    __gtype_name__ = "PostcardProtonDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    dialog_stack: Gtk.Stack = Gtk.Template.Child()
    email_row: Adw.EntryRow = Gtk.Template.Child()
    display_name_row: Adw.EntryRow = Gtk.Template.Child()
    password_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    totp_row: Adw.EntryRow = Gtk.Template.Child()
    cancel_button: Gtk.Button = Gtk.Template.Child()
    sign_in_button: Gtk.Button = Gtk.Template.Child()
    progress_cancel_button: Gtk.Button = Gtk.Template.Child()

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self._cancelled = False
        # One login attempt at a time; a newer attempt or a cancel orphans the
        # worker still running, whose completion is then dropped.
        self._generation = 0

        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.progress_cancel_button.connect("clicked", self._on_progress_cancelled)
        self.connect("closed", self._on_closed)
        self.email_row.connect("changed", self._update_sign_in_sensitivity)
        self.password_row.connect("changed", self._update_sign_in_sensitivity)
        self.sign_in_button.connect("clicked", self._on_sign_in_clicked)
        self._update_sign_in_sensitivity()

    def _on_closed(self, *_args: object) -> None:
        # A worker still running drops its result instead of saving an
        # account for a dialog the user dismissed.
        self._cancelled = True
        self._generation += 1

    def _update_sign_in_sensitivity(self, *_args: object) -> None:
        self.sign_in_button.set_sensitive(
            "@" in self.email_row.get_text().strip()
            and bool(self.password_row.get_text())
        )

    def _on_sign_in_clicked(self, _button: Gtk.Button) -> None:
        email = self.email_row.get_text().strip()
        password = self.password_row.get_text()
        if "@" not in email or not password:
            # Belt-and-braces; the insensitive button already guards this.
            return
        if any(
            account.email.casefold() == email.casefold()
            for account in self._db.accounts()
        ):
            self._toast(_("This account is already added."))
            return
        totp = self.totp_row.get_text().strip() or None
        if totp is not None and not proton_session.is_valid_totp(totp):
            self._toast(_("Enter the six-digit code from your authenticator app."))
            self.totp_row.grab_focus()
            return
        display_name = (
            self.display_name_row.get_text().strip() or email.partition("@")[0]
        )
        self.dialog_stack.set_visible_child_name(PAGE_PROGRESS)
        self._generation += 1
        threading.Thread(
            target=self._login_worker,
            args=(email, password, display_name, totp, self._generation),
            daemon=True,
        ).start()

    # Runs on the worker thread: network only, no Gtk/database access. The
    # helper proves the SRP login, which is the slow part.
    def _login_worker(
        self,
        email: str,
        password: str,
        display_name: str,
        totp: str | None,
        generation: int,
    ) -> None:
        try:
            credential = proton_session.login(email, password, totp)
        except Exception as error:
            if isinstance(error, ProtonError):
                code = error.code
            else:
                code = type(error).__name__
            logger.exception(
                "could not complete the Proton sign-in for %s (code %s)",
                email,
                code,
            )
            GLib.idle_add(self._on_login_failed, error, generation)
        else:
            GLib.idle_add(
                self._on_login_done, credential, email, display_name, generation
            )

    def _is_stale(self, generation: int) -> bool:
        return self._cancelled or generation != self._generation

    def _on_login_done(
        self,
        credential: Credential,
        email: str,
        display_name: str,
        generation: int,
    ) -> bool:
        if self._is_stale(generation):
            return False
        try:
            account = self._db.save_account(
                email=email,
                display_name=display_name,
                imap_host=PROTON_HOST,
                imap_port=PROTON_PORT,
                smtp_host=PROTON_HOST,
                smtp_port=PROTON_PORT,
                protocol=PROTOCOL_PROTON,
            )
            secrets.store_proton_session(account.id, credential)
        except Exception as error:
            logger.exception("could not save the Proton account %s", email)
            _is_auth_failure, message = errors.classify(error, PROTON_HOST)
            self._toast(message)
            self._back_to_form()
            return False
        self.emit("account-added")
        self.close()
        return False

    def _on_login_failed(self, error: Exception, generation: int) -> bool:
        if self._is_stale(generation):
            return False
        self._back_to_form()
        if isinstance(error, ProtonError) and error.code in _RETRYABLE_TOTP:
            self._toast(
                _("That code didn't work. It may have expired; enter the current one.")
            )
            self.totp_row.set_text("")
            self.totp_row.grab_focus()
            return False
        _is_auth_failure, message = errors.classify(error, PROTON_HOST)
        self._toast(message)
        return False

    def _on_progress_cancelled(self, _button: Gtk.Button) -> None:
        # Unlike the browser flows there is nothing outside the dialog to
        # abandon: the attempt is orphaned and the form keeps what was typed.
        self._generation += 1
        self.dialog_stack.set_visible_child_name(PAGE_FORM)

    def _back_to_form(self) -> None:
        self.dialog_stack.set_visible_child_name(PAGE_FORM)

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))
