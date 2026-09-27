"""Status-bar widgets: token budget bar, cache indicator, session cost. See §8.3, §10."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from sealedlore.engine.budget import BudgetReport
from sealedlore.engine.retrieval import RetrievalReport
from sealedlore.gui import theme
from sealedlore.models.node import Usage

# Each section of the prompt's colour on the budget meter (gui/theme.py).
SECTION_COLOURS = {
    "system": "meter_system",
    "summaries": "meter_summaries",
    "history": "meter_history",
    "tail": "meter_tail",
}


class NoticeStatusBar(QStatusBar):
    """A status bar whose messages swap with its widgets instead of hiding them.

    QStatusBar hides its normal widgets while a message shows, and in doing so
    clears their "explicitly hidden" flag; any relayout of the bar during the
    message (a label's text changing after a turn is enough) then shows them
    again, drawn over the message text until it times out. So the message
    here is a page of our own: the left-hand widgets on one page, the notice
    on the other, and QStatusBar's own message is never used. Qt's status
    tips (hovering a menu item) arrive through `route_status_tips`.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._widgets = QWidget()
        self._row = QHBoxLayout(self._widgets)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._notice = QLabel()
        self._notice.setObjectName("statusLabel")
        self._notice.setTextFormat(Qt.PlainText)
        self._notice.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._stack.addWidget(self._widgets)
        self._stack.addWidget(self._notice)
        super().addWidget(self._stack, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.clearMessage)

    def add_left(self, widget: QWidget, stretch: int = 0) -> None:
        """A widget on the left, where a notice replaces it while it shows."""
        self._row.addWidget(widget, stretch)

    def showMessage(self, text: str, timeout: int = 0) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        if not text:
            self.clearMessage()
            return
        self._notice.setText(text)
        self._notice.setToolTip(text)
        self._stack.setCurrentWidget(self._notice)
        self.messageChanged.emit(text)
        if timeout > 0:
            self._timer.start(timeout)

    def clearMessage(self) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        if self._stack.currentWidget() is self._notice:
            self._notice.clear()
            self._stack.setCurrentWidget(self._widgets)
            self.messageChanged.emit("")

    def currentMessage(self) -> str:  # noqa: N802 - Qt naming
        return self._notice.text() if self._stack.currentWidget() is self._notice else ""

    def route_status_tips(self, window: QWidget) -> None:
        """Send the window's status tips here rather than to QStatusBar's own message."""
        self._tips = _StatusTips(self)
        window.installEventFilter(self._tips)


class _StatusTips(QObject):
    def __init__(self, bar: NoticeStatusBar) -> None:
        super().__init__(bar)
        self._bar = bar

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if event.type() == QEvent.StatusTip:
            self._bar.showMessage(event.tip())
            return True
        return False


class BudgetBar(QWidget):
    """A segmented bar: one slice per prompt section, sized by token share."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("budgetBar")
        self.setFixedHeight(10)
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._report: BudgetReport | None = None

    def sizeHint(self) -> QSize:
        return QSize(240, 10)

    def set_report(self, report: BudgetReport | None) -> None:
        self._report = report
        if report is None:
            self.setToolTip("")
        else:
            lines = [f"{name}: ~{tokens:,}" for name, tokens in report.by_section.items() if tokens]
            lines.append(f"total: ~{report.total:,} / {report.budget:,}")
            self.setToolTip("\n".join(lines))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), theme.colour("meter_track"))

        report = self._report
        if report is None or report.budget <= 0 or report.total <= 0:
            return

        if report.over_budget:
            painter.fillRect(self.rect(), theme.colour("meter_over"))
            return

        width = self.width()
        offset = 0.0
        for name, tokens in report.by_section.items():
            if tokens <= 0:
                continue
            span = width * (tokens / report.budget)
            painter.fillRect(
                int(offset),
                0,
                max(int(span), 1),
                self.height(),
                theme.colour(SECTION_COLOURS.get(name, "meter_track")),
            )
            offset += span


class ElidingLabel(QLabel):
    """A status readout that gives way when the bar is short of room: under a
    large text size the readouts alone held the window at 1,471px. It asks
    for its whole text but can shrink to a few characters, cut with "…";
    the full text is its tooltip unless it has one of its own."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        hint = super().minimumSizeHint()
        return QSize(
            min(hint.width(), self.fontMetrics().horizontalAdvance("Mmmm…")), hint.height()
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        rect = self.contentsRect()
        text = self.text()
        if self.fontMetrics().horizontalAdvance(text) <= rect.width():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(
            rect,
            int(Qt.AlignVCenter | Qt.AlignLeft),
            self.fontMetrics().elidedText(text, Qt.ElideRight, rect.width()),
        )

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        if not self.toolTip() or self.property("toolTipIsText"):
            self.setToolTip(text)
            self.setProperty("toolTipIsText", True)

    def setToolTip(self, tip: str) -> None:  # noqa: N802 - Qt naming
        self.setProperty("toolTipIsText", False)
        super().setToolTip(tip)


class StatusStrip(QWidget):
    """Owns the three status-bar readouts so the window can update them in one call."""

    def __init__(self) -> None:
        super().__init__()
        self.budget_label = ElidingLabel("no prompt assembled")
        self.budget_label.setObjectName("statusLabel")
        self.bar = BudgetBar()
        self.lore_label = ElidingLabel("")
        self.lore_label.setObjectName("statusLabel")
        self.archive_label = ElidingLabel("")
        self.archive_label.setObjectName("statusLabel")
        self.cache_label = ElidingLabel("cache —")
        self.cache_label.setObjectName("statusLabel")
        self.cost_label = ElidingLabel("cost —")
        self.cost_label.setObjectName("statusLabel")
        # Pictures being drawn in the background (gui/image_jobs.py).
        self.pictures_label = ElidingLabel("")
        self.pictures_label.setObjectName("statusLabel")
        self.pictures_label.hide()

        self._session_cost = 0.0
        self._story_cost: float | None = None
        self._story_unpriced = 0
        self._story_calls = 0
        self._story_estimated = 0
        self._session_estimated = False
        # NanoGPT reports `cost` on some responses and omits it on others, so
        # a running total of 0.0 may mean "free" or "not told". Don't claim a
        # turn cost nothing when the endpoint simply didn't say.
        self._cost_reported = False

    def widgets(self) -> list[QWidget]:
        return [
            self.budget_label,
            self.bar,
            self.lore_label,
            self.archive_label,
            self.cache_label,
            self.cost_label,
            self.pictures_label,
        ]

    def set_pictures(self, count: int) -> None:
        self.pictures_label.setText(
            f"drawing {count} picture{'s' if count != 1 else ''}…" if count else ""
        )
        self.pictures_label.setVisible(bool(count))

    def set_lore(self, report: RetrievalReport | None) -> None:
        """§7's small indicator: how lore was found, and whether it fell back."""
        if report is not None and report.mode == "whole":
            self.lore_label.setText(f"lore all {len(report.standing)}")
            self.lore_label.setToolTip(
                f"The whole lorebook ({len(report.standing)} entries) is sent every turn, cached"
            )
            return
        if report is None or (not report.injected and not report.fallback_reason):
            self.lore_label.setText("")
            self.lore_label.setToolTip("")
            return

        count = len(report.injected)
        if report.fallback_reason:
            self.lore_label.setText(f"lore {count} ⚠")
            self.lore_label.setToolTip(
                f"Keyword matching only — embeddings unavailable ({report.fallback_reason})"
            )
            return

        how = {
            "picker": "picked after the last passage",
            "jev": "scored by Jev",
        }.get(report.selector, "semantic" if report.used_embeddings else "keyword")
        if report.selector_note:
            how += f" (similarity this turn: {report.selector_note})"
        self.lore_label.setText(f"lore {count}")
        self.lore_label.setToolTip(f"{count} entries injected (~{report.tokens:,} tokens), {how}")

    def set_archive(self, chapters: int) -> None:
        """How much of the story now reaches the model as summary (§5.2)."""
        if chapters <= 0:
            self.archive_label.setText("")
            self.archive_label.setToolTip("")
            return
        self.archive_label.setText(f"{chapters} archived")
        self.archive_label.setToolTip(
            f"{chapters} chapter summaries stand in for the oldest turns on this branch"
        )

    def set_budget(self, report: BudgetReport | None, *, approximate: bool = True) -> None:
        self.bar.set_report(report)
        if report is None:
            self.budget_label.setText("no prompt assembled")
            return
        tilde = "~" if approximate else ""
        text = f"{tilde}{report.total:,} / {report.budget:,} ({report.utilization:.0%})"
        if report.over_budget:
            text += "  over budget"
        elif report.warning:
            text += "  ⚠"
        self.budget_label.setText(text)

    def add_cost(self, usage: Usage) -> None:
        """Fold spend into the session total without touching the cache readout.

        Summariser calls are real spend but say nothing about how the *turn*
        cached, so they must not overwrite the cache indicator (§8.3).
        """
        # A reported zero is a cost (a subscription's included model): it
        # counts as reported, so a free session doesn't read "unknown".
        if usage.cost or usage.cost_reported:
            self._session_cost += usage.cost
            self._cost_reported = True
            self._session_estimated |= usage.cost_estimated
        self._show_cost()

    def set_story_cost(self, cost: float, unpriced: int, calls: int, estimated: int = 0) -> None:
        """The whole story's spend, from its API log (§8.3)."""
        self._story_cost = cost if calls else None
        self._story_calls = calls
        self._story_unpriced = unpriced
        self._story_estimated = estimated
        self._show_cost()

    def _show_cost(self) -> None:
        about = "~" if self._session_estimated else ""
        session = f"{about}${self._session_cost:.4f}" if self._cost_reported else "unknown"
        text = f"session {session}"
        tip = "Spend since this story was opened."
        if not self._cost_reported:
            tip += " Unknown: no call has reported a cost or had a listed price yet."
        if self._story_cost is not None and self._story_unpriced >= self._story_calls:
            # Nothing priced at all: a total would read "≥ $0.0000".
            text += "  ·  story unknown"
            tip += (
                f" Story: none of its {self._story_calls} call(s) reported a cost or had a "
                "listed price (Story → Usage and cost…)."
            )
        elif self._story_cost is not None:
            # No cost at all for some calls makes the total a lower bound;
            # costs worked out from listed prices make it an estimate.
            prefix = "≥ " if self._story_unpriced else ("~" if self._story_estimated else "")
            text += f"  ·  story {prefix}${self._story_cost:.4f}"
            tip += " Story: every call in its API log (Story → Usage and cost…)."
            if self._story_estimated:
                tip += (
                    f" {self._story_estimated} call(s) didn't report a cost; estimated from "
                    "the model's listed prices."
                )
            if self._story_unpriced:
                tip += f" {self._story_unpriced} call(s) have no cost and no known price."
        self.cost_label.setText(text)
        self.cost_label.setToolTip(tip)

    def add_usage(self, usage: Usage) -> None:
        self.add_cost(usage)
        if usage.cache_read_tokens or usage.cache_creation_tokens:
            self.cache_label.setText(
                f"cache {usage.cache_read_tokens:,} read / {usage.cache_creation_tokens:,} written"
            )
        elif usage.prompt_tokens:
            self.cache_label.setText("cache miss")
        else:
            self.cache_label.setText("cache —")

    def reset_session_cost(self) -> None:
        self._session_cost = 0.0
        self._cost_reported = False
        self._story_cost = None
        self._story_unpriced = 0
        self._story_estimated = 0
        self._session_estimated = False
        self.cost_label.setText("cost —")
        self.cache_label.setText("cache —")
        self.set_lore(None)
        self.set_archive(0)
        self.set_budget(None)
        self.bar.setToolTip("")
        self.budget_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
