"""Help → Models SealedLore recommends and adjusts for…: the models measured
best for each use (models/config.py's lists, the ones offered under Choose
beside a field), and every model SealedLore treats differently, what it does
and why (engine/model_tuning.py), read-only.

The author (Nov 18 2026): the app should show which models it is tuned for
and how, and the player should clearly see which are the best models. Each
model field says so beside it ("★ recommended", "· tuned prompts"); the
model list has a SealedLore column; this is the whole of it in one place.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.model_tuning import described, is_tuned
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.config import RECOMMENDED_TEXT_MODELS

TITLE = "Models SealedLore recommends and adjusts for"
RECOMMENDED_TAB = "Recommended"
ADJUSTED_TAB = "Adjusted for"
RECOMMENDED_INTRO = (
    "The models measured best for each use, the first the one SealedLore starts with. "
    "Each is offered under Choose beside the field where that model is chosen, and a "
    "field shows “★” beside one of them. Claude models are reached on Anthropic's own "
    "API when you have that endpoint (Settings → Endpoints)."
)
INTRO = (
    "SealedLore sends every model the same story, but for some models it words a text its "
    "own way or leaves one out, sends less of the lore with a turn, or shapes the request "
    "the way the model's own service requires. These are all of them. A model field shows "
    "“· tuned” beside a model they apply to there, “· tuned prompts” when its prompt is "
    "worded for it.\n\n"
    "A prompt is worded for a model only where blind judges preferred what it wrote that way. "
    "The storyteller's prompt was tried in other wordings for Claude Opus 5.5, Claude Sonnet "
    "4.6, GLM 5.3, MiMo V2.6 Pro and DeepSeek V3.2, each aimed at what that model gets wrong, "
    "and none did better than the usual one without a new fault, so those models are sent it "
    "as it is (but for Opus 5.5's one sentence below)."
)
KINDS = {"prompt": "Prompt", "lore": "Lore", "request": "Request"}
# Each use the lists cover, in the order a story calls them, as Settings →
# Models names the field (and Played by on a card, for a character).
USES = (
    (
        "story",
        "Story model",
        "storyteller",
        "The storyteller: writes every passage. Five were ranked by blind judges over single "
        "turns and whole runs of them: Claude Opus 5.5 first, GLM 5.3 and Claude Sonnet 4.6 "
        "next, then MiMo V2.6 Pro and DeepSeek V3.2, which aren't recommended.",
    ),
    (
        "character",
        "A character's own model",
        "character",
        "Plays a character with a model of their own (a trial; Played by on their card).",
    ),
    ("scene_model", "Scene model", "", "Reads the scene after every passage."),
    ("plot_model", "Plot model", "", "Decides when the plot's events happen."),
    ("lore_model", "Lore model", "", "Picks the lore a turn needs, for a large lorebook."),
    (
        "record_model",
        "Public record model",
        "",
        "Keeps what everyone knows, for characters played by their own model (a trial).",
    ),
)


def _text(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _page(intro: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.addWidget(_text(intro))
    body = QWidget()
    rows = QVBoxLayout(body)
    rows.setSpacing(14)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(body)
    layout.addWidget(scroll, 1)
    return page, rows


class ModelTuningDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        recommended, rows = _page(RECOMMENDED_INTRO)
        self.recommendations: list[QLabel] = []
        for key, name, role, about in USES:
            choices = RECOMMENDED_TEXT_MODELS.get(key, ())
            if not choices:
                continue
            head = QLabel(name)
            head.setObjectName("peopleHeading")
            rows.addWidget(head)
            rows.addWidget(_text(about))
            for choice in choices:
                tag = f" ({choice.tag})" if choice.tag else ""
                tuned = " · tuned prompts" if role and is_tuned(choice.id, role) else ""
                line = _text(f"★ {choice.label}{tag}{tuned}\n{choice.note}")
                rows.addWidget(line)
                self.recommendations.append(line)
        rows.addStretch(1)
        self.tabs.addTab(recommended, RECOMMENDED_TAB)

        adjusted, rows = _page(INTRO)
        self.entries: list[QLabel] = []
        for models, kind, what, why in described():
            head = QLabel(f"{models}  ·  {KINDS[kind]}")
            head.setObjectName("peopleHeading")
            head.setWordWrap(True)
            rows.addWidget(head)
            text = _text(f"{what}\n\nWhy: {why}")
            rows.addWidget(text)
            self.entries.append(text)
        rows.addStretch(1)
        self.tabs.addTab(adjusted, ADJUSTED_TAB)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        size_to_screen(self, 0.45, 0.65, at_least=QSize(600, 480))


def show_model_tuning(parent: QWidget) -> None:
    dialog = ModelTuningDialog(parent)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()  # read, then freed
