"""Hosts the author marked bad for a model (Help → Model trouble).

One app-wide store, like the hosts catalog, so the route dialog can show them
wherever it is opened from. The window attaches its config and a way to save
it. A mark only keeps a host from being chosen by hand: NanoGPT's routing may
still use it, since sending anything to exclude a host bills the call
pay-as-you-go, even on the subscription (checked Sept 30 2026: a GLM 5.3
request with `ignore` cost $0.0004 where the plain one cost $0).

A memory-only chat's marks are kept for the session only: the config learns
nothing from such a chat, not even which models it used.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QObject, Signal

from sealedlore.ids import utc_now_iso
from sealedlore.models.config import BadHost, Config


class BadHosts(QObject):
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config: Config | None = None
        self._save: Callable[[], None] | None = None
        self._session: dict[str, dict[str, BadHost]] = {}
        self.keep = True  # False while a memory-only chat is open

    def attach(self, config: Config, save: Callable[[], None]) -> None:
        self._config, self._save = config, save
        self.changed.emit()

    def marked(self, model: str) -> dict[str, BadHost]:
        """The model's bad hosts, by host id."""
        found = dict(self._config.bad_hosts.get(model, {})) if self._config else {}
        found.update(self._session.get(model, {}))
        return found

    def mark(self, model: str, host: str, reason: str) -> None:
        entry = BadHost(reason=reason, at=utc_now_iso())
        if self.keep and self._config is not None:
            self._config.bad_hosts.setdefault(model, {})[host] = entry
            if self._save is not None:
                self._save()
        else:
            self._session.setdefault(model, {})[host] = entry
        self.changed.emit()

    def allow(self, model: str, host: str) -> None:
        self._session.get(model, {}).pop(host, None)
        if self._config is not None and host in self._config.bad_hosts.get(model, {}):
            del self._config.bad_hosts[model][host]
            if not self._config.bad_hosts[model]:
                del self._config.bad_hosts[model]
            if self._save is not None:
                self._save()
        self.changed.emit()

    def forget_session(self) -> None:
        """When the memory-only chat that made them closes."""
        if self._session:
            self._session.clear()
            self.changed.emit()


_store: BadHosts | None = None


def bad_hosts() -> BadHosts:
    """The app's one store, parented to the application."""
    global _store
    if _store is None:
        _store = BadHosts(QCoreApplication.instance())
    return _store
