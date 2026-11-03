"""The window's side of the cast board (gui/cast_board.py): it takes the
whole window, the story's panels, the box to type in and the row above the
story put away until Back to the story (or Esc), and the pictures it asks
for are drawn and kept as everywhere else. A mixin for `MainWindow`.

The author (Nov 3 2026): "a Cast tab that can expand to the full screen so
people can easily view all the characters, all their photos for different
emotions and dress/pose types and easy fill in missing ones, add custom
types and so on".
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMessageBox

from sealedlore.engine.look_sheet import label_key, wanted_along
from sealedlore.engine.looks import look_words
from sealedlore.gui.cast_board import BOARD_LABEL, CastBoard
from sealedlore.gui.fill_gaps import ask_fill_gaps
from sealedlore.gui.picture_labels import edit_labels
from sealedlore.gui.ref_images import PICTURE_FILTER, store_reference

BOARD_TIP = (
    "Every character's pictures in the whole window: each as a sheet of looks and "
    "expressions, with what is missing ready to draw"
)


class CastBoardWindow:
    def _init_board(self) -> None:
        self.cast_board = CastBoard(self._story_pictures)
        self.centre_stack.addWidget(self.cast_board)
        board = self.cast_board
        board.back_requested.connect(lambda: self.show_cast_board(False))
        board.picture_action.connect(self._board_picture)
        board.fill_requested.connect(self._board_fill)
        board.asked_requested.connect(self._board_asked)
        board.sheet_changed.connect(self._board_changed)
        board.files_requested.connect(self._board_files)
        self.image_jobs.changed.connect(self._refresh_board)
        # What the board put away, each as it was, to bring back as it was.
        self._board_away: list[tuple[object, bool]] = []

    def _board_action(self, view_menu) -> None:
        self.board_action = QAction(f"{BOARD_LABEL}…", self)
        self.board_action.setStatusTip(BOARD_TIP)
        self.board_action.triggered.connect(lambda: self.show_cast_board(True))
        view_menu.addAction(self.board_action)

    def board_shown(self) -> bool:
        return self.centre_stack.currentWidget() is self.cast_board

    def show_cast_board(self, on: bool = True, card_id: str | None = None) -> None:
        """The board in the whole window, on `card_id`'s sheet (None:
        everyone), or the story back as it was."""
        session = self.session
        if on and (session is None or session.story.chat):
            return
        if on:
            if not self.board_shown():
                self.close_find()
                self.show_map(False)
                self._board_away = [(w, w.isVisible()) for w in self._board_hides()]
                for widget, _visible in self._board_away:
                    widget.setVisible(False)
                self.centre_stack.setCurrentWidget(self.cast_board)
                self.cast_board.forget_pictures()
            self._hide_banners()
            self.cast_board.aim(card_id)
            self._refresh_board()
            self.cast_board.setFocus()
            return
        if not self.board_shown():
            return
        self.centre_stack.setCurrentWidget(self.transcript)
        away, self._board_away = self._board_away, []
        for widget, visible in away:
            widget.setVisible(visible)
        # The banners as the story stands now (a job may have ended meanwhile).
        self.refresh_summaries()
        self._sync_held_away()
        self._sync_stage()
        self._reshare_docks()

    def _hide_banners(self) -> None:
        """The story's banners (over budget, stale chapters, the one played
        away) are about the story, not the board: put away while it shows,
        and brought back from the story when it closes."""
        for banner in (self.over_budget, self.staleness, self.held_away):
            banner.hide()

    def _board_hides(self) -> list:
        """What the board puts away: the story's panels, the box to type in
        and the row above the story. The menus stay."""
        return [
            self.stories_dock,
            self.inspector_dock,
            self.composer,
            self.stories_button,
            self.view_toggle,
            self.find_button,
            self.place_button,
            self.branch_bar,
        ]

    def _board_cards(self) -> list:
        """The characters as the Cast page shows them (a plot-hidden one only
        with spoilers on)."""
        session = self.session
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        return [c for c in (*session.cast, *session.supporting) if c.id not in hidden]

    def _refresh_board(self) -> None:
        """The board from the story as it stands, when it is showing."""
        session = self.session
        if session is None or not self.board_shown():
            return
        self._hide_banners()
        drawing: Counter = Counter()
        for job in self.image_jobs.pending(session.story.id):
            request = job.request
            if request.card_id and not request.card_first:
                key = (
                    request.card_id,
                    label_key(request.card_look),
                    label_key(request.card_expression),
                )
                drawing[key] += 1
        wanted = {
            character_id: dict(counts)
            for character_id, counts in wanted_along(session._looks_path()).items()
        }
        self.cast_board.set_cards(
            self._board_cards(),
            shown=session.looks_now(),
            wanted=wanted,
            drawing=dict(drawing),
            begun=bool(session.nodes),
            can_change=not self._busy,
        )

    # --- what the board asks for ---------------------------------------------------------------

    def _board_card(self, card_id: str):
        session = self.session
        if session is None:
            return None
        return next((c for c in (*session.cast, *session.supporting) if c.id == card_id), None)

    def _board_changed(self, card_id: str) -> None:
        """A card changed on the board: kept, and every page that shows it
        brought up to date."""
        if self.session is None:
            return
        self._on_cast_changed()
        self.cast_panel.refresh_pictures()
        self.people_page.forget_pictures()
        self._refresh_people()
        self._refresh_board()

    def _board_refused(self) -> bool:
        if self._busy:
            self.statusBar().showMessage(
                "Cards can be changed once the story's current job is done.", 6000
            )
            return True
        return False

    def _board_picture(self, what: str, card_id: str, ref_id: str) -> None:
        card = self._board_card(card_id)
        ref = next((r for r in card.reference_images if r.id == ref_id), None) if card else None
        if ref is None:
            return
        if what == "whole":
            words = look_words(ref) if (ref.look or ref.expression) else ref.caption
            self._show_whole(
                f"{card.name} · {words}" if words else card.name,
                self.session.pictures.read(ref.file),
            )
            return
        if what == "show":
            self._show_look(card_id, ref_id)
            self._refresh_board()
            return
        if what in ("edit", "again", "set"):
            self._on_card_picture(card_id, ref_id, what)
            return
        if self._board_refused():
            return
        if what == "labels":
            if edit_labels(self, ref, card.reference_images):
                self._board_changed(card_id)
        elif what == "portrait":
            card.reference_images.remove(ref)
            card.reference_images.insert(0, ref)
            self._board_changed(card_id)
        elif what == "remove":
            answer = QMessageBox.question(
                self,
                "Remove this picture?",
                f"Take this picture off {card.name}'s card? Pictures drawn for the story stay "
                "in Media.",
            )
            if answer == QMessageBox.Yes and ref in card.reference_images:
                card.reference_images.remove(ref)
                self._board_changed(card_id)

    def _board_fill(self, card_id: str, gaps: list) -> None:
        session = self.session
        card = self._board_card(card_id)
        if card is None or self._card_pictures_refused(session):
            return
        requests = ask_fill_gaps(
            self._dialog_parent(),
            session,
            card,
            gaps,
            catalog=self.image_catalog,
            browse=self._browse_pictures,
        )
        self._drain_side_costs()
        if requests:
            self._start_card_pictures(requests)
            self._refresh_board()

    def _board_asked(self, card_id: str, words: str) -> None:
        """Draw a look as the story asked for it ("Draw them as …"), from the
        picture they are shown with now."""
        card = self._board_card(card_id)
        if card is None or not card.reference_images:
            return
        shown = self.session.looks_now().get(card_id) or card.reference_images[0].id
        self._on_card_picture(card_id, shown, "edit", wanted=words)

    def _board_files(self, card_id: str, look: str, expression: str) -> None:
        """Pictures from files onto their card, labelled for the cell they
        were asked for in (none: unlabelled)."""
        card = self._board_card(card_id)
        store = self.session.pictures if self.session is not None else None
        if card is None or store is None or self._board_refused():
            return
        from sealedlore.gui.crop_dialog import cropped_picture

        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Pictures of {card.name}", str(Path.home()), PICTURE_FILTER
        )
        added, problems = 0, []
        for number, path in enumerate(paths, 1):
            counter = f"{number} of {len(paths)}" if len(paths) > 1 else ""
            try:
                picture = cropped_picture(self, Path(path), "portrait", counter=counter)
                if picture is None:
                    continue
                ref = store_reference(*picture, store)
                ref.look, ref.expression = look, expression
                card.reference_images.append(ref)
                added += 1
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        if problems:
            QMessageBox.warning(self, "Couldn't add", "\n".join(problems))
        if added:
            self._board_changed(card_id)
