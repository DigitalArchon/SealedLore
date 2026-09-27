"""The transcript view.

A column of per-message widgets rather than one rich-text document: in-character
prose, narration, OOC and director messages each need their own treatment, and
later phases hang per-message controls off these same frames.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.dice import chip_text
from sealedlore.gui import theme
from sealedlore.gui.ref_images import load_pixmap
from sealedlore.models.aside import Aside
from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage
from sealedlore.models.node import (
    CHAT_USER_ID,
    DIRECTOR_SPEAKER_ID,
    NARRATOR_SPEAKER_ID,
    Node,
    Usage,
)

# How far from the bottom still counts as "reading the newest text".
FOLLOW_TOLERANCE_PX = 48
# Messages built when a story opens; earlier ones are built a page at a time
# as the reader scrolls up. Building all 498 of a long story's messages took
# 11s of layout, and every branch switch paid it again.
PAGE = 40
# The most messages built at once. Scrolling builds a page at one end and
# drops one at the other past this; a jump rebuilds around its target. Qt's
# layout and styling grow with what is built: a text-size change took 8.4s
# with 500 messages built (tools/bench/transcript_bench.py).
MAX_BUILT = 3 * PAGE
# A picture in the transcript is at most this tall; click it for full size.
PICTURE_MAX_HEIGHT = 480
# The widest the column of messages grows, centred beyond it: at 1920px lines
# ran to ~1,170px, far past a comfortable reading measure.
READING_WIDTH = 920
COLUMN_MARGIN = 18

# With a background picture: the view and column see through to it, a wash
# keeps the text readable, and the message frames go translucent.
WASH_ALPHA = 150


def background_qss() -> str:
    """The messages made see-through over a background picture, in the
    current theme's colours (gui/theme.py)."""
    see = theme.translucent
    return f"""
#transcript, #transcriptContainer {{ background: transparent; }}
#message {{ background: {see("panel", 220)}; }}
#message[kind="character"] {{ background: {see("turn_bg", 220)}; }}
#message[kind="narration"] {{ background: {see("narration_bg", 220)}; }}
#message[kind="director"] {{ background: {see("director_bg", 220)}; }}
#message[kind="aside"] {{ background: {see("aside_bg", 220)}; }}
#message[kind="picture"] {{ background: {see("picture_bg", 220)}; }}
#message[kind="summary"] {{ background: {see("summary_bg", 220)}; }}
#message[archived="true"] {{ background: {see("archived_bg", 215)}; }}
#placeholder {{ background: transparent; }}
"""


@dataclass(frozen=True)
class PendingPicture:
    job_id: str
    text: str


# The pictures anchored after a node (None: before the first): a finished
# one as (record, its file), or one still being drawn.
PicturesFor = Callable[[str | None], "Sequence[tuple[GeneratedImage, Path] | PendingPicture]"]


def speaker_label_for(speaker_id: str, cast: Sequence[Character]) -> str:
    if speaker_id == NARRATOR_SPEAKER_ID:
        return "Narrator"
    if speaker_id == CHAT_USER_ID:
        return "You"
    if speaker_id == DIRECTOR_SPEAKER_ID:
        # "Direction", the glossary's word for the author's instruction turn:
        # "Director" is the plot's (engine/director.py), a different thing.
        return "Direction"
    character = next((c for c in cast if c.id == speaker_id), None)
    return character.name if character else "Unknown"


def kind_for(speaker_id: str) -> str:
    """The QSS selector class for an author turn's visual treatment."""
    if speaker_id == NARRATOR_SPEAKER_ID:
        return "narration"
    if speaker_id == DIRECTOR_SPEAKER_ID:
        return "director"
    return "character"


def speaker_label(node: Node, cast_by_id: dict[str, Character], chat: bool = False) -> str:
    if node.kind == "assistant":
        return "Assistant" if chat else "Story"
    return speaker_label_for(node.speaker_id, list(cast_by_id.values()))


def word_count(text: str) -> int:
    return len(text.split())


def usage_line(usage: Usage) -> str:
    """Per-message spend (§8.3). A missing cost is left out, never shown as $0."""
    if not usage.prompt_tokens and not usage.completion_tokens:
        return ""
    parts = [f"{usage.prompt_tokens:,} in"]
    if usage.cache_read_tokens:
        share = usage.cache_read_tokens / usage.prompt_tokens if usage.prompt_tokens else 0
        parts.append(f"{usage.cache_read_tokens:,} cached ({share:.0%})")
    elif usage.cache_creation_tokens:
        parts.append(f"{usage.cache_creation_tokens:,} written to cache")
    parts.append(f"{usage.completion_tokens:,} out")
    if usage.cost:
        about = "~" if usage.cost_estimated else ""
        parts.append(f"{about}${usage.cost:.4f}")
    return "  ·  ".join(parts)


def message_kind(node: Node) -> str:
    if node.kind == "assistant":
        return "assistant"
    return kind_for(node.speaker_id)


class TextBody(QLabel):
    """A word-wrapped label that remembers its height at each width.

    Qt asks a label's height for a width several times while it lays out a
    column (3,515 asks for 500 messages), and each ask lays the text out
    again: about a third of the time it took to build back to the start of a
    long story (tools/bench/transcript_bench.py). The answer only changes with
    the text, the font or the style (padding, text size), so those clear it.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._heights: dict[int, int] = {}

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._heights.clear()
        super().setText(text)

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        height = self._heights.get(width)
        if height is None:
            height = self._heights[width] = super().heightForWidth(width)
        return height

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._heights.clear()
        super().changeEvent(event)


class MessageWidget(QFrame):
    regenerate_requested = Signal(str)
    take_back_requested = Signal(str)
    # Toggle this passage as evidence for the next settings review.
    review_mark_requested = Signal(str)
    edit_committed = Signal(str, str)
    # ⋯ → New branch from here…: (node id, the new text, the branch's name or
    # "" for the next "Branch N"), as a new branch.
    rewrite_committed = Signal(str, str, str)
    delete_requested = Signal(str)
    reroll_requested = Signal(str)
    # (node id, +1 or -1): show the neighbouring take.
    variant_requested = Signal(str, int)
    illustrate_requested = Signal(str)
    # A simple chat's message kept word for word, or not (engine/chat.py).
    keep_full_requested = Signal(str, bool)

    def __init__(
        self,
        label: str,
        text: str,
        kind: str,
        ooc: str | None = None,
        *,
        node_id: str = "",
        archived: bool = False,
        rerollable: bool = False,
        variant: tuple[int, int] | None = None,
        usage: Usage | None = None,
        reasoning: str | None = None,
        marked: bool = False,
        branch_start: str | None = None,
        keep_full: bool | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("message")
        self._marked = marked
        # None: not a chat, so no Keep in full.
        self._keep_full = keep_full
        self._rewriting = False
        self.setProperty("kind", kind)
        # Archived messages are still the story and still readable; they just
        # reach the model as a chapter summary from here on (§5.2).
        self.setProperty("archived", "true" if archived else "false")
        self.node_id = node_id
        self._editor: QPlainTextEdit | None = None
        self._edit_row: QWidget | None = None
        self._branch_name: QLineEdit | None = None
        # The name a new branch gets when its field is left blank ("Branch 3").
        self.default_branch_name: Callable[[], str] = lambda: ""

        self.layout_ = QVBoxLayout(self)
        layout = self.layout_
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        self.header = QLabel(
            label
            + ("  ·  summarised" if archived else "")
            + ("  ·  kept in full" if keep_full else "")
        )
        self.header.setTextFormat(Qt.PlainText)
        self.header.setObjectName("messageHeader")

        self.edit_button = QToolButton()
        self.edit_button.setObjectName("messageAction")
        self.edit_button.setText("Edit")
        self.edit_button.setToolTip(
            "Correct this message in place (summaries covering it are marked stale). "
            "To take the story another way, use ⋯ → New branch from here…"
        )
        self.edit_button.clicked.connect(self.begin_edit)
        # Hidden when unwanted, never shown here: shown before it has a
        # parent, a widget becomes a window of its own, and on X11 that made
        # and destroyed a native window for every message (1.7ms each, most
        # of the time to build a long story on the display; nothing offscreen).
        if not node_id:
            self.edit_button.hide()

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)
        header_row.addWidget(self.header)
        self.variant_label: QLabel | None = None
        if variant is not None and variant[1] > 1:
            header_row.addSpacing(6)
            self._add_variant_cycler(header_row, *variant)
        header_row.addStretch(1)
        header_row.addWidget(self.edit_button)
        self.more_button: QToolButton | None = None
        self.actions_: dict[str, QAction] = {}
        if node_id:
            self._add_more_menu(header_row, kind == "assistant", rerollable)
        layout.addLayout(header_row)
        # Where a branch begins: the one place in the story it shows.
        self.branch_label: QLabel | None = None
        if branch_start:
            self.branch_label = QLabel(f"⑂ Branch “{branch_start}” starts here")
            self.branch_label.setObjectName("branchStart")
            self.branch_label.setTextFormat(Qt.PlainText)
            layout.addWidget(self.branch_label)

        self.body = TextBody(text)
        # Model prose, names and imported text: never interpreted as HTML.
        self.body.setTextFormat(Qt.PlainText)
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.body)

        self.ooc: QLabel | None = None
        if ooc:
            self.ooc = TextBody(f"OOC — {ooc}")
            self.ooc.setTextFormat(Qt.PlainText)
            self.ooc.setObjectName("messageOoc")
            self.ooc.setWordWrap(True)
            layout.addWidget(self.ooc)

        # Reasoning is shown collapsed and never fed back to the model (§8.1).
        self.reasoning: QLabel | None = None
        if reasoning and reasoning.strip():
            toggle = QToolButton()
            toggle.setObjectName("reasoningToggle")
            toggle.setText("Reasoning ▸")
            toggle.setCheckable(True)
            self.reasoning = TextBody(reasoning.strip())
            self.reasoning.setTextFormat(Qt.PlainText)
            self.reasoning.setObjectName("messageReasoning")
            self.reasoning.setWordWrap(True)
            self.reasoning.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.reasoning.hide()

            def show_reasoning(shown: bool) -> None:
                self.reasoning.setVisible(shown)
                toggle.setText("Reasoning ▾" if shown else "Reasoning ▸")

            toggle.toggled.connect(show_reasoning)
            layout.addWidget(toggle, 0, Qt.AlignLeft)
            layout.addWidget(self.reasoning)

        self.usage: QLabel | None = None
        line = usage_line(usage) if usage is not None else ""
        if line:
            self.usage = QLabel(line)
            self.usage.setObjectName("messageUsage")
            layout.addWidget(self.usage)

    def _add_variant_cycler(self, row: QHBoxLayout, position: int, count: int) -> None:
        """‹ 2 / 4 › — swiping between takes is moving along the tree (§2.2, §10)."""
        previous = QToolButton()
        previous.setObjectName("variantButton")
        previous.setText("‹")
        previous.setToolTip("Previous take")
        previous.setEnabled(position > 1)
        previous.clicked.connect(lambda: self.variant_requested.emit(self.node_id, -1))
        self.variant_label = QLabel(f"{position} / {count}")
        self.variant_label.setObjectName("variantLabel")
        following = QToolButton()
        following.setObjectName("variantButton")
        following.setText("›")
        following.setToolTip("Next take")
        following.setEnabled(position < count)
        following.clicked.connect(lambda: self.variant_requested.emit(self.node_id, 1))
        row.addWidget(previous)
        row.addWidget(self.variant_label)
        row.addWidget(following)

    def _add_more_menu(self, row: QHBoxLayout, assistant: bool, rerollable: bool) -> None:
        menu = QMenu(self)
        if not assistant:
            # The author's own turn, back in the composer with the settings it
            # was sent with: the alternative is copying the text out, deleting
            # the turn and its reply, and pasting it back in by hand.
            take_back = menu.addAction("Take back this turn")
            take_back.setToolTip(
                "Return this turn to the composer to edit and send again; "
                "it and everything after it are removed"
            )
            take_back.triggered.connect(lambda: self.take_back_requested.emit(self.node_id))
            self.actions_["take_back"] = take_back
        if assistant:
            regenerate = menu.addAction("Regenerate this passage")
            regenerate.setToolTip(
                "Write another take of this passage, in its place; this one is kept (‹ ›), "
                "and the story after it stays"
            )
            regenerate.triggered.connect(lambda: self.regenerate_requested.emit(self.node_id))
            self.actions_["regenerate"] = regenerate
        if rerollable:
            # Regenerate keeps the roll; this is the deliberate way to roll again.
            reroll = menu.addAction("Reroll the dice")
            reroll.triggered.connect(lambda: self.reroll_requested.emit(self.node_id))
            self.actions_["reroll"] = reroll
        if assistant:
            # A settings review otherwise sees only the newest replies; this
            # lets the author point at the passage they mean.
            mark = menu.addAction("Use as evidence in a review")
            mark.setCheckable(True)
            mark.setChecked(self._marked)
            mark.setToolTip(
                "Send this passage, with the turn it answered, to the next settings review"
            )
            mark.triggered.connect(lambda: self.review_mark_requested.emit(self.node_id))
            self.actions_["review_mark"] = mark
            illustrate = menu.addAction("Illustrate this passage…")
            illustrate.setToolTip("Write an image prompt for the moment this passage ends on")
            illustrate.triggered.connect(lambda: self.illustrate_requested.emit(self.node_id))
            self.actions_["illustrate"] = illustrate
        if self._keep_full is not None:
            keep = menu.addAction("Keep in full (never summarise)")
            keep.setCheckable(True)
            keep.setChecked(self._keep_full)
            keep.setToolTip("Sent word for word even once this part of the chat is summarised")
            keep.triggered.connect(
                lambda checked: self.keep_full_requested.emit(self.node_id, checked)
            )
            self.actions_["keep_full"] = keep
        rewrite = menu.addAction("New branch from here…")
        rewrite.setToolTip(
            "Start a new branch at this message, changed or as it is"
            + (" (your turn is answered at once)" if not assistant else "")
            + "; the story as it is now is kept, under its own name"
        )
        rewrite.triggered.connect(lambda: self.begin_edit(rewrite=True))
        self.actions_["rewrite"] = rewrite
        # Last and apart: it removes everything after the message too (and
        # asks first). A button on every card put it a slip away from Edit.
        menu.addSeparator()
        delete = menu.addAction("Delete from here…")
        delete.setToolTip("Delete this message and everything after it")
        delete.triggered.connect(lambda: self.delete_requested.emit(self.node_id))
        self.actions_["delete"] = delete
        self.more_button = QToolButton()
        self.more_button.setObjectName("messageAction")
        self.more_button.setText("⋯")
        self.more_button.setToolTip("More: regenerate or take back, a new branch from here, delete")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setMenu(menu)
        row.addWidget(self.more_button)

    def set_text(self, text: str) -> None:
        self.body.setText(text)

    # --- inline editing (§5.3, §10) ---------------------------------------

    def begin_edit(self, rewrite: bool = False) -> None:
        """Swap the body for an editor in place, so the column doesn't reflow.

        With `rewrite`, saving makes a new branch from this message instead of
        correcting it (the author's rule: editing an earlier message forks).
        """
        if self._editor is not None or not self.node_id:
            return
        self._rewriting = rewrite

        editor = QPlainTextEdit(self.body.text())
        editor.setObjectName("messageEditor")
        editor.setMinimumHeight(120)

        save = QPushButton("Save as a new branch" if rewrite else "Save")
        save.setObjectName("sendButton")
        save.clicked.connect(self._commit_edit)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel_edit)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(save)
        self._branch_name = None
        if rewrite:
            # Blank is the next "Branch N", as the placeholder says.
            name = QLineEdit()
            name.setObjectName("branchNameField")
            default = self.default_branch_name()
            name.setPlaceholderText(f"Name (blank: {default})" if default else "Branch name")
            name.setToolTip("What to call the new branch; it can be renamed from the branch bar")
            name.setMaxLength(80)
            name.returnPressed.connect(self._commit_edit)
            row_layout.addWidget(name, 1)
            self._branch_name = name
        row_layout.addWidget(cancel)
        if not rewrite:
            row_layout.addStretch(1)

        index = self.layout_.indexOf(self.body)
        self.layout_.insertWidget(index + 1, editor)
        self.layout_.insertWidget(index + 2, row)
        self.body.hide()
        self.edit_button.setEnabled(False)
        self._editor = editor
        self._edit_row = row
        editor.setFocus(Qt.OtherFocusReason)

    def cancel_edit(self) -> None:
        if self._editor is None:
            return
        self._editor.deleteLater()
        self._editor = None
        self._branch_name = None
        if self._edit_row is not None:
            self._edit_row.deleteLater()
            self._edit_row = None
        self.body.show()
        self.edit_button.setEnabled(True)

    def _commit_edit(self) -> None:
        if self._editor is None:
            return
        text = self._editor.toPlainText()
        rewriting = self._rewriting
        name = self._branch_name.text().strip() if self._branch_name is not None else ""
        self.cancel_edit()
        # The window rebuilds the transcript from story state, so the widget
        # does not update itself here — one writer, as with generation.
        if rewriting:
            self.rewrite_committed.emit(self.node_id, text, name)
        else:
            self.edit_committed.emit(self.node_id, text)


class AsideWidget(QFrame):
    """A question put to the model out of character, and its answer.

    Shown where it was asked, but it is not the story: the model never sees it
    as history, and it has no Edit, because there is nothing to keep in sync.
    """

    delete_requested = Signal(str)

    def __init__(self, question: str, answer: str = "", *, aside_id: str = "") -> None:
        super().__init__()
        self.setObjectName("message")
        self.setProperty("kind", "aside")
        self.aside_id = aside_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        self.header = QLabel("Question  ·  out of character, not part of the story")
        self.header.setObjectName("messageHeader")
        delete = QToolButton()
        delete.setObjectName("messageAction")
        delete.setText("Delete")
        delete.setToolTip("Delete this question and its answer")
        delete.clicked.connect(lambda: self.delete_requested.emit(self.aside_id))
        if not aside_id:  # hidden, never shown before it has a parent (see Edit)
            delete.hide()

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)
        header_row.addWidget(self.header)
        header_row.addStretch(1)
        header_row.addWidget(delete)
        layout.addLayout(header_row)

        self.question = TextBody(f"You asked: {question}")
        self.question.setTextFormat(Qt.PlainText)
        self.question.setObjectName("asideQuestion")
        self.question.setWordWrap(True)
        self.question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.question)

        self.body = TextBody(answer)
        self.body.setTextFormat(Qt.PlainText)
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.body)

    def set_text(self, text: str) -> None:
        self.body.setText(text)


class PictureView(QWidget):
    """A picture scaled to the column's width, at most `max_height` tall."""

    clicked = Signal()

    def __init__(self, pixmap: QPixmap, max_height: int = PICTURE_MAX_HEIGHT) -> None:
        super().__init__()
        self._pixmap = pixmap
        self._max_height = max_height
        self._scaled: QPixmap | None = None
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setCursor(Qt.PointingHandCursor)

    def _size_for(self, width: int) -> QSize:
        if self._pixmap.isNull() or width <= 0:
            return QSize(max(width, 0), 0)
        size = self._pixmap.size().scaled(width, self._max_height, Qt.KeepAspectRatio)
        return size

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._size_for(width).height()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        width = self.width() or 480
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, self.heightForWidth(self.width()) if self.width() else 0)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        target = self._size_for(self.width())
        if target.isEmpty():
            return
        if self._scaled is None or self._scaled.size() != target:
            self._scaled = self._pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._scaled)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


def picture_caption(image: GeneratedImage) -> str:
    parts = ["Picture", image.model.rsplit("/", 1)[-1], image.size]
    if image.references:
        count = len(image.references)
        parts.append(f"{count} reference{'s' if count != 1 else ''}")
    if image.cost is not None:
        parts.append(f"{'' if image.cost_reported else '~'}${image.cost:.3f}")
    return "  ·  ".join(parts)


class ImageMessageWidget(QFrame):
    """A generated picture, shown after the passage it pictures.

    Like an aside it isn't the story: nothing here is ever sent to the storyteller.
    """

    # (action, image id): open, prompt, save, reference, again, background, delete.
    action_requested = Signal(str, str)

    def __init__(self, image: GeneratedImage, path: Path) -> None:
        super().__init__()
        self.setObjectName("message")
        self.setProperty("kind", "picture")
        self.image_id = image.id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        self.header = QLabel(picture_caption(image))
        self.header.setObjectName("messageHeader")
        self.header.setToolTip(image.prompt)
        menu = QMenu(self)
        for key, text in IMAGE_ACTIONS:
            if key == "-":
                menu.addSeparator()
                continue
            action = menu.addAction(text)
            action.triggered.connect(
                lambda _checked=False, k=key: self.action_requested.emit(k, self.image_id)
            )
        more = QToolButton()
        more.setObjectName("messageAction")
        more.setText("⋯")
        more.setToolTip("Open, save, use as a reference or a background, generate again")
        more.setPopupMode(QToolButton.InstantPopup)
        more.setMenu(menu)
        self.more_button = more

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)
        header_row.addWidget(self.header)
        header_row.addStretch(1)
        header_row.addWidget(more)
        layout.addLayout(header_row)

        pixmap = load_pixmap(path)
        if pixmap.isNull():
            missing = QLabel(f"The picture file is missing ({image.file}).")
            missing.setObjectName("hintLabel")
            layout.addWidget(missing)
            self.picture = None
        else:
            self.picture = PictureView(pixmap)
            self.picture.setToolTip("Click to open full size")
            self.picture.clicked.connect(lambda: self.action_requested.emit("open", self.image_id))
            layout.addWidget(self.picture)


IMAGE_ACTIONS = (
    ("open", "Open full size"),
    ("prompt", "Show the prompt"),
    ("save", "Save as…"),
    ("-", ""),
    ("reference", "Use as a reference picture for…"),
    ("background", "Set as background"),
    ("again", "Generate again…"),
    ("-", ""),
    ("delete", "Delete picture"),
)


class PendingPictureWidget(QFrame):
    """A picture still being drawn, where it will appear. Play goes on meanwhile."""

    stop_requested = Signal(str)

    def __init__(self, job_id: str, text: str) -> None:
        super().__init__()
        self.setObjectName("message")
        self.setProperty("kind", "picture")
        self.job_id = job_id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        label = QLabel(text)
        label.setObjectName("messageHeader")
        label.setWordWrap(True)
        stop = QToolButton()
        stop.setObjectName("messageAction")
        stop.setText("Stop")
        stop.setToolTip(
            "Stop waiting for this picture. The image service may still finish it "
            "and charge for it."
        )
        stop.clicked.connect(lambda: self.stop_requested.emit(self.job_id))
        layout.addWidget(label, 1)
        layout.addWidget(stop)


class ColumnLayout(QVBoxLayout):
    """Tells its column when its measurements go stale."""

    def __init__(self, column: TranscriptColumn) -> None:
        super().__init__(column)
        self._column = column

    def invalidate(self) -> None:
        self._column.forget_heights()
        super().invalidate()


class TranscriptColumn(QWidget):
    """The scrolled column, sized by what word wrapping actually needs.

    `QScrollArea` in `widgetResizable` mode takes its child's height from
    `sizeHint`, and a layout's `sizeHint` adds up each child's *unconstrained*
    hint — for a word-wrapped label that is the height it would need at its
    natural width, not at the width it really got. The two differ by ~60px per
    message here, so a long story ended up scrolling into hundreds of pixels
    of empty space below the last passage. Reporting `heightForWidth` as the
    hint fixes it, because the layout computes that one correctly.

    Measuring is the expensive part: it lays out every label's text. Qt asks
    for the same widths again and again while it settles (with and without
    the scrollbar), so heights are kept per width until the layout changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self._heights: dict[int, int] = {}

    def forget_heights(self) -> None:
        self._heights.clear()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        layout = self.layout()
        if layout is None:
            return super().sizeHint().height()
        height = self._heights.get(width)
        if height is None:
            height = self._heights[width] = layout.heightForWidth(width)
        return height

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        hint = super().sizeHint()
        # The scroll area has already set our width to the viewport's by the
        # time it asks; on the very first pass fall back to the plain hint.
        width = self.width() or hint.width()
        return QSize(hint.width(), self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # Overridden for the same reason, and it has to be: the scroll area
        # cannot shrink the column below its minimum, so leaving this summed
        # from unconstrained hints keeps the dead space whatever sizeHint says.
        hint = super().minimumSizeHint()
        width = self.width() or hint.width()
        return QSize(hint.width(), self.heightForWidth(width))


class SummaryCardWidget(QFrame):
    """A chapter (or merged part) as the storyteller is given it."""

    def __init__(self, title: str, text: str, *, stale: bool = False) -> None:
        super().__init__()
        self.setObjectName("message")
        self.setProperty("kind", "summary")
        self.setToolTip("Edit it in the Story so far tab.")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)
        header = QLabel(title + ("  ·  out of date" if stale else ""))
        header.setTextFormat(Qt.PlainText)
        header.setObjectName("messageHeader")
        layout.addWidget(header)
        self.body = TextBody(text)
        self.body.setTextFormat(Qt.PlainText)
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.body)


class ViewToggle(QWidget):
    """As written | As sent to the storyteller, above the transcript.

    The story as written keeps every passage, archived ones included. The
    storyteller never sees archived prose again, only its chapter summary,
    and until this view that was visible only as raw prompt text.
    """

    changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("viewToggle")
        self.written = QToolButton()
        self.written.setText("As written")
        self.written.setToolTip("Every passage, including those now summarised for the storyteller")
        self.sent = QToolButton()
        self.sent.setText("As sent to the storyteller")
        self.sent.setToolTip(
            "Chapter summaries where prose has been archived, then the passages sent word "
            "for word (Ctrl+Shift+V)"
        )
        for button in (self.written, self.sent):
            button.setObjectName("viewToggleButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
        self.written.setChecked(True)
        self.sent.toggled.connect(self.changed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.written)
        layout.addWidget(self.sent)
        layout.addStretch(1)

    def set_mode(self, storyteller: bool) -> None:
        (self.sent if storyteller else self.written).setChecked(True)

    def set_chat(self, chat: bool) -> None:
        """A simple chat has a model, not a storyteller."""
        self.sent.setText("As sent to the model" if chat else "As sent to the storyteller")


@dataclass(frozen=True)
class ChapterCard:
    title: str
    text: str
    stale: bool = False


# One unit of the transcript: a node (or None, before the first) and what
# builds its widgets, the node's asides and pictures with it. A window of
# units is built at a time (see `TranscriptView._lo`).
_Unit = tuple["str | None", Callable[[], None]]


def _line_top(label: QLabel, position: int) -> int:
    """How far down a word-wrapped plain-text label the line holding
    `position` starts, laid out as the label lays it out."""
    document = QTextDocument()
    document.setDefaultFont(label.font())
    document.setDocumentMargin(0)
    document.setPlainText(label.text())
    document.setTextWidth(label.contentsRect().width())
    block = document.findBlock(position)
    top = document.documentLayout().blockBoundingRect(block).top()
    line = block.layout().lineForTextPosition(position - block.position())
    return label.contentsRect().top() + int(top + (line.y() if line.isValid() else 0))


# The reply's mark ("signed", not "verified": the attested key signed a record
# NanoGPT keeps for this reply's id; the reply's text is not compared to it,
# the manual, "Private scenes") and the author's message's (how it was sent).
TEE_MARKS = {
    "verified": "  ·  TEE signed",
    "failed": "  ·  TEE ⚠ signature mismatch",
    "unchecked": "  ·  TEE signature unavailable",
    "encrypted": "  ·  end-to-end encrypted",
    "attested": "  ·  sent to the attested TEE",
}
TEE_MARK_TIPS = {
    "verified": (
        "TEE signed: the enclave's attested key signed the record NanoGPT keeps for "
        "this reply's id. That proves the key was in the attested enclave, not that this "
        "text is what was signed: the record hashes the request and reply as NanoGPT's "
        "gateway saw them, which the app can't reproduce. The text passed that gateway "
        "in the clear."
    ),
    "failed": ("The record NanoGPT returned for this reply was not signed by the attested key."),
    "unchecked": "This model's provider keeps no signature for its replies.",
    "encrypted": (
        "End-to-end encrypted: sealed on this machine to the attested enclave's key, and "
        "the reply opened with it. NanoGPT relayed ciphertext."
    ),
    "attested": "Sent to the enclave that was attested when this chat or scene began.",
}


class TranscriptView(QScrollArea):
    regenerate_requested = Signal(str)
    take_back_requested = Signal(str)
    review_mark_requested = Signal(str)
    edit_committed = Signal(str, str)
    delete_requested = Signal(str)
    delete_aside_requested = Signal(str)
    reroll_requested = Signal(str)
    rewrite_committed = Signal(str, str, str)
    variant_requested = Signal(str, int)
    illustrate_requested = Signal(str)
    image_action_requested = Signal(str, str)
    stop_image_requested = Signal(str)
    keep_full_requested = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("transcript")
        # A simple chat (`set_chat`): its labels, Keep in full, and a TEE
        # chat's private look.
        self._chat = False
        self._tee_look = False
        # For a new branch's name field: the name it gets if left blank.
        self.default_branch_name: Callable[[], str] = lambda: ""
        self._background: QPixmap | None = None
        self._background_scaled: QPixmap | None = None
        self._background_for = QSize()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = TranscriptColumn()
        self._container.setObjectName("transcriptContainer")
        self._layout = ColumnLayout(self._container)
        self._layout.setContentsMargins(COLUMN_MARGIN, COLUMN_MARGIN, COLUMN_MARGIN, COLUMN_MARGIN)
        self._layout.setSpacing(12)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self._streaming: MessageWidget | AsideWidget | None = None
        self._placeholder: QWidget | None = None
        # The window (see _show_units): every unit of the view, the built
        # ones `[_lo, _hi)` and the widgets each placed, where the next widget
        # goes, and the rows above and below saying what isn't built.
        self._units: list[_Unit] = []
        self._unit_of: dict[str, int] = {}
        self._lo = 0
        self._hi = 0
        self._unit_widgets: dict[int, list[QWidget]] = {}
        self._building: int | None = None
        # Widgets placed outside any unit: a streaming passage, a question
        # being answered, the author's turn just sent. They sit after the
        # newest unit, so while there are any the window stays at the end.
        self._loose: list[QWidget] = []
        self._insert_at: int | None = None
        self._earlier: QPushButton | None = None
        self._earlier_row: QWidget | None = None
        self._later: QPushButton | None = None
        self._later_row: QWidget | None = None
        # While earlier pages go in above, hold the reader's distance from
        # the bottom rather than their distance from the top.
        self._anchor_bottom: int | None = None
        # A message held where the reader had it across a rebuild (`keep_place`),
        # until they scroll, or the view goes to the end or to a message.
        self._anchor_node: tuple[str, int] | None = None
        self._adjusting = False
        # (message id, text) for every message the view shows, built or not:
        # what Find searches (gui/find_bar.py). The label holding a match.
        self._texts: list[tuple[str, str]] = []
        self._found: QLabel | None = None
        self._anchor_match: tuple[QLabel, int] | None = None

        # Word-wrapped labels only reach their final height once the layout
        # has run, so "scroll to the end" has to survive the range growing
        # afterwards. Follow the bottom until the reader scrolls away from it.
        self._follow = True
        bar = self.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range_changed)
        bar.valueChanged.connect(self._on_value_changed)
        # Scrolling copies the viewport's pixels; a fixed background has to be
        # repainted instead, or it would scroll with the text.
        bar.valueChanged.connect(self._repaint_background)

    # --- background -------------------------------------------------------

    def restyle_background(self) -> None:
        """The see-through messages again, in the theme now chosen."""
        if self._background is not None:
            self.setStyleSheet(background_qss())
            self.viewport().update()

    def set_background(self, path: Path | None) -> bool:
        """Show a picture behind the text, or none. False if it couldn't be read."""
        pixmap = load_pixmap(path) if path is not None else QPixmap()
        self._background = None if pixmap.isNull() else pixmap
        self._background_scaled = None
        self.setStyleSheet(background_qss() if self._background is not None else "")
        self.viewport().update()
        return path is None or self._background is not None

    @property
    def has_background(self) -> bool:
        return self._background is not None

    def _repaint_background(self, *_args) -> None:
        if self._background is not None:
            self.viewport().update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Centre the column once the viewport is wider than a reading measure.
        side = max(COLUMN_MARGIN, (self.viewport().width() - READING_WIDTH) // 2)
        if self._layout.contentsMargins().left() != side:
            self._layout.setContentsMargins(side, COLUMN_MARGIN, side, COLUMN_MARGIN)
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._background is not None:
            rect = self.viewport().rect()
            if self._background_scaled is None or self._background_for != rect.size():
                self._background_scaled = self._background.scaled(
                    rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
                self._background_for = rect.size()
            scaled = self._background_scaled
            painter = QPainter(self.viewport())
            painter.drawPixmap(
                0,
                0,
                scaled,
                (scaled.width() - rect.width()) // 2,
                (scaled.height() - rect.height()) // 2,
                rect.width(),
                rect.height(),
            )
            painter.fillRect(rect, theme.colour("wash", WASH_ALPHA))
            painter.end()
        super().paintEvent(event)

    # --- building ---------------------------------------------------------

    def clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._streaming = None
        self._placeholder = None
        self._units = []
        self._unit_of = {}
        self._lo = self._hi = 0
        self._unit_widgets = {}
        self._building = None
        self._loose = []
        self._insert_at = None
        self._earlier = self._earlier_row = None
        self._later = self._later_row = None
        self._anchor_bottom = None
        self._texts = []
        self._found = None
        self._anchor_match = None

    def _place(self, widget: QWidget) -> None:
        """At the end of the window, or where a page is going in; recorded
        against the unit being built so it can be dropped with it."""
        if self._insert_at is None:
            self._layout.insertWidget(self._end_index(), widget)
        else:
            self._layout.insertWidget(self._insert_at, widget)
            self._insert_at += 1
        if self._building is not None:
            self._unit_widgets.setdefault(self._building, []).append(widget)
        else:
            self._loose.append(widget)

    def _end_index(self) -> int:
        """Where the column's built messages end: before the row below, or
        before the closing stretch."""
        if self._later_row is not None:
            return self._layout.indexOf(self._later_row)
        return self._layout.count() - 1

    def add_message(
        self,
        label: str,
        text: str,
        kind: str,
        ooc: str | None = None,
        *,
        node_id: str = "",
        archived: bool = False,
        rerollable: bool = False,
        variant: tuple[int, int] | None = None,
        usage: Usage | None = None,
        reasoning: str | None = None,
        marked: bool = False,
        branch_start: str | None = None,
        keep_full: bool | None = None,
    ) -> MessageWidget:
        widget = MessageWidget(
            label,
            text,
            kind,
            ooc,
            node_id=node_id,
            archived=archived,
            rerollable=rerollable,
            variant=variant,
            usage=usage,
            reasoning=reasoning,
            marked=marked,
            branch_start=branch_start,
            keep_full=keep_full,
        )
        widget.keep_full_requested.connect(self.keep_full_requested)
        widget.review_mark_requested.connect(self.review_mark_requested)
        widget.reroll_requested.connect(self.reroll_requested)
        widget.rewrite_committed.connect(self.rewrite_committed)
        widget.default_branch_name = self.default_branch_name
        widget.variant_requested.connect(self.variant_requested)
        widget.illustrate_requested.connect(self.illustrate_requested)
        widget.regenerate_requested.connect(self.regenerate_requested)
        widget.take_back_requested.connect(self.take_back_requested)
        widget.edit_committed.connect(self.edit_committed)
        widget.delete_requested.connect(self.delete_requested)
        self._place(widget)
        return widget

    def add_picture(self, image: GeneratedImage, path: Path) -> ImageMessageWidget:
        widget = ImageMessageWidget(image, path)
        widget.action_requested.connect(self.image_action_requested)
        self._place(widget)
        return widget

    def add_pending_picture(self, job_id: str, text: str) -> PendingPictureWidget:
        widget = PendingPictureWidget(job_id, text)
        widget.stop_requested.connect(self.stop_image_requested)
        self._place(widget)
        return widget

    def add_aside(self, question: str, answer: str = "", *, aside_id: str = "") -> AsideWidget:
        widget = AsideWidget(question, answer, aside_id=aside_id)
        widget.delete_requested.connect(self.delete_aside_requested)
        self._place(widget)
        return widget

    def show_path(
        self,
        nodes: Sequence[Node],
        cast: Sequence[Character],
        archived_ids: frozenset[str] = frozenset(),
        asides_for: Callable[[str | None], Sequence[Aside]] | None = None,
        empty_text: str = "Nothing written yet. Type below to begin the story.",
        show_rolls: bool = True,
        variant_of: Callable[[str], tuple[int, int]] | None = None,
        marked_ids: frozenset[str] = frozenset(),
        pictures_for: PicturesFor | None = None,
        branch_starts: Mapping[str, str] | None = None,
    ) -> None:
        cast_by_id = {character.id: character for character in cast}
        self.clear()

        def add_asides(anchor: str | None) -> None:
            for aside in asides_for(anchor) if asides_for else ():
                self.add_aside(aside.question, aside.answer, aside_id=aside.id)
            for item in pictures_for(anchor) if pictures_for else ():
                if isinstance(item, tuple):
                    self.add_picture(*item)
                else:
                    self.add_pending_picture(item.job_id, item.text)

        def build(node: Node) -> None:
            self._add_node(
                node,
                cast_by_id,
                archived=node.id in archived_ids,
                rerollable=(
                    node is nodes[-1] and node.kind == "assistant" and node.meta.roll is not None
                ),
                variant=variant_of(node.id) if variant_of else None,
                marked=node.id in marked_ids,
                show_rolls=show_rolls,
                branch_start=(branch_starts or {}).get(node.id),
            )
            add_asides(node.id)

        units: list[_Unit] = [(None, lambda: add_asides(None))]
        units += [(node.id, lambda node=node: build(node)) for node in nodes]
        self._show_units(units)
        self._texts = [(node.id, node.content) for node in nodes]
        if not nodes and self._layout.count() == 1:
            self.show_placeholder(empty_text)
        self.scroll_to_end()

    def show_storyteller_view(
        self,
        chapters: Sequence[ChapterCard],
        nodes: Sequence[Node],
        cast: Sequence[Character],
        *,
        excluded_ids: frozenset[str] = frozenset(),
        show_rolls: bool = True,
        variant_of: Callable[[str], tuple[int, int]] | None = None,
        marked_ids: frozenset[str] = frozenset(),
        branch_starts: Mapping[str, str] | None = None,
    ) -> None:
        """The story history as the storyteller is given it: chapter summaries
        where prose has been archived, then the prose still sent verbatim.
        Asides and pictures are left out: it never sees them."""
        cast_by_id = {character.id: character for character in cast}
        self.clear()

        def note() -> None:
            label = QLabel(
                "What the storyteller is given as the story so far: summaries where the "
                "prose has been archived, then the passages sent word for word. The world, "
                "cast, lore and scene are in the Context tab."
            )
            label.setObjectName("viewNote")
            label.setWordWrap(True)
            self._place(label)

        def card(chapter: ChapterCard) -> None:
            self._place(SummaryCardWidget(chapter.title, chapter.text, stale=chapter.stale))

        def build(node: Node) -> None:
            self._add_node(
                node,
                cast_by_id,
                unsent=node.id in excluded_ids,
                rerollable=(
                    node is nodes[-1] and node.kind == "assistant" and node.meta.roll is not None
                ),
                variant=variant_of(node.id) if variant_of else None,
                marked=node.id in marked_ids,
                show_rolls=show_rolls,
                branch_start=(branch_starts or {}).get(node.id),
            )

        units: list[_Unit] = [(None, note)]
        units += [(None, lambda chapter=chapter: card(chapter)) for chapter in chapters]
        units += [(node.id, lambda node=node: build(node)) for node in nodes]
        self._show_units(units)
        self._texts = [(node.id, node.content) for node in nodes]
        self.scroll_to_end()

    def set_chat(self, chat: bool, *, tee: bool = False) -> None:
        """Before a rebuild: whether the view is a simple chat's, and a TEE one."""
        self._chat = chat
        self._tee_look = chat and tee

    def _add_node(
        self,
        node: Node,
        cast_by_id: dict[str, Character],
        *,
        archived: bool = False,
        unsent: bool = False,
        rerollable: bool = False,
        variant: tuple[int, int] | None = None,
        marked: bool = False,
        show_rolls: bool = True,
        branch_start: str | None = None,
    ) -> MessageWidget:
        label = speaker_label(node, cast_by_id, self._chat)
        if node.kind == "assistant":
            # How the author judges whether a length preset is working.
            label += f"  ·  {word_count(node.content)} words"
            if node.meta.roll is not None and show_rolls:
                label += f"  ·  {chip_text(node.meta.roll)}"
        if unsent:
            label += "  ·  not sent: over the budget"
        if node.meta.private_summary_of:
            label = "Private scene  ·  summary"
        elif node.meta.private_span or self._tee_look:
            if node.meta.private_span:
                label += "  ·  private"
            # "signed", not "verified": the attested key signed a record for this
            # reply's id; the reply's content is not compared to it (the manual,
            # "Private scenes").
            label += TEE_MARKS.get(node.meta.tee or "", "")
        widget = self.add_message(
            label,
            node.content,
            message_kind(node),
            node.ooc,
            node_id=node.id,
            archived=archived or unsent,
            rerollable=rerollable,
            variant=variant,
            usage=node.meta.usage if node.kind == "assistant" else None,
            reasoning=node.meta.reasoning if node.kind == "assistant" else None,
            marked=marked,
            branch_start=branch_start,
            keep_full=node.meta.keep_full if self._chat else None,
        )
        if self._tee_look:
            widget.setProperty("private", "true")
            widget.setToolTip("A TEE chat: every message goes to the attested model only.")
        if node.meta.private_span:
            widget.setProperty("private", "true")
            widget.setToolTip("Written in a private scene: only the private model ever saw it.")
        if node.meta.tee in TEE_MARK_TIPS:
            widget.setToolTip(TEE_MARK_TIPS[node.meta.tee])
        if node.meta.private_summary_of:
            widget.setProperty("privatesummary", "true")
            widget.setToolTip(
                "The approved summary of a private scene: all any other model knows of it."
            )
        return widget

    # --- paging -------------------------------------------------------------

    def _show_units(self, units: list[_Unit]) -> None:
        """The newest `PAGE` messages built; the rest wait above."""
        self._units = units
        self._unit_of = {node_id: i for i, (node_id, _b) in enumerate(units) if node_id}
        self._unit_widgets = {}
        self._lo = self._hi = len(units)
        self._build_range(self._back(len(units), PAGE), len(units))
        self._sync_rows()

    # --- the window -----------------------------------------------------------

    def _messages(self, lo: int, hi: int) -> int:
        return sum(1 for node_id, _b in self._units[lo:hi] if node_id is not None)

    def _back(self, index: int, count: int) -> int:
        """The unit `count` messages before `index`, or 0 once no message is
        left above (so the leading asides, note and chapter cards come too)."""
        taken = 0
        while index > 0 and taken < count:
            index -= 1
            if self._units[index][0] is not None:
                taken += 1
        if not any(node_id is not None for node_id, _b in self._units[:index]):
            index = 0
        return index

    def _forward(self, index: int, count: int) -> int:
        """The unit just past `count` messages from `index`."""
        taken = 0
        while index < len(self._units) and taken < count:
            if self._units[index][0] is not None:
                taken += 1
            index += 1
        return index

    def _build_range(self, lo: int, hi: int) -> None:
        """Build units `[lo, hi)`, joining the window at whichever end they
        touch (the window is always one run of units)."""
        if lo >= hi:
            return
        above = hi <= self._lo
        if self._lo == self._hi:  # nothing built
            self._insert_at = 1 if self._earlier_row is not None else 0
            self._lo, self._hi = lo, hi
        elif above:
            self._insert_at = 1 if self._earlier_row is not None else 0
            self._lo = lo
        else:
            self._insert_at = self._end_index()
            self._hi = hi
        for index in range(lo, hi):
            self._building = index
            self._units[index][1]()
        self._building = None
        self._insert_at = None

    def _drop_range(self, lo: int, hi: int) -> None:
        """Take units `[lo, hi)` out: only ever from an end of the window."""
        for index in range(lo, hi):
            for widget in self._unit_widgets.pop(index, []):
                try:
                    holds_match = self._found is not None and (
                        widget is self._found or widget.isAncestorOf(self._found)
                    )
                except RuntimeError:  # the match's label is already gone
                    holds_match = True
                if holds_match:
                    self._found = None
                    self._anchor_match = None
                # Hiding a focused widget hands focus to the next one, and the
                # area scrolls to show it: dropping a page with focus in it
                # walked the view down message by message.
                focus = QApplication.focusWidget()
                if focus is not None and (focus is widget or widget.isAncestorOf(focus)):
                    focus.clearFocus()
                self._layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
        if lo <= self._lo:
            self._lo = hi
        else:
            self._hi = lo

    def _trim(self, keep_top: bool) -> None:
        """Past `MAX_BUILT` messages, drop whole units from the far end:
        below if the page went in above (`keep_top`), else above. Never below
        while anything loose (a streaming passage) sits after the window, and
        never the reader's message (`_anchor_node`) or half a page past it:
        the window may run over for a while rather than cut what's on screen.
        (Dropping the anchor made the next re-anchor move the window back,
        and paging up from the bottom of the view went round in circles.)"""
        excess = self._messages(self._lo, self._hi) - MAX_BUILT
        if excess <= 0:
            return
        anchor = self._unit_of.get(self._anchor_node[0]) if self._anchor_node else None
        if keep_top:
            if self._loose:
                return
            cut, dropped = self._hi, 0
            while cut > self._lo and dropped < excess:
                cut -= 1
                dropped += self._units[cut][0] is not None
            if anchor is not None:
                cut = max(cut, self._forward(anchor, PAGE // 2))
            if cut < self._hi:
                self._drop_range(cut, self._hi)
        else:
            cut = self._forward(self._lo, excess)
            if anchor is not None:
                keep, taken = anchor, 0
                while keep > self._lo and taken < PAGE // 2:
                    keep -= 1
                    taken += self._units[keep][0] is not None
                cut = min(cut, keep)
            if cut > self._lo:
                self._drop_range(self._lo, cut)

    def _hold_place(self) -> None:
        """Keep the reader's first visible message where it is while a page
        goes in or out around it."""
        place = self.place_of()
        if place is None:
            return
        self._follow = False
        self._anchor_bottom = None
        self._anchor_node = place
        self._layout.activate()
        self._apply_anchor()
        QTimer.singleShot(0, self._apply_anchor)

    @property
    def unbuilt(self) -> int:
        """Messages above the first one built."""
        return self._messages(0, self._lo)

    @property
    def unbuilt_later(self) -> int:
        """Messages below the last one built."""
        return self._messages(self._hi, len(self._units))

    @property
    def at_latest(self) -> bool:
        return self._hi >= len(self._units)

    def _row(self, text: str, jump: str, on_page, on_jump) -> tuple[QWidget, QPushButton]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        page = QPushButton(text)
        page.setCursor(Qt.PointingHandCursor)
        page.clicked.connect(lambda: on_page())
        to_end = QPushButton(jump)
        to_end.setObjectName("jumpButton")
        to_end.setCursor(Qt.PointingHandCursor)
        to_end.clicked.connect(lambda: on_jump())
        layout.addWidget(page, 1)
        layout.addWidget(to_end)
        return row, page

    def _sync_rows(self) -> None:
        """The rows above and below the window, with how much each hides."""
        earlier, later = self.unbuilt, self.unbuilt_later
        if earlier and self._earlier_row is None:
            self._earlier_row, self._earlier = self._row(
                "", "Start of story", self.show_earlier, self.jump_to_start
            )
            self._earlier.setObjectName("earlierButton")
            self._layout.insertWidget(0, self._earlier_row)
        if self._earlier_row is not None and self._earlier is not None:
            if earlier:
                self._earlier.setText(
                    f"Show earlier passages ({earlier} more)  ·  or scroll to the top"
                )
            else:
                self._layout.removeWidget(self._earlier_row)
                self._earlier_row.deleteLater()
                self._earlier_row = self._earlier = None
        if later and self._later_row is None:
            self._later_row, self._later = self._row(
                "", "Latest", self.show_later, self.jump_to_latest
            )
            self._later.setObjectName("laterButton")
            self._layout.insertWidget(self._layout.count() - 1, self._later_row)
        if self._later_row is not None and self._later is not None:
            if later:
                self._later.setText(
                    f"Show later passages ({later} more)  ·  or scroll to the bottom"
                )
            else:
                self._layout.removeWidget(self._later_row)
                self._later_row.deleteLater()
                self._later_row = self._later = None

    def show_earlier(self, count: int = PAGE, *, keep_place: bool = True) -> None:
        """Build `count` more messages above, keeping the reader where they
        are, and drop from below past `MAX_BUILT`."""
        if self._lo == 0:
            return
        if keep_place:
            self._hold_place()
        self._build_range(self._back(self._lo, count), self._lo)
        self._trim(keep_top=True)
        self._sync_rows()
        if keep_place:
            self._hold_place_again()

    def show_later(self, count: int = PAGE, *, keep_place: bool = True) -> None:
        """Build `count` more messages below, and drop from above past
        `MAX_BUILT`, keeping the reader where they are."""
        if self.at_latest:
            return
        if keep_place:
            self._hold_place()
        self._build_range(self._hi, self._forward(self._hi, count))
        self._trim(keep_top=False)
        self._sync_rows()
        if keep_place:
            self._hold_place_again()

    def _hold_place_again(self) -> None:
        if self._anchor_node is not None:
            self._layout.activate()
            self._apply_anchor()
            QTimer.singleShot(0, self._apply_anchor)

    def _jump_to(self, index: int) -> None:
        """Rebuild the window around unit `index`: about half a page either
        side, a whole page in all, clamped to the story."""
        self._drop_range(self._lo, self._hi)
        self._lo = self._hi = index
        lo = self._back(index, PAGE // 2)
        hi = self._forward(index, PAGE - self._messages(lo, index))
        lo = self._back(lo, max(0, PAGE - self._messages(lo, hi)))
        self._follow = False
        self._anchor_bottom = None
        self._build_range(lo, hi)
        self._sync_rows()

    def jump_to_start(self) -> None:
        """The first page of the story, from the top."""
        if not self._units or self._loose:
            return
        self._jump_to(0)
        self._layout.activate()
        self._set_value(0)
        QTimer.singleShot(0, lambda: self._set_value(0))

    def jump_to_latest(self) -> None:
        """The newest page, followed at the bottom as when the story opened."""
        if self._units and not self.at_latest:
            self._drop_range(self._lo, self._hi)
            self._lo = self._hi = len(self._units)
            self._build_range(self._back(len(self._units), PAGE), len(self._units))
            self._sync_rows()
        self.scroll_to_end()

    def ensure_at_latest(self) -> None:
        """Before anything is written at the end: a streaming passage, a
        question, the author's turn."""
        if not self.at_latest:
            self.jump_to_latest()

    def _bring_into_window(self, node_id: str) -> None:
        """Build the window to include a message: extend it when the message
        is within a page of an edge, rebuild around it when further."""
        index = self._unit_of.get(node_id)
        if index is None or self._lo <= index < self._hi:
            return
        if index < self._lo:
            gap = self._messages(index, self._lo)
            # With something streaming at the end, only grow (never jump away).
            if gap <= PAGE or self._loose:
                self.show_earlier(gap, keep_place=False)
                return
        else:
            gap = self._messages(self._hi, index + 1)
            if gap <= PAGE:
                self.show_later(gap, keep_place=False)
                return
        self._jump_to(index)

    def show_placeholder(
        self,
        text: str,
        actions: Sequence[tuple[str, Callable[[], None]]] = (),
        *,
        more_actions: Sequence[tuple[str, Callable[[], None]]] = (),
        rich: bool = False,
    ) -> None:
        """A note where the messages would be, with buttons for what to do
        next (with no story open: New story, Import…). `more_actions` are a
        second centred row: five buttons on one line were wider than the
        transcript at 1366px. `rich` is the first-run welcome: rich text,
        left-aligned, links opened in the browser."""
        holder = QWidget()
        holder.setObjectName("placeholderBox")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(text)
        label.setWordWrap(True)
        if rich:
            label.setObjectName("welcome")
            label.setTextFormat(Qt.RichText)
            label.setOpenExternalLinks(True)
            label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        else:
            label.setObjectName("placeholder")
            label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        for buttons in (actions, more_actions):
            if not buttons:
                continue
            row = QHBoxLayout()
            row.addStretch(1)
            for caption, handler in buttons:
                button = QPushButton(caption)
                button.setObjectName("placeholderButton")
                button.clicked.connect(handler)
                row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)
        self._placeholder = holder
        self._layout.insertWidget(self._layout.count() - 1, holder)
        if rich:
            # Read from the top: taller than the view at 768px, and followed
            # to the bottom it opened on its last steps, heading out of sight.
            self._follow = False
            self._set_value(0)

    # --- streaming --------------------------------------------------------

    def begin_streaming(self) -> None:
        self.ensure_at_latest()
        self._streaming = self.add_message("Story", "", "assistant")
        self.scroll_to_end()

    def begin_aside(self, question: str) -> None:
        if self._placeholder is not None:
            self._placeholder.hide()
        self.ensure_at_latest()
        self._streaming = self.add_aside(question)
        self.scroll_to_end()

    def append_streaming(self, text: str) -> None:
        if self._streaming is None:
            self.begin_streaming()
        assert self._streaming is not None
        self._streaming.set_text(self._streaming.body.text() + text)
        self.scroll_to_end()

    def end_streaming(self) -> None:
        self._streaming = None

    def message_widget(self, node_id: str) -> MessageWidget | None:
        """The built widget for a node, moving the window to it if need be."""
        self._bring_into_window(node_id)
        return self._built_widget(node_id)

    def _built_widget(self, node_id: str) -> MessageWidget | None:
        """The widget for a node if it is built; the window doesn't move."""
        for widget in self._container.findChildren(MessageWidget):
            # A rebuild's old widgets linger until their deferred delete; only
            # the ones in the column are the story.
            if widget.node_id == node_id and self._layout.indexOf(widget) >= 0:
                return widget
        return None

    def place_of(self, node_id: str | None = None) -> tuple[str, int] | None:
        """Where the reader is: a message (this one, or the first in view) and
        its distance from the top of the view, for `keep_place` after a rebuild."""
        top = self.verticalScrollBar().value()
        if node_id is not None:
            widget = self.message_widget(node_id)
            return (node_id, widget.y() - top) if widget is not None else None
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if isinstance(widget, MessageWidget) and widget.node_id:
                if widget.y() + widget.height() > top:
                    return widget.node_id, widget.y() - top
        return None

    def keep_place(self, place: tuple[str, int] | None) -> None:
        """Put the view back where `place_of` found it. Saving an edit rebuilt
        the transcript and dropped the reader at the bottom, lost, when Cancel
        left them where they were."""
        if place is None:
            return
        # The rebuild built the newest page; the window goes back to the
        # message first (re-anchoring itself never moves it).
        self._bring_into_window(place[0])
        self._follow = False
        self._anchor_bottom = None
        self._anchor_match = None
        self._anchor_node = place
        # Held against the rebuild's own scroll to the end, and re-applied as
        # word-wrapped heights settle (`_on_range_changed`).
        QTimer.singleShot(0, self._apply_anchor)

    def _apply_anchor(self) -> None:
        if self._anchor_node is None:
            return
        node_id, offset = self._anchor_node
        # Only among what is built: re-anchoring must never move the window.
        widget = self._built_widget(node_id)
        if widget is None:
            self._anchor_node = None
            return
        if widget is not None and widget.height() > 0:
            self._set_value(widget.y() - offset)

    def scroll_to_node(self, node_id: str) -> None:
        """Bring a message to the top of the view (the chapter navigator)."""
        widget = self.message_widget(node_id)
        if widget is None:
            return
        self._follow = False
        self._anchor_bottom = None
        self._anchor_match = None
        # Held at the top like a kept place, until the reader scrolls: a
        # freshly built window's heights settle over several layout passes,
        # and setting the value once left the message far down the view.
        self._anchor_node = (node_id, 0)
        self._layout.activate()
        self._apply_anchor()
        QTimer.singleShot(0, self._apply_anchor)

    @property
    def searchable_texts(self) -> list[tuple[str, str]]:
        """(message id, text) for each message shown, in story order."""
        return list(self._texts)

    def show_match(self, node_id: str, start: int, length: int) -> bool:
        """Build back to a message if need be, select the match in it and
        bring it into view, a third of the way down. False if the message
        isn't shown (or is being edited, where its label is hidden)."""
        self.clear_match()
        widget = self.message_widget(node_id)
        if widget is None or widget._editor is not None:
            return False
        label = widget.body
        label.setSelection(start, length)
        self._found = label
        self._follow = False
        self._anchor_bottom = None
        self._anchor_node = None
        # Held, like a kept place, until the reader scrolls: freshly built
        # pages have no final heights until the layout runs, and the range
        # grows after this returns.
        self._anchor_match = (label, start)
        self._layout.activate()
        self._reveal_match()
        QTimer.singleShot(0, self._reveal_match)
        return True

    def _reveal_match(self) -> None:
        if self._anchor_match is None:
            return
        label, start = self._anchor_match
        try:
            y = label.mapTo(self._container, QPoint(0, _line_top(label, start))).y()
        except RuntimeError:  # its message was rebuilt away
            self._anchor_match = None
            return
        self._set_value(y - self.viewport().height() // 3)

    def clear_match(self) -> None:
        self._anchor_match = None
        found, self._found = self._found, None
        if found is not None:
            try:
                found.setSelection(0, 0)
            except RuntimeError:  # its message was rebuilt away
                pass

    def scroll_to_end(self) -> None:
        self._follow = True
        self._anchor_bottom = None
        self._anchor_node = None
        self._anchor_match = None
        QTimer.singleShot(0, self._jump_to_bottom)

    def _jump_to_bottom(self) -> None:
        if self._anchor_node is not None:
            # A place kept since this was asked for (the rebuild's own jump).
            self._apply_anchor()
            return
        bar = self.verticalScrollBar()
        self._set_value(bar.maximum())

    def _set_value(self, value: int) -> None:
        self._adjusting = True
        try:
            self.verticalScrollBar().setValue(value)
        finally:
            self._adjusting = False

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._anchor_match is not None:
            self._reveal_match()
        elif self._anchor_node is not None:
            self._apply_anchor()
        elif self._anchor_bottom is not None:
            self._set_value(maximum - self._anchor_bottom)
        elif self._follow:
            self._set_value(maximum)

    def _on_value_changed(self, value: int) -> None:
        bar = self.verticalScrollBar()
        if self._adjusting:
            return
        # The reader moved: they are where they chose to be now.
        self._anchor_bottom = None
        self._anchor_node = None
        self._anchor_match = None
        # Only the newest page is followed as it grows.
        self._follow = self.at_latest and value >= bar.maximum() - FOLLOW_TOLERANCE_PX
        if value <= bar.minimum() and self._lo > 0:
            QTimer.singleShot(0, self.show_earlier)
        elif value >= bar.maximum() > 0 and not self.at_latest:
            QTimer.singleShot(0, self.show_later)
