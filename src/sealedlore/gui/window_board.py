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

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMessageBox

from sealedlore.engine.look_sheet import label_key, sheet_for, wanted_along
from sealedlore.engine.looks import look_words
from sealedlore.gui.cast_board import BOARD_LABEL, CastBoard
from sealedlore.gui.fill_gaps import Waiting, ask_fill_gaps
from sealedlore.gui.picture_labels import edit_labels
from sealedlore.gui.ref_images import PICTURE_FILTER, store_reference
from sealedlore.storage.template_pictures import (
    forget_kept,
    keep_pictures,
    read_kept,
    template_of,
)

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
        board.files_dropped.connect(
            lambda card_id, look, expression, paths: self._board_files(
                card_id, look, expression, paths=paths
            )
        )
        board.spoilers_toggled.connect(self._board_spoilers)
        board.keep_requested.connect(self._board_keep)
        board.restyle_requested.connect(self._board_restyle)
        self.image_jobs.changed.connect(self._refresh_board)
        # A new look's first picture failed: the rest of its row is let go
        # (once the failure is out of the pending list).
        self.image_jobs.failed.connect(lambda *_args: QTimer.singleShot(0, self._start_waiting))
        # What the board put away, each as it was, to bring back as it was.
        self._board_away: list[tuple[object, bool]] = []
        # Gaps approved to be drawn from a new look's first picture once it
        # is in: (story id, what was approved). For the run only.
        self._board_waiting: list[tuple[str, Waiting]] = []

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
                self.show_collection(False)
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
        # The banners, the branch bar and the place button as the story
        # stands now (a job may have ended meanwhile).
        self.refresh_summaries()
        self._sync_held_away()
        self.refresh_branch_bar()
        self._sync_place_button()
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
        for story_id, waiting in self._board_waiting:
            if story_id == session.story.id:
                key = (waiting.card_id, label_key(waiting.look), label_key(waiting.expression))
                drawing[key] += 1
        template = template_of(session.story)
        kept = read_kept(template, self.root) if template else None
        self.cast_board.set_template(template, kept.count if kept else 0)
        self.cast_board.set_cards(
            self._board_cards(),
            shown=session.looks_now(),
            wanted=wanted,
            drawing=dict(drawing),
            begun=bool(session.nodes),
            can_change=not self._busy,
            hidden=session.hidden_ids(),
            spoilers=self.config.show_plot_spoilers,
        )

    def _board_restyle(self, card_id: str) -> None:
        """Restyle… on the board: the character shown, or everyone shown."""
        board, session = self.cast_board, self.session
        if session is None:
            return
        if card_id:
            card = session.card(card_id)
            cards = [card] if card is not None else []
        else:
            cards = list(board._cards)
        if self.restyle_pictures(cards):
            self._refresh_board()

    def _board_keep(self) -> None:
        """Every character's pictures, labels and sheets kept for new stories
        from the story's template (storage/template_pictures.py)."""
        session = self.session
        template = template_of(session.story) if session is not None else ""
        if not template:
            return
        characters = [*session.cast, *session.supporting, *session.story.setup.set_aside]
        count = sum(len(c.reference_images) for c in characters)
        kept = read_kept(template, self.root)
        choice = self._keep_choice(template, count, kept)
        if choice == "keep":
            try:
                kept = keep_pictures(
                    template, session.story.title, characters, session.pictures.read, self.root
                )
            except OSError as exc:
                self._warn_plain("Couldn't keep the pictures", str(exc))
                return
            self.statusBar().showMessage(
                f"Kept {kept.count} pictures for new stories from {template}.", 8000
            )
        elif choice == "forget":
            forget_kept(template, self.root)
            self.statusBar().showMessage(f"The pictures kept for {template} are forgotten.", 8000)
        self._refresh_board()

    def _keep_choice(self, template: str, count: int, kept) -> str:
        """Keep them ("keep"), forget the ones kept ("forget"), or ""."""
        box = QMessageBox(self)
        box.setWindowTitle(f"Keep these pictures for {template}")
        box.setText(
            f"Keep every character's pictures, with their labels and sheets ({count} pictures), "
            f"for new stories from {template}?\n\nStarting {template} again, you can add them or "
            f"leave them out. {template} itself stays as it is."
            + (
                f"\n\nKept for it now: {kept.count} pictures from “{kept.kept_from}”. These "
                "replace them."
                if kept
                else ""
            )
        )
        keep = box.addButton("Keep them", QMessageBox.AcceptRole)
        forget = None
        if kept:
            forget = box.addButton("Forget the ones kept", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        box.deleteLater()
        return "keep" if clicked is keep else "forget" if forget and clicked is forget else ""

    def _board_spoilers(self, on: bool) -> None:
        """Show spoilers on the board: the setting the Plot page's Show plot
        sets, so every page shows the same people."""
        self._on_spoilers_toggled(on)
        self._refresh_board()

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
        requests, waiting = ask_fill_gaps(
            self._dialog_parent(),
            session,
            card,
            gaps,
            catalog=self.image_catalog,
            browse=self._browse_pictures,
        )
        self._drain_side_costs()
        self._board_waiting += [(session.story.id, item) for item in waiting]
        if requests:
            self._start_card_pictures(requests)
        if requests or waiting:
            self._refresh_board()

    def _start_waiting(self) -> None:
        """Gaps waiting for a new look's first picture: drawn from it once it
        is in, with the prompt approved; let go, and said, when it failed.
        The open story's, and the author's own characters'
        (gui/window_collection.py)."""
        if not self._board_waiting:
            return
        sessions = {}
        if self.session is not None:
            sessions[self.session.story.id] = self.session
        if self._collection is not None:
            sessions[self._collection.story.id] = self._collection
        keep: list[tuple[str, Waiting]] = []
        started: dict[str, list] = {}
        let_go = []
        listing = self.image_catalog.models
        for story_id, waiting in self._board_waiting:
            session = sessions.get(story_id)
            card = session.card(waiting.card_id) if session is not None else None
            if card is None or (session is self.session and self._busy):
                # While the story's job runs its pictures aren't taken onto
                # its cards yet: a row waiting there waits on.
                keep.append((story_id, waiting))
                continue
            gap = sheet_for(card).gap(waiting.look, waiting.expression)
            if gap is None:
                continue  # filled meanwhile
            if gap.source is not None:
                info = next((m for m in listing if m.id == waiting.model), None)
                started.setdefault(story_id, []).append(
                    session.card_request(
                        card,
                        waiting.prompt,
                        model=waiting.model,
                        info=info,
                        first=False,
                        caption="",
                        source=gap.source,
                        look=waiting.look,
                        expression=waiting.expression,
                    )
                )
            elif self._look_being_drawn(card.id, waiting.look, story_id):
                keep.append((story_id, waiting))
            else:
                let_go.append(waiting)
        self._board_waiting = keep
        for story_id, requests in started.items():
            session = sessions[story_id]
            if not self._card_pictures_refused(session):
                self._start_card_pictures(requests, session)
        if let_go:
            looks = sorted({w.look for w in let_go})
            self.statusBar().showMessage(
                f"The first picture of {', '.join(looks)} didn't come, so the rest of its row "
                "wasn't drawn: fill it again from its sheet.",
                12000,
            )
        self._refresh_board()
        self._refresh_collection()

    def _look_being_drawn(self, card_id: str, look: str, story_id: str | None = None) -> bool:
        story_id = story_id or self.session.story.id
        return any(
            job.request.card_id == card_id and label_key(job.request.card_look) == label_key(look)
            for job in self.image_jobs.pending(story_id)
        )

    def _board_asked(self, card_id: str, words: str) -> None:
        """Draw a look as the story asked for it ("Draw them as …"), from the
        picture they are shown with now."""
        card = self._board_card(card_id)
        if card is None or not card.reference_images:
            return
        shown = self.session.looks_now().get(card_id) or card.reference_images[0].id
        self._on_card_picture(card_id, shown, "edit", wanted=words)

    def _board_files(
        self, card_id: str, look: str, expression: str, *, paths: list | None = None
    ) -> None:
        """Pictures from files onto their card, labelled for the cell they
        were asked for or dropped on (none: unlabelled); each offered for
        cropping first, as every picture from disk is."""
        card = self._board_card(card_id)
        store = self.session.pictures if self.session is not None else None
        if card is None or store is None or self._board_refused():
            return
        from sealedlore.gui.crop_dialog import cropped_picture

        if paths is None:
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
