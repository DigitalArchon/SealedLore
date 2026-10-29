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
        chat: bool = False,
    ) -> None:
        """`chat`: named as a chat's cards are ("Part 3"), and the chat intact."""
        if not summaries:
            self.hide()
            return
        unit = "Part" if chat else "Chapter"
        if positions:
            named = ", ".join(f"{unit} {position}" for position in positions)
        else:
            named = f"{len(summaries)} {unit.lower()}s"
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
            f"{head} The edit itself is saved and the {'chat' if chat else 'story'} is "
            "intact. Nothing is rebuilt until you ask, so make any other edits first. "
            f"Rebuilding runs in the background and costs a summariser call per {unit.lower()}; "
            "each new summary changes the cached prompt prefix once."
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

    def show_over(self, *, full_total: int, budget: int, left_out: int, chat: bool = False) -> None:
        dropped = (
            f"the oldest {left_out} message{'s are' if left_out != 1 else ' is'} being left out "
            "of every prompt"
            if left_out
            else "the next turn will leave the oldest messages out"
        )
        if chat:
            self.message.setText(
                f"The chat is over its context budget (~{full_total:,} of {budget:,} tokens), "
                f"so {dropped}, and the model no longer sees them. Archiving summarises "
                "them into parts so the chat keeps the gist."
            )
        else:
            self.message.setText(
                f"The story is over its context budget (~{full_total:,} of {budget:,} tokens), "
                f"so {dropped}, and the storyteller no longer sees them. Archiving summarises "
                "them into chapters so the story keeps the gist."
            )
        self.show()


class HeldAwayBanner(QFrame):
    """The character the author plays isn't on the scene's roster.

    Moving to another branch or take, deleting, or taking a turn back can
    land on a passage where the roster has them elsewhere (another line's
    read sent them away). The storyteller is then told the author's own
    character is out of the scene, and Speaking as can't offer them.
    Nothing changes on its own: putting them back cuts the scene to them
    (`StorySession.hold`), which can contradict the passage standing here,
    so it is the author's click.
    """

    put_back_requested = Signal()
    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("stalenessBanner")

        self.message = QLabel()
        self.message.setObjectName("stalenessText")
        self.message.setWordWrap(True)

        self.put_back_button = QPushButton()
        self.put_back_button.clicked.connect(self.put_back_requested)
        self.dismiss_button = QPushButton("Leave it")
        self.dismiss_button.clicked.connect(self.dismissed)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addWidget(self.put_back_button)
        actions.addWidget(self.dismiss_button)
        actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addWidget(self.message)
        layout.addLayout(actions)
        self.hide()

    def show_away(self, name: str) -> None:
        self.message.setText(
            f"You are playing {name}, but the scene here doesn't have {name} in it: the "
            f"storyteller is told {name} is elsewhere, and Speaking as can't offer {name}."
        )
        self.put_back_button.setText(f"Put {name} back in the scene")
        self.put_back_button.setToolTip(
            f"The story moves to where {name} is, as choosing them under Playing does; "
            "the next passage fills in who is with them"
        )
        self.dismiss_button.setToolTip(
            f"Keep the scene as it is. Choosing {name} under Playing again puts them back later"
        )
        self.show()
