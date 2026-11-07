"""The cast board: a story's characters in the whole window, laid out as Your
characters is (gui/collection_page.py). The list down the left (Everyone,
the cast, the supporting characters, the model's suggestions), and the one
chosen on the right in two tabs: their card (gui/cast_panel.py) and their
pictures as a sheet of looks and expressions (gui/cast_board.py). Everyone
shows everyone's pictures at once.

The author (Nov 7 2026): a story had a People page for who is present, a
Cast page for the cards and more pictures, and the cast board for more
control still, besides Your characters for the whole app; bring the story's
together in one place, almost identical to Your characters, and make it
plain which is which: Your characters a collection for any story, the cast
board this story's own (with a way to keep them in Your characters). The
Cast page went; the People page opens this.

The page holds the window's cast panel, which it lends to the new story
guide and Setup (without the Pictures tab: `detach`, `attach`). What it asks
for is the window's (gui/window_board.py).
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

from sealedlore.gui.cast_board import KEEP_LABEL, SAVE_LABEL, SPOILERS_LABEL, CastBoard
from sealedlore.gui.cast_panel import CARD_TAB, CastPanel
from sealedlore.gui.wrap_row import WrapRow

PAGE_TITLE = "Cast board"
PICTURES_TAB = "Pictures"
EVERYONE_TAB = "Everyone's pictures"
# What the page is, said at its top: this story's own, not Your characters.
SCOPE_WORDS = (
    "This story's characters: their cards and pictures as this story has them. Changes "
    "here stay in this story. {save} keeps any of them in Your characters, for other "
    "stories and chats."
)
# In a simple chat: characters for their pictures alone (the author, Nov 7 2026).
CHAT_SCOPE_WORDS = (
    "This chat's characters, for their pictures alone: nothing about them is sent to the "
    "chat's model. The pictures they are shown with on the People page go with a picture "
    "you ask for. {save} keeps any of them in Your characters."
)


class StoryCastPage(QWidget):
    """See the module. `panel` and `board` are the window's; the page shows
    them and keeps them in step."""

    back_requested = Signal()

    def __init__(self, panel: CastPanel, board: CastBoard, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("castBoardPage")
        # Esc in a field gives the page the focus first (main_window._on_escape).
        self.setFocusPolicy(Qt.ClickFocus)
        self.panel = panel
        self.board = board
        board.embed()

        self.title = QLabel(PAGE_TITLE)
        self.title.setObjectName("boardTitle")
        self.scope = QLabel(SCOPE_WORDS.format(save=SAVE_LABEL.rstrip("…")))
        self.scope.setObjectName("scopeNote")
        self.scope.setWordWrap(True)
        self.spoilers = QCheckBox(SPOILERS_LABEL)
        self.spoilers.setToolTip(board.spoilers.toolTip())
        self.spoilers.toggled.connect(board.spoilers_toggled)
        self.save_button = QPushButton(SAVE_LABEL)
        self.save_button.setToolTip(board.save_button.toolTip())
        self.save_button.clicked.connect(
            lambda: board.save_requested.emit(self.panel.current.id if self._one() else "")
        )
        self.keep_button = QPushButton(KEEP_LABEL)
        self.keep_button.clicked.connect(board.keep_requested)
        self.keep_button.hide()
        self.back_button = QPushButton("Back to the story")
        self.back_button.setToolTip("Esc")
        self.back_button.clicked.connect(self.back_requested)
        header = WrapRow(group_spacing=12)
        header.add_group([self.title])
        header.add_group([self.spoilers])
        header.add_group([self.save_button, self.keep_button, self.back_button], right=True)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 6)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.scope)
        layout.addLayout(self._body, 1)

        panel.current_changed.connect(self._panel_chose)
        board.character_chosen.connect(self._board_chose)
        self.attach()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # Shown only in the whole window, the story's panels put away: it
        # must not widen the story's place in the stack it shares.
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, 0)

    # --- the panel, lent and given back ---------------------------------------

    def attach(self) -> None:
        """The panel in the page, with the Pictures tab and Everyone."""
        panel = self.panel
        if panel.parent() is not self:
            self._body.addWidget(panel)
            panel.show()
        if panel.tabs.indexOf(self.board) < 0:
            panel.tabs.addTab(self.board, PICTURES_TAB)
            panel.tabs.setTabToolTip(
                panel.tabs.indexOf(self.board),
                "Their pictures as a sheet of looks and expressions, the missing ones ready "
                "to draw",
            )
            self.board.show()
        panel.set_everyone(True)
        # Suggestions come from a story's prose; a chat's characters are
        # brought in by hand.
        panel.suggestions_box.setVisible(not panel.chat_mode)
        # The page's header has it, for the one shown or everyone.
        panel.save_button.setVisible(False)
        self._sync_tabs()

    def detach(self) -> CastPanel:
        """The panel taken out, to stand in a dialog (the guide's Characters
        step, Setup's Characters tab): the list and the card alone."""
        panel = self.panel
        index = panel.tabs.indexOf(self.board)
        if index >= 0:
            panel.tabs.removeTab(index)
            self.board.setParent(self)
            self.board.hide()
        panel.set_everyone(False)
        panel.tabs.setTabVisible(panel.tabs.indexOf(panel.tabs.widget(0)), True)
        panel.setParent(None)
        # Suggestions come from the story's prose, and there is none yet.
        panel.suggestions_box.setVisible(False)
        panel.save_button.setVisible(True)
        return panel

    def set_chat_mode(self, on: bool) -> None:
        """A simple chat's characters (pictures alone) or a story's."""
        words = CHAT_SCOPE_WORDS if on else SCOPE_WORDS
        self.scope.setText(words.format(save=SAVE_LABEL.rstrip("…")))
        self.back_button.setText("Back to the chat" if on else "Back to the story")
        if not self.lent():
            self.panel.suggestions_box.setVisible(not on)

    def lent(self) -> bool:
        return self.panel.parent() is not self

    # --- what is shown --------------------------------------------------------

    def show_character(self, card_id: str | None, *, card: bool = False) -> None:
        """A character's card (`card`) or pictures, or everyone's pictures
        (None)."""
        panel = self.panel
        if card_id is None:
            panel.show_everyone()
        else:
            panel.select_character(card_id)
        self._sync_tabs()
        if card_id is not None and not panel.everyone_shown():
            panel.tabs.setCurrentIndex(0 if card else panel.tabs.indexOf(self.board))

    def show_card(self) -> None:
        self.panel.tabs.setCurrentIndex(0)

    def sync_from_board(self) -> None:
        """The board's template and spoilers, on the page's own buttons."""
        template, tip = self.board.template
        self.keep_button.setVisible(bool(template))
        self.keep_button.setToolTip(tip)
        self.spoilers.blockSignals(True)
        self.spoilers.setChecked(self.board.spoilers.isChecked())
        self.spoilers.blockSignals(False)
        self.spoilers.setVisible(self.board.offers_spoilers)
        # Nobody to keep yet, or no picture to keep.
        self.save_button.setEnabled(self.board.has_cards)
        self.keep_button.setEnabled(self.board.has_pictures)

    def _one(self) -> bool:
        return self.panel.current is not None and not self.panel.everyone_shown()

    def _panel_chose(self, card_id: str) -> None:
        """Another one in the list: the board on them too (or everyone)."""
        if self.lent():
            return
        if self.panel.everyone_shown():
            self.board.show_character(None)
        elif card_id:
            self.board.show_character(card_id)
        self._sync_tabs()

    def _board_chose(self, card_id: str) -> None:
        """One chosen on Everyone's pictures: theirs, in the list too."""
        self.panel.select_character(card_id)
        self._sync_tabs()
        self.panel.tabs.setCurrentIndex(self.panel.tabs.indexOf(self.board))

    def _sync_tabs(self) -> None:
        """Everyone: their pictures alone (no card to show); one of them:
        their card and their pictures."""
        tabs = self.panel.tabs
        index = tabs.indexOf(self.board)
        if index < 0:
            return
        everyone = self.panel.everyone_shown()
        tabs.setTabVisible(0, not everyone)
        tabs.setTabText(index, EVERYONE_TAB if everyone else PICTURES_TAB)
        tabs.setTabText(0, CARD_TAB)
        if everyone:
            tabs.setCurrentIndex(index)
        self.save_button.setText(SAVE_LABEL)
