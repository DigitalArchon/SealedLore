"""View → AI calls this session…: every call this run made to a model
(call_log.py), for the author to see where their stories go and what they
cost. Nothing that was said is shown, because nothing of it is kept: what
each call was for, the model, the provider and any host asked for, how its
content travelled, its tokens, time and cost. In memory only, for this run.

A cost the endpoint stated is shown as it is; one it didn't is estimated
from the model's listed prices (marked ~), as the story's cost is; with
neither it says so, never $0.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.call_log import (
    CALLS,
    DECISION,
    EMBEDDINGS,
    PICTURE,
    PLAIN_HTTPS,
    PLAIN_LOCAL,
    SPEED_TEST,
    TEXT,
    VIDEO,
    Call,
)
from sealedlore.engine.pricing import estimate_cost
from sealedlore.engine.usage import KIND_LABELS
from sealedlore.gui.plain_text import plain_tip
from sealedlore.gui.sizing import size_to_screen
from sealedlore.gui.winprivacy import copy_text
from sealedlore.models.config import Config
from sealedlore.models.node import Usage

COLUMNS = (
    "Time",
    "For",
    "Model",
    "Provider",
    "Sent",
    "Received",
    "Took",
    "Cost",
    "Encryption",
    "Status",
)
INTRO = (
    "Every call SealedLore has made to a model since it started: what it was for, where it "
    "went, how it travelled, and what it cost. None of what was said is kept here, and none "
    "of this is saved: it is gone when SealedLore closes. Costs marked ~ are estimated from "
    "the model's listed prices."
)
# What a call is for when no story logged it, by kind.
_BY_KIND = {
    PICTURE: "Picture",
    VIDEO: "Video",
    EMBEDDINGS: "Lore vectors",
    DECISION: "Lore picks (Jev)",
}


def purpose_words(call: Call) -> str:
    """What a call was for, as the usage report names it."""
    purpose = call.purpose
    if purpose == SPEED_TEST:
        return "Speed test"
    if purpose.endswith("request"):
        label = KIND_LABELS.get(purpose[: -len("request")] + "response")
        if label:
            return label
    return _BY_KIND.get(call.kind, "")


class CallLogDialog(QDialog):
    """`config`: the settings, for the models' listed prices."""

    def __init__(self, config: Callable[[], Config], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI calls this session")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._config = config
        self._shown_version = -1
        self._fitted = False
        intro = QLabel(INTRO)
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        self.totals = QLabel()
        self.totals.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.copy = QPushButton("Copy")
        self.copy.setToolTip("Every row as text, to paste somewhere")
        self.copy.clicked.connect(lambda: copy_text(self.as_text()))
        self.clear = QPushButton("Clear")
        self.clear.setToolTip("Forget the calls listed so far")
        self.clear.clicked.connect(CALLS.clear)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy)
        buttons.addWidget(self.clear)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.totals)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        size_to_screen(self, 0.75, 0.6, at_least=QSize(900, 420))
        # Calls finish on other threads: the list is read again whenever it
        # has changed, a second at most behind.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()

    # --- what is shown ---------------------------------------------------------

    def refresh(self) -> None:
        if CALLS.version == self._shown_version:
            return
        self._shown_version = CALLS.version
        calls = list(reversed(CALLS.calls()))  # newest first
        at_top = self.table.verticalScrollBar().value() == 0
        self.table.setRowCount(len(calls))
        for row, call in enumerate(calls):
            cost, cost_tip = self.cost_words(call)
            values = (
                time.strftime("%H:%M:%S", time.localtime(call.started)),
                purpose_words(call) or "—",
                call.model or "—",
                provider_words(call),
                f"{call.tokens_in:,}" if call.tokens_in else "",
                f"{call.tokens_out:,}" if call.tokens_out else "",
                f"{call.seconds:.1f}s" if call.seconds is not None else "…",
                cost,
                protection_words(call.protection),
                status_words(call),
            )
            tips = {
                4: cached_words(call),
                7: cost_tip,
                8: call.protection,
                9: call.detail,
            }
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                tip = tips.get(column) or value
                item.setToolTip(plain_tip(tip))
                if column in (4, 5, 6, 7):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)
        if calls and not self._fitted:
            # Once, to the first calls: after that, as the author dragged them.
            self.table.resizeColumnsToContents()
            self._fitted = True
        if at_top:
            self.table.scrollToTop()
        self.totals.setText(self.totals_words(calls))

    def cost_words(self, call: Call) -> tuple[str, str]:
        """The cost as shown, and what it is."""
        if call.cost is not None:
            if call.cost == 0:
                return (
                    "$0",
                    "The endpoint said this call cost nothing (as for a model a subscription "
                    "includes)",
                )
            return f"${call.cost:.4f}", "As the endpoint stated it"
        estimate = self.estimate(call)
        if estimate is not None:
            return f"~${estimate:.4f}", "Estimated from the model's listed prices"
        if call.status == "running":
            return "", ""
        return "—", "The endpoint stated no cost, and the model's price isn't known"

    def estimate(self, call: Call) -> float | None:
        if call.kind != TEXT or not (call.tokens_in or call.tokens_out):
            return None
        config = self._config()
        prices = config.model_prices
        price = prices.get(call.model) or next(
            (p for key, p in prices.items() if key.split("::", 1)[-1] == call.model), None
        )
        if price is None:
            return None
        usage = Usage(
            prompt_tokens=call.tokens_in,
            completion_tokens=call.tokens_out,
            cache_read_tokens=call.cache_read,
            cache_creation_tokens=call.cache_written,
        )
        return estimate_cost(usage, price, call.model, cache_ttl=config.cache_ttl)

    def totals_words(self, calls: list[Call]) -> str:
        if not calls:
            return "No calls yet this session."
        stated = sum(c.cost for c in calls if c.cost is not None)
        estimated = 0.0
        unpriced = 0
        for call in calls:
            if call.cost is not None:
                continue
            guess = self.estimate(call)
            if guess is not None:
                estimated += guess
            elif call.status == "done":
                unpriced += 1
        sent = sum(c.tokens_in for c in calls)
        received = sum(c.tokens_out for c in calls)
        words = (
            f"{len(calls)} call{'s' if len(calls) != 1 else ''} · {sent:,} tokens sent · "
            f"{received:,} received · ${stated:.4f} as stated"
        )
        if estimated:
            words += f" + ~${estimated:.4f} estimated"
        if unpriced:
            words += f" · {unpriced} with no price known"
        return words

    def as_text(self) -> str:
        """Every row, tab-separated, with a header line."""
        lines = ["\t".join(COLUMNS)]
        for row in range(self.table.rowCount()):
            cells = []
            for column in range(len(COLUMNS)):
                item = self.table.item(row, column)
                cells.append(item.text() if item is not None else "")
            lines.append("\t".join(cells))
        return "\n".join(lines)


def provider_words(call: Call) -> str:
    """The endpoint, its host, and a route asked of it."""
    where = call.endpoint or call.host or "—"
    if call.host and call.host not in where:
        where += f" ({call.host})"
    if call.route:
        where += f" · route: {call.route}"
    return where


def protection_words(protection: str) -> str:
    """How a call travelled, short enough for its column (the whole is its
    tooltip)."""
    if protection.startswith("End-to-end encrypted"):
        service = protection.split("(", 1)[-1].split(")", 1)[0] if "(" in protection else ""
        words = f"🔐 End-to-end · {service}" if service else "🔐 End-to-end"
        return words + (" ✓" if "attested key" in protection else "")
    if protection.startswith("TEE"):
        service = protection.split("(", 1)[-1].split(")", 1)[0] if "(" in protection else ""
        return f"TEE · {service}" if service else "TEE"
    if protection == PLAIN_LOCAL:
        return "Unencrypted, local network"
    if protection == PLAIN_HTTPS:
        return "HTTPS only"
    return protection


def cached_words(call: Call) -> str:
    parts = []
    if call.cache_read:
        parts.append(f"{call.cache_read:,} read from the cache")
    if call.cache_written:
        parts.append(f"{call.cache_written:,} written to it")
    return "Of the tokens sent: " + ", ".join(parts) if parts else ""


def status_words(call: Call) -> str:
    return {
        "running": "Running…",
        "done": "Done",
        "failed": "Failed",
        "stopped": "Stopped",
    }.get(call.status, call.status)
