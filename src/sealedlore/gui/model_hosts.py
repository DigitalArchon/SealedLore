"""A model's hosts, fetched for the route dialog (providers/model_hosts.py).

One app-wide catalog owns the fetches, as `AttestationTests` does: on daemon
threads nobody waits for (gui/workers.run_detached), so neither a dialog
closed while its hosts were loading nor quitting waits on one. Each model's
hosts are kept for an hour.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject, Signal

from sealedlore.gui.workers import run_detached
from sealedlore.providers.base import ProviderError
from sealedlore.providers.model_hosts import ModelHosts, fetch_model_hosts

MAX_AGE_SECONDS = 3600


class HostsCatalog(QObject):
    # (base URL, model)
    loaded = Signal(str, str)
    # (base URL, model, why)
    failed = Signal(str, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._found: dict[tuple[str, str], tuple[float, ModelHosts]] = {}
        self._running: set[tuple[str, str]] = set()

    def hosts(self, base_url: str, model: str) -> ModelHosts | None:
        """What was fetched in the last hour, or None."""
        found = self._found.get((base_url, model))
        if found is None or time.monotonic() - found[0] > MAX_AGE_SECONDS:
            return None
        return found[1]

    def loading(self, base_url: str, model: str) -> bool:
        return (base_url, model) in self._running

    def ensure(self, base_url: str, model: str) -> None:
        """Fetch unless kept or already on its way; `loaded` or `failed` follows."""
        key = (base_url, model)
        if self.hosts(base_url, model) is not None or key in self._running:
            return

        def job() -> ModelHosts | str:
            try:
                return fetch_model_hosts(base_url, model)
            except ProviderError as exc:
                return str(exc)
            except Exception as exc:  # noqa: BLE001 - shown, never fatal
                return f"couldn't list the hosts: {exc}"

        self._running.add(key)
        run_detached(self, job, lambda found, _error: self._done(key, found), name="model-hosts")

    def _done(self, key: tuple[str, str], found: ModelHosts | str | None) -> None:
        self._running.discard(key)
        found = found if found is not None else "no answer"
        if isinstance(found, ModelHosts):
            self._found[key] = (time.monotonic(), found)
            self.loaded.emit(*key)
        else:
            self.failed.emit(key[0], key[1], found)


_catalog: HostsCatalog | None = None


def hosts_catalog() -> HostsCatalog:
    """The app's one catalog, parented to the application."""
    global _catalog
    if _catalog is None:
        _catalog = HostsCatalog(QCoreApplication.instance())
    return _catalog
