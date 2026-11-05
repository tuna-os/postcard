"""Credential storage on the xdg-desktop-portal Secret API (prototype).

NOT WIRED IN: nothing imports this module. It answers issue #68 -- whether
the app can drop --talk-name=org.freedesktop.secrets by keeping its secrets
in an encrypted file keyed by org.freedesktop.portal.Secret, the oo7
file-backend pattern. Findings and the recommendation live in
docs/secret-portal-spike.md.

The portal hands out one opaque per-app master secret, not per-item storage,
so a portal client pairs with a file store that seals a dict of secrets with
a caller-supplied cipher. No cipher ships here on purpose: wiring this needs
a real AEAD, and a placeholder would be worse than none.

Over the 200-line core target on purpose: the spike lane allows exactly one
module, so transport and store share it. Wiring must split them.
"""

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets as stdlib_secrets
from typing import NamedTuple, Protocol

from gi.repository import Gio, GLib

from .net import NET_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SECRET_INTERFACE = "org.freedesktop.portal.Secret"  # noqa: S105
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
PORTAL_SECRET_VERSION = 1

_RESPONSE_OK = 0
_DB_KEY_BYTES = 32
_DB_KEY_PURPOSE = "mail-cache"
_SALT_BYTES = 16
_MAX_SECRET_BYTES = 65536


class PortalSecretError(Exception):
    """The portal or the sealed file gave nothing usable; fail closed."""


class CipherError(Exception):
    """The sealed payload did not open; the file is damaged or foreign."""


class MasterSecret(NamedTuple):
    secret: bytes
    token: str | None


class PortalTransport(Protocol):
    """One portal call: the master secret plus the token for the next one."""

    def retrieve_secret(self, token: str | None) -> MasterSecret: ...


class Cipher(Protocol):
    """An authenticated cipher the caller trusts. open() raises CipherError
    when the payload does not verify."""

    def seal(self, key: bytes, plaintext: bytes) -> bytes: ...
    def open(self, key: bytes, sealed: bytes) -> bytes: ...


class DBusPortalTransport:
    """The live portal client, over the same Gio D-Bus calls as goa.py.

    Not yet exercised against a real portal: that needs a sandboxed run on a
    desktop whose backend implements org.freedesktop.impl.portal.Secret.
    """

    def __init__(self, timeout_ms: int = NET_TIMEOUT_SECONDS * 1000) -> None:
        self._timeout_ms = timeout_ms

    def retrieve_secret(self, token: str | None) -> MasterSecret:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error as error:
            raise PortalSecretError("could not reach the session bus") from error
        handle_token = "postcard_secret_" + GLib.uuid_string_random().replace("-", "_")
        read_fd, write_fd = os.pipe()
        return self._exchange(bus, read_fd, write_fd, token, handle_token)

    def _exchange(
        self,
        bus: Gio.DBusConnection,
        read_fd: int,
        write_fd: int,
        token: str | None,
        handle_token: str,
    ) -> MasterSecret:
        """One Request round trip, reading the secret off the pipe. Closes
        both ends of it; the portal holds its own copy of the write end once
        the call is out, and ours must close for the read end to see EOF."""
        sender = (bus.get_unique_name() or "").removeprefix(":").replace(".", "_")
        request_path = f"{PORTAL_OBJECT_PATH}/request/{sender}/{handle_token}"
        context = GLib.MainContext.new()
        context.push_thread_default()
        try:
            outcome: dict[str, object] = {}
            loop = GLib.MainLoop(context)

            def _on_response(
                _bus: Gio.DBusConnection,
                _sender: str | None,
                _path: str,
                _interface: str,
                _signal: str,
                parameters: GLib.Variant,
                _data: object,
            ) -> None:
                response, results = parameters.unpack()
                outcome["response"] = response
                outcome["results"] = results
                loop.quit()

            def _on_timeout() -> bool:
                loop.quit()
                return False

            subscription = bus.signal_subscribe(
                PORTAL_BUS_NAME,
                REQUEST_INTERFACE,
                "Response",
                request_path,
                None,
                Gio.DBusSignalFlags.NONE,
                _on_response,
                None,
            )
            timeout = GLib.timeout_add(self._timeout_ms, _on_timeout)
            try:
                self._request_secret(write_fd, token, handle_token)
                loop.run()
            finally:
                os.close(write_fd)
                bus.signal_unsubscribe(subscription)
                GLib.source_remove(timeout)
            try:
                if "response" not in outcome:
                    raise PortalSecretError("the Secret portal request timed out")
                response = outcome["response"]
                results = outcome["results"]
                if response != _RESPONSE_OK or not isinstance(results, dict):
                    raise PortalSecretError(
                        f"the Secret portal refused with response {response}"
                    )
                secret = _read_secret(read_fd)
            finally:
                os.close(read_fd)
            next_token = results.get("token")
            if not isinstance(next_token, str):
                next_token = None
            return MasterSecret(secret, next_token)
        finally:
            context.pop_thread_default()

    def _request_secret(
        self, write_fd: int, token: str | None, handle_token: str
    ) -> None:
        fd_list = Gio.UnixFDList.new()
        index = fd_list.append(write_fd)
        options: dict[str, GLib.Variant] = {
            "handle_token": GLib.Variant("s", handle_token)
        }
        if token is not None:
            options["token"] = GLib.Variant("s", token)
        parameters = GLib.Variant("(ha{sv})", (index, options))
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
            | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS,
            None,
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            SECRET_INTERFACE,
            None,
        )
        try:
            proxy.call_with_unix_fd_list_sync(
                "RetrieveSecret",
                parameters,
                Gio.DBusCallFlags.NONE,
                self._timeout_ms,
                fd_list,
                None,
            )
        except GLib.Error as error:
            raise PortalSecretError(
                f"could not request the portal secret: {error}"
            ) from error


def _read_secret(read_fd: int) -> bytes:
    chunks = []
    total = 0
    while chunk := os.read(read_fd, 4096):
        total += len(chunk)
        if total > _MAX_SECRET_BYTES:
            raise PortalSecretError(
                f"the portal secret exceeded {_MAX_SECRET_BYTES} bytes"
            )
        chunks.append(chunk)
    secret = b"".join(chunks)
    if not secret:
        raise PortalSecretError("the portal wrote an empty secret")
    return secret


class _Envelope(NamedTuple):
    salt: bytes
    token: str | None
    sealed: bytes


class PortalSecretStore:
    """Per-account passwords plus the cache key, sealed in one file.

    Every operation re-reads the file and re-retrieves the master secret, so
    concurrent writers last-write-wins; a mail client writes rarely enough
    that no locking is worth it here.
    """

    def __init__(
        self, path: str | os.PathLike[str], transport: PortalTransport, cipher: Cipher
    ) -> None:
        self._path = os.fspath(path)
        self._transport = transport
        self._cipher = cipher

    def store_password(self, account_id: int, password: str) -> None:
        secrets = self._load()
        secrets[_account_key(account_id)] = password
        self._save(secrets)

    def lookup_password(self, account_id: int) -> str | None:
        return self._load().get(_account_key(account_id))

    def clear_password(self, account_id: int) -> bool:
        secrets = self._load()
        key = _account_key(account_id)
        if key not in secrets:
            return False
        del secrets[key]
        self._save(secrets)
        return True

    def database_key(self) -> bytes:
        """The SQLCipher key, generating it once, mirroring secrets.py: a
        malformed stored key raises with the file left untouched."""
        secrets = self._load()
        stored = secrets.get(_DB_KEY_PURPOSE)
        if stored is None:
            key = stdlib_secrets.token_bytes(_DB_KEY_BYTES)
            secrets[_DB_KEY_PURPOSE] = key.hex()
            self._save(secrets)
            return key
        try:
            key = bytes.fromhex(stored)
        except ValueError:
            key = b""
        if len(key) != _DB_KEY_BYTES:
            logger.warning("the portal cache key is malformed; keeping it untouched")
            raise PortalSecretError(f"the cache key in {self._path} is malformed")
        return key

    def _load(self) -> dict[str, str]:
        envelope = self._read_envelope()
        master = self._retrieve(envelope.token if envelope else None)
        if envelope is None:
            return {}
        try:
            plaintext = self._cipher.open(
                _file_key(master.secret, envelope.salt), envelope.sealed
            )
        except CipherError as error:
            raise PortalSecretError(
                f"could not open the sealed secrets in {self._path}"
            ) from error
        return _decode_secrets(plaintext, self._path)

    def _save(self, secrets: dict[str, str]) -> None:
        envelope = self._read_envelope()
        master = self._retrieve(envelope.token if envelope else None)
        salt = envelope.salt if envelope else stdlib_secrets.token_bytes(_SALT_BYTES)
        key = _file_key(master.secret, salt)
        sealed = self._cipher.seal(key, json.dumps(secrets).encode("utf-8"))
        payload = json.dumps(
            {"salt": salt.hex(), "token": master.token, "sealed": sealed.hex()}
        ).encode("utf-8")
        tmp_path = f"{self._path}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError as error:
            raise PortalSecretError(
                f"could not write the sealed secrets to {tmp_path}"
            ) from error
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_path, self._path)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise PortalSecretError(
                f"could not write the sealed secrets to {self._path}"
            ) from error

    def _retrieve(self, token: str | None) -> MasterSecret:
        try:
            master = self._transport.retrieve_secret(token)
        except PortalSecretError:
            raise
        except Exception as error:
            raise PortalSecretError("could not retrieve the portal secret") from error
        return master

    def _read_envelope(self) -> _Envelope | None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise PortalSecretError(
                f"could not read the sealed secrets in {self._path}"
            ) from error
        return _parse_envelope(raw, self._path)


def _account_key(account_id: int) -> str:
    return f"account:{account_id}"


def _file_key(master: bytes, salt: bytes) -> bytes:
    """One HKDF-SHA256 expansion (RFC 5869); the portal secret is key material
    of unknown length, so it is never used as a cipher key directly."""
    pseudo_random = hmac.new(salt, master, hashlib.sha256).digest()
    info = b"postcard portal file key v1\x01"
    return hmac.new(pseudo_random, info, hashlib.sha256).digest()


def _parse_envelope(raw: object, path: str) -> _Envelope:
    if not isinstance(raw, dict):
        raise PortalSecretError(f"the sealed secrets in {path} are not an object")
    salt = _hex_field(raw, "salt", path)
    sealed = _hex_field(raw, "sealed", path)
    token = raw.get("token")
    if token is not None and not isinstance(token, str):
        raise PortalSecretError(f"the sealed secrets in {path} carry a bad token")
    if len(salt) != _SALT_BYTES:
        raise PortalSecretError(f"the sealed secrets in {path} carry a bad salt")
    return _Envelope(salt, token, sealed)


def _hex_field(fields: dict[str, object], name: str, path: str) -> bytes:
    value = fields.get(name)
    if not isinstance(value, str):
        raise PortalSecretError(f"the sealed secrets in {path} have no {name}")
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        raise PortalSecretError(f"the {name} in {path} is not hex") from None
    return decoded


def _decode_secrets(plaintext: bytes, path: str) -> dict[str, str]:
    try:
        raw = json.loads(plaintext.decode("utf-8"))
    except ValueError as error:
        raise PortalSecretError(f"the sealed secrets in {path} are damaged") from error
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise PortalSecretError(f"the sealed secrets in {path} are damaged")
    return raw
