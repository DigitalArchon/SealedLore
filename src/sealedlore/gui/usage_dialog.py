"""Usage and cost for a whole story (§8.3), read from its API log."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.usage import UsageLine, UsageReport

COLUMNS = ["", "Calls", "Tokens in", "Cached", "Written to cache", "Tokens out", "Cost"]


def _cells(line: UsageLine) -> list[str]:
    rate = line.cache_hit_rate
    cached = f"{line.cache_read_tokens:,}" + (f" ({rate:.0%})" if rate else "")
    if line.calls == line.unpriced:
        cost = "not reported"
    else:
        about = "~" if line.estimated else ""
        cost = f"{about}${line.cost:.4f}" + (
            f"  (+{line.unpriced} unpriced)" if line.unpriced else ""
        )
    return [
        line.label,
        f"{line.calls:,}",
        f"{line.prompt_tokens:,}",
        cached,
        f"{line.cache_creation_tokens:,}",
        f"{line.completion_tokens:,}",
        cost,
    ]


class UsageDialog(QDialog):
    def __init__(self, title: str, report: UsageReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Usage — {title}")
        self.setMinimumWidth(760)

        rows = [*report.lines, report.total] if report.lines else []
        table = QTableWidget(len(rows), len(COLUMNS))
        table.setHorizontalHeaderLabels(COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for row, line in enumerate(rows):
            for column, text in enumerate(_cells(line)):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if line is report.total:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                table.setItem(row, column, item)

        note = QLabel(
            "Every call this story has made, from its API log: turns on every branch, "
            "questions, chapter summaries, character scans and skill suggestions. "
            "Where the endpoint didn't report a call's cost, it is estimated from the "
            "model's listed prices and marked ~. A call with neither is counted as "
            "unpriced, not free."
            if report.lines
            else "No calls yet."
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(table)
        layout.addWidget(note)
        layout.addWidget(buttons)
