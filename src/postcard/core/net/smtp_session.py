import logging
import smtplib
from contextlib import suppress

from . import NET_TIMEOUT_SECONDS, ssl_context_for
from .auth import MECHANISM_LOGIN, MECHANISM_XOAUTH2, Credential, xoauth2_response

logger = logging.getLogger(__name__)


class SmtpError(Exception):
    """Raised when talking to the server fails (bad login, dropped link, ..)

    unknown names the recipients whose delivery state is unknowable: the
    link died mid-send, so the server may have taken the message for some
    of them before it went. Retrying one sends to all of them again.
    """

    def __init__(self, message: str = "", *, unknown: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.unknown = unknown


class SmtpRefusedError(SmtpError):
    """The server answered, refusing the message for some recipients.

    refused is who to retry; accepted already got it and must never be
    re-sent. Total when accepted is empty, partial otherwise. The message
    names addresses only, never the server's verbatim reason: that text is
    diagnostics, logged at the boundary, not user-facing copy.
    """

    def __init__(
        self,
        refused: tuple[str, ...],
        accepted: tuple[str, ...] = (),
        unknown: tuple[str, ...] = (),
    ) -> None:
        if not refused:
            raise ValueError("a refusal names at least one refused recipient")
        self.refused = refused
        self.accepted = accepted
        message = f"the server refused these recipients: {', '.join(refused)}"
        if accepted:
            message += f" (already sent to {', '.join(accepted)})"
        super().__init__(message, unknown=unknown)


class SmtpSession:
    def __init__(self, host: str, port: int, security: str = "tls") -> None:
        self._host = host
        self._port = port
        self._security = security
        self._smtp: smtplib.SMTP | None = None

    def connect(self) -> None:
        context = ssl_context_for(self._host)
        if self._security == "starttls":
            smtp = smtplib.SMTP(self._host, self._port, timeout=NET_TIMEOUT_SECONDS)
            self._smtp = smtp
            try:
                smtp.starttls(context=context)
            except Exception:
                # Fail closed: a STARTTLS the server refused, or a handshake
                # that failed (a wrong-host certificate among it), must never
                # leave a plaintext session behind for sign_in to spill the
                # password over. The original error propagates untouched, so
                # errors.classify still words an ssl.SSLError as a
                # secure-connection failure.
                with suppress(Exception):
                    smtp.close()
                self._smtp = None
                raise
        else:
            self._smtp = smtplib.SMTP_SSL(
                self._host, self._port, timeout=NET_TIMEOUT_SECONDS, context=context
            )

    def sign_in(self, credential: Credential) -> None:
        smtp = self._require_smtp()
        try:
            if credential.mechanism == MECHANISM_XOAUTH2:
                # login() greets the server on our behalf; auth() does not, and
                # SMTP_SSL has not greeted it either by this point.
                smtp.ehlo_or_helo_if_needed()
                smtp.auth(
                    "XOAUTH2",
                    lambda _challenge=None: xoauth2_response(
                        credential.user, credential.secret
                    ),
                    initial_response_ok=True,
                )
            elif credential.mechanism == MECHANISM_LOGIN:
                smtp.login(credential.user, credential.secret)
            else:
                raise SmtpError(f"unsupported mechanism {credential.mechanism}")
        except smtplib.SMTPException as error:
            raise SmtpError(str(error)) from error

    def send_raw(self, from_addr: str, recipients: list[str], raw: bytes) -> None:
        try:
            refused = self._require_smtp().sendmail(from_addr, recipients, raw)
        except smtplib.SMTPException as error:
            raise SmtpError(
                f"could not send to {', '.join(recipients)}: {error}",
                unknown=tuple(recipients),
            ) from error
        if refused:
            # sendmail reports per-recipient refusals by returning them, not by
            # raising: an empty dict is the only proof every recipient took it.
            # The reasons stay in the log; the exception names addresses only,
            # since its message reaches the user verbatim through classify().
            for address, (code, response) in refused.items():
                logger.warning(
                    "SMTP server refused %s: %s %s",
                    address,
                    code,
                    response.decode("ascii", "replace").strip(),
                )
            raise SmtpRefusedError(
                refused=tuple(refused),
                accepted=tuple(
                    address for address in recipients if address not in refused
                ),
            )

    def _require_smtp(self) -> smtplib.SMTP:
        # Never return None: `if self._smtp is not None` around the sendmail
        # call meant a session that never connected returned normally, and
        # mail_sync.send_message reported the mail as sent.
        if self._smtp is None:
            raise SmtpError(f"not connected to {self._host}:{self._port}")
        return self._smtp

    def quit(self) -> None:
        # Same contract as ImapSession.logout: called from a `finally:`, so it
        # must never raise over the top of the real error.
        try:
            if self._smtp is not None:
                self._smtp.quit()
        except Exception:
            logger.debug("SMTP quit to %s failed", self._host, exc_info=True)
