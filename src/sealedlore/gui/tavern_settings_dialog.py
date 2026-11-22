"""The Lore page's SillyTavern settings…: SillyTavern's own World Info
settings for a story whose lore fires as SillyTavern's does
(`TavernWorldInfo`, engine/tavern_firing.py). Its defaults are SillyTavern's;
a lorebook file doesn't carry them (in SillyTavern they are the user's)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.fields import set_spin_value
from sealedlore.models.lore import TavernWorldInfo

INTRO = (
    "SillyTavern's World Info settings, for this story. A lorebook doesn't carry them "
    "(in SillyTavern they are yours, for every chat), so they start at SillyTavern's "
    "own: set them as you have them there to fire the lorebook exactly as you're used to."
)


class TavernSettingsDialog(QDialog):
    def __init__(self, settings: TavernWorldInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SillyTavern settings")
        column = QVBoxLayout(self)
        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        column.addWidget(intro)
        form = QFormLayout()
        column.addLayout(form)
        self.scan_depth = self._number(settings.scan_depth, 0, 1000)
        self.scan_depth.setToolTip("How many of the latest messages are searched for keys.")
        self.budget_percent = self._number(settings.budget_percent, 1, 100, suffix=" %")
        self.budget_percent.setToolTip("The share of the story's context the fired lore may take.")
        self.budget_cap = self._number(settings.budget_cap, 0, 1_000_000, none="no cap")
        self.min_activations = self._number(settings.min_activations, 0, 1000, none="off")
        self.min_activations.setToolTip(
            "While fewer entries than this fired, the search reaches one message further back."
        )
        self.min_depth_max = self._number(
            settings.min_activations_depth_max, 0, 1000, none="the whole story"
        )
        self.max_recursion_steps = self._number(
            settings.max_recursion_steps, 0, 1000, none="unlimited"
        )
        self.recursive = QCheckBox("An entry's text may fire others (recursive scan)")
        self.recursive.setChecked(settings.recursive)
        self.case_sensitive = QCheckBox("Keys match case")
        self.case_sensitive.setChecked(settings.case_sensitive)
        self.whole_words = QCheckBox("Keys match whole words")
        self.whole_words.setChecked(settings.match_whole_words)
        self.group_scoring = QCheckBox("Group scoring (the best match wins its group)")
        self.group_scoring.setChecked(settings.use_group_scoring)
        self.include_names = QCheckBox("Search messages with their speakers' names")
        self.include_names.setChecked(settings.include_names)
        form.addRow("Scan depth", self.scan_depth)
        form.addRow("Budget", self.budget_percent)
        form.addRow("Budget cap (tokens)", self.budget_cap)
        form.addRow("Min activations", self.min_activations)
        form.addRow("Max depth for them", self.min_depth_max)
        form.addRow("Max recursion steps", self.max_recursion_steps)
        for box in (
            self.recursive,
            self.case_sensitive,
            self.whole_words,
            self.group_scoring,
            self.include_names,
        ):
            form.addRow(box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        reset = QPushButton("SillyTavern's defaults")
        reset.clicked.connect(lambda: self._load(TavernWorldInfo()))
        buttons.addButton(reset, QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    @staticmethod
    def _number(value: int, low: int, high: int, *, suffix: str = "", none: str = "") -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        set_spin_value(box, value)
        if suffix:
            box.setSuffix(suffix)
        if none:
            box.setSpecialValueText(none)
        return box

    def _load(self, settings: TavernWorldInfo) -> None:
        set_spin_value(self.scan_depth, settings.scan_depth)
        set_spin_value(self.budget_percent, settings.budget_percent)
        set_spin_value(self.budget_cap, settings.budget_cap)
        set_spin_value(self.min_activations, settings.min_activations)
        set_spin_value(self.min_depth_max, settings.min_activations_depth_max)
        set_spin_value(self.max_recursion_steps, settings.max_recursion_steps)
        self.recursive.setChecked(settings.recursive)
        self.case_sensitive.setChecked(settings.case_sensitive)
        self.whole_words.setChecked(settings.match_whole_words)
        self.group_scoring.setChecked(settings.use_group_scoring)
        self.include_names.setChecked(settings.include_names)

    def settings(self) -> TavernWorldInfo:
        return TavernWorldInfo(
            scan_depth=self.scan_depth.value(),
            budget_percent=self.budget_percent.value(),
            budget_cap=self.budget_cap.value(),
            min_activations=self.min_activations.value(),
            min_activations_depth_max=self.min_depth_max.value(),
            max_recursion_steps=self.max_recursion_steps.value(),
            recursive=self.recursive.isChecked(),
            case_sensitive=self.case_sensitive.isChecked(),
            match_whole_words=self.whole_words.isChecked(),
            use_group_scoring=self.group_scoring.isChecked(),
            include_names=self.include_names.isChecked(),
        )
