"""The transcript view.

A column of per-message widgets rather than one rich-text document: in-character
prose, narration, OOC and director messages each need their own treatment, and
later phases hang per-message controls off these same frames.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.continuation import continuable
from sealedlore.engine.dice import chip_text
from sealedlore.engine.reasoning import caught_note
from sealedlore.gui import theme
from sealedlore.gui.markdown_text import markdown_html, shown_text
from sealedlore.gui.picture_words import EDIT_LABEL, USE_CHARACTER_LABEL, USE_PLACE_LABEL
from sealedlore.gui.ref_images import pixmap_of, pixmap_within
from sealedlore.gui.sizing import GrowingText
from sealedlore.gui.video_view import PendingVideoWidget, VideoItem, VideoMessageWidget
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
from sealedlore.models.private import TeeUpstream
from sealedlore.providers.tee import upstream_label, upstream_words

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

# A streaming reply is drawn at most this often; what comes between waits
# (TranscriptView.append_streaming). The phone's is sent every 150 ms.
STREAM_DRAW_MS = 100

# Under a message's editor while its text is blank, Save off.
EMPTY_EDIT = "A message can't be empty. To remove it, use Delete from here… in its ⋯ menu."


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


@dataclass(frozen=True)
class OpenEdit:
    """A message's editor as it stood before the view was rebuilt, to open
    it again with what was typed (`MessageWidget.resume_edit`): a rebuild
    for anything else (the text size, dice shown, another message saved)
    destroyed it, mid-sentence, with nothing said."""

    rewrite: bool
    text: str
    ooc: str | None
    branch_name: str
    # (the text, the note) as the editor first opened, to tell what was
    # typed; None: all of it was (`MessageWidget.edit_again`).
    opened: tuple[str, str | None] | None
    focused: bool
    cursor: int


@dataclass(frozen=True)
class FailedPictureItem:
    """A picture that didn't come back, where it was meant to appear."""

    job_id: str
    text: str
    # What went wrong, and what Retry would send (shown as its tooltip).
    detail: str
    retry_tip: str = ""


# The pictures anchored after a node (None: before the first): a finished
# one as (record, its file), or one still being drawn.
# A picture with its bytes (None: its file is missing), or one still being drawn.
# A video (gui/video_view.VideoItem) comes the same way, done or not.
PictureItem = "tuple[GeneratedImage, bytes | None] | PendingPicture | FailedPictureItem | VideoItem"
PicturesFor = Callable[[str | None], f"Sequence[{PictureItem}]"]


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
        self._source: str | None = None

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._heights.clear()
        super().setText(text)

    def set_markdown(self, text: str, *, keep: bool = True) -> None:
        """Show the text as Markdown reads, never as HTML (gui/markdown_text.py):
        plain, exactly as written, when Markdown would change nothing.
        `keep`: its drawing kept for the next time (not while it streams)."""
        self._source = text
        rich = markdown_html(text, keep=keep)
        self.setTextFormat(Qt.PlainText if rich is None else Qt.RichText)
        self.setText(text if rich is None else rich)

    def hold(self, text: str) -> None:
        """The text as it now is, to be drawn a moment later (a reply
        streaming in: `TranscriptView.append_streaming`)."""
        self._source = text

    @property
    def source(self) -> str:
        """The text as written (a Markdown label holds its rich text)."""
        return self._source if self._source is not None else self.text()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        height = self._heights.get(width)
        if height is None:
            height = self._heights[width] = super().heightForWidth(width)
        return height

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._heights.clear()
        super().changeEvent(event)


def may_retake(turn_scene: str | None, open_scene: str | None) -> bool:
    """The session's rule for a private scene's messages, on the window's
    side (StorySession.regenerate): a passage may be taken again, and a turn
    taken back, when its author turn was written in the scene open now
    (`open_scene`, its id), or in the story itself while none is open
    (`turn_scene`: the turn's `meta.private_span`). An ended scene's messages
    stay as they are, and an open one touches only its own. Offered anywhere
    else, each ended in "Generation failed" (or, taken back, put the scene's
    words in the box to send to the story's model)."""
    return turn_scene == open_scene


def may_branch(scene: str | None, open_scene: str | None) -> bool:
    """The session's rule for a new branch (StorySession.rewrite_from): never
    while a private scene is open, and never from a scene's own message."""
    return open_scene is None and scene is None


def _retakeable_ids(nodes: Sequence[Node]) -> set[str]:
    """The passages Regenerate applies to: those answering an author turn.
    An opening written as given has none before it, and a private scene's
    summary is the author's approved text. Whether a private scene lets it
    be offered now is `may_retake`'s (`MessageWidget.offer_for_scene`)."""
    return {
        node.id
        for before, node in zip(nodes, nodes[1:], strict=False)
        if node.kind == "assistant"
        and before.kind == "user"
        and node.meta.private_summary_of is None
    }


class MessageWidget(QFrame):
    regenerate_requested = Signal(str)
    # More of this passage, written into it (engine/continuation.py).
    continue_requested = Signal(str)
    take_back_requested = Signal(str)
    # Toggle this passage as evidence for the next settings review.
    review_mark_requested = Signal(str)
    # (node id, text, the OOC note or None when the message has no such field).
    edit_committed = Signal(str, str, object)
    # The editor closed without saving (the transcript may be rebuilt now).
    edit_closed = Signal()
    # ⋯ → New branch from here…: (node id, the new text, the branch's name or
    # "" for the next "Branch N"), as a new branch.
    rewrite_committed = Signal(str, str, str, object)
    delete_requested = Signal(str)
    reroll_requested = Signal(str)
    # (node id, +1 or -1): show the neighbouring take.
    variant_requested = Signal(str, int)
    illustrate_requested = Signal(str)
    video_requested = Signal(str)
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
        retakeable: bool = True,
        continuable: bool = False,
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
        self._save_button: QPushButton | None = None
        # The story is being written: nothing here may change it (set_busy).
        self._busy = False
        self._edit_row: QWidget | None = None
        self._branch_name: QLineEdit | None = None
        # Under the editor while its text is blank: why Save is off.
        self._empty_note: QLabel | None = None
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
            f"To take the {'chat' if keep_full is not None else 'story'} another way, "
            "use ⋯ → New branch from here…"
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
            self._add_more_menu(
                header_row, kind == "assistant", rerollable, retakeable, continuable
            )
        layout.addLayout(header_row)
        # Where a branch begins: the one place in the story it shows.
        self.branch_label: QLabel | None = None
        if branch_start:
            self.branch_label = QLabel(f"⑂ Branch “{branch_start}” starts here")
            self.branch_label.setObjectName("branchStart")
            self.branch_label.setTextFormat(Qt.PlainText)
            layout.addWidget(self.branch_label)

        # Who was there, as they looked (View → Characters on each passage).
        self.faces_row: QWidget | None = None
        self.body = TextBody()
        # Model prose, names and imported text: Markdown, never HTML.
        self.body.set_markdown(text)
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.body)

        self.kind = kind
        self._ooc_text = ooc or ""
        self._ooc_editor: QPlainTextEdit | None = None
        self._edit_opened: tuple[str, str | None] = ("", None)
        # The private scenes it and its author turn were written in (`set_scenes`).
        self._scene: str | None = None
        self._turn_scene: str | None = None
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

    def add_faces(self, faces: Sequence[tuple[str, QPixmap]]) -> None:
        """A row of small pictures over the passage: who was in the scene at
        it, in the look each was shown with then (engine/looks.py)."""
        if not faces:
            return
        row = QWidget(self)
        row.setObjectName("passageFaces")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        for name, pixmap, *opened in faces:
            face = _FaceLabel(row, opened[0] if opened else None)
            face.setPixmap(pixmap)
            face.setToolTip(f"{name}: click to see the picture full size" if opened else name)
            face.setAccessibleName(name)
            line.addWidget(face)
        line.addStretch(1)
        self.layout_.insertWidget(self.layout_.indexOf(self.body), row)
        self.faces_row = row

    def add_note(self, text: str) -> None:
        """A line under the passage that can't be missed: why it was slow."""
        note = TextBody(text)
        note.setTextFormat(Qt.PlainText)
        note.setObjectName("messageNote")
        note.setWordWrap(True)
        index = self.layout_.indexOf(self.usage) if self.usage is not None else -1
        if index < 0:
            self.layout_.addWidget(note)
        else:
            self.layout_.insertWidget(index, note)

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

    def _add_more_menu(
        self,
        row: QHBoxLayout,
        assistant: bool,
        rerollable: bool,
        retakeable: bool = True,
        continuable: bool = False,
    ) -> None:
        menu = QMenu(self)
        # A chat's are messages and replies, and it is a chat: a story's words
        # read oddly there (the mode audit, Oct 29 2026).
        chat = self._keep_full is not None
        if not assistant:
            # The author's own turn, back in the composer with the settings it
            # was sent with: the alternative is copying the text out, deleting
            # the turn and its reply, and pasting it back in by hand.
            take_back = menu.addAction("Take back this message" if chat else "Take back this turn")
            take_back.setToolTip(
                "Return this turn to the composer to edit and send again; "
                "it and everything after it are removed"
            )
            take_back.triggered.connect(lambda: self.take_back_requested.emit(self.node_id))
            self.actions_["take_back"] = take_back
        if assistant and retakeable:
            regenerate = menu.addAction(
                "Regenerate this reply" if chat else "Regenerate this passage"
            )
            regenerate.setToolTip(
                "Write another take of this reply, in its place; this one is kept (‹ ›), "
                "and the chat after it stays"
                if chat
                else "Write another take of this passage, in its place; this one is kept "
                "(‹ ›), and the story after it stays"
            )
            regenerate.triggered.connect(lambda: self.regenerate_requested.emit(self.node_id))
            self.actions_["regenerate"] = regenerate
        if assistant and continuable:
            carry_on = menu.addAction("Continue this reply" if chat else "Continue this passage")
            carry_on.setToolTip(
                "Write more of this reply, carried on from where it stops"
                if chat
                else "Write more of this passage, carried on from where it stops; "
                "Length (below) says how much"
            )
            carry_on.triggered.connect(lambda: self.continue_requested.emit(self.node_id))
            self.actions_["continue"] = carry_on
        if rerollable:
            # Regenerate keeps the roll; this is the deliberate way to roll again.
            reroll = menu.addAction("Reroll the dice")
            reroll.triggered.connect(lambda: self.reroll_requested.emit(self.node_id))
            self.actions_["reroll"] = reroll
        if assistant and self._keep_full is None:
            # A settings review otherwise sees only the newest replies; this
            # lets the author point at the passage they mean. `_keep_full` is
            # set only in a chat, which has no review to be evidence in (the
            # action marked a reply and promised one).
            mark = menu.addAction("Use as evidence in a review")
            mark.setCheckable(True)
            mark.setChecked(self._marked)
            mark.setToolTip(
                "Send this passage, with the turn it answered, to the next settings review"
            )
            mark.triggered.connect(lambda: self.review_mark_requested.emit(self.node_id))
            self.actions_["review_mark"] = mark
        if assistant:
            illustrate = menu.addAction(
                "Illustrate this reply…" if chat else "Illustrate this passage…"
            )
            illustrate.setToolTip(
                "Write an image prompt for this reply"
                if chat
                else "Write an image prompt for the moment this passage ends on"
            )
            illustrate.triggered.connect(lambda: self.illustrate_requested.emit(self.node_id))
            self.actions_["illustrate"] = illustrate
            film = menu.addAction("Video of this reply…" if chat else "Video of this passage…")
            film.setToolTip(
                "A few seconds of video of "
                + ("this reply" if chat else "the moment this passage ends on")
                + ". Far dearer than a picture: you see the price before anything is sent"
            )
            film.triggered.connect(lambda: self.video_requested.emit(self.node_id))
            self.actions_["video"] = film
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
            + (
                f" (your {'message' if chat else 'turn'} is answered at once)"
                if not assistant
                else ""
            )
            + f"; the {'chat' if chat else 'story'} as it is now is kept, under its own name"
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
        # The dots say "more"; a chevron beside them said it twice.
        self.more_button.setProperty("dots", True)
        self.more_button.setToolTip("More: regenerate or take back, a new branch from here, delete")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setMenu(menu)
        row.addWidget(self.more_button)

    def set_text(self, text: str, *, keep: bool = True) -> None:
        self.body.set_markdown(text, keep=keep)

    def set_scenes(self, scene: str | None, turn_scene: str | None) -> None:
        """The private scene this message was written in, and the one of the
        author turn it answers (None: the story's own), for `offer_for_scene`."""
        self._scene, self._turn_scene = scene, turn_scene

    def offer_for_scene(self, open_scene: str | None) -> None:
        """What ⋯ offers here as the private scene open now allows (None: no
        scene is open): Regenerate and Take back by `may_retake`, New branch
        from here by `may_branch`, as the session decides them."""
        retake = may_retake(self._turn_scene, open_scene)
        for key, allowed in (
            ("regenerate", retake),
            ("reroll", retake),
            ("take_back", may_retake(self._scene, open_scene)),
            ("rewrite", may_branch(self._scene, open_scene)),
        ):
            action = self.actions_.get(key)
            if action is not None:
                action.setVisible(allowed)

    # --- inline editing (§5.3, §10) ---------------------------------------

    def begin_edit(self, rewrite: bool = False, *, focus: bool = True) -> None:
        """Swap the body for an editor in place, so the column doesn't reflow.

        With `rewrite`, saving makes a new branch from this message instead of
        correcting it (the author's rule: editing an earlier message forks).
        `focus`: the keyboard goes to the editor (not for one opened again
        after a rebuild, `resume_edit`, unless it had it).
        """
        if self._editor is not None or not self.node_id:
            return
        self._rewriting = rewrite

        # As tall as the message, up to most of the view (gui/sizing.py).
        editor = GrowingText(self.body.source, lines=6)
        editor.setObjectName("messageEditor")
        # An author's turn carries its OOC note: edited with the text, and
        # on a new branch from here (the author, Oct 2026: only Take back
        # could change it; Edit and New branch from here kept it as it was).
        # A chat has no notes: its model is sent the message as written, so
        # a note typed here went nowhere (the author, Oct 29 2026).
        ooc_editor: QPlainTextEdit | None = None
        if self.kind != "assistant" and self._keep_full is None:
            ooc_editor = GrowingText(self._ooc_text, lines=3)
            ooc_editor.setObjectName("oocEditor")
            ooc_editor.setPlaceholderText(
                "OOC note (optional): a fact or instruction for the storyteller, not dialogue"
            )
            ooc_editor.setToolTip(
                "Out-of-character addendum: authoritative fact or instruction, attached to "
                "this turn with the same authority as Direction. Blank for none."
            )

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
        if ooc_editor is not None:
            self.layout_.insertWidget(index + 2, ooc_editor)
            if self.ooc is not None:
                self.ooc.hide()
        self.layout_.insertWidget(index + (3 if ooc_editor is not None else 2), row)
        # Saved blank, a chat's message went out as nothing on every later
        # request (an API may refuse that outright) and a turn as its bare
        # label; a blank new branch closed and did nothing, without a word.
        empty_note = QLabel(EMPTY_EDIT)
        empty_note.setObjectName("hintLabel")
        empty_note.setWordWrap(True)
        empty_note.hide()
        self.layout_.insertWidget(self.layout_.indexOf(row) + 1, empty_note)
        editor.textChanged.connect(self._sync_save)
        self.body.hide()
        self.edit_button.setEnabled(False)
        self._editor = editor
        self._ooc_editor = ooc_editor
        self._edit_row = row
        self._empty_note = empty_note
        self._save_button = save
        # What the editors held as they opened. Compared with the message
        # itself, an editor left alone already differed: a plain text box
        # gives a no-break space back as a space, and a line or paragraph
        # separator as a new line.
        self._edit_opened = self._edit_now()
        self._sync_save()
        if focus:
            editor.setFocus(Qt.OtherFocusReason)

    def _edit_now(self) -> tuple[str, str | None]:
        """(the text, the OOC note or None with no note's field), as typed."""
        assert self._editor is not None
        ooc = self._ooc_editor.toPlainText() if self._ooc_editor is not None else None
        return self._editor.toPlainText(), ooc

    def edit_changed(self) -> bool:
        """The open editor holds something other than it opened with: the
        text, the OOC note, or a new branch's name."""
        if self._editor is None:
            return False
        named = self._branch_name is not None and bool(self._branch_name.text().strip())
        return self._edit_now() != self._edit_opened or named

    def edit_state(self) -> OpenEdit:
        """The open editor as it stands, to open it again once the view is
        rebuilt (`resume_edit`)."""
        assert self._editor is not None
        text, ooc = self._edit_now()
        focus = QApplication.focusWidget()
        return OpenEdit(
            rewrite=self._rewriting,
            text=text,
            ooc=ooc,
            branch_name=self._branch_name.text() if self._branch_name is not None else "",
            opened=self._edit_opened,
            focused=focus is not None and self.isAncestorOf(focus),
            cursor=self._editor.textCursor().position(),
        )

    def resume_edit(self, state: OpenEdit) -> None:
        """An editor open before the view was rebuilt, open again here with
        what was typed in it. Only what was typed: an editor left alone opens
        on the message as it is now."""
        self.begin_edit(state.rewrite, focus=False)
        if self._editor is None:
            return
        text, ooc = state.opened if state.opened is not None else (None, None)
        if state.text != text:
            self._editor.setPlainText(state.text)
        if self._ooc_editor is not None and state.ooc is not None and state.ooc != ooc:
            self._ooc_editor.setPlainText(state.ooc)
        if self._branch_name is not None:
            self._branch_name.setText(state.branch_name)
        if state.focused:
            cursor = self._editor.textCursor()
            cursor.setPosition(min(state.cursor, len(self._editor.toPlainText())))
            self._editor.setTextCursor(cursor)
            self._editor.setFocus(Qt.OtherFocusReason)

    def edit_again(
        self, text: str, ooc: str | None, branch_name: str = "", *, rewrite: bool = False
    ) -> None:
        """The editor open again with `text`, the note and a branch's name in
        it: a save the window refused (a new branch from a private scene's
        message) had closed it, and the text went with it."""
        self.resume_edit(
            OpenEdit(
                rewrite=rewrite,
                text=text,
                ooc=ooc,
                branch_name=branch_name,
                opened=None,
                focused=True,
                cursor=len(text),
            )
        )

    def commit_edit(self) -> None:
        """Save, as its button does (Ctrl+Enter in the editor)."""
        self._commit_edit()

    def escape_edit(self) -> None:
        """Esc in the editor: cancelled, asked first when it was changed."""
        if self.edit_changed():
            answer = QMessageBox.question(
                self,
                "Discard this edit?",
                "Close the editor without saving what you changed?",
                QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Discard:
                return
        self.cancel_edit()

    def cancel_edit(self, *, committing: bool = False) -> None:
        if self._editor is None:
            return
        self._editor.deleteLater()
        self._editor = None
        self._branch_name = None
        self._save_button = None
        if self._ooc_editor is not None:
            self._ooc_editor.deleteLater()
            self._ooc_editor = None
        if self._edit_row is not None:
            self._edit_row.deleteLater()
            self._edit_row = None
        if self._empty_note is not None:
            self._empty_note.deleteLater()
            self._empty_note = None
        self.body.show()
        if self.ooc is not None:
            self.ooc.show()
        self.edit_button.setEnabled(not self._busy)
        if not committing:
            self.edit_closed.emit()

    def set_busy(self, busy: bool) -> None:
        """While the story is being written nothing on a message may change
        it: Edit and ⋯ are off, and an editor already open stays open with
        its Save waiting (the text is the author's; it used to be thrown
        away without a word, the review of Oct 20 2026)."""
        self._busy = busy
        self.edit_button.setEnabled(not busy and self._editor is None)
        if self.more_button is not None:
            self.more_button.setEnabled(not busy)
        self._sync_save()

    def _sync_save(self) -> None:
        if self._save_button is None:
            return
        blank = self._edit_blank()
        self._save_button.setEnabled(not self._busy and not blank)
        self._save_button.setToolTip(
            "Saved once the current job is done" if self._busy else EMPTY_EDIT if blank else ""
        )
        if self._empty_note is not None:
            self._empty_note.setVisible(blank)

    def _edit_blank(self) -> bool:
        return self._editor is not None and not self._editor.toPlainText().strip()

    def editing(self) -> bool:
        return self._editor is not None

    def _commit_edit(self) -> None:
        if self._editor is None or self._busy:
            return  # the editor stays, and Save with it, until the job is done
        if self._edit_blank():
            return  # the note under it says why; Delete from here… removes a message
        if not self._rewriting and self._edit_now() == self._edit_opened:
            # Saved as it opened: nothing to save. Saved anyway, the message
            # was written again (a no-break space a space), and the chapter
            # over it marked out of date and a part merged from it dropped,
            # for nothing. A new branch from here may begin unchanged.
            self.cancel_edit()
            return
        text = self._editor.toPlainText()
        rewriting = self._rewriting
        name = self._branch_name.text().strip() if self._branch_name is not None else ""
        ooc = self._ooc_editor.toPlainText() if self._ooc_editor is not None else None
        self.cancel_edit(committing=True)
        # The window rebuilds the transcript from story state, so the widget
        # does not update itself here — one writer, as with generation.
        if rewriting:
            self.rewrite_committed.emit(self.node_id, text, name, ooc)
        else:
            self.edit_committed.emit(self.node_id, text, ooc)


class AsideWidget(QFrame):
    """A question put to the model out of character, and its answer.

    Shown where it was asked, but it is not the story: the model never sees it
    as history, and it has no Edit, because there is nothing to keep in sync.
    """

    delete_requested = Signal(str)

    def __init__(
        self,
        question: str,
        answer: str = "",
        *,
        aside_id: str = "",
        tee: str | None = None,
        tee_upstream: TeeUpstream | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("message")
        self.setProperty("kind", "aside")
        self.aside_id = aside_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        # An attested TEE/ model's answer says how its check went, as a passage does.
        self.header = QLabel(
            "Question  ·  out of character, not part of the story"
            + TEE_MARKS.get(tee or "", "")
            + run_by(tee, tee_upstream)
        )
        self.header.setObjectName("messageHeader")
        if tee in TEE_MARK_TIPS:
            self.setToolTip(tee_tip(tee, tee_upstream))
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

        self.body = TextBody()
        self.body.set_markdown(answer)
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.body)

    def set_text(self, text: str, *, keep: bool = True) -> None:
        self.body.set_markdown(text, keep=keep)


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
            # As it was decoded, when that is the size shown: no second copy.
            self._scaled = (
                self._pixmap
                if self._pixmap.size() == target
                else self._pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._scaled)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


def picture_caption(image: GeneratedImage) -> str:
    if image.from_card:
        return "  ·  ".join(
            part for part in (f"From {image.from_card}'s card", image.card_file) if part
        )
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

    def __init__(self, image: GeneratedImage, data: bytes | None, *, chat: bool = False) -> None:
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
            if chat and key in PLACES_ONLY:
                continue
            if image.from_card and key in NOT_DRAWN:
                continue
            action = menu.addAction(text)
            action.triggered.connect(
                lambda _checked=False, k=key: self.action_requested.emit(k, self.image_id)
            )
        more = QToolButton()
        more.setObjectName("messageAction")
        more.setText("⋯")
        more.setProperty("dots", True)
        more.setToolTip("Open, save, use as a reference or a background, generate again or edit")
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

        # No larger than the column shows it: the file has the rest, and
        # everything done with a picture reads the file.
        pixmap = pixmap_within(data, QSize(READING_WIDTH, PICTURE_MAX_HEIGHT))
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


# What a picture offers only where there are places: a chat has none, and
# it did nothing there (the mode audit, Oct 29 2026). A chat's characters
# take pictures as a story's do (Nov 7 2026): Use as a character's picture…
# and a video's last frame are offered there too.
PLACES_ONLY = frozenset({"place"})
# What a picture from a card's own files has nothing for: no prompt drew it.
NOT_DRAWN = frozenset({"prompt", "again"})

IMAGE_ACTIONS = (
    ("open", "Open full size"),
    ("prompt", "Show the prompt"),
    ("save", "Save as…"),
    ("-", ""),
    ("character", USE_CHARACTER_LABEL),
    ("place", USE_PLACE_LABEL),
    ("background", "Set as background"),
    ("again", "Generate again…"),
    ("edit", EDIT_LABEL),
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


class FailedPictureWidget(QFrame):
    """A picture that failed, in the place it was meant to appear: what went
    wrong, and Retry (the same request again), Edit… (the picture dialog
    with it filled in: another prompt, model or pictures) or Dismiss."""

    action_requested = Signal(str, str)  # (job id, "retry" | "edit" | "dismiss")

    def __init__(self, item: FailedPictureItem) -> None:
        super().__init__()
        self.setObjectName("message")
        self.setProperty("kind", "picture")
        self.job_id = item.job_id
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        header = QLabel(item.text)
        header.setObjectName("messageHeader")
        header.setWordWrap(True)
        layout.addWidget(header)
        detail = QLabel(item.detail)
        detail.setObjectName("messageNote")
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(detail)
        row = QHBoxLayout()
        row.addStretch(1)
        for action, label, tip in (
            ("retry", "Retry", item.retry_tip or "Send the same request again."),
            (
                "edit",
                "Edit…",
                "Open the picture dialog with this request filled in, to change the "
                "prompt, the model or the pictures before drawing it again.",
            ),
            ("dismiss", "Dismiss", "Take this away from the story."),
        ):
            button = QToolButton()
            button.setObjectName("messageAction")
            button.setText(label)
            button.setToolTip(tip)
            button.clicked.connect(
                lambda _=False, a=action: self.action_requested.emit(self.job_id, a)
            )
            row.addWidget(button)
        layout.addLayout(row)


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
            height = layout.heightForWidth(width)
            if height < 0:
                # Nothing here wraps: every message in the column is open in
                # an editor. Told -1, the area squeezed them into its view.
                height = layout.sizeHint().height()
            self._heights[width] = height
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
        self.body = TextBody()
        self.body.set_markdown(text)
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.body)


FULL_STORY_LABEL = "Full story"
FULL_CHAT_LABEL = "Full chat"
STORYTELLER_VIEW_LABEL = "What the storyteller sees"
CHAT_VIEW_LABEL = "What the model sees"


class ViewToggle(QWidget):
    """Full story | What the storyteller sees, above the transcript.

    The story as written keeps every passage, archived ones included. The
    storyteller never sees archived prose again, only its chapter summary,
    and until this view that was visible only as raw prompt text.
    """

    changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("viewToggle")
        self.written = QToolButton()
        # Plain words (Oct 18 2026): "As written" and "As sent to the
        # storyteller" were the program's own terms.
        self.written.setText(FULL_STORY_LABEL)
        self.written.setToolTip(
            "The whole story, every message, including those the storyteller now gets "
            "as a chapter summary"
        )
        self.sent = QToolButton()
        self.sent.setText(STORYTELLER_VIEW_LABEL)
        self.sent.setToolTip(
            "What the storyteller is sent: chapter summaries where older messages have "
            "been summarised, then the messages it gets word for word (Ctrl+Shift+V)"
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
        self.sent.setText(CHAT_VIEW_LABEL if chat else STORYTELLER_VIEW_LABEL)
        self.written.setText(FULL_CHAT_LABEL if chat else FULL_STORY_LABEL)
        self.written.setToolTip(
            "The whole chat, every message, including those the model now gets as a summary"
            if chat
            else "The whole story, every message, including those the storyteller now gets "
            "as a chapter summary"
        )
        self.sent.setToolTip(
            "What the model is sent: summaries where older messages have been summarised, "
            "then the messages it gets word for word (Ctrl+Shift+V)"
            if chat
            else "What the storyteller is sent: chapter summaries where older messages have "
            "been summarised, then the messages it gets word for word (Ctrl+Shift+V)"
        )


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
    """How far down a word-wrapped label the line holding `position`
    starts, laid out as the label lays it out."""
    document = QTextDocument()
    document.setDefaultFont(label.font())
    document.setDocumentMargin(0)
    if label.textFormat() == Qt.RichText:
        document.setHtml(label.text())
    else:
        document.setPlainText(label.text())
    document.setTextWidth(label.contentsRect().width())
    block = document.findBlock(position)
    top = document.documentLayout().blockBoundingRect(block).top()
    line = block.layout().lineForTextPosition(position - block.position())
    return label.contentsRect().top() + int(top + (line.y() if line.isValid() else 0))


class _FaceLabel(QLabel):
    """A face over a passage: a click shows the picture whole."""

    def __init__(self, parent: QWidget, opened: Callable[[], None] | None) -> None:
        super().__init__(parent)
        self._opened = opened
        if opened is not None:
            self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._opened is not None:
            self._opened()
            return
        super().mouseReleaseEvent(event)


# The reply's mark (providers/tee.py `check_reply`: "verified" when the
# attested key's record is for the exact bytes received, "signed" when they
# couldn't be compared, "unchecked" when no record could be read; the manual,
# "Private scenes") and the author's message's (how it was sent, and whether a
# reply's record shows the attested enclave received it). Anything short of
# verified says so in the mark itself (the author, Nov 2026: warn wherever it
# is less secure than wanted; Nov 11 2026: "always make clear to the user what
# is actually happening and if it is properly attested or not"), not only in
# its tooltip.
TEE_MARKS = {
    "verified": "  ·  TEE verified",
    "signed": "  ·  TEE signed, text not compared",
    "failed": "  ·  TEE ⚠ check failed",
    "unchecked": "  ·  TEE ⚠ reply not verified",
    "encrypted": "  ·  end-to-end encrypted",
    "received": "  ·  received by the attested TEE",
    "unproven": "  ·  TEE ⚠ not shown to reach the attested enclave",
}
# What every mark short of end-to-end encryption leaves out.
_CLEAR = (
    " The text passed NanoGPT's servers in the clear. A private/ model is end-to-end "
    "encrypted instead."
)
TEE_MARK_TIPS = {
    "verified": (
        "TEE verified: the attested enclave's key signed this reply's record, and the "
        "record is for the exact text this app received. For a model RedPill serves, "
        "that enclave is RedPill's gateway, which passed the request on to the machine "
        "running the model: the gateway's signed receipt, read too, says that machine "
        "was verified, which is the gateway's word, not checked by this app (the model "
        "button says what was attested)." + _CLEAR
    ),
    "signed": (
        "TEE signed: the attested enclave's key signed this reply's record, but the "
        "reply's text can't be compared with it (NanoGPT rebuilds this model's replies), "
        "so a change on the way here wouldn't show. As for a verified reply, the machine "
        "running the model is the gateway's word." + _CLEAR
    ),
    "failed": (
        "This reply doesn't match its record: not signed by the attested key, not the "
        "text that was signed, or its receipt doesn't hold (another key, another reply, "
        "one served before this request was sent, or a model that wasn't verified). The "
        "model is attested again before anything more is sent to it."
    ),
    "unchecked": (
        "Not verified: no signed record of this reply could be read (this model's "
        "provider keeps none, as Chutes doesn't, or the check couldn't be reached), so "
        "nothing shows it came from the enclave attested for this chat or scene. "
        "NanoGPT routes each request, and a model may be served elsewhere when its "
        "provider is busy." + _CLEAR
    ),
    "encrypted": (
        "End-to-end encrypted: sealed on this machine to the attested enclave's key, and "
        "the reply opened with it. NanoGPT relayed ciphertext."
    ),
    "received": (
        "Received by the attested TEE: sent while the model's attestation held, and a "
        "reply's record, signed by the attested key, names this exchange." + _CLEAR
    ),
    "unproven": (
        "Sent to the TEE model while its attestation held, but no reply's signed record "
        "shows the attested enclave received it: NanoGPT routes each request, and this "
        "model's provider signs no replies (Chutes) or the check couldn't be made. A "
        "model may be served elsewhere when its provider is busy." + _CLEAR
    ),
}


def run_by(tee: str | None, upstream: TeeUpstream | None) -> str:
    """Beside a reply's TEE mark: who ran its model, by RedPill's record
    (Settings → Private, "Name the provider behind each TEE reply"), only
    beside a mark whose receipt the provider's name rests on."""
    if tee not in ("verified", "signed") or upstream is None or not upstream.name:
        return ""
    return f"  ·  run by {upstream_label(upstream)}"


def tee_tip(tee: str, upstream: TeeUpstream | None) -> str:
    """A TEE mark's tooltip, with who ran the model where that was looked up."""
    tip = TEE_MARK_TIPS[tee]
    if tee in ("verified", "signed") and upstream is not None:
        tip += "\n\n" + upstream_words(upstream)
    return tip


def sent_mark(message: Node, reply: Node | None) -> str | None:
    """The author's message's TEE mark as shown. Before Nov 11 2026 it was
    "attested" whatever the reply showed: read now by the reply shown with it."""
    mark = message.meta.tee
    if mark != "attested":
        return mark
    shown = reply is not None and reply.meta.tee in ("verified", "signed")
    return "received" if shown else "unproven"


class TranscriptView(QScrollArea):
    regenerate_requested = Signal(str)
    continue_requested = Signal(str)
    take_back_requested = Signal(str)
    review_mark_requested = Signal(str)
    # (node id, text, the OOC note or None when the message has no such field).
    edit_committed = Signal(str, str, object)
    delete_requested = Signal(str)
    delete_aside_requested = Signal(str)
    reroll_requested = Signal(str)
    rewrite_committed = Signal(str, str, str, object)
    variant_requested = Signal(str, int)
    illustrate_requested = Signal(str)
    video_requested = Signal(str)
    image_action_requested = Signal(str, str)
    stop_image_requested = Signal(str)
    failed_picture_action = Signal(str, str)
    # (action, video id): a finished video's menu, or a pending one's button.
    video_action_requested = Signal(str, str)
    keep_full_requested = Signal(str, bool)
    # A message's editor closed without saving.
    edit_closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("transcript")
        # The story is being written: no message may change it (set_busy).
        self._busy = False
        # The private scene open now, for what ⋯ offers (`offer_for_scene`).
        self._open_scene: str | None = None
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
        # A reply is drawn whole as it grows (its Markdown read again each
        # time): at most once every STREAM_DRAW_MS, what came meanwhile held
        # (`TextBody.hold`) and drawn when it is up or the reply ends. Drawn
        # on every piece, a long reply's work grew with its square.
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(STREAM_DRAW_MS)
        self._stream_timer.timeout.connect(self._draw_stream)
        self._stream_held = False
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

    def set_background(self, data: bytes | None) -> bool:
        """Show a picture behind the text, or none. False if it couldn't be read."""
        pixmap = pixmap_of(data) if data is not None else QPixmap()
        self._background = None if pixmap.isNull() else pixmap
        self._background_scaled = None
        self.setStyleSheet(background_qss() if self._background is not None else "")
        self.viewport().update()
        return data is None or self._background is not None

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
        self._stream_timer.stop()
        self._stream_held = False
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
        retakeable: bool = True,
        continuable: bool = False,
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
            retakeable=retakeable,
            continuable=continuable,
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
        widget.video_requested.connect(self.video_requested)
        widget.regenerate_requested.connect(self.regenerate_requested)
        widget.continue_requested.connect(self.continue_requested)
        widget.take_back_requested.connect(self.take_back_requested)
        widget.edit_committed.connect(self.edit_committed)
        widget.edit_closed.connect(self.edit_closed)
        widget.delete_requested.connect(self.delete_requested)
        widget.set_busy(self._busy)
        self._place(widget)
        return widget

    def set_busy(self, busy: bool) -> None:
        """The story is being written (or not): every message built now,
        and every one built later, knows (`MessageWidget.set_busy`)."""
        self._busy = busy
        for widget in self._container.findChildren(MessageWidget):
            if self._layout.indexOf(widget) >= 0:
                widget.set_busy(busy)

    def offer_continue(self, can: bool) -> None:
        """Whether the latest passage's ⋯ → Continue is offered, as the
        session says now: entering a private scene withdraws it from a passage
        written before the scene, and that rebuilds nothing."""
        for widget in self._container.findChildren(MessageWidget):
            action = widget.actions_.get("continue")
            if action is not None and self._layout.indexOf(widget) >= 0:
                action.setVisible(can)

    def offer_for_scene(self, open_scene: str | None) -> None:
        """The private scene open now (None: none is), for what ⋯ offers on
        every message built and every one built later
        (`MessageWidget.offer_for_scene`): entering a scene rebuilds nothing."""
        self._open_scene = open_scene
        for widget in self._container.findChildren(MessageWidget):
            if self._layout.indexOf(widget) >= 0:
                widget.offer_for_scene(open_scene)

    def open_editor(self, *, focused: bool = False) -> MessageWidget | None:
        """The message whose editor is open (with the keyboard's focus in it,
        if `focused`), or None."""
        focus = QApplication.focusWidget()
        for widget in self.open_editors():
            if not focused or (focus is not None and widget.isAncestorOf(focus)):
                return widget
        return None

    def open_editors(self) -> list[MessageWidget]:
        """Every message whose editor is open: more than one may be."""
        return [
            widget
            for widget in self._container.findChildren(MessageWidget)
            if self._layout.indexOf(widget) >= 0 and widget.editing()
        ]

    def editing(self) -> bool:
        """Whether a message's editor is open."""
        return bool(self.open_editors())

    def _open_edits(self) -> list[tuple[str, OpenEdit]]:
        """Every open editor as it stands, before a rebuild (`_reopen`)."""
        return [(widget.node_id, widget.edit_state()) for widget in self.open_editors()]

    def _reopen(self, edits: list[tuple[str, OpenEdit]]) -> None:
        """The editors open before a rebuild, open again on the messages
        still shown, with what was typed in them. A message no longer shown
        loses its editor: the window asks before that (`_edit_allows_leave`)."""
        for node_id, state in edits:
            if node_id in self._unit_of:
                widget = self.message_widget(node_id)
                if widget is not None:
                    widget.resume_edit(state)

    def add_picture(self, image: GeneratedImage, data: bytes | None) -> ImageMessageWidget:
        widget = ImageMessageWidget(image, data, chat=self._chat)
        widget.action_requested.connect(self.image_action_requested)
        self._place(widget)
        return widget

    def add_video(self, item: VideoItem) -> QFrame:
        widget = (
            VideoMessageWidget(item, chat=self._chat)
            if item.record.status == "done"
            else PendingVideoWidget(item)
        )
        widget.action_requested.connect(self.video_action_requested)
        self._place(widget)
        return widget

    def add_pending_picture(self, job_id: str, text: str) -> PendingPictureWidget:
        widget = PendingPictureWidget(job_id, text)
        widget.stop_requested.connect(self.stop_image_requested)
        self._place(widget)
        return widget

    def add_failed_picture(self, item: FailedPictureItem) -> FailedPictureWidget:
        widget = FailedPictureWidget(item)
        widget.action_requested.connect(self.failed_picture_action)
        self._place(widget)
        return widget

    def add_aside(
        self,
        question: str,
        answer: str = "",
        *,
        aside_id: str = "",
        tee: str | None = None,
        tee_upstream: TeeUpstream | None = None,
    ) -> AsideWidget:
        widget = AsideWidget(
            question, answer, aside_id=aside_id, tee=tee, tee_upstream=tee_upstream
        )
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
        empty_actions: Sequence[tuple[str, Callable[[], None]]] = (),
        can_continue: bool | None = None,
        faces_for: Callable[[str], Sequence[tuple[str, QPixmap]]] | None = None,
    ) -> None:
        """`empty_actions`: buttons under `empty_text` when there are no
        messages (Start the story…). `can_continue`: the session's word on
        whether the last message can be carried on (a private scene's rule);
        None reads it from the messages alone."""
        cast_by_id = {character.id: character for character in cast}
        edits = self._open_edits()
        self.clear()
        self._faces_for = faces_for

        def add_asides(anchor: str | None) -> None:
            for aside in asides_for(anchor) if asides_for else ():
                self.add_aside(
                    aside.question,
                    aside.answer,
                    aside_id=aside.id,
                    tee=aside.tee,
                    tee_upstream=aside.tee_upstream,
                )
            for item in pictures_for(anchor) if pictures_for else ():
                if isinstance(item, tuple):
                    self.add_picture(*item)
                elif isinstance(item, VideoItem):
                    self.add_video(item)
                elif isinstance(item, FailedPictureItem):
                    self.add_failed_picture(item)
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
                retakeable=node.id in retakeable,
                continuable=node is nodes[-1] and carries_on,
                variant=variant_of(node.id) if variant_of else None,
                marked=node.id in marked_ids,
                show_rolls=show_rolls,
                branch_start=(branch_starts or {}).get(node.id),
                turn=turn_of.get(node.id),
                reply=reply_of.get(node.id),
            )
            add_asides(node.id)

        retakeable = _retakeable_ids(nodes)
        turn_of = {node.id: before for before, node in zip(nodes, nodes[1:], strict=False)}
        reply_of = {before.id: node for before, node in zip(nodes, nodes[1:], strict=False)}
        carries_on = continuable(nodes) if can_continue is None else can_continue

        units: list[_Unit] = [(None, lambda: add_asides(None))]
        units += [(node.id, lambda node=node: build(node)) for node in nodes]
        self._show_units(units)
        self._texts = [(node.id, node.content) for node in nodes]
        if not nodes and self._layout.count() == 1:
            self.show_placeholder(empty_text, empty_actions)
        self.scroll_to_end()
        self._reopen(edits)

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
        edits = self._open_edits()
        self.clear()
        self._faces_for = None  # what the storyteller is sent: words only

        def note() -> None:
            label = QLabel(
                "What the model is given of the chat so far: summaries where older "
                "messages have been summarised, then the messages sent word for word. The "
                "system prompt is on the Prompt page (View → Prompt page (advanced))."
                if self._chat
                else "What the storyteller is given as the story so far: summaries where the "
                "prose has been archived, then the passages sent word for word. The world, "
                "cast, lore and scene are on the Prompt page (View → Prompt page (advanced))."
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
                retakeable=node.id in retakeable,
                variant=variant_of(node.id) if variant_of else None,
                marked=node.id in marked_ids,
                show_rolls=show_rolls,
                branch_start=(branch_starts or {}).get(node.id),
                turn=turn_of.get(node.id),
                reply=reply_of.get(node.id),
            )

        retakeable = _retakeable_ids(nodes)
        turn_of = {node.id: before for before, node in zip(nodes, nodes[1:], strict=False)}
        reply_of = {before.id: node for before, node in zip(nodes, nodes[1:], strict=False)}
        units: list[_Unit] = [(None, note)]
        units += [(None, lambda chapter=chapter: card(chapter)) for chapter in chapters]
        units += [(node.id, lambda node=node: build(node)) for node in nodes]
        self._show_units(units)
        self._texts = [(node.id, node.content) for node in nodes]
        self.scroll_to_end()
        self._reopen(edits)

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
        retakeable: bool = True,
        continuable: bool = False,
        variant: tuple[int, int] | None = None,
        marked: bool = False,
        show_rolls: bool = True,
        branch_start: str | None = None,
        turn: Node | None = None,
        reply: Node | None = None,
    ) -> MessageWidget:
        """`turn`: the message before this one, the author turn a passage
        answers, for what a private scene lets ⋯ offer. `reply`: the one
        after it, which an older TEE mark on the author's is read by."""
        label = speaker_label(node, cast_by_id, self._chat)
        tee = sent_mark(node, reply) if node.kind == "user" else node.meta.tee
        if node.kind == "assistant":
            # How the author judges whether a length preset is working.
            label += f"  ·  {word_count(node.content)} words"
            if node.meta.roll is not None and show_rolls:
                label += f"  ·  {chip_text(node.meta.roll)}"
        if unsent:
            label += "  ·  not sent: over the budget"
        # A chat's private part is a part, as it says everywhere else
        # (private_dialogs.chat_words); a story's is a scene.
        part = "part" if self._chat else "scene"
        if node.meta.private_summary_of:
            label = f"Private {part}  ·  summary"
        elif node.meta.private_span or self._tee_look:
            if node.meta.private_span:
                label += "  ·  private"
            # How the reply's check went (TEE_MARKS), or how the message went,
            # and who ran the model, where that was looked up.
            label += TEE_MARKS.get(tee or "", "") + run_by(tee, node.meta.tee_upstream)
        widget = self.add_message(
            label,
            node.content,
            message_kind(node),
            # A chat sends no notes; one left by an older edit isn't shown.
            None if self._chat else node.ooc,
            node_id=node.id,
            archived=archived or unsent,
            rerollable=rerollable,
            retakeable=retakeable,
            continuable=continuable,
            variant=variant,
            usage=node.meta.usage if node.kind == "assistant" else None,
            reasoning=node.meta.reasoning if node.kind == "assistant" else None,
            marked=marked,
            branch_start=branch_start,
            keep_full=node.meta.keep_full if self._chat else None,
        )
        if self._tee_look:
            widget.setProperty("private", "true")
            widget.setToolTip(
                "A TEE chat: every message goes to its TEE model only, and only while its "
                "attestation holds; each message's mark says what its check showed."
            )
        if node.meta.private_span:
            widget.setProperty("private", "true")
            widget.setToolTip(f"Written in a private {part}: only the private model ever saw it.")
        if tee in TEE_MARK_TIPS:
            widget.setToolTip(tee_tip(tee, node.meta.tee_upstream))
        if node.meta.private_summary_of:
            widget.setProperty("privatesummary", "true")
            widget.setToolTip(
                f"The approved summary of a private {part}: all any other model knows of it."
            )
        for model in node.meta.reasoning_caught:
            widget.add_note("⚠ " + caught_note(model))
        # A failure or warning from sending or reading this message: the
        # status bar's notice is gone in seconds (engine `_kept_notice`).
        for note in node.meta.notes:
            widget.add_note("⚠ " + note)
        faces_for = getattr(self, "_faces_for", None)
        if faces_for is not None and node.kind == "assistant":
            widget.add_faces(faces_for(node.id))
        # The private scene it was written in, and a passage's turn's, for ⋯.
        own = node.meta.private_span
        answered = turn.meta.private_span if turn is not None and node.kind == "assistant" else own
        widget.set_scenes(own, answered)
        widget.offer_for_scene(self._open_scene)
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
        and paging up from the bottom of the view went round in circles.)
        Nor a message whose editor is open (`_editing_units`)."""
        excess = self._messages(self._lo, self._hi) - MAX_BUILT
        if excess <= 0:
            return
        anchor = self._unit_of.get(self._anchor_node[0]) if self._anchor_node else None
        editing = self._editing_units()
        if keep_top:
            if self._loose:
                return
            cut, dropped = self._hi, 0
            while cut > self._lo and dropped < excess:
                cut -= 1
                dropped += self._units[cut][0] is not None
            if anchor is not None:
                cut = max(cut, self._forward(anchor, PAGE // 2))
            if editing:
                cut = max(cut, editing[-1] + 1)
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
            if editing:
                cut = min(cut, editing[0])
            if cut > self._lo:
                self._drop_range(self._lo, cut)

    def _editing_units(self) -> list[int]:
        """The built units holding a message whose editor is open, in story
        order. The window keeps them: dropped (going to a chapter or a Find
        match far off, sending a turn while reading back), the editor went
        with what was typed in it, and nothing was said."""
        return sorted(
            index
            for index, widgets in self._unit_widgets.items()
            if any(isinstance(widget, MessageWidget) and widget.editing() for widget in widgets)
        )

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
        QTimer.singleShot(0, self, self._apply_anchor)

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
            QTimer.singleShot(0, self, self._apply_anchor)

    def _jump_to(self, index: int) -> None:
        """Rebuild the window around unit `index`: about half a page either
        side, a whole page in all, clamped to the story. With a message's
        editor open, the window runs on to it instead (`_run_on_to`)."""
        if self._editing_units():
            self._run_on_to(index)
            return
        self._drop_range(self._lo, self._hi)
        self._lo = self._hi = index
        lo = self._back(index, PAGE // 2)
        hi = self._forward(index, PAGE - self._messages(lo, index))
        lo = self._back(lo, max(0, PAGE - self._messages(lo, hi)))
        self._follow = False
        self._anchor_bottom = None
        self._build_range(lo, hi)
        self._sync_rows()

    def _run_on_to(self, index: int) -> None:
        """The window built on, from where it is to unit `index` and half a
        page past it, and trimmed of what no editor holds: one run of units
        still, and the editors in it. Slower than a jump for a message far
        off, and only while a message far off is being edited."""
        if index < self._lo:
            self._build_range(self._back(index, PAGE // 2), self._lo)
            self._trim(keep_top=True)
        elif index >= self._hi:
            self._build_range(self._hi, self._forward(index, PAGE // 2))
            self._trim(keep_top=False)
        self._follow = False
        self._anchor_bottom = None
        self._sync_rows()

    def jump_to_start(self) -> None:
        """The first page of the story, from the top."""
        if not self._units or self._loose:
            return
        self._jump_to(0)
        self._layout.activate()
        self._set_value(0)
        QTimer.singleShot(0, self, lambda: self._set_value(0))

    def jump_to_latest(self) -> None:
        """The newest page, followed at the bottom as when the story opened."""
        if self._units and not self.at_latest:
            if self._editing_units():
                self._run_on_to(len(self._units) - 1)
            else:
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
        # Room under the buttons: they sat on the box's bottom edge.
        layout.setContentsMargins(0, 0, 0, 24 if actions or more_actions else 0)
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
        self._draw_stream()
        self.ensure_at_latest()
        # The label the rebuild gives it afterwards (`speaker_label`): a
        # chat's header read "Story" while the reply streamed, then flipped.
        self._streaming = self.add_message("Assistant" if self._chat else "Story", "", "assistant")
        self.scroll_to_end()

    def begin_continuing(self, node_id: str) -> None:
        """Continue: the new text streams onto the end of the passage it
        carries on, in its own message."""
        self._draw_stream()
        self.ensure_at_latest()
        self._streaming = self.message_widget(node_id)
        if self._streaming is None:
            self.begin_streaming()
            return
        self.scroll_to_end()

    def begin_aside(self, question: str) -> None:
        self._draw_stream()
        if self._placeholder is not None:
            self._placeholder.hide()
        self.ensure_at_latest()
        self._streaming = self.add_aside(question)
        self.scroll_to_end()

    def append_streaming(self, text: str) -> None:
        """A piece of the reply: drawn now if nothing was drawn in the last
        STREAM_DRAW_MS, else held and drawn when that is up. Its drawing is
        never kept (gui/markdown_text.py): every prefix of the reply was."""
        if self._streaming is None:
            self.begin_streaming()
        assert self._streaming is not None
        body = self._streaming.body
        if self._stream_timer.isActive():
            body.hold(body.source + text)
            self._stream_held = True
            return
        self._streaming.set_text(body.source + text, keep=False)
        self._stream_timer.start()
        self.scroll_to_end()

    def _draw_stream(self) -> None:
        """What streamed in since the last drawing, drawn now."""
        widget = self._streaming
        if widget is None or not self._stream_held:
            return
        self._stream_held = False
        widget.set_text(widget.body.source, keep=False)
        self._stream_timer.start()
        self.scroll_to_end()

    def end_streaming(self) -> None:
        # The last pieces, held, are drawn: nothing that came is left out.
        self._draw_stream()
        self._stream_timer.stop()
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
        QTimer.singleShot(0, self, self._apply_anchor)

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
        QTimer.singleShot(0, self, self._apply_anchor)

    @property
    def searchable_texts(self) -> list[tuple[str, str]]:
        """(message id, text) for each message shown, in story order, as the
        reader sees it (Markdown's marks gone): Find selects at its offsets."""
        return [(node_id, shown_text(text)) for node_id, text in self._texts]

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
        QTimer.singleShot(0, self, self._reveal_match)
        return True

    def select_match(self, node_id: str, start: int, length: int) -> bool:
        """Select a match again in its message, where it is built, without
        moving the reader: a rebuild (a turn, a theme or text size) made new
        labels, and the count still said "6 of 6" with nothing selected."""
        self.clear_match()
        widget = self._built_widget(node_id)
        if widget is None or widget._editor is not None:
            return False
        widget.body.setSelection(start, length)
        self._found = widget.body
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
        QTimer.singleShot(0, self, self._jump_to_bottom)

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
            QTimer.singleShot(0, self, self.show_earlier)
        elif value >= bar.maximum() > 0 and not self.at_latest:
            QTimer.singleShot(0, self, self.show_later)
