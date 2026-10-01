"""Choosing a model from the endpoint's own list, instead of copying ids from a website.

`ModelCatalog` fetches the list off the GUI thread and keeps it for an hour per
endpoint. `ModelPickerDialog` shows it searchable and sortable; `ModelField` is
a text box with a Browse… button, used wherever a chat model is chosen. Typing
an id by hand still works — other endpoints, local servers, a model the list
leaves out.
"""

from __future__ import annotations

import inspect
import weakref
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.catalog import ModelInfo, chat_models, matches, short_count
from sealedlore.engine.routing import describe, routable, route_body
from sealedlore.gui.fields import line_edit, set_field_text
from sealedlore.gui.route_dialog import RouteDialog
from sealedlore.gui.worker import GenerationWorker
from sealedlore.models.config import ProviderConfig
from sealedlore.models.route import ModelRoute
from sealedlore.providers.base import ChatProvider
from sealedlore.providers.private_catalog import encrypted_counterpart, privacy_label
from sealedlore.providers.tee import is_tee

CATALOG_MAX_AGE = timedelta(hours=1)

# A provider to fetch with, the endpoint it talks to (the cache key), and
# whether it was made just for this fetch (and so is closed after); or a
# ValueError saying why there's none (no endpoint set, not https…).
ProviderSource = Callable[[], tuple[ChatProvider, str, bool]]
# Opens the picker for a field's current text; the chosen id, or None.
Browse = Callable[[str], "str | None"]
# Browse… for a field that takes a route: (current model, its route) → the
# model and route chosen, or None if cancelled.
BrowseRoute = Callable[[str, "ModelRoute | None"], "tuple[str, ModelRoute | None] | None"]
# The endpoint's base URL as it stands, for a field's route; None: no routes.
RouteEndpoint = Callable[[], "str | None"]

SHORT_ROUTES = {
    "latency": "⚡ Fastest start",
    "speed": "⚡ Fastest",
    "price": "⚡ Cheapest",
}


def route_label(route: ModelRoute | None) -> tuple[str, str]:
    """A route button's text and tooltip: "Route…" for the subscription's
    routing, else the route in short."""
    if route is None:
        return (
            "Route…",
            "Subscription routing: NanoGPT chooses the host. Click to choose the fastest, "
            "the cheapest or one host instead (billed pay-as-you-go).",
        )
    text = f"⚡ {route.host}" if route.priority == "host" else SHORT_ROUTES[route.priority]
    return (
        text,
        f"Route: {describe(route)}. Billed pay-as-you-go, even on the subscription. "
        "Click to change it.",
    )


COLUMNS = ("Model", "Name", "Privacy", "Context", "In $/M", "Out $/M", "Reasoning", "Hosts")


class ModelCatalog(QObject):
    """The endpoint's chat models, fetched on a worker thread and cached."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.models: dict[str, ModelInfo] | None = None
        self.error: str | None = None
        self._endpoint: str | None = None
        self._fetched_at: datetime | None = None
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None

    @property
    def loading(self) -> bool:
        return self._thread is not None

    def wait(self) -> None:
        """Before the owner closes: a QThread destroyed while running takes
        the process down. The fetch gives up by itself within its timeout."""
        if self._thread is not None:
            self._thread.wait(35_000)

    def ensure(self, source: ProviderSource, *, force: bool = False) -> None:
        """Fetch unless a fresh list for the same endpoint is already here."""
        if self.loading:
            return
        try:
            provider, endpoint, owned = source()
        except ValueError as exc:
            self.models, self.error = None, str(exc)
            self.changed.emit()
            return
        fresh = (
            self._fetched_at is not None and datetime.now(UTC) - self._fetched_at < CATALOG_MAX_AGE
        )
        if not force and endpoint == self._endpoint and self.models is not None and fresh:
            return
        self._endpoint = endpoint
        self.error = None
        result: dict[str, dict] = {}

        def job():
            try:
                result.update(provider.fetch_model_prices())
            finally:
                close = getattr(provider, "close", None)
                if owned and close is not None:
                    close()  # one made from unsaved Settings fields
            return
            yield  # a generator, so GenerationWorker can run it

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._on_finished(result))
        self._worker, self._thread = worker, thread
        self.changed.emit()
        thread.start()

    def _on_failed(self, message: str) -> None:
        self.error = message

    def _on_finished(self, listing: dict[str, dict]) -> None:
        self._worker, self._thread = None, None
        if self.error is None:
            models = chat_models(listing)
            if models:
                self.models = models
                self._fetched_at = datetime.now(UTC)
            else:
                self.models = None
                self.error = "The endpoint didn't list any models."
        self.changed.emit()


class _Row(QTreeWidgetItem):
    """Sorts numeric columns by number, not by their text."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self.data(column, Qt.UserRole), other.data(column, Qt.UserRole)
        if mine is not None or theirs is not None:
            # Unknowns sort last whichever way.
            return (mine is None, mine or 0) < (theirs is None, theirs or 0)
        return self.text(column).lower() < other.text(column).lower()


def _price(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _hosts(info: ModelInfo) -> str:
    """How many of NanoGPT's hosts serve the model: "—" for a TEE or
    encrypted model, whose enclave is its host, and blank when none are listed."""
    if is_tee(info.id):
        return "—"
    return str(len(info.hosts)) if info.hosts else ""


def _hosts_tip(info: ModelInfo) -> str:
    if is_tee(info.id):
        return "Runs in its attested enclave: no choice of host."
    if len(info.hosts) < 2:
        return "One host: no choice of route." if info.hosts else "No hosts listed."
    return (
        f"{len(info.hosts)} hosts, so a route can be chosen for it (fastest, cheapest, "
        "or one host): " + ", ".join(info.hosts)
    )


class ModelPickerDialog(QDialog):
    def __init__(
        self,
        catalog: ModelCatalog,
        source: ProviderSource,
        *,
        current: str = "",
        parent: QWidget | None = None,
        allowed: Callable[[str], bool] | None = None,
        allowed_note: str = "",
        route_endpoint: str | None = None,
        route: ModelRoute | None = None,
    ) -> None:
        """`route_endpoint`: the base URL, when the model is chosen for a field
        that takes a route (Settings → Models); its current `route`."""
        super().__init__(parent)
        self.setWindowTitle("Choose a model")
        self.route_endpoint = route_endpoint
        self.chosen_route = route
        # Only the models this choice may move to (a TEE chat's), and why.
        self.allowed = allowed
        self.allowed_note = allowed_note
        self.resize(1120, 600)
        self.catalog = catalog
        self.source = source
        self.current = current.strip()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search — e.g. “sonnet 4.6”, “opus”, “llama 70b”")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)

        self.table = QTreeWidget()
        self.table.setObjectName("modelTable")
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHeaderLabels(list(COLUMNS))
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        header = self.table.header()
        # Ids are what matter and run long ("anthropic/claude-sonnet-4.6:thinking"):
        # a wide, adjustable column for them, the rest of the width to the name.
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.resizeSection(0, 330)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda *_: self._accept())
        self.table.currentItemChanged.connect(self._sync_buttons)

        self.privacy_note = QLabel()
        self.privacy_note.setObjectName("hintLabel")
        self.privacy_note.setWordWrap(True)
        self.privacy_note.hide()
        # A TEE model with an end-to-end encrypted twin: choose that instead.
        # The author: clicking a TEE row whose Privacy column named the
        # private/ version picked the TEE one, and the private rows sort far
        # from their twins.
        self.twin_button = QPushButton()
        self.twin_button.hide()
        self.twin_button.clicked.connect(self._choose_twin)
        # A model with several hosts: which serves it (gui/route_dialog.py).
        self.route_button = QPushButton("Choose a route…")
        self.route_button.setToolTip(
            "This model has several hosts on NanoGPT: choose the fastest, the cheapest, "
            "or one of them (billed pay-as-you-go)"
        )
        self.route_button.clicked.connect(self._choose_route)
        self.route_button.setVisible(route_endpoint is not None)
        self.route_button.setEnabled(False)
        self._twin: str | None = None
        # Test the chosen TEE/ or private/ model's enclave before using it.
        from sealedlore.gui.attest_check import AttestationButton

        self.attest = AttestationButton(self._attest_settings)
        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self.catalog.ensure(self.source, force=True))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use this model")
        self.buttons.addButton(self.refresh_button, QDialogButtonBox.ResetRole)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.privacy_note)
        layout.addWidget(self.twin_button, 0, Qt.AlignLeft)
        layout.addWidget(self.route_button, 0, Qt.AlignLeft)
        layout.addWidget(self.attest)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self.catalog.changed.connect(self._populate)
        self._populate()
        self.catalog.ensure(self.source)
        self.search.setFocus()

    def done(self, result: int) -> None:
        # The catalog outlives the dialog; don't leave it calling a dead one.
        try:
            self.catalog.changed.disconnect(self._populate)
        except (RuntimeError, TypeError):
            pass
        super().done(result)

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clear()
        models = self.catalog.models
        if self.catalog.loading:
            self.status.setText("Loading the endpoint's models…")
        elif self.catalog.error:
            self.status.setText(f"Couldn't load the model list: {self.catalog.error}")
        self.refresh_button.setEnabled(not self.catalog.loading)
        if models:
            for info in models.values():
                self.table.addTopLevelItem(self._row(info))
        self.table.setSortingEnabled(True)
        self._filter()
        self._select_current()

    def _row(self, info: ModelInfo) -> QTreeWidgetItem:
        privacy, note = self._privacy(info.id)
        row = _Row(
            [
                info.id,
                info.name,
                privacy,
                short_count(info.context_length),
                _price(info.prompt_price),
                _price(info.completion_price),
                "✓" if info.reasoning else "",
                _hosts(info),
            ]
        )
        row.setData(0, Qt.UserRole + 1, info.id)
        row.setData(3, Qt.UserRole, info.context_length)
        row.setData(4, Qt.UserRole, info.prompt_price)
        row.setData(5, Qt.UserRole, info.completion_price)
        if _hosts(info) not in ("", "—"):
            row.setData(7, Qt.UserRole, len(info.hosts))
        for column in (3, 4, 5, 6, 7):
            row.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
        tip = info.description or info.name or info.id
        if info.max_output_tokens:
            tip += f"\n\nWrites up to {info.max_output_tokens:,} tokens per reply."
        for column in range(len(COLUMNS)):
            row.setToolTip(column, note if column == 2 and note else tip)
        row.setToolTip(7, _hosts_tip(info))
        return row

    def _matches(self, info: ModelInfo, query: str) -> bool:
        """The usual search (engine/catalog.matches), and the privacy words:
        "tee", "private", "encrypted" and "e2ee" find both kinds, so a model's
        TEE and encrypted versions come up together."""
        label, _note = self._privacy(info.id)
        if label.startswith(("🔐", "TEE ·")):
            words = "tee private encrypted e2ee end-to-end"
        else:
            words = "tee" if label else ""
        haystack = f"{info.id} {info.name} {info.owner} {words}".lower()
        return matches(info, query) or all(word in haystack for word in query.lower().split())

    def _privacy(self, model_id: str) -> tuple[str, str]:
        """The Privacy column: end-to-end encrypted, TEE, or nothing. A TEE
        model with an encrypted twin says so (the author: users should see
        which offer full Private Mode, not just TEE)."""
        label, note = privacy_label(model_id)
        if label.startswith("🔐"):
            label = "🔐 Encrypted"  # short: the Name column needs the room
        twin = encrypted_counterpart(model_id, (self.catalog.models or {}).keys())
        if twin:
            label = "TEE · 🔐 version"
            note += f" {twin} is the same model, end-to-end encrypted."
        return label, note

    def _filter(self) -> None:
        models = self.catalog.models or {}
        query = self.search.text()
        shown = 0
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            model_id = row.data(0, Qt.UserRole + 1)
            info = models.get(model_id)
            hidden = (
                info is None
                or (self.allowed is not None and not self.allowed(model_id))
                or not self._matches(info, query)
            )
            row.setHidden(hidden)
            shown += not hidden
        if models and not self.catalog.loading:
            total = len(models)
            text = f"{total} models" if shown == total else f"{shown} of {total} models"
            if self.allowed_note:
                text += f" ({self.allowed_note})"
            self.status.setText(text)
        current = self.table.currentItem()
        if current is None or current.isHidden():
            first = next(
                (
                    self.table.topLevelItem(i)
                    for i in range(self.table.topLevelItemCount())
                    if not self.table.topLevelItem(i).isHidden()
                ),
                None,
            )
            self.table.setCurrentItem(first)
        self._sync_buttons()

    def _select_current(self) -> None:
        if not self.current:
            return
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            if row.data(0, Qt.UserRole + 1) == self.current and not row.isHidden():
                self.table.setCurrentItem(row)
                self.table.scrollToItem(row, QAbstractItemView.PositionAtCenter)
                return

    def _sync_buttons(self, *_args) -> None:
        chosen = self.selected_model()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(chosen is not None)
        # What the chosen model keeps private, under the list.
        note = self._privacy(chosen)[1] if chosen else ""
        self.privacy_note.setText(note)
        self.privacy_note.setVisible(bool(note))
        twin = encrypted_counterpart(chosen or "", (self.catalog.models or {}).keys())
        if twin and self.allowed is not None and not self.allowed(twin):
            twin = None
        self._twin = twin
        self.twin_button.setText(f"🔐 Use the end-to-end encrypted version: {twin}" if twin else "")
        self.twin_button.setVisible(bool(twin))
        self.attest.model_changed(chosen or "")
        self.route_button.setEnabled(self._routable_row())

    def selected_model(self) -> str | None:
        row = self.table.currentItem()
        return row.data(0, Qt.UserRole + 1) if row is not None and not row.isHidden() else None

    def _attest_settings(self) -> ProviderConfig | None:
        """The endpoint this picker lists, with the chosen model, for Test attestation."""
        chosen = self.selected_model()
        try:
            provider, _endpoint, owned = self.source()
        except ValueError:
            return None
        config = getattr(provider, "config", None)
        if owned:
            close = getattr(provider, "close", None)
            if close is not None:
                close()
        if not chosen or not isinstance(config, ProviderConfig):
            return None
        return config.model_copy(update={"model": chosen})

    def _choose_twin(self) -> None:
        """Select the encrypted twin and take it."""
        if not self._twin:
            return
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            if row.data(0, Qt.UserRole + 1) == self._twin:
                if row.isHidden():
                    self.search.clear()  # the search hid it: show it
                self.table.setCurrentItem(row)
                self._accept()
                return

    def _routable_row(self) -> bool:
        chosen = self.selected_model()
        info = (self.catalog.models or {}).get(chosen or "")
        return (
            self.route_endpoint is not None
            and info is not None
            and routable(self.route_endpoint, info.id)
            and len(info.hosts) >= 2
        )

    def _choose_route(self) -> None:
        chosen = self.selected_model()
        if not chosen or self.route_endpoint is None:
            return
        dialog = RouteDialog(self.route_endpoint, chosen, self.chosen_route, self)
        if dialog.exec() == QDialog.Accepted:
            self.chosen_route = dialog.route()
        dialog.deleteLater()

    def _accept(self) -> None:
        if self.selected_model() is not None:
            self.accept()


class ModelField(QWidget):
    """A model id: typed, or chosen from the endpoint's list with Browse…."""

    textChanged = Signal(str)

    def __init__(
        self,
        text: str = "",
        browse: Browse | None = None,
        *,
        route_endpoint: RouteEndpoint | None = None,
        browse_route: BrowseRoute | None = None,
        hosts_of: Callable[[str], tuple[str, ...] | None] | None = None,
    ) -> None:
        """`route_endpoint` (held weakly) gives the field a route: NanoGPT's
        host for its model, chosen from the label beside it; `browse_route`
        is then Browse…, and returns the route with the model. `hosts_of`
        (held weakly): a model's hosts from the endpoint's list, None while
        unknown; a model with fewer than two has no route."""
        super().__init__()
        self.route: ModelRoute | None = None
        self._route_endpoint = _weak(route_endpoint) if route_endpoint else None
        self._hosts_of = _weak(hosts_of) if hosts_of else None
        self._browse_route = _weak(browse_route) if browse_route else None
        self.edit = line_edit(text)
        self.edit.textChanged.connect(self.textChanged)
        self.button = QToolButton()
        self.button.setText("Browse…")
        self.button.setToolTip("Choose from the models your endpoint offers")
        self.button.setVisible(browse is not None or browse_route is not None)
        self._browse = browse
        self.button.clicked.connect(self._open)
        # Says, beside the field, whether the model is end-to-end encrypted or
        # only TEE; the tooltip says what each protects.
        self.privacy = QLabel()
        self.privacy.setObjectName("hintLabel")
        # Which of NanoGPT's hosts serves the model: short here, whole in
        # the tooltip; a click opens the route dialog.
        self.route_button = QToolButton()
        self.route_button.setObjectName("routeButton")
        self.route_button.clicked.connect(self._edit_route)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.privacy)
        layout.addWidget(self.route_button)
        layout.addWidget(self.button)
        self.edit.textChanged.connect(self._show_privacy)
        self.edit.textChanged.connect(lambda _text: self._show_route())
        self._show_privacy(text)
        self._show_route()

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        set_field_text(self.edit, text)

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self.edit.setPlaceholderText(text)

    def placeholderText(self) -> str:  # noqa: N802 - Qt naming
        return self.edit.placeholderText()

    def setReadOnly(self, read_only: bool) -> None:  # noqa: N802 - Qt naming
        self.edit.setReadOnly(read_only)
        self.button.setEnabled(not read_only)

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt naming
        self.edit.setToolTip(text)

    def _show_privacy(self, text: str) -> None:
        label, note = privacy_label(text.strip())
        # Short beside the field; the whole label and note in the tooltip.
        self.privacy.setText("🔐 E2EE" if label.startswith("🔐") else label)
        self.privacy.setToolTip(note)
        self.privacy.setVisible(bool(label))

    def _open(self) -> None:
        current = self.edit.text().strip() or self.edit.placeholderText()
        browse_route = self._browse_route() if self._browse_route is not None else None
        if browse_route is not None:
            chosen = browse_route(current, self.route)
            if chosen:
                self.edit.setText(chosen[0])
                self.set_route(chosen[1])
            return
        if self._browse is None:
            return
        picked = self._browse(current)
        if picked:
            self.edit.setText(picked)

    # --- the route --------------------------------------------------------

    def _endpoint(self) -> str | None:
        getter = self._route_endpoint() if self._route_endpoint is not None else None
        return getter() if getter is not None else None

    def set_route(self, route: ModelRoute | None) -> None:
        self.route = route if route is not None and route.paid else None
        self._show_route()

    def _can_route(self) -> bool:
        """A model in the field (a blank one takes its fallback's route),
        on NanoGPT, not in an enclave, and not known to have one host."""
        endpoint = self._endpoint()
        model = self.edit.text().strip()
        if endpoint is None or not model or not routable(endpoint, model):
            return False
        hosts_of = self._hosts_of() if self._hosts_of is not None else None
        hosts = hosts_of(model) if hosts_of is not None else None
        return hosts is None or len(hosts) >= 2

    def effective_route(self) -> ModelRoute | None:
        """The route as it applies to the model now in the field: none for a
        model with no choice of host, and a host chosen for another model is
        the subscription's routing."""
        if not self._can_route():
            return None
        return self.route if route_body(self.route, self.edit.text().strip()) else None

    def _show_route(self) -> None:
        shown = self._can_route()
        self.route_button.setVisible(shown)
        if not shown:
            return
        text, tip = route_label(self.effective_route())
        self.route_button.setText(text)
        self.route_button.setToolTip(tip)

    def _edit_route(self) -> None:
        endpoint = self._endpoint()
        model = self.edit.text().strip()
        if endpoint is None or not model:
            return
        dialog = RouteDialog(endpoint, model, self.effective_route(), self)
        if dialog.exec() == QDialog.Accepted:
            self.set_route(dialog.route())
        dialog.deleteLater()


def pick_model(
    catalog: ModelCatalog,
    source: ProviderSource,
    current: str,
    parent: QWidget | None,
    *,
    allowed: Callable[[str], bool] | None = None,
    allowed_note: str = "",
) -> str | None:
    """Open the picker; the chosen model id, or None if cancelled."""
    dialog = ModelPickerDialog(
        catalog,
        source,
        current=current,
        parent=parent,
        allowed=allowed,
        allowed_note=allowed_note,
    )
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.selected_model()


def pick_model_and_route(
    catalog: ModelCatalog,
    source: ProviderSource,
    current: str,
    route: ModelRoute | None,
    endpoint: str | None,
    parent: QWidget | None,
) -> tuple[str, ModelRoute | None] | None:
    """The picker for a field that takes a route: the model chosen and its
    route (Choose a route…, or the one it had), or None if cancelled."""
    dialog = ModelPickerDialog(
        catalog, source, current=current, parent=parent, route_endpoint=endpoint, route=route
    )
    if dialog.exec() != QDialog.Accepted:
        return None
    chosen = dialog.selected_model()
    return (chosen, dialog.chosen_route) if chosen else None


def _weak(method):
    """A bound method held weakly: a field holding its own dialog's method
    makes a cycle, and collecting such cycles crashed PySide (attest_check)."""
    return weakref.WeakMethod(method) if inspect.ismethod(method) else lambda: method
