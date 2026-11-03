"""Choosing a story's picture style (engine/picture_styles.py): one of the
ready-made styles, the default, or the author's own words. Setup, the new
story guide and the portraits dialog all use it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QWidget

from sealedlore.engine.picture_styles import BY_ID, DEFAULT_LABEL, OWN_LABEL, STYLES
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.gui.fields import set_field_text

OWN = "own"  # the combo's data for the author's own words
KEEP = "keep"  # a picture changed in its own style (`keep=`)
KEEP_LABEL = "Keep its style"
KEEP_TIP = "The picture is changed as it is drawn now."
OWN_PLACEHOLDER = "e.g. painterly science-fiction concept art"
DEFAULT_TIP = "Every picture realistic, like a photograph."
# A simple chat has no style of its own: its pictures' writer chooses one.
WRITER_CHOOSES = "Any style: the writer chooses"
WRITER_CHOOSES_TIP = "The prompt writer chooses a style that suits what is asked for."
OWN_TIP = "Describe the look in your own words: they go into every picture's prompt."


class PictureStyleField(QWidget):
    """A ready-made style, the default or the author's own words, as
    (`Story.picture_style`, `Story.image_style`)."""

    changed = Signal()

    def __init__(
        self,
        picture_style: str = "",
        image_style: str = "",
        *,
        texts: PromptTexts = DEFAULT_TEXTS,
        parent: QWidget | None = None,
        default: tuple[str, str] = (DEFAULT_LABEL, DEFAULT_TIP),
        keep: bool = False,
        from_picture: Callable[[], str | None] | None = None,
    ) -> None:
        """`default`: what the first choice, no style, is called and its
        tooltip. `keep`: a picture being changed, first offered as it is
        (`KEEP`, chosen when `picture_style` is KEEP). `from_picture`: offers
        From a picture…, which gives the words for a picture's style (None
        when there are none), taken as one's own words."""
        super().__init__(parent)
        self.combo = QComboBox()
        if keep:
            self.combo.addItem(KEEP_LABEL, KEEP)
            self.combo.setItemData(0, KEEP_TIP, Qt.ToolTipRole)
        self.combo.addItem(default[0], "")
        self.combo.setItemData(self.combo.count() - 1, default[1], Qt.ToolTipRole)
        for style in STYLES:
            self.combo.addItem(style.label, style.id)
            # What it puts into a prompt, as the editor has it.
            self.combo.setItemData(self.combo.count() - 1, texts[style.key], Qt.ToolTipRole)
        self.combo.addItem(OWN_LABEL, OWN)
        self.combo.setItemData(self.combo.count() - 1, OWN_TIP, Qt.ToolTipRole)
        self.own = QLineEdit()
        self.own.setPlaceholderText(OWN_PLACEHOLDER)
        self.own.setToolTip(OWN_TIP)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.combo)
        layout.addWidget(self.own, 1)
        self.from_picture: QPushButton | None = None
        if from_picture is not None:
            self.from_picture = QPushButton("From a picture…")
            self.from_picture.setToolTip(
                "A picture whose style the story takes: a model that sees pictures describes it "
                "in words, yours to read and change"
            )
            self.from_picture.clicked.connect(lambda: self._take_from_picture(from_picture))
            layout.addWidget(self.from_picture)
        self.set_value(picture_style, image_style)
        self.combo.currentIndexChanged.connect(self._on_choice)
        self.own.textChanged.connect(lambda _text: self.changed.emit())

    def set_value(self, picture_style: str, image_style: str) -> None:
        own = " ".join(image_style.split())
        blocked = self.combo.blockSignals(True)
        if picture_style == KEEP and self.combo.findData(KEEP) >= 0:
            self.combo.setCurrentIndex(self.combo.findData(KEEP))
        elif picture_style in BY_ID:
            self.combo.setCurrentIndex(self.combo.findData(picture_style))
        elif own:
            self.combo.setCurrentIndex(self.combo.findData(OWN))
        else:
            self.combo.setCurrentIndex(self.combo.findData(""))
        self.combo.blockSignals(blocked)
        blocked = self.own.blockSignals(True)
        set_field_text(self.own, own)
        self.own.blockSignals(blocked)
        self._sync()

    def value(self) -> tuple[str, str]:
        """(a ready-made style's id or "", the author's own words or ""), or
        (KEEP, "") for a picture kept in its own style."""
        chosen = self.combo.currentData()
        if chosen == OWN:
            words = " ".join(self.own.text().split())
            return "", words
        return chosen or "", ""

    def _take_from_picture(self, describe: Callable[[], str | None]) -> None:
        words = describe()
        if words:
            self.set_value("", words)
            self.changed.emit()

    def _on_choice(self) -> None:
        self._sync()
        if self.combo.currentData() == OWN:
            self.own.setFocus()
        self.changed.emit()

    def _sync(self) -> None:
        self.own.setVisible(self.combo.currentData() == OWN)
        self.combo.setToolTip(self.combo.currentData(Qt.ToolTipRole) or "")
