"""Native Proton Mail support, reserved but not implemented.

Proton has no IMAP endpoint of its own -- the Bridge the README describes is a
separate paid product that translates on localhost. A native integration talks
to Proton's HTTPS API directly, with keys unlocked on this device:

- `go-proton-api <https://github.com/ProtonMail/go-proton-api>`_ for auth
  (SRP), messages, labels and the event loop,
- `gopenpgp <https://github.com/ProtonMail/gopenpgp>`_ for message encryption
  and signature verification,
- `go-srp <https://github.com/ProtonMail/go-srp>`_ for the password proof.

This module exists now so a Proton account row fails closed: mail_sync raises
instead of falling through to the IMAP path and dialling a meaningless host.
Tracked in `#7 <https://github.com/tuna-os/postcard/issues/7>`_.
"""

MECHANISM_PROTON = "proton-session"


class ProtonError(Exception):
    def __init__(self, code: str) -> None:
        # Error codes come from a fixed vocabulary, never server text.
        self.code = code
        super().__init__("The Proton Mail operation could not be completed.")


ERROR_CODES = frozenset({"unsupported_operation", "request_failed"})
