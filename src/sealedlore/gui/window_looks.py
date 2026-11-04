"""The window's side of looks (engine/looks.py) beyond the People page: the
stage above the box the author types in and the faces on each passage, both
View switches and off by default. The author (Nov 2 2026): both cost scroll
speed and room, but someone may want to turn their story into a kind of
visual novel. A mixin for `MainWindow`."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QSize
from PySide6.QtGui import QAction, QPixmap

from sealedlore.engine.looks import faces_along
from sealedlore.engine.scene_state import same_person
from sealedlore.gui.ref_images import pixmap_within
from sealedlore.gui.stage import STAGE_HEIGHT
from sealedlore.models.node import Node
from sealedlore.storage.repository import save_config

# The height of a face on a passage.
FACE_HEIGHT = 56
STAGE_LABEL = "Characters above the box you type in"
PASSAGES_LABEL = "Characters on each passage"


class LooksWindow:
    def _init_looks(self) -> None:
        # Pictures as shown, by (story, file, height): read once each (a
        # picture's file never changes; a new picture is a new file).
        self._face_pixmaps: dict[tuple[str, str, int], QPixmap] = {}
        self.centre_stack.currentChanged.connect(lambda _index: self._sync_stage())

    def _looks_actions(self, view_menu) -> None:
        self.stage_action = QAction(STAGE_LABEL, self)
        self.stage_action.setCheckable(True)
        self.stage_action.setChecked(self.config.looks_stage)
        self.stage_action.setStatusTip(
            "Who is in the scene now, large, in the look each is shown with, between the "
            "story and the box you type in"
        )
        self.stage_action.toggled.connect(self.set_looks_stage)
        view_menu.addAction(self.stage_action)
        self.passage_faces_action = QAction(PASSAGES_LABEL, self)
        self.passage_faces_action.setCheckable(True)
        self.passage_faces_action.setChecked(self.config.looks_passages)
        self.passage_faces_action.setStatusTip(
            "Small pictures over each passage: who was in the scene then, as they looked"
        )
        self.passage_faces_action.toggled.connect(self.set_looks_passages)
        view_menu.addAction(self.passage_faces_action)

    def set_looks_stage(self, on: bool) -> None:
        if self.config.looks_stage != on:
            self.config.looks_stage = on
            save_config(self.config, root=self.root)
        self._sync_stage()

    def set_looks_passages(self, on: bool) -> None:
        if self.config.looks_passages == on:
            return
        self.config.looks_passages = on
        save_config(self.config, root=self.root)
        if not self._busy:
            self.reload_transcript()

    # --- pictures ---------------------------------------------------------------------

    def _face_pixmap(self, file: str, height: int) -> QPixmap:
        session = self.session
        key = (session.story.id, file, height)
        pixmap = self._face_pixmaps.get(key)
        if pixmap is None:
            data = session.pictures.read(file) if file else None
            pixmap = pixmap_within(data, QSize(height * 2, height)) if data else QPixmap()
            self._face_pixmaps[key] = pixmap
        return pixmap

    # --- the faces on each passage ---------------------------------------------------

    def _seen_characters(self) -> list:
        """The characters the author may see, as the Cast and People pages."""
        session = self.session
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        return [c for c in (*session.cast, *session.supporting) if c.id not in hidden]

    def _faces_along(self, nodes: Sequence[Node]) -> dict | None:
        """Each passage's faces, (character, picture), or None when the
        switch is off (or a chat, which has no characters): the transcript's
        and the phone's."""
        session = self.session
        if not self.config.looks_passages or session is None or session.story.chat:
            return None
        span = session.open_span
        return faces_along(
            nodes,
            self._seen_characters(),
            start_present=session.story.scene.present_character_ids,
            held_id=session.story.held_character_id,
            open_span=span.id if span is not None else None,
            same_person=same_person,
        )

    def _opens_whole(self, name: str, file: str) -> Callable[[], None]:
        """What a face's click does: the picture whole, read when asked."""

        def open_it() -> None:
            if self.session is not None:
                self._show_whole(name, self.session.pictures.read(file))

        return open_it

    def _passage_faces(
        self, nodes: Sequence[Node]
    ) -> Callable[[str], list[tuple[str, QPixmap]]] | None:
        """What each passage shown carries over it, or None when the switch
        is off."""
        faces = self._faces_along(nodes)
        if faces is None:
            return None

        def faces_for(node_id: str) -> list[tuple]:
            return [
                (
                    character.name,
                    self._face_pixmap(ref.file, FACE_HEIGHT),
                    self._opens_whole(character.name, ref.file),
                )
                for character, ref in faces.get(node_id, [])
            ]

        return faces_for

    # --- the stage --------------------------------------------------------------------

    def _stage_people(self) -> list[tuple[str, str, str]]:
        """Who the stage shows when it is wanted: (name, picture id, file),
        the one played first; none when it is off or there is no story."""
        session = self.session
        if not self.config.looks_stage or session is None or session.story.chat:
            return []
        if not session.nodes:
            return []
        return [(p.name, p.shown, p.picture) for p in self._people_view().here if p.picture]

    def _sync_stage(self) -> None:
        """The stage shown, with who is in the scene now, when it is wanted
        and the story is showing; else put away."""
        shown = self.centre_stack.currentWidget() is self.transcript
        if shown:
            people = [
                (name, self._face_pixmap(file, STAGE_HEIGHT), self._opens_whole(name, file))
                for name, _picture, file in self._stage_people()
            ]
            shown = bool(people)
            if shown:
                self.stage.set_people(people)
        self.stage.setVisible(shown)
