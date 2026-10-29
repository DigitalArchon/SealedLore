"""Status-bar widgets: token budget bar, cache indicator, session cost. See §8.3, §10."""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QPainter
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
from sealedlore.engine.routing import describe_sent
from sealedlore.gui import theme
from sealedlore.models.node import Usage

# Each section of the prompt's colour on the budget meter (gui/theme.py).
SECTION_COLOURS = {
    "system": "meter_system",
    "summaries": "meter_summaries",
    "history": "meter_history",
    "tail": "meter_tail",
}


DISMISS_TIP = "Click to dismiss."


class _Notice(QLabel):
    """The notice text: a click puts the controls under it back at once
    (the author: the reminder after changing the storyteller's model hid
    the model and route buttons for seconds)."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class NoticeStatusBar(QStatusBar):
    """A status bar whose messages swap with its widgets instead of hiding them.

    QStatusBar hides its normal widgets while a message shows, and in doing so
    clears their "explicitly hidden" flag; any relayout of the bar during the
    message (a label's text changing after a turn is enough) then shows them
    again, drawn over the message text until it times out. So the message
    here is a page of our own: the left-hand widgets on one page, the notice
    on the other, and QStatusBar's own message is never used. Qt's status
    tips (hovering a menu item) arrive through `route_status_tips`. A click
    on the notice dismisses it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._widgets = QWidget()
        self._row = QHBoxLayout(self._widgets)
        self._row.setContentsMargins(4, 2, 0, 2)
        # The model, route and clock buttons each stand apart.
        self._row.setSpacing(8)
        self._notice = _Notice()
        self._notice.setObjectName("statusLabel")
        self._notice.setTextFormat(Qt.PlainText)
        self._notice.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._notice.setCursor(Qt.PointingHandCursor)
        self._notice.clicked.connect(self.clearMessage)
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
        self._notice.setToolTip(f"{text}\n\n{DISMISS_TIP}")
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


# A wait for a read after the passage counts up once it passes this many
# seconds (live: an 86 s scene read showed only "Reading the scene with …").
WAIT_SAID_AFTER = 10
# The seconds a notice keeps the bar before a wait's count takes it back:
# the next notice used to replace it before it could be read.
NOTICE_HOLD = 6


class WaitCounter(QObject):
    """A long wait for a read after the passage (`GenerationWorker.waiting`)
    counts up in the status bar: "Reading the scene with … 45 s; Stop skips
    it". Stop does: the read's call is cancelled, and the roster, clock or
    map stays as it was. A notice shown meanwhile keeps the bar for
    `NOTICE_HOLD` seconds first, so a read's result isn't replaced at once
    by the wait for the next."""

    def __init__(self, bar: NoticeStatusBar, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(bar)
        self._bar = bar
        self._clock = clock
        self._text: str | None = None
        self._since = 0.0
        self._shown = ""
        self._other_at = float("-inf")
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)
        bar.messageChanged.connect(self._message_changed)

    def _message_changed(self, text: str) -> None:
        if text and text != self._shown:
            self._other_at = self._clock()

    def wait(self, text: str, since: float) -> None:
        self._text, self._since = text, since
        if self._clock() - self._other_at >= NOTICE_HOLD:
            self._show(text)
        self._timer.start()
        self.tick()

    def stop(self) -> None:
        self._text = None
        self._timer.stop()

    def waiting(self) -> bool:
        return self._text is not None

    def tick(self) -> None:
        if self._text is None:
            self._timer.stop()
            return
        now = self._clock()
        elapsed = int(now - self._since)
        if elapsed < WAIT_SAID_AFTER or now - self._other_at < NOTICE_HOLD:
            return
        self._show(f"{self._text} {elapsed} s; Stop skips it.")

    def _show(self, text: str) -> None:
        self._shown = text
        self._bar.showMessage(text, 12000)


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


class CostLabel(ElidingLabel):
    """The spend readout. A click opens Story → Usage and cost… (the
    author, Oct 29 2026): the breakdown is one click from the numbers it
    explains, not only in the Story menu."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.text():
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


def _short(tokens: int) -> str:
    """2,283 as "2.3k", 60,000 as "60k": a readout, the exact count in its tooltip."""
    if tokens < 1000:
        return f"{tokens}"
    thousands = tokens / 1000
    return f"{thousands:.1f}k" if thousands < 10 else f"{thousands:.0f}k"


class StatusStrip(QWidget):
    """Owns the three status-bar readouts so the window can update them in one call."""

    def __init__(self) -> None:
        super().__init__()
        # Plain words first (Oct 18 2026, the author: the bar was technical
        # to a new player); the numbers stay in each readout's tooltip.
        self.budget_label = ElidingLabel("")
        self.budget_label.setObjectName("statusLabel")
        self.bar = BudgetBar()
        self.lore_label = ElidingLabel("")
        self.lore_label.setObjectName("statusLabel")
        self.archive_label = ElidingLabel("")
        self.archive_label.setObjectName("statusLabel")
        self.cache_label = ElidingLabel("")
        self.cache_label.setObjectName("statusLabel")
        self.cost_label = CostLabel("")
        self.cost_label.setObjectName("statusLabel")
        self.cost_label.setCursor(Qt.PointingHandCursor)
        # Pictures being drawn in the background (gui/image_jobs.py).
        self.pictures_label = ElidingLabel("")
        self.pictures_label.setObjectName("statusLabel")
        self.pictures_label.hide()
        # Whether NanoGPT followed the routes asked for (engine/routing.py):
        # shown once a routed call has come back, ⚠ when one wasn't followed.
        self.route_label = ElidingLabel("")
        self.route_label.setObjectName("statusLabel")
        self.route_label.hide()

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
            self.route_label,
        ]

    def set_routes(self, checks: list) -> None:
        """`checks`: (when, RouteCheck, roles) for each route used, from
        `StorySession.route_checks`. The author: if a route isn't being met,
        the window should say so where it can be seen."""
        if not checks:
            self.route_label.setText("")
            self.route_label.setToolTip("")
            self.route_label.hide()
            return
        missed = [c for c in checks if c[1].met is False]
        lines = []
        for at, check, roles in checks:
            who = ", ".join(roles) or "earlier calls"
            state = {True: "as asked", False: "⚠ NOT followed", None: "can't tell"}[check.met]
            cost = f", ${check.cost:.4f}" if check.cost is not None else ""
            lines.append(
                f"{who}: {check.model} · {describe_sent(check.provider)}: {state} "
                f"(last call {at[11:16]} UTC{cost}). {check.reason[:1].upper()}{check.reason[1:]}."
            )
        self.route_label.setText(f"⚠ route not followed ({len(missed)})" if missed else "⚡ routes")
        self.route_label.setToolTip(
            "NanoGPT names no host in its replies, so each routed call's bill is the "
            "evidence:\n\n" + "\n".join(lines)
        )
        self.route_label.show()

    def set_pictures(self, count: int) -> None:
        self.pictures_label.setText(
            f"drawing {count} picture{'s' if count != 1 else ''}…" if count else ""
        )
        self.pictures_label.setVisible(bool(count))

    def set_lore(self, report: RetrievalReport | None) -> None:
        """§7's small indicator: how lore was found, and whether it fell back."""
        if report is not None and report.mode == "whole":
            self.lore_label.setText(f"Lore: all {len(report.standing)} sent")
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
            self.lore_label.setText(f"Lore: {count} sent ⚠")
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
        self.lore_label.setText(f"Lore: {count} sent")
        self.lore_label.setToolTip(f"{count} entries injected (~{report.tokens:,} tokens), {how}")

    def set_archive(self, chapters: int) -> None:
        """How much of the story now reaches the model as summary (§5.2)."""
        if chapters <= 0:
            self.archive_label.setText("")
            self.archive_label.setToolTip("")
            return
        self.archive_label.setText(f"{chapters} chapter{'s' if chapters != 1 else ''} summarised")
        self.archive_label.setToolTip(
            f"{chapters} chapter summaries stand in for the oldest turns on this branch"
        )

    def set_budget(self, report: BudgetReport | None, *, approximate: bool = True) -> None:
        self.bar.set_report(report)
        if report is None:
            self.budget_label.setText("")
            self.budget_label.setToolTip("")
            return
        tilde = "~" if approximate else ""
        of = f"({_short(report.total)} of {_short(report.budget)})"
        text = f"Context {report.utilization:.0%} used {of}"
        if report.over_budget:
            text = f"Context over budget {of}"
        elif report.warning:
            text += "  ⚠"
        self.budget_label.setText(text)
        self.budget_label.setToolTip(
            f"{tilde}{report.total:,} of {report.budget:,} tokens: what the storyteller is "
            "sent next turn, against the budget set in File → Settings… → Context. Older "
            "messages are summarised into chapters before it fills."
        )

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
        text, tip = cost_words(
            session=self._session_cost,
            session_reported=self._cost_reported,
            session_estimated=self._session_estimated,
            total=self._story_cost,
            calls=self._story_calls,
            unpriced=self._story_unpriced,
            estimated=self._story_estimated,
        )
        self.cost_label.setText(text)
        self.cost_label.setToolTip(f"{tip} Click for the full breakdown." if tip else tip)

    def add_usage(self, usage: Usage) -> None:
        self.add_cost(usage)
        if usage.cache_read_tokens or usage.cache_creation_tokens:
            self.cache_label.setText(
                f"Cache: {_short(usage.cache_read_tokens)} reused, "
                f"{_short(usage.cache_creation_tokens)} new"
            )
        elif usage.prompt_tokens:
            self.cache_label.setText("Cache: none reused")
        else:
            self.cache_label.setText("")
        self.cache_label.setToolTip(
            f"Last turn: {usage.cache_read_tokens:,} tokens of the prompt reused from the "
            f"endpoint's cache (cheaper and quicker), {usage.cache_creation_tokens:,} written "
            "to it for the next turn."
        )

    def reset_session_cost(self) -> None:
        self._session_cost = 0.0
        self._cost_reported = False
        self._story_cost = None
        self._story_unpriced = 0
        self._story_estimated = 0
        self._session_estimated = False
        self.cost_label.setText("")
        self.cache_label.setText("")
        self.set_lore(None)
        self.set_archive(0)
        self.set_budget(None)
        self.bar.setToolTip("")
        self.budget_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)


def cost_words(
    *,
    session: float,
    session_reported: bool,
    session_estimated: bool,
    total: float | None,
    calls: int,
    unpriced: int,
    estimated: int,
    whole: str = "Story",
    opened: str = "this story was opened",
    report: str = "Story → Usage and cost…",
) -> tuple[str, str]:
    """The spend line and its tooltip: this session's, and the whole
    story's from its API log. `whole`, `opened` and `report` name what is
    counted, and where the full report is. Empty before anything is sent."""
    if not session_reported and not calls:
        # Nothing has been sent yet: nothing to say.
        return "", ""
    about = "~" if session_estimated else ""
    spent = f"{about}${session:.4f}" if session_reported else "unpriced"
    text = f"This session {spent}"
    tip = f"Spend since {opened}."
    if not session_reported:
        tip += (
            " Unpriced: no call has reported a cost or had a listed price yet (an "
            "endpoint such as Anthropic's lists no prices)."
        )
    if total is not None and unpriced >= calls:
        # Nothing priced at all: a total would read "≥ $0.0000" (the
        # author, Oct 18 2026: say unpriced).
        text += f"  ·  {whole} unpriced ({calls} calls)"
        tip += (
            f" {whole}: none of its {calls} call(s) reported a cost or had a "
            f"listed price ({report})."
        )
    elif total is not None:
        # No cost at all for some calls makes the total a lower bound;
        # costs worked out from listed prices make it an estimate.
        prefix = "≥ " if unpriced else ("~" if estimated else "")
        text += f"  ·  {whole} {prefix}${total:.4f}"
        tip += f" {whole}: every call in its API log ({report})."
        if estimated:
            tip += (
                f" {estimated} call(s) didn't report a cost; estimated from "
                "the model's listed prices."
            )
        if unpriced:
            tip += f" {unpriced} call(s) have no cost and no known price."
    return text, tip
