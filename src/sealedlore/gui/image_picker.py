"""Choosing an image model: the text model picker's search and table
(gui/model_picker.py) over the endpoint's image models.

The author (Sept 2026): the image model was chosen from a plain combo box,
"far more basic than the text model, and search is limited". The listing is
the window's `ImageCatalog` (kept in config for a day); Refresh fetches it
again on the catalog's thread, never the dialog's.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.image_jobs import ImageCatalog
from sealedlore.gui.model_picker import _Row
from sealedlore.providers.images import ImageModelInfo

COLUMNS = ("Model", "Name", "References", "Sizes", "$ / picture", "At once")
REFERENCES, SIZES, PRICE, AT_ONCE = 2, 3, 4, 5


def matches(info: ImageModelInfo, query: str) -> bool:
    """Every word of the query appears in the id, name, description or sizes."""
    haystack = f"{info.id} {info.name} {info.description} {' '.join(info.resolutions)}".lower()
    return all(word in haystack for word in query.lower().split())


def price_range(info: ImageModelInfo) -> tuple[float | None, str]:
    """The cheapest price a picture, for sorting, and the range as shown."""
    prices = sorted(info.per_image.values())
    if not prices:
        return None, ""
    low, high = prices[0], prices[-1]
    return low, f"{low:.3f}" if low == high else f"{low:.3f}–{high:.3f}"


class ImagePickerDialog(QDialog):
    def __init__(
        self, catalog: ImageCatalog, *, current: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose an image model")
        self.resize(1000, 560)
        self.catalog = catalog
        self.current = current.strip()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search — e.g. “seedream”, “flux”, “4k”, “edit”")
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
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.resizeSection(0, 300)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (REFERENCES, PRICE, AT_ONCE):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(SIZES, QHeaderView.Interactive)
        header.resizeSection(SIZES, 170)
        self.table.itemDoubleClicked.connect(lambda *_: self._accept())
        self.table.currentItemChanged.connect(self._sync_buttons)

        self.detail = QLabel()
        self.detail.setObjectName("hintLabel")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self.catalog.ensure(force=True))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use this model")
        self.buttons.addButton(self.refresh_button, QDialogButtonBox.ResetRole)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.detail)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self.catalog.changed.connect(self._populate)
        self._populate()
        self.catalog.ensure()
        self.search.setFocus()

    def done(self, result: int) -> None:
        # The catalog outlives the dialog; don't leave it calling a dead one.
        try:
            self.catalog.changed.disconnect(self._populate)
        except (RuntimeError, TypeError):
            pass
        super().done(result)

    def _models(self) -> dict[str, ImageModelInfo]:
        return {info.id: info for info in self.catalog.models}

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clear()
        for info in self._models().values():
            self.table.addTopLevelItem(self._row(info))
        self.table.setSortingEnabled(True)
        self.refresh_button.setEnabled(not self.catalog.loading)
        self._filter()
        self._select_current()

    def _row(self, info: ImageModelInfo) -> QTreeWidgetItem:
        low, prices = price_range(info)
        row = _Row(
            [
                info.id,
                info.name,
                str(info.max_inputs) if info.max_inputs else "none",
                ", ".join(info.resolutions),
                prices,
                str(info.max_outputs),
            ]
        )
        row.setData(0, Qt.UserRole + 1, info.id)
        row.setData(REFERENCES, Qt.UserRole, info.max_inputs)
        row.setData(PRICE, Qt.UserRole, low)
        row.setData(AT_ONCE, Qt.UserRole, info.max_outputs)
        for column in (REFERENCES, PRICE, AT_ONCE):
            row.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
        tip = self._describe(info)
        for column in range(len(COLUMNS)):
            row.setToolTip(column, tip)
        return row

    @staticmethod
    def _describe(info: ImageModelInfo) -> str:
        takes = (
            f"Takes up to {info.max_inputs} reference pictures."
            if info.max_inputs
            else "Takes no reference pictures."
        )
        sizes = ", ".join(
            f"{size} ${price:.3f}" if (price := info.per_image.get(size)) is not None else size
            for size in info.resolutions
        )
        lines = [info.description or info.name or info.id, takes]
        if sizes:
            lines.append(f"Sizes: {sizes}.")
        return "\n".join(line for line in lines if line)

    def _filter(self) -> None:
        models = self._models()
        query = self.search.text()
        shown = 0
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            info = models.get(row.data(0, Qt.UserRole + 1))
            hidden = info is None or not matches(info, query)
            row.setHidden(hidden)
            shown += not hidden
        total = len(models)
        if self.catalog.loading:
            self.status.setText("Loading the endpoint's image models…")
        elif not total:
            self.status.setText("No image models listed yet: Refresh fetches them.")
        else:
            self.status.setText(
                f"{total} image models" if shown == total else f"{shown} of {total} image models"
            )
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
        elif self.search.text():
            # Still a match: keep it in view, or the note describes a row off screen.
            self.table.scrollToItem(current, QAbstractItemView.PositionAtCenter)
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
        info = self._models().get(chosen or "")
        self.detail.setText(self._describe(info) if info is not None else "")

    def selected_model(self) -> str | None:
        item = self.table.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(0, Qt.UserRole + 1)

    def _accept(self) -> None:
        if self.selected_model() is not None:
            self.accept()


def pick_image_model(catalog: ImageCatalog, current: str, parent: QWidget | None) -> str | None:
    dialog = ImagePickerDialog(catalog, current=current, parent=parent)
    try:
        return dialog.selected_model() if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()
