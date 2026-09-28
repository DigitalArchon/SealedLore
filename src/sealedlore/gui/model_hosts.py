"""A model's hosts, fetched for the route dialog (providers/model_hosts.py).

One app-wide catalog owns the threads, as `AttestationTests` does: a dialog
closed while its hosts were loading must not take a running thread down with
it. Each model's hosts are kept for an hour.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from sealedlore.gui.worker import GenerationWorker
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
        self._running: dict[tuple[str, str], tuple[QThread, GenerationWorker]] = {}

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
        result: list[ModelHosts | str] = []

        def job():
            try:
                result.append(fetch_model_hosts(base_url, model))
            except ProviderError as exc:
                result.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - shown, never fatal
                result.append(f"couldn't list the hosts: {exc}")
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        # Cleanup on the thread, never the worker's own finished.
        thread.finished.connect(lambda: self._done(key, result))
        self._running[key] = (thread, worker)
        thread.start()

    def _done(self, key: tuple[str, str], result: list) -> None:
        self._running.pop(key, None)
        found = result[0] if result else "no answer"
        if isinstance(found, ModelHosts):
            self._found[key] = (time.monotonic(), found)
            self.loaded.emit(*key)
        else:
            self.failed.emit(key[0], key[1], found)

    def wait(self, msecs: int = 35_000) -> None:
        """Before the app closes: a QThread destroyed while running takes it down."""
        for thread, _worker in list(self._running.values()):
            thread.wait(msecs)


_catalog: HostsCatalog | None = None


def hosts_catalog() -> HostsCatalog:
    """The app's one catalog, parented to the application."""
    global _catalog
    if _catalog is None:
        _catalog = HostsCatalog(QCoreApplication.instance())
    return _catalog
