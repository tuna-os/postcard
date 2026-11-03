import logging
from collections.abc import Callable
from gettext import gettext as _

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib

from .composer_window import composer_for_mailto
from .core import APP_ID, secrets
from .core.net import errors
from .core.store.database import CacheError, Database
from .preferences_dialog import PostcardPreferencesDialog
from .tray import Tray
from .window import PostcardMainWindow

logger = logging.getLogger(__name__)

MAILTO_SCHEME = "mailto:"


class PostcardApplication(Adw.Application):
    __gtype_name__ = "PostcardApplication"

    def __init__(self, version: str) -> None:
        super().__init__(
            application_id=APP_ID,
            # HANDLES_OPEN so the desktop can hand us mailto: links.
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
            resource_base_path="/in/gxanshu/postcard",
        )
        self.version = version
        self.db: Database | None
        self._db_error: CacheError | None = None
        self._cache_alert_open = False
        try:
            self.db = Database(key=secrets.database_key())
        except CacheError as error:
            # A locked keyring waits for the user on first activation; the app
            # never falls back to a plaintext cache.
            logger.warning("could not open the mail cache: %s", error.code)
            self.db = None
            self._db_error = error
        self.settings = Gio.Settings(schema_id=APP_ID)
        self._should_start_hidden = False

        self.tray = Tray(self)

        # For autostart: build the window (so the sync timer runs) but skip
        # presenting it. The Background portal puts this flag in the autostart
        # entry it writes -- see "Start at Login" in preferences_dialog.py.
        self.add_main_option(
            "hidden",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            _("Start in the background without showing a window"),
            None,
        )

        self._create_action("about", self.on_about_action)
        self._create_action(
            "preferences", self.on_preferences_action, ["<control>comma"]
        )
        self._create_action(
            "new-window", self.on_new_window_action, ["<control><shift>n"]
        )
        self._create_action("quit", lambda *_: self.quit(), ["<control>q"])
        self._create_action("focus-mail", lambda *_: self.do_activate())
        self._create_action(
            "open-mail", self.on_open_mail, param_type=GLib.VariantType.new("(is)")
        )

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.tray.start()
        self.settings.connect(
            "changed::run-in-background", self._on_run_in_background_changed
        )
        self.tray.set_shown(self.settings.get_boolean("run-in-background"))

    def _on_run_in_background_changed(self, settings: Gio.Settings, key: str) -> None:
        self.tray.set_shown(settings.get_boolean(key))

    def _main_window(self) -> PostcardMainWindow | None:
        return next(
            (w for w in self.get_windows() if isinstance(w, PostcardMainWindow)), None
        )

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        self._should_start_hidden = options.contains("hidden")
        return Adw.Application.do_handle_local_options(self, options)

    def do_activate(self) -> None:
        db = self.db
        if db is None:
            self._show_cache_error()
            return
        win = self._main_window() or PostcardMainWindow(self, db, self.settings)
        if self._should_start_hidden:
            # Only the launch activation stays hidden; later ones raise it.
            self._should_start_hidden = False
            return
        win.present()

    def _show_cache_error(self) -> None:
        assert self._db_error is not None
        if not self._cache_alert_open:
            # A parentless alert holds nothing itself; without this the
            # application quits the moment after showing it.
            self.hold()
            self._cache_alert_open = True
        _is_auth_failure, message = errors.classify(self._db_error, "")
        dialog = Adw.AlertDialog(
            heading=_("Could Not Open the Mail Cache"), body=message
        )
        dialog.add_response("retry", _("Retry"))
        dialog.add_response("quit", _("Quit"))
        dialog.set_default_response("retry")
        dialog.set_close_response("quit")
        dialog.choose(self.props.active_window, None, self._on_cache_alert_done)

    def _on_cache_alert_done(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult
    ) -> None:
        if dialog.choose_finish(result) != "retry":
            self._cache_alert_open = False
            self.release()
            self.quit()
            return
        try:
            self.db = Database(key=secrets.database_key())
        except CacheError as error:
            logger.warning("could not open the mail cache: %s", error.code)
            self._db_error = error
            self._show_cache_error()
            return
        self._cache_alert_open = False
        self.do_activate()
        self.release()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:
        for file in files:
            uri = file.get_uri()
            if uri.lower().startswith(MAILTO_SCHEME):
                self._open_mailto(uri)
            else:
                logger.warning("ignoring unsupported URI %s", uri)

    # A mailto: link opens the composer and nothing else; the main window is
    # only presented when there is no account to compose from.
    def _open_mailto(self, uri: str) -> None:
        db = self.db
        if db is None:
            self.do_activate()
            return
        win = self._main_window()
        if win is not None:
            win.open_mailto(uri)
            return
        accounts = db.accounts()
        if not accounts:
            logger.warning("no account to compose %s from, opening the window", uri)
            self.do_activate()
            return
        composer_for_mailto(self, db, accounts[0], self.settings, uri).present()

    # Register an app.<name> action, optionally with keyboard accelerators.
    def _create_action(
        self,
        name: str,
        callback: Callable[..., None],
        shortcuts: list[str] | None = None,
        param_type: GLib.VariantType | None = None,
    ) -> None:
        action = Gio.SimpleAction.new(name, param_type)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    MAINTAINERS = [
        "Anshu Meena https://github.com/gxanshu",
    ]

    def on_about_action(self, *_args: object) -> None:
        about = Adw.AboutDialog.new_from_appdata(
            "/in/gxanshu/postcard/metainfo.xml", self.version
        )
        about.set_translator_credits(_("translator-credits"))
        about.set_developers(self.MAINTAINERS)
        about.set_copyright("© 2026 Anshu")
        about.present(self.props.active_window)

    def on_preferences_action(self, *_args: object) -> None:
        dialog = PostcardPreferencesDialog(self.settings)
        dialog.present(self.props.active_window)

    def on_open_mail(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        self.do_activate()
        win = self._main_window()
        if win is not None:
            folder_id, uid = param.unpack()
            win.open_email(folder_id, uid)

    def on_new_window_action(self, *_args: object) -> None:
        if self.db is not None:
            PostcardMainWindow(self, self.db, self.settings).present()
