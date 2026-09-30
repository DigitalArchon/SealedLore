"""Choose which of NanoGPT's hosts serves a model: a route (models/route.py).

The author asked for it with the figures to choose by: each host's precision,
privacy class, NanoGPT's own measurement of its first token and speed, its
price and whether it caches, beside what NanoGPT's own routing does. The
choices are a priority (fastest first word, fastest overall, cheapest, or
one host) and a precision floor, ticked by default: FP4 hosts measured less
accurate on the scene read. Any route but the subscription's is paid.

The hosts come from the app-wide catalog (gui/model_hosts.py), never a
thread of this dialog's own.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.routing import PRIORITY_LABELS
from sealedlore.gui.bad_hosts import bad_hosts
from sealedlore.gui.model_hosts import hosts_catalog
from sealedlore.models.route import ModelRoute
from sealedlore.providers.model_hosts import Host, ModelHosts

HOST_COLUMNS = (
    "Host",
    "Precision",
    "Privacy",
    "First token",
    "Tokens/s",
    "In $/M",
    "Out $/M",
    "Caches",
)
PRIORITY_TIPS = {
    "subscription": "NanoGPT's own choice: on the subscription, usually the cheapest host at "
    "FP8 or better. Free for a model the subscription includes.",
    "latency": "The host that starts answering soonest. Measured best for the scene read "
    "(with FP8 or better): a third of the wait, as accurate.",
    "speed": "NanoGPT's own mix of the first token and the writing speed.",
    "price": "The cheapest host.",
    "host": "This host first; another if it's down, still held to the precision floor.",
}


def _seconds(ms: float | None) -> str:
    return "" if ms is None else f"{ms / 1000:.1f}s"


def _number(value: float | None, digits: int = 0) -> str:
    return "" if value is None else f"{value:.{digits}f}"


class _HostRow(QTreeWidgetItem):
    """Sorts by the number behind a figure, unknowns last."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self.data(column, Qt.UserRole), other.data(column, Qt.UserRole)
        if mine is not None or theirs is not None:
            return (mine is None, mine or 0) < (theirs is None, theirs or 0)
        return self.text(column).lower() < other.text(column).lower()


class RouteDialog(QDialog):
    """Hosts below the precision floor are hidden while it is ticked (the
    author: greyed, they stood between the reader and the usable ones)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        route: ModelRoute | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Route for {model}")
        self.resize(960, 560)
        self.base_url, self.model = base_url, model
        route = route or ModelRoute()

        intro = QLabel(
            f"NanoGPT runs {model} on several hosts and normally chooses one itself. A route "
            "chooses for you. <b>Any route but the subscription's is billed pay-as-you-go, "
            "even for a model the subscription includes.</b>"
        )
        intro.setWordWrap(True)

        self.choices = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        choice_box = QVBoxLayout()
        for priority, label in PRIORITY_LABELS.items():
            radio = QRadioButton(label)
            radio.setToolTip(PRIORITY_TIPS[priority])
            self.choices.addButton(radio)
            self.radios[priority] = radio
            if priority == "host":
                self.host = QComboBox()
                self.host.setEnabled(False)
                row = QHBoxLayout()
                row.addWidget(radio)
                row.addWidget(self.host, 1)
                choice_box.addLayout(row)
            else:
                choice_box.addWidget(radio)
        self.radios[route.priority].setChecked(True)
        self._wanted_host = route.host if route.host_model == model else None

        self.fp8 = QCheckBox("FP8 or better")
        self.fp8.setChecked(route.fp8)
        self.fp8.setToolTip(
            "Only hosts running the model at 8-bit precision or more. Hosts at FP4, or "
            "whose precision isn't listed, are left out: on the scene read an FP4 host "
            "was slower and less accurate."
        )
        self.privacy_note = QLabel(
            "Privacy is each host's own terms. Your NanoGPT account or API key can require "
            "hosts that keep nothing (its data retention setting), and that wins over any "
            "route: a host here that keeps prompts is then never used, and NanoGPT serves "
            "the call elsewhere (the status bar shows ⚠ route not followed)."
        )
        self.privacy_note.setObjectName("hintLabel")
        self.privacy_note.setWordWrap(True)

        self.table = QTreeWidget()
        self.table.setColumnCount(len(HOST_COLUMNS))
        self.table.setHeaderLabels(list(HOST_COLUMNS))
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAlternatingRowColors(True)
        header = self.table.header()
        for column in range(len(HOST_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        # The privacy terms give way, whole in their tooltips, before the
        # table scrolls sideways.
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setStretchLastSection(False)
        self.table.itemClicked.connect(self._host_clicked)

        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)
        # Hosts marked bad (Help → Model trouble) are shown greyed with why,
        # and can't be chosen until allowed again.
        self.allow_button = QPushButton("Allow again")
        self.allow_button.setToolTip("Let the selected host, marked bad, be chosen again")
        self.allow_button.setEnabled(False)
        self.allow_button.clicked.connect(self._allow_again)
        self.table.itemSelectionChanged.connect(self._sync_allow)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Use this route")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)

        top = QHBoxLayout()
        top.addLayout(choice_box, 1)
        side = QVBoxLayout()
        side.addWidget(self.fp8)
        side.addWidget(self.privacy_note)
        side.addStretch(1)
        top.addLayout(side, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        under = QHBoxLayout()
        under.addWidget(self.status, 1)
        under.addWidget(self.allow_button, 0, Qt.AlignTop)
        layout.addLayout(under)
        layout.addWidget(buttons)

        self.choices.buttonToggled.connect(lambda *_: self._sync())
        self.fp8.toggled.connect(lambda _on: self._show_hosts())
        self.host.currentIndexChanged.connect(lambda _i: self._sync())
        catalog = hosts_catalog()
        catalog.loaded.connect(self._on_loaded)
        catalog.failed.connect(self._on_failed)
        self._hosts: ModelHosts | None = catalog.hosts(base_url, model)
        if self._hosts is None:
            self.status.setText("Loading NanoGPT's hosts for this model…")
            catalog.ensure(base_url, model)
        self._show_hosts()

    def done(self, result: int) -> None:
        # The catalog outlives the dialog; don't leave it calling a dead one.
        catalog = hosts_catalog()
        for signal, slot in ((catalog.loaded, self._on_loaded), (catalog.failed, self._on_failed)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)

    # --- the hosts ----------------------------------------------------------

    def _on_loaded(self, base_url: str, model: str) -> None:
        if (base_url, model) == (self.base_url, self.model):
            self._hosts = hosts_catalog().hosts(base_url, model)
            self._show_hosts()

    def _on_failed(self, base_url: str, model: str, why: str) -> None:
        if (base_url, model) == (self.base_url, self.model):
            self.status.setText(
                f"Couldn't load the hosts ({why}). The routes by priority still work; "
                "choosing one host needs the list."
            )
            self._sync()

    def _usable(self, host: Host) -> bool:
        return host.available and (host.fp8_or_better or not self.fp8.isChecked())

    def _bad(self) -> dict:
        return bad_hosts().marked(self.model)

    def _sync_allow(self) -> None:
        item = self.table.currentItem()
        host_id = item.data(0, Qt.UserRole + 1) if item is not None else None
        self.allow_button.setEnabled(bool(host_id) and host_id in self._bad())

    def _allow_again(self) -> None:
        item = self.table.currentItem()
        host_id = item.data(0, Qt.UserRole + 1) if item is not None else None
        if host_id:
            bad_hosts().allow(self.model, host_id)
            self._show_hosts()

    def _show_hosts(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clear()
        hosts = self._hosts
        if hosts is not None:
            auto = _HostRow(
                [
                    "NanoGPT's choice",
                    hosts.auto_quantization or "",
                    "",
                    _seconds(hosts.auto_first_token_ms),
                    _number(hosts.auto_tokens_per_second),
                    "",
                    "",
                    "",
                ]
            )
            auto.setData(3, Qt.UserRole, -1.0)  # always first when sorted by first token
            auto.setToolTip(0, "What the subscription's routing does now, as NanoGPT measures it")
            self.table.addTopLevelItem(auto)
            shown = [host for host in hosts.available if self._usable(host)]
            bad = self._bad()
            for host in shown:
                marked = bad.get(host.id)
                row = _HostRow(
                    [
                        f"{host.name} · marked bad" if marked else host.name,
                        host.quantization or "unknown",
                        host.privacy_label,
                        _seconds(host.first_token_ms),
                        _number(host.tokens_per_second),
                        _number(host.input_price, 2),
                        _number(host.output_price, 2),
                        {True: "yes", False: "no"}.get(host.caching, ""),
                    ]
                )
                row.setData(0, Qt.UserRole + 1, host.id)
                row.setToolTip(2, host.privacy_label)
                row.setData(3, Qt.UserRole, host.first_token_ms)
                row.setData(4, Qt.UserRole, host.tokens_per_second)
                row.setData(5, Qt.UserRole, host.input_price)
                row.setData(6, Qt.UserRole, host.output_price)
                if marked is not None:
                    when = (marked.at or "")[:10]
                    why = f": {marked.reason}" if marked.reason else ""
                    for column in range(len(HOST_COLUMNS)):
                        row.setForeground(column, self.palette().placeholderText())
                    row.setToolTip(0, f"Marked bad {when}{why}. Select it and press Allow again.")
                self.table.addTopLevelItem(row)
            self.table.setSortingEnabled(True)
            self.table.sortByColumn(3, Qt.AscendingOrder)
            count = len(hosts.available)
            hidden = count - len(shown)
            below = f", {hidden} more below FP8 (untick it to see them)" if hidden else ""
            self.status.setText(
                f"{len(shown)} hosts{below}. The figures are NanoGPT's current measurements "
                "and time the first token: a model that reasons takes longer to its first "
                "word. Test speed… in Settings measures it."
                if count >= 2
                else "This model has only one host: there is no route to choose."
            )
        self._fill_host_choice()
        self._sync()

    def _fill_host_choice(self) -> None:
        current = self.host.currentData() or self._wanted_host
        self.host.blockSignals(True)
        self.host.clear()
        hosts = self._hosts.available if self._hosts is not None else ()
        bad = self._bad()
        for host in sorted(hosts, key=lambda h: (h.first_token_ms is None, h.first_token_ms or 0)):
            if self._usable(host) and host.id not in bad:
                self.host.addItem(host.name, host.id)
        index = self.host.findData(current) if current else -1
        if index < 0 and current and not self._hosts:
            # Not loaded (yet): keep the host that was chosen.
            self.host.addItem(current, current)
            index = self.host.count() - 1
        self.host.setCurrentIndex(max(index, 0) if self.host.count() else -1)
        self.host.blockSignals(False)

    def _host_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        host_id = item.data(0, Qt.UserRole + 1)
        if not host_id:
            self.radios["subscription"].setChecked(True)
            return
        index = self.host.findData(host_id)
        if index >= 0:  # a host marked bad isn't in the list
            self.host.setCurrentIndex(index)
            self.radios["host"].setChecked(True)

    def _sync(self) -> None:
        few = self._hosts is not None and len(self._hosts.available) < 2
        for priority, radio in self.radios.items():
            radio.setEnabled(priority == "subscription" or not few)
        if few:
            self.radios["subscription"].setChecked(True)
        priority = self._priority()
        self.host.setEnabled(priority == "host" and self.host.count() > 0)
        self.fp8.setEnabled(priority != "subscription")
        self.ok_button.setEnabled(priority != "host" or self.host.currentData() is not None)

    # --- the answer ---------------------------------------------------------

    def _priority(self) -> str:
        return next(
            (priority for priority, radio in self.radios.items() if radio.isChecked()),
            "subscription",
        )

    def route(self) -> ModelRoute | None:
        """The route chosen; None for the subscription's routing."""
        priority = self._priority()
        if priority == "subscription":
            return None
        if priority == "host":
            return ModelRoute(
                priority="host",
                fp8=self.fp8.isChecked(),
                host=self.host.currentData(),
                host_model=self.model,
            )
        return ModelRoute(priority=priority, fp8=self.fp8.isChecked())
