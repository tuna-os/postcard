import logging
import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, GObject, Gtk

from .core import secrets
from .core.models.account import PROTOCOL_TUTA, TUTA_HOST, TUTA_PORT
from .core.net import errors, tuta_session
from .core.net.auth import Credential
from .core.net.tuta_session import TutaError
from .core.store.database import Database

logger = logging.getLogger(__name__)

PAGE_FORM = "form"
PAGE_PROGRESS = "progress"
PAGE_TOTP = "totp"

# Second-factor failures that deserve another code, not the form: the code
# may simply have rotated in the thirty seconds it took to type it.
_RETRYABLE_TOTP = frozenset({"invalid_totp", "second_factor_failed"})


@Gtk.Template(resource_path="/in/gxanshu/postcard/ui/tuta-dialog.ui")
class PostcardTutaDialog(Adw.Dialog):
    __gtype_name__ = "PostcardTutaDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    dialog_stack: Gtk.Stack = Gtk.Template.Child()
    email_row: Adw.EntryRow = Gtk.Template.Child()
    display_name_row: Adw.EntryRow = Gtk.Template.Child()
    password_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    cancel_button: Gtk.Button = Gtk.Template.Child()
    sign_in_button: Gtk.Button = Gtk.Template.Child()
    progress_cancel_button: Gtk.Button = Gtk.Template.Child()
    totp_row: Adw.EntryRow = Gtk.Template.Child()
    totp_verify_button: Gtk.Button = Gtk.Template.Child()
    totp_cancel_button: Gtk.Button = Gtk.Template.Child()

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
        # The in-flight sign-in, kept in memory only so the TOTP retry can
        # re-run the login. Never written anywhere; cleared on cancel.
        self._pending: tuple[str, str, str] | None = None

        self.cancel_button.connect("clicked", lambda _b: self.close())
        self.progress_cancel_button.connect("clicked", self._on_progress_cancelled)
        self.totp_cancel_button.connect("clicked", self._on_totp_cancelled)
        self.connect("closed", self._on_closed)
        self.email_row.connect("changed", self._update_sign_in_sensitivity)
        self.password_row.connect("changed", self._update_sign_in_sensitivity)
        self.totp_row.connect("changed", self._update_verify_sensitivity)
        self.sign_in_button.connect("clicked", self._on_sign_in_clicked)
        self.totp_verify_button.connect("clicked", self._on_verify_clicked)
        self._update_sign_in_sensitivity()
        self._update_verify_sensitivity()

    def _on_closed(self, *_args: object) -> None:
        # A worker still running drops its result instead of saving an
        # account for a dialog the user dismissed.
        self._cancelled = True
        self._generation += 1
        self._pending = None

    def _update_sign_in_sensitivity(self, *_args: object) -> None:
        # Any domain is accepted: Tuta hosts custom-domain addresses, and the
        # provider was chosen by opening this dialog, not inferred.
        self.sign_in_button.set_sensitive(
            "@" in self.email_row.get_text().strip()
            and bool(self.password_row.get_text())
        )

    def _update_verify_sensitivity(self, *_args: object) -> None:
        self.totp_verify_button.set_sensitive(
            tuta_session.is_valid_totp(self.totp_row.get_text().strip())
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
        display_name = (
            self.display_name_row.get_text().strip() or email.partition("@")[0]
        )
        self._start_login(email, password, display_name, totp=None)

    def _on_verify_clicked(self, _button: Gtk.Button) -> None:
        code = self.totp_row.get_text().strip()
        if not tuta_session.is_valid_totp(code):
            # Belt-and-braces; the insensitive button already guards this.
            return
        if self._pending is None:
            self.dialog_stack.set_visible_child_name(PAGE_FORM)
            return
        email, password, display_name = self._pending
        self._start_login(email, password, display_name, totp=code)

    def _start_login(
        self, email: str, password: str, display_name: str, *, totp: str | None
    ) -> None:
        self.dialog_stack.set_visible_child_name(PAGE_PROGRESS)
        self._generation += 1
        self._pending = (email, password, display_name)
        threading.Thread(
            target=self._login_worker,
            args=(email, password, display_name, totp, self._generation),
            daemon=True,
        ).start()

    # Runs on the worker thread: network only, no Gtk/database access. The
    # helper runs the KDF and unlocks the keys, which is the slow part.
    def _login_worker(
        self,
        email: str,
        password: str,
        display_name: str,
        totp: str | None,
        generation: int,
    ) -> None:
        try:
            credential = tuta_session.login(email, password, totp)
        except Exception as error:
            logger.exception("could not complete the Tuta sign-in for %s", email)
            GLib.idle_add(self._on_login_failed, error, generation, totp is not None)
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
                imap_host=TUTA_HOST,
                imap_port=TUTA_PORT,
                smtp_host=TUTA_HOST,
                smtp_port=TUTA_PORT,
                protocol=PROTOCOL_TUTA,
            )
            secrets.store_tuta_session(account.id, credential)
        except Exception as error:
            logger.exception("could not save the Tuta account %s", email)
            _is_auth_failure, message = errors.classify(error, TUTA_HOST)
            self._toast(message)
            self._back_to_form()
            return False
        self._pending = None
        self.emit("account-added")
        self.close()
        return False

    def _on_login_failed(
        self, error: Exception, generation: int, for_totp: bool
    ) -> bool:
        if self._is_stale(generation):
            return False
        if isinstance(error, TutaError) and for_totp and error.code in _RETRYABLE_TOTP:
            self._toast(
                _("That code didn't work. It may have expired; enter the current one.")
            )
            self._show_totp_page()
            return False
        if (
            isinstance(error, TutaError)
            and not for_totp
            and error.code == "second_factor_required"
            and self._pending is not None
        ):
            self._show_totp_page()
            return False
        _is_auth_failure, message = errors.classify(error, TUTA_HOST)
        self._toast(message)
        self._back_to_form()
        return False

    def _show_totp_page(self) -> None:
        self.totp_row.set_text("")
        self.dialog_stack.set_visible_child_name(PAGE_TOTP)
        self.totp_row.grab_focus()

    def _on_progress_cancelled(self, _button: Gtk.Button) -> None:
        # Unlike the browser flows there is nothing outside the dialog to
        # abandon: the attempt is orphaned and the form keeps what was typed.
        self._generation += 1
        self._pending = None
        self.dialog_stack.set_visible_child_name(PAGE_FORM)

    def _on_totp_cancelled(self, _button: Gtk.Button) -> None:
        self._generation += 1
        self._pending = None
        self.dialog_stack.set_visible_child_name(PAGE_FORM)

    def _back_to_form(self) -> None:
        self._pending = None
        self.dialog_stack.set_visible_child_name(PAGE_FORM)

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))
