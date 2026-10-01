"""Non-modal banners above the transcript.

Editing a message that a chapter summary covers leaves the two disagreeing,
and this is how the author finds out. It never blocks the edit and never
rebuilds anything on its own — those are both explicit requirements. The
rebuild it offers runs in the background (engine/session_rebuild.py).
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from sealedlore.models.summary import Summary


class StalenessBanner(QFrame):
    rebuild_requested = Signal()
    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("stalenessBanner")

        self.message = QLabel()
        self.message.setObjectName("stalenessText")
        self.message.setWordWrap(True)

        self.rebuild_button = QPushButton("Rebuild affected summaries")
        self.rebuild_button.clicked.connect(self.rebuild_requested)
        self.dismiss_button = QPushButton("Leave them")
        self.dismiss_button.setToolTip(
            "Keep the summaries as they are; they stay marked stale in the Story so far tab"
        )
        self.dismiss_button.clicked.connect(self.dismissed)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addWidget(self.rebuild_button)
        actions.addWidget(self.dismiss_button)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addWidget(self.message)
        layout.addLayout(actions)
        self.hide()

    def show_stale(
        self,
        summaries: Sequence[Summary],
        *,
        positions: Sequence[int] = (),
        rebuilding: bool = False,
    ) -> None:
        if not summaries:
            self.hide()
            return
        if positions:
            named = ", ".join(f"Chapter {position}" for position in positions)
        else:
            named = f"{len(summaries)} chapters"
        self.rebuild_button.setEnabled(not rebuilding)
        if rebuilding:
            self.message.setText(
                f"Rebuilding {named} in the background. Keep playing: the old summaries are "
                "sent until the new ones are in."
            )
            self.show()
            return
        if len(summaries) == 1:
            head = f"{named} covers text you have since edited, so its summary is out of date."
        else:
            head = f"{named} cover text you have since edited, so their summaries are out of date."
        self.message.setText(
            f"{head} The edit itself is saved and the story is intact. Nothing is rebuilt "
            "until you ask, so make any other edits first. Rebuilding runs in the background "
            "and costs a summariser call per chapter; each new summary changes the cached "
            "prompt prefix once."
        )
        self.show()


class OverBudgetBanner(QFrame):
    """The prompt is over its context budget and nothing will archive it.

    Only with automatic archival off: the oldest prose is falling out of every
    prompt, and the author is the one who has to act — archive now, or let the
    app do it from here on.
    """

    archive_requested = Signal()
    automatic_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("stalenessBanner")

        self.message = QLabel()
        self.message.setObjectName("stalenessText")
        self.message.setWordWrap(True)

        self.archive_button = QPushButton("Archive now")
        self.archive_button.setToolTip(
            "Summarise the oldest turns into chapters until the prompt is back under "
            "the archive target (Settings → Context → Archive down to)"
        )
        self.archive_button.clicked.connect(self.archive_requested)
        self.automatic_button = QPushButton("Archive automatically")
        self.automatic_button.setToolTip(
            "Turn on automatic archival: chapters are written in the background as the "
            "story fills its budget, without holding up a turn"
        )
        self.automatic_button.clicked.connect(self.automatic_requested)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addWidget(self.archive_button)
        actions.addWidget(self.automatic_button)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addWidget(self.message)
        layout.addLayout(actions)
        self.hide()

    def show_over(self, *, full_total: int, budget: int, left_out: int) -> None:
        dropped = (
            f"the oldest {left_out} message{'s are' if left_out != 1 else ' is'} being left out "
            "of every prompt"
            if left_out
            else "the next turn will leave the oldest messages out"
        )
        self.message.setText(
            f"The story is over its context budget (~{full_total:,} of {budget:,} tokens), "
            f"so {dropped}, and the storyteller no longer sees them. Archiving summarises "
            "them into chapters so the story keeps the gist."
        )
        self.show()
