"""A character's skills (§4.1) as a table: a domain and its tier a row, each
tier in a combo. A story's Cast page and the author's own characters
(gui/card_form.py) share it."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHeaderView, QTableWidget, QTableWidgetItem

from sealedlore.engine.dice import TIER_ORDER

# Past this many skills the table scrolls rather than pushing the form down.
SKILL_ROWS_VISIBLE = 8


class SkillsTable(QTableWidget):
    # A domain or a tier changed by hand (not by `show_tiers`).
    edited = Signal()

    def __init__(self) -> None:
        super().__init__(0, 2)
        self.setHorizontalHeaderLabels(["Skill", "Tier"])
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        # Rows as tall as the tier combos in them, and a table as tall as its
        # rows (`_fit`): left to the form, it was squeezed to one row and
        # every skill after the first needed the scroll wheel.
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.itemChanged.connect(lambda _item: self.edited.emit())

    def _tier_combo(self, tier: str) -> QComboBox:
        combo = QComboBox()
        for value in TIER_ORDER:
            combo.addItem(value, value)
        combo.setCurrentIndex(max(combo.findData(tier), 0))
        combo.currentIndexChanged.connect(lambda _index: self.edited.emit())
        return combo

    def show_tiers(self, tiers: dict) -> None:
        self.blockSignals(True)
        self.setRowCount(0)
        for domain, tier in tiers.items():
            self._append_row(domain, tier)
        self.blockSignals(False)
        self._fit()

    def _append_row(self, domain: str, tier: str) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setItem(row, 0, QTableWidgetItem(domain))
        self.setCellWidget(row, 1, self._tier_combo(tier))
        self._fit()

    def _fit(self) -> None:
        """Show every skill without scrolling, up to SKILL_ROWS_VISIBLE of them."""
        self.resizeRowsToContents()
        rows = [self.rowHeight(row) for row in range(self.rowCount())]
        empty_row = self.verticalHeader().defaultSectionSize()
        body = sum(rows[:SKILL_ROWS_VISIBLE]) if rows else empty_row
        header = self.horizontalHeader().sizeHint().height()
        self.setFixedHeight(header + body + 2 * self.frameWidth())

    def tiers(self) -> dict:
        tiers = {}
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            combo = self.cellWidget(row, 1)
            domain = item.text().strip().lower() if item else ""
            if domain and isinstance(combo, QComboBox):
                tiers[domain] = combo.currentData()
        return tiers

    def add_skill(self) -> None:
        """A new row, its name being typed: nothing changes until it has one."""
        self.blockSignals(True)
        self._append_row("", "competent")
        self.blockSignals(False)
        row = self.rowCount() - 1
        self.setCurrentCell(row, 0)
        self.editItem(self.item(row, 0))

    def remove_skill(self) -> bool:
        """The chosen row taken out; whether there was one."""
        row = self.currentRow()
        if row < 0:
            return False
        self.removeRow(row)
        self._fit()
        return True
