"""Which drivers exist, and which provider id each one serves.

Builtins are registered in code. Anything else arrives as an installed Python
distribution advertising an entry point under ``postcard.providers``, and
loads only when its id is named in the trust file -- metadata first, code
only for the trusted. Every lookup failure fails closed: an unknown,
disabled or incompatible provider raises, and no caller may fall back to
IMAP/SMTP on seeing one.
"""

import importlib.metadata
import logging
import os
from collections.abc import Iterable

from gi.repository import GLib

from ..models.account import (
    PROTOCOL_GRAPH,
    PROTOCOL_IMAP,
    PROTOCOL_JMAP,
    PROTOCOL_PROTON,
    PROTOCOL_TUTA,
)
from .api import (
    ENTRY_POINT_GROUP,
    PROVIDER_API_VERSION,
    TRUST_FILE_NAME,
    DisabledProviderError,
    DuplicateProviderError,
    IncompatibleProviderError,
    ProviderDescriptor,
    UnknownProviderError,
)
from .driver import ProviderDriver

logger = logging.getLogger(__name__)

# The ids accounts may already store. An add-on reusing one is rejected:
# a duplicate can never replace a builtin.
BUILTIN_IDS = frozenset(
    {PROTOCOL_IMAP, PROTOCOL_GRAPH, PROTOCOL_TUTA, PROTOCOL_PROTON, PROTOCOL_JMAP}
)


def default_trust_path() -> str:
    """Where the trust file lives when no path is given: beside the database."""
    return os.path.join(GLib.get_user_data_dir(), "postcard", TRUST_FILE_NAME)


def read_trust_list(path: str) -> frozenset[str]:
    """The provider ids trusted in this file, one per line.

    Blank lines and ``#`` comments are ignored. A missing or unreadable file
    trusts nothing rather than failing: add-ons stay disabled, builtins are
    unaffected. The file names ids only -- never code, paths or URLs -- so
    there is nothing in it to execute, download or auto-load.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeError):
        logger.warning("could not read the provider trust file at %s", path)
        return frozenset()
    return frozenset(
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    )


def _driver_from(point: importlib.metadata.EntryPoint) -> ProviderDriver:
    """Import one entry point's driver: a ProviderDriver instance or a no-arg
    class returning one. Anything else is a load failure, not a driver."""
    loaded = point.load()
    driver = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(driver, ProviderDriver):
        raise TypeError(
            f"{point.value} is not a ProviderDriver (got {type(driver).__name__})"
        )
    return driver


class ProviderRegistry:
    """The drivers, by provider id.

    Register builtins first, then call ``discover`` once at startup: trust is
    read then, so trusting another provider takes a restart to take effect.
    ``load_errors`` records, per add-on name, why it did not load; one broken
    add-on never stops the rest from loading.
    """

    def __init__(self) -> None:
        self._drivers: dict[str, ProviderDriver] = {}
        self._disabled: set[str] = set()
        self._incompatible: set[str] = set()
        self.load_errors: dict[str, str] = {}

    @property
    def registered_ids(self) -> frozenset[str]:
        return frozenset(self._drivers)

    def register(self, driver: ProviderDriver) -> None:
        """Serve this driver's id from now on. A taken id raises instead of
        replacing: duplicates lose, whoever registers second."""
        descriptor = driver.descriptor
        if not descriptor.id:
            raise ValueError("a provider driver needs a non-empty id")
        if descriptor.id in self._drivers:
            raise DuplicateProviderError(
                f"mail provider '{descriptor.id}' is already registered"
            )
        self._drivers[descriptor.id] = driver

    def driver_for(self, protocol: str) -> ProviderDriver:
        """The driver serving this provider id, or a fail-closed error."""
        if protocol in self._drivers:
            return self._drivers[protocol]
        if protocol in self._disabled:
            raise DisabledProviderError(
                f"Mail provider '{protocol}' is installed but not trusted. "
                "Trust it and restart to sync this account."
            )
        if protocol in self._incompatible:
            raise IncompatibleProviderError(
                f"Mail provider '{protocol}' speaks another version of the "
                "provider API and cannot load."
            )
        raise UnknownProviderError(
            f"Mail provider '{protocol}' is not available, so this account "
            "cannot sync. Its stored mail stays where it is."
        )

    def descriptor_for(self, protocol: str) -> ProviderDescriptor | None:
        """The driver's descriptor, or None when no driver serves this id.

        For the UI, which disables what it cannot describe: a missing
        descriptor means nothing is supported, never a fallback guess.
        """
        try:
            return self.driver_for(protocol).descriptor
        except (UnknownProviderError, DisabledProviderError, IncompatibleProviderError):
            return None

    def discover(
        self,
        trusted_ids: frozenset[str] | None = None,
        entry_points: Iterable[importlib.metadata.EntryPoint] | None = None,
    ) -> None:
        """Load trusted add-on drivers into this registry, in place.

        ``trusted_ids`` defaults to the trust file beside the database;
        ``entry_points`` defaults to the installed distributions' advertised
        drivers and exists only so tests can hand over fakes. Discovery
        itself never executes add-on code: only an entry point whose name is
        trusted gets imported, and then in isolation -- an import or version
        failure is recorded under its name and the rest still load.
        """
        if trusted_ids is None:
            trusted_ids = read_trust_list(default_trust_path())
        if entry_points is None:
            try:
                entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
            except Exception:
                logger.warning("could not list installed mail providers", exc_info=True)
                return
        for point in entry_points:
            self._load_one(point, trusted_ids)

    def _load_one(
        self,
        point: importlib.metadata.EntryPoint,
        trusted_ids: frozenset[str],
    ) -> None:
        name = point.name
        if name in BUILTIN_IDS or name in self._drivers:
            self.load_errors[name] = (
                f"mail provider '{name}' is already provided; "
                "a duplicate cannot replace it"
            )
            return
        if name not in trusted_ids:
            # Not imported: listing an entry point reads installed metadata,
            # and untrusted code stays exactly that -- listed, never run.
            self._disabled.add(name)
            return
        try:
            driver = _driver_from(point)
            descriptor = driver.descriptor
        except Exception as error:
            logger.warning(
                "could not load mail provider '%s': %s",
                name,
                error,
                exc_info=True,
            )
            self.load_errors[name] = f"could not load mail provider '{name}': {error}"
            return
        if descriptor.id != name:
            self.load_errors[name] = (
                f"mail provider '{name}' served the id '{descriptor.id}'; "
                "trust was granted to its own name, so it cannot load"
            )
            return
        if not descriptor.is_compatible_with(PROVIDER_API_VERSION):
            self._incompatible.add(name)
            self.load_errors[name] = (
                f"mail provider '{name}' speaks API versions "
                f"{descriptor.min_api_version}-{descriptor.max_api_version}; "
                f"this build speaks {PROVIDER_API_VERSION}"
            )
            return
        self._drivers[name] = driver
