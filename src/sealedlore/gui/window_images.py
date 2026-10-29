"""The main window's side of pictures. A mixin for `MainWindow`.

Three rules:
- Nothing is sent to an image model without the author seeing and approving
  the whole request in `ImageDialog`; what they approve is what is sent.
- A picture's job never touches the session. Its record reaches the story's
  picture store (images.json, or a memory-only chat's memory) here, on the
  GUI thread, never through `session.save()`.
- Pictures outlive their passages: only the author deletes one.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QMessageBox,
    QToolButton,
    QVBoxLayout,
)

from sealedlore.engine.picture_shapes import BACKGROUND
from sealedlore.engine.picture_use import KEEP_DRAW
from sealedlore.engine.places import newest_picture
from sealedlore.gui.crop_dialog import cropped_picture
from sealedlore.gui.image_dialog import (
    FitPicture,
    ImageDialog,
    ImageViewer,
    named_for,
    save_picture_as,
)
from sealedlore.gui.image_jobs import FailedPicture, ImageCatalog, ImageJobs
from sealedlore.gui.image_picker import pick_image_model
from sealedlore.gui.images_panel import ImagesPanel
from sealedlore.gui.picture_use import CHARACTER, PLACE, UsePictureDialog
from sealedlore.gui.picture_variant import PictureVariantDialog, VariantSubject
from sealedlore.gui.portraits_dialog import PortraitsDialog
from sealedlore.gui.ref_images import PICTURE_FILTER, pixmap_of
from sealedlore.gui.transcript import FailedPictureItem, PendingPicture
from sealedlore.gui.winprivacy import exclude_from_capture
from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Usage
from sealedlore.providers.images import IMAGE_TIMEOUT_SECONDS
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.picture_store import PictureStore, file_name
from sealedlore.storage.repository import save_config


class ImagesWindow:
    def _init_images(self) -> None:
        """Called from `_build_ui`: the jobs, the Media tab and the transcript's signals."""
        self.image_jobs = ImageJobs(self.root, self)
        self.image_catalog = ImageCatalog(
            self.config, self.root, self, in_memory=lambda: self.memory_chat
        )
        self.image_jobs.finished.connect(self._on_image_finished)
        self.image_jobs.failed.connect(self._on_image_failed)
        self.image_jobs.changed.connect(self._on_image_jobs_changed)
        self._images: list[GeneratedImage] = []
        self.images_panel = ImagesPanel()
        self.images_panel.action_requested.connect(self._image_action)
        self.images_panel.generate_requested.connect(lambda: self.generate_image())
        self.images_panel.save_all_requested.connect(self.save_all_pictures)
        self.images_panel.card_action_requested.connect(self._card_picture_action)
        self.images_panel.background_action_requested.connect(self._background_action)
        self.images_panel.added_action_requested.connect(self._added_picture_action)
        self.transcript.illustrate_requested.connect(
            lambda node_id: self.generate_image(anchor_id=node_id)
        )
        self.transcript.image_action_requested.connect(self._image_action)
        self.transcript.stop_image_requested.connect(self.image_jobs.stop)
        self.transcript.failed_picture_action.connect(self._failed_picture_action)
        self.composer.image_requested.connect(lambda: self.generate_image())

    def _image_actions(self, story_menu) -> None:
        """Called from `_build_actions`: the Story menu's picture entries."""
        story_menu.addSeparator()
        # The window says "picture" everywhere else.
        self.image_action = QAction("Draw a &picture…", self)
        self.image_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
        self.image_action.setStatusTip(
            "A picture of what is happening; you approve the prompt before anything is sent"
        )
        self.image_action.triggered.connect(lambda: self.generate_image())
        story_menu.addAction(self.image_action)
        self.portraits_action = QAction("Draw pictures for the &cards…", self)
        self.portraits_action.setStatusTip(
            "A portrait of each character and a picture of each lore entry with none, from "
            "their cards; you see every prompt first"
        )
        self.portraits_action.triggered.connect(lambda: self.draw_card_pictures())
        story_menu.addAction(self.portraits_action)
        # The place the scene is at, as a picture of the place itself (the
        # author, Oct 24 2026); also a button above the story.
        self.place_action = QAction("Picture of this p&lace…", self)
        self.place_action.setStatusTip(
            "A picture of the place the story is at, with nobody in it, kept on its lore "
            "entry and shown behind the story while it is there"
        )
        self.place_action.triggered.connect(self.draw_place_picture)
        story_menu.addAction(self.place_action)

    def _build_place_button(self) -> None:
        """Called from `__init__` before the centre is laid out: the button
        above the story for a picture of the place the scene is at (the
        author: "a simple button … not disruptive, just present for quick
        use"). Shown while a story has a location; its words say whether
        the place has a picture already."""
        self.place_button = QToolButton()
        self.place_button.setObjectName("viewToggleButton")
        self.place_button.setText("Picture of this place…")
        self.place_button.clicked.connect(self.draw_place_picture)
        self.place_button.hide()
        # The place picture behind the story now (its ref id, "" for none).
        self._place_shown: str | None = None

    def _place_wanted(self) -> bool:
        session = self.session
        return (
            session is not None
            and not session.story.chat
            and not session.in_private
            and bool((session.story.scene.location or "").strip())
        )

    def _sync_place_button(self) -> None:
        """The button and the Story menu's entry: shown for a story at a
        place, worded for whether the place has a picture."""
        wanted = self._place_wanted()
        self.place_button.setVisible(wanted)
        self.place_action.setVisible(wanted)
        if not wanted:
            return
        session = self.session
        entry = session.current_place()
        has = newest_picture(entry) is not None
        name = entry.title if entry is not None else (session.story.scene.location or "")
        self.place_button.setText("New picture of this place…" if has else "Picture of this place…")
        tip = (
            f"{name}: a picture of the place itself, with nobody in it, kept on its lore "
            "entry and shown behind the story while the story is there."
        )
        if has:
            tip += " A new one is drawn from the last, as the place is now."
        self.place_button.setToolTip(tip)
        can = not self._busy and session.config.images() is not None
        self.place_button.setEnabled(can)
        self.place_action.setEnabled(can)

    def _sync_place_picture(self) -> None:
        """After the scene or a card changed: the button's words, and the
        picture behind the story when the place's picture is another."""
        self._sync_place_button()
        found = self.session.place_picture() if self._place_wanted() else None
        shown = found[1].id if found is not None else ""
        if self.session is not None and shown != self._place_shown:
            self._apply_background()

    def draw_place_picture(self) -> bool:
        """A picture of the place the scene is at (the button above the
        story, Story → Picture of this place…)."""
        if not self._place_wanted():
            return False
        return self.generate_image(place=True)

    def _background_actions(self, view_menu) -> None:
        """Called from `_build_actions`: the View menu's background entries (a
        way of looking at the story, so they are View's, not Story's)."""
        view_menu.addSeparator()
        self.background_action = QAction("&Background picture…", self)
        self.background_action.setStatusTip("Show a picture behind the story's text")
        self.background_action.triggered.connect(self.choose_background)
        view_menu.addAction(self.background_action)
        self.clear_background_action = QAction("Clear bac&kground", self)
        self.clear_background_action.triggered.connect(self.clear_background)
        view_menu.addAction(self.clear_background_action)

    def _update_image_controls(self) -> None:
        has_story = self.session is not None
        busy = self._busy
        self.image_action.setEnabled(has_story and not busy)
        self.portraits_action.setEnabled(
            has_story
            and not busy
            and not self.session.story.chat
            and not self.session.in_private
            and any(self.session.picture_cards())
        )
        self.background_action.setEnabled(has_story and not busy)
        self.clear_background_action.setEnabled(
            has_story and not busy and bool(self.session and self.session.story.background_image)
        )
        self.images_panel.generate_button.setEnabled(has_story and not busy)
        self._sync_place_button()

    # --- the open story's pictures --------------------------------------------

    def _story_pictures(self) -> PictureStore | None:
        """Where the open story keeps its pictures: its folder, or memory."""
        return self.session.pictures if self.session is not None else None

    def _load_images(self) -> None:
        self._images = self.session.generated_images() if self.session is not None else []

    def _pictures_for(self, anchor: str | None) -> list:
        """The pictures and videos the story shows after a passage (`anchor`),
        or before the first (None). A picture drawn for a card or to be the
        background, with no passage, belongs to none: it goes on its card or
        behind the story, and is in Media. They stacked above the opening,
        every portrait of the Start dialog with them. (In the scene… has its
        passage, and shows after it.)"""
        if self.session is None:
            return []
        story_id = self.session.story.id
        store = self.session.pictures
        items: list = [
            (image, store.read(image.file))
            for image in self._images
            if image.anchor_node_id == anchor and not _passage_less(image)
        ]
        for job in self.image_jobs.pending(story_id):
            if job.request.anchor_node_id == anchor and not _passage_less(job.request):
                model = job.request.model.rsplit("/", 1)[-1]
                items.append(
                    PendingPicture(
                        job.id,
                        f"Drawing a picture with {model} ({job.request.size})… "
                        "Play on; it appears here when it's done.",
                    )
                )
        for failure in self.image_jobs.failed_for(story_id):
            if failure.request.anchor_node_id == anchor and not _passage_less(failure.request):
                items.append(_failed_item(failure))
        return items + self._videos_for(anchor)

    def refresh_images(self) -> None:
        self._load_images()
        self._load_videos()
        if self.session is None:
            self.images_panel.set_images([], None, set())
            return
        session = self.session
        on_path = {node.id for node in session.full_path()}
        # The cards as the Cast and Lore pages show them: a plot-hidden one
        # only with spoilers on.
        cast, lore = self._media_cards()
        self.images_panel.set_images(
            self._images,
            session.pictures,
            on_path,
            videos=self._videos,
            cast=cast,
            lore=lore,
            background=session.story.background_image,
            background_drawn_as=session.story.background_drawn_as,
            drawn_for_cards=self._drawn_for_cards(),
            added=list(session.story.reference_images),
            chat=session.story.chat,
        )

    def _media_cards(self) -> tuple[list, list]:
        """The cards as the Cast and Lore pages show them: a plot-hidden one
        only with spoilers on."""
        session = self.session
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        return (
            [c for c in (*session.cast, *session.supporting) if c.id not in hidden],
            [e for e in session.bundle.lore if e.id not in hidden],
        )

    def _drawn_for_cards(self) -> set[str]:
        """Every picture drawn as a card's, on any card, shown or not, set
        aside or not: in Media and Save all under its card, or not at all."""
        session = self.session
        cards = [
            *session.cast,
            *session.supporting,
            *session.story.setup.set_aside,
            *session.bundle.lore,
        ]
        return {ref.drawn_as for card in cards for ref in card.reference_images if ref.drawn_as}

    def _refresh_image_views(self) -> None:
        """The Media tab now; the transcript once no passage is streaming into
        it, and no message is being edited."""
        self.refresh_images()
        if not self._busy:
            self._reload_unless_editing()

    # --- generating -------------------------------------------------------------

    def generate_image(
        self,
        *,
        anchor_id: str | None = None,
        prefill: GeneratedImage | None = None,
        card=None,
        background: bool = False,
        place: bool = False,
        place_entry: LoreEntry | None = None,
        source: ImageRef | None = None,
    ) -> bool:
        """`card`: a picture of that character alone, in the scene now, added
        to their card (In the scene… on the Cast page). `background`: a
        picture to go behind the story's text (Media → the background).
        `place`: a picture of the place the scene is at, for its lore entry
        (engine/places.py), or of `place_entry`. `source`: a picture kept on
        the card or entry to draw from (gui/picture_use.py): a portrait of
        the person it shows, or the place it shows with nobody in it. True
        when a picture is being drawn."""
        if self.session is None:
            return False
        if self._busy:
            self.statusBar().showMessage(
                "A picture can be asked for once the story's current job is done.", 6000
            )
            return False
        if self.session.config.images() is None:
            self._warn_plain("No endpoint", "Set up an endpoint in Settings first.")
            return False
        if anchor_id is not None and not any(n.id == anchor_id for n in self.session.nodes):
            anchor_id = None
        dialog = ImageDialog(
            self.session,
            anchor_id=anchor_id,
            prefill=prefill,
            catalog=self.image_catalog,
            browse=self._browse_models,
            parent=self._dialog_parent(),
            card=card,
            background=background,
            place_mode=place,
            place=(place_entry or self.session.current_place()) if place else None,
            location=(
                (self.session.story.scene.location or "")
                if place and place_entry is None
                else place_entry.title
                if place_entry is not None
                else ""
            ),
            source=source,
        )
        if self._shows_private():
            exclude_from_capture(dialog, True)  # the private model's prompt
        accepted = dialog.exec()
        writer = dialog.writer_model()
        request = dialog.request if accepted else None
        if getattr(dialog, "made_place", False):
            # A lore entry made for the place: the Lore page lists it.
            self.refresh_panels()
            self._sync_place_button()
        # Freed once read: a child of the window, it held the story it was
        # opened in for as long as the window lived.
        dialog.deleteLater()
        # The writer picked in the dialog sticks, as Settings → Models →
        # Image prompt writer (blank: the story model).
        # (Not in private or a TEE chat: their own model writes, and the
        # setting stays as the author left it for stories. Nor from a chat
        # kept in memory only, which teaches the settings nothing.)
        private = self.session.in_private or self.session.tee_chat or self.session.memory_only
        if not private and writer != self.config.image_prompt_model:
            self.config.image_prompt_model = writer
            save_config(self.config, root=self.root)
        # The prompt writer's call, paid whether or not a picture follows.
        for usage in self.session.unreported_usage:
            self.status_strip.add_cost(usage)
        self.session.unreported_usage.clear()
        self._refresh_story_cost()
        if not accepted or request is None:
            return False
        client = self.session.image_client()
        if client is None:
            return False
        self.image_jobs.start(request, client, self.session.pictures)
        self.statusBar().showMessage(
            "Drawing the picture in the background. It appears after its passage when done.",
            8000,
        )
        self._refresh_image_views()
        return True

    # --- pictures for the cards ------------------------------------------------

    def _card_pictures_refused(self, session) -> bool:
        """Why pictures for the cards can't be asked for now, said; or False."""
        if session is None or session.story.chat:
            return True
        if self._busy:
            self.statusBar().showMessage(
                "Pictures can be drawn once the story's current job is done.", 6000
            )
            return True
        if session.in_private:
            self.statusBar().showMessage("Pictures wait until the private scene ends.", 6000)
            return True
        if session.config.images() is None:
            self._warn_plain("No endpoint", "Set up an image endpoint in Settings first.")
            return True
        return False

    def _browse_pictures(self, current: str) -> str | None:
        return pick_image_model(self.image_catalog, current, self)

    def _drain_side_costs(self) -> None:
        """A prompt writer's call, paid whether or not anything is drawn."""
        session = self.session
        for usage in session.unreported_usage:
            self.status_strip.add_cost(usage)
        session.unreported_usage.clear()
        self._refresh_story_cost()

    def _start_card_pictures(self, requests: list) -> None:
        session = self.session
        for request in requests:
            client = session.image_client()
            if client is None:
                return
            self.image_jobs.start(request, client, session.pictures)
        count = len(requests)
        self.statusBar().showMessage(
            f"Drawing {count} picture{'' if count == 1 else 's'} in the background: each "
            "goes on its card when done.",
            8000,
        )
        self._refresh_image_views()

    def draw_card_pictures(self, characters: list | None = None, lore: list | None = None) -> bool:
        """Offer a picture of each card (`characters` and `lore`; by default
        those with none, else every card the models may see): every prompt
        is shown before anything is drawn. True when drawing began."""
        session = self.session
        if self._card_pictures_refused(session):
            return False
        if characters is None and lore is None:
            characters, lore = session.without_pictures()
            if not characters and not lore:
                characters, lore = session.picture_cards()
        characters, lore = list(characters or []), list(lore or [])
        if not characters and not lore:
            return False
        dialog = PortraitsDialog(
            session,
            characters,
            lore=lore,
            catalog=self.image_catalog,
            browse=self._browse_pictures,
            parent=self._dialog_parent(),
        )
        accepted = dialog.exec()
        chosen = dialog.chosen_model() if accepted else ""
        requests = dialog.requests() if accepted else []
        dialog.deleteLater()  # read; held the story otherwise
        self._drain_side_costs()
        if not accepted:
            return False
        if chosen != self.config.portrait_model:
            self.config.portrait_model = chosen
            save_config(self.config, root=self.root)
        self._start_card_pictures(requests)
        return bool(requests)

    def _on_card_picture(self, card_id: str, ref_id: str, what: str) -> None:
        """A picture asked for on a card (the Cast or Lore page): "new",
        "again" or "edit" of picture `ref_id`, or "scene"."""
        session = self.session
        if self._card_pictures_refused(session):
            return
        card = session.card(card_id)
        characters, lore = session.picture_cards()
        if card is None:
            return
        if not any(c.id == card_id for c in (*characters, *lore)):
            # Said, not silently nothing: the first picture would be the
            # first a model heard of it.
            name = card.name if isinstance(card, Character) else card.title
            self.statusBar().showMessage(
                f"No picture is drawn of {name} yet: the models can't see it (the plot hasn't "
                "brought it in, it waits to be mentioned, or it is turned off).",
                10000,
            )
            return
        if what == "new":
            person = any(c.id == card_id for c in characters)
            self.draw_card_pictures([card] if person else [], [] if person else [card])
            return
        if what == "scene":
            self.generate_image(card=card)
            return
        source = next((r for r in card.reference_images if r.id == ref_id), None)
        if source is None:
            return
        dialog = PictureVariantDialog(
            session,
            card,
            source,
            edit=what == "edit",
            catalog=self.image_catalog,
            browse=self._browse_pictures,
            parent=self._dialog_parent(),
        )
        accepted = dialog.exec()
        request = dialog.request() if accepted else None
        dialog.deleteLater()  # read; held the story otherwise
        self._drain_side_costs()
        if request is not None:
            self._start_card_pictures([request])

    def _card_picture_action(self, action: str, card_id: str, ref_id: str) -> None:
        """A card's picture in the Media tab: see it whole, go to its card,
        save it, or draw it again or edit it, as on its card."""
        session = self.session
        card = session.card(card_id) if session is not None else None
        ref = next((r for r in card.reference_images if r.id == ref_id), None) if card else None
        if ref is None:
            return
        if action in ("again", "edit"):
            self._on_card_picture(card_id, ref_id, action)
            return
        if action in (CHARACTER, PLACE):
            self._use_picture(session.pictures.read(ref.file), action)
            return
        name = card.name if isinstance(card, Character) else card.title
        if action == "show":
            if isinstance(card, Character):
                self.right_tabs.setCurrentWidget(self.cast_page)
                self.cast_panel.select_character(card.id)
            else:
                self.right_tabs.setCurrentWidget(self.lore_panel)
                self.lore_panel.select_entry(card.id)
            return
        data = session.pictures.read(ref.file)
        if action == "save":
            save_picture_as(self, data, file_name(ref.file))
            return
        self._show_whole(name + (f" · {ref.caption}" if ref.caption else ""), data)

    def _take_card_pictures(self) -> None:
        """Pictures drawn for the open story's cards go on them, and one drawn
        to be the background goes behind the story."""
        if self.session is None or self._busy:
            return  # the worker owns the story; taken when it is done
        names = self.session.take_card_pictures()
        if names:
            # The pictures only: the author may be typing on a card.
            self.cast_panel.refresh_pictures()
            self.lore_panel.refresh_pictures()
            self.people_page.forget_pictures()
            self._refresh_people()
            self.refresh_images()
            self.statusBar().showMessage(f"Picture ready: {', '.join(names)}.", 8000)
            # A place's picture landed: behind the story, if the story is there.
            self._sync_place_picture()
        try:
            changed = self.session.take_background_pictures()
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't use the new background", str(exc))
            return
        if changed:
            self._apply_background()
            self.refresh_images()
            self._update_controls()
            self.statusBar().showMessage("The new background is in.", 8000)

    def _on_image_jobs_changed(self) -> None:
        count = len(self.image_jobs.pending())
        self.status_strip.set_pictures(count)

    def _on_image_finished(
        self, _job_id: str, story_id: str, records: list, store: PictureStore
    ) -> None:
        try:
            store.add_images(records)
        except OSError as exc:
            self._warn_plain("Couldn't keep the picture", str(exc))
            return
        open_story = self.session is not None and self.session.story.id == story_id
        if open_story:
            # Pictures are spend in this session too, not only in the story total.
            for record in records:
                if record.cost is not None:
                    self.status_strip.add_cost(
                        Usage(
                            cost=record.cost,
                            cost_reported=record.cost_reported,
                            cost_estimated=not record.cost_reported,
                        )
                    )
            self._refresh_story_cost()
            self._refresh_image_views()
            self.statusBar().showMessage("Picture ready.", 8000)
            if any(record.card_id or record.background for record in records):
                self._take_card_pictures()
        else:
            self.statusBar().showMessage(
                "A picture for another story is ready; it's in that story's Media tab.", 8000
            )

    def _on_image_failed(self, job_id: str, story_id: str, message: str) -> None:
        failure = self.image_jobs.failures.get(job_id)
        in_story = failure is not None and not _passage_less(failure.request)
        if failure is not None and not in_story:
            # A card's picture or a background has no place in the story to
            # be marked in: the notice is all there is.
            self.image_jobs.dismiss(job_id)
        if self.session is not None and self.session.story.id == story_id:
            self._refresh_story_cost()
            self._refresh_image_views()
        if not message:
            self.statusBar().showMessage("Stopped waiting for the picture.", 8000)
            return
        if failure is not None and failure.timed_out:
            text = (
                f"No picture came back within {_waited()}. The image service may still "
                "finish it and charge for it."
            )
        else:
            text = f"The picture failed: {message}"
        if in_story:
            text += " It's marked in the story, with Retry, Edit… and Dismiss."
        self.statusBar().showMessage(text, 30000)

    def _failed_picture_action(self, job_id: str, action: str) -> None:
        """Retry, Edit… or Dismiss on a picture that failed, in the story."""
        failure = self.image_jobs.failures.get(job_id)
        session = self.session
        if failure is None or session is None or session.story.id != failure.request.story_id:
            return
        if action == "dismiss":
            self.image_jobs.dismiss(job_id)
            self._refresh_image_views()
        elif action == "retry":
            # The request the author approved, sent again as it stands, on
            # the picture endpoint as Settings has it now.
            client = session.image_client()
            if client is None:
                self._warn_plain("No endpoint", "Set up an image endpoint in Settings first.")
                return
            self.image_jobs.dismiss(job_id)
            self.image_jobs.start(failure.request, client, failure.store)
            self.statusBar().showMessage("Drawing the picture again in the background.", 8000)
            self._refresh_image_views()
        elif action == "edit":
            request = failure.request
            card = None
            if request.card_id is not None:
                card = next(
                    (c for c in (*session.cast, *session.supporting) if c.id == request.card_id),
                    None,
                )
            prefill = GeneratedImage(
                anchor_node_id=request.anchor_node_id,
                file="",
                prompt=request.prompt,
                direction=request.direction,
                model=request.model,
                size=request.size,
                references=list(request.references),
                private_span=request.private_span,
            )
            if self.generate_image(anchor_id=request.anchor_node_id, prefill=prefill, card=card):
                self.image_jobs.dismiss(job_id)
                self._refresh_image_views()

    # --- a picture's menu --------------------------------------------------------

    def _image(self, image_id: str) -> GeneratedImage | None:
        if self.session is None:
            return None
        images = self.session.pictures.images()
        return next((image for image in images if image.id == image_id), None)

    def _image_action(self, action: str, image_id: str) -> None:
        image = self._image(image_id)
        if image is None or self.session is None:
            return
        store = self.session.pictures
        data = store.read(image.file)
        if action == "open":
            viewer = ImageViewer(image, data, self)
            viewer.setAttribute(Qt.WA_DeleteOnClose)  # with its picture, once closed
            viewer.exec()
        elif action == "prompt":
            box = QMessageBox(self)
            box.setWindowTitle("Prompt")
            box.setText(image.prompt)
            refs = "\n".join(
                f"image {i}: {use.owner_name}" + (f" ({use.caption})" if use.caption else "")
                for i, use in enumerate(image.references, start=1)
            )
            box.setInformativeText(
                f"{image.model} · {image.size}" + (f"\n\n{refs}" if refs else "")
            )
            box.exec()
        elif action == "save":
            save_picture_as(self, data, file_name(image.file))
        elif action in (CHARACTER, PLACE):
            self._use_picture(data, action)
        elif action == "background" and data is not None:
            self._set_background_data(data, picture_kind(data) or ".png", drawn_as=image.id)
        elif action == "again":
            self.generate_image(
                anchor_id=image.anchor_node_id, prefill=image, background=image.background
            )
        elif action == "edit":
            self._edit_picture(VariantSubject.of_picture(image))
        elif action == "delete":
            answer = QMessageBox.question(
                self, "Delete picture", "Delete this picture? Its file is removed."
            )
            if answer == QMessageBox.Yes:
                store.remove_image(image.id)
                self._refresh_image_views()

    def save_all_pictures(self) -> None:
        """Media → Save all…: every picture and video of the story into a folder the
        author picks, named in the order they were made. Nothing there is
        overwritten. The way out for a memory-only chat's pictures, and a
        convenience for any story."""
        if self.session is None:
            return
        session = self.session
        # What Media shows: a picture drawn as a card's is saved under its
        # card's name, not twice, and a card the plot still hides isn't
        # (its name was in the file's).
        drawn_for_cards = self._drawn_for_cards()
        images = [i for i in session.pictures.images() if i.id not in drawn_for_cards]
        # Finished videos too: they are as much the story's as its pictures.
        videos = [v for v in session.pictures.videos() if v.status == "done" and v.file]
        # And every card's pictures, named by their card (cast-john-1.jpg).
        cards: list[tuple[str, str]] = []
        shown_cast, shown_lore = self._media_cards()
        for kind, owners in (("cast", shown_cast), ("lore", shown_lore)):
            for owner in owners:
                name = owner.name if kind == "cast" else owner.title
                slug = _slug(name)
                for number, ref in enumerate(owner.reference_images, start=1):
                    suffix = Path(ref.file).suffix
                    cards.append((ref.file, f"{kind}-{slug or 'card'}-{number}{suffix}"))
        # The background, unless it is a copy of a picture saved above.
        background = session.story.background_image
        if background and not any(i.id == session.story.background_drawn_as for i in images):
            cards.append((background, f"background{Path(background).suffix}"))
        for number, ref in enumerate(session.story.reference_images, start=1):
            cards.append((ref.file, f"added-{number}{Path(ref.file).suffix}"))
        if not images and not videos and not cards:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Save all pictures", str(Path.home()))
        if not chosen:
            return
        folder = Path(chosen)
        store = self.session.pictures
        written, missing = 0, 0
        try:
            made = sorted([*images, *videos], key=lambda record: record.created_at)
            for number, image in enumerate(made, start=1):
                data = store.read(image.file)
                if data is None:
                    missing += 1
                    continue
                name = named_for(file_name(image.file), data)
                target = folder / f"{number:03d}-{name}"
                extra = 2
                while target.exists():
                    target = folder / f"{number:03d}-{extra}-{name}"
                    extra += 1
                target.write_bytes(data)
                written += 1
            for source, name in cards:
                data = store.read(source)
                if data is None:
                    missing += 1
                    continue
                name = named_for(name, data)
                target = folder / name
                extra = 2
                while target.exists():
                    target = folder / f"{Path(name).stem}-{extra}{Path(name).suffix}"
                    extra += 1
                target.write_bytes(data)
                written += 1
        except OSError as exc:
            self._warn_plain("Couldn't save the pictures", f"{folder}: {exc.strerror or exc}")
            return
        note = f" ({missing} missing)" if missing else ""
        self.statusBar().showMessage(
            f"Saved {written} picture{'s' if written != 1 else ''} to {folder}{note}", 8000
        )

    def _use_picture(self, data: bytes | None, kind: str | None = None) -> None:
        """Use as a character's picture… / Use as a place's picture… on any
        picture (a story picture's menu, Media, a video's last frame): the
        author frames the part that matters and says what it becomes
        (gui/picture_use.py); it goes on the card, and, asked for, the
        picture dialog opens to draw a portrait or a picture of the place
        from it."""
        session = self.session
        if session is None or data is None or session.story.chat:
            return
        if self._busy:
            self.statusBar().showMessage("Wait for the story's current job to finish.", 6000)
            return
        if session.in_private:
            self.statusBar().showMessage("Pictures wait until the private scene ends.", 6000)
            return
        characters, lore = session.picture_cards()
        if not characters and not lore and not (session.story.scene.location or "").strip():
            self.statusBar().showMessage("No card to keep it on yet.", 6000)
            return
        dialog = UsePictureDialog(session, data, kind, self._dialog_parent())
        try:
            if not dialog.exec() or dialog.result_use is None:
                return
            use = dialog.result_use
        finally:
            dialog.deleteLater()  # with its picture
        if session is not self.session:
            return
        card = use.card
        made = False
        if card is None:
            # A lore entry for the scene's location, as Picture of this place… makes.
            card = session.ensure_place_entry(use.new_title, session.story.scene.location or "")
            made = True
        try:
            ref = session.keep_card_picture(
                card, use.data, use.suffix, caption=use.caption, how=use.how
            )
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't keep the picture", str(exc))
            return
        name = card.name if isinstance(card, Character) else card.title
        if made:
            self.refresh_panels()
        else:
            self.cast_panel.refresh_pictures()
            self.lore_panel.refresh_pictures()
            self.people_page.forget_pictures()
            self._refresh_people()
            self.refresh_images()
        # The place's picture changed: behind the story, if the story is there.
        self._sync_place_picture()
        self.statusBar().showMessage(f"Kept on {name}'s card.", 6000)
        if use.how == KEEP_DRAW:
            if isinstance(card, Character):
                self.generate_image(card=card, source=ref)
            else:
                self.generate_image(place=True, place_entry=card, source=ref)

    # --- the background ----------------------------------------------------------

    def _apply_background(self) -> None:
        """Behind the story's text: the current place's picture while the
        story is there (`Config.location_backgrounds`, engine/places.py),
        else the story's own background, else nothing."""
        story = self.session.story if self.session is not None else None
        # The People page offers a background until there is one.
        self._refresh_people()
        self._place_shown = None
        if story is None:
            self.transcript.set_background(None)
            return
        data = None
        if self.config.location_backgrounds and self._place_wanted():
            found = self.session.place_picture()
            if found is not None:
                data = self.session.pictures.read(found[1].file)
                self._place_shown = found[1].id if data is not None else None
        if data is None:
            self._place_shown = ""
            if story.background_image:
                data = self.session.pictures.read(story.background_image)
        # A missing file leaves no background, quietly: nothing to warn about.
        if data is None or not self.transcript.set_background(data):
            self.transcript.set_background(None)

    def _set_background_from(self, source: Path) -> None:
        """A picture file as the background, cropped as the author chooses
        (first to the story area's own shape): copied into the story, cleaned."""
        if self.session is None:
            return
        area = self.transcript.viewport().size()
        try:
            picture = cropped_picture(
                self, source, BACKGROUND, background=(area.width(), area.height())
            )
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't use that picture", str(exc))
            return
        if picture is not None:
            self._set_background_data(*picture)

    def _set_background_data(self, data: bytes, suffix: str, *, drawn_as: str = "") -> None:
        """`drawn_as`: the picture drawn here it is a copy of (Set as background)."""
        if self.session is None:
            return
        if self._busy:
            self.statusBar().showMessage("Wait for the story's current job to finish.", 6000)
            return
        try:
            self.session.set_background(data, suffix, drawn_as=drawn_as)
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't use that picture", str(exc))
            return
        self._apply_background()
        self._update_controls()
        self.refresh_images()

    def choose_background(self) -> None:
        if self.session is None:
            return
        if self._busy:  # asked before the picture is chosen and cropped, not after
            self.statusBar().showMessage("Wait for the story's current job to finish.", 6000)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Background picture", str(Path.home()), PICTURE_FILTER
        )
        if path:
            self._set_background_from(Path(path))

    def clear_background(self) -> None:
        if self.session is None or self._busy:
            return
        self.session.clear_background()
        self._apply_background()
        self._update_controls()
        self.refresh_images()

    def _background_action(self, action: str) -> None:
        """The background in the Media tab: see it whole, save it, draw a new
        one or edit it (each drawn one goes behind the story when it comes
        back), choose a file instead, or clear it."""
        session = self.session
        if session is None or not session.story.background_image:
            return
        relative = session.story.background_image
        data = session.pictures.read(relative)
        if action == "open":
            self._show_whole("Background", data)
        elif action == "save":
            save_picture_as(self, data, f"background{Path(relative).suffix}")
        elif action == "draw":
            self.generate_image(prefill=session.background_picture(), background=True)
        elif action == "edit":
            self._edit_picture(VariantSubject.of_background(relative, session.background_picture()))
        elif action == "file":
            self.choose_background()
        elif action == PLACE:
            self._use_picture(data, PLACE)
        elif action == "clear":
            self.clear_background()

    def _edit_picture(self, subject: VariantSubject) -> None:
        """Edit… on a picture of the story or on the background: the change,
        the whole request and its price shown before anything is sent."""
        session = self.session
        if session is None:
            return
        if self._busy:
            self.statusBar().showMessage(
                "A picture can be edited once the story's current job is done.", 6000
            )
            return
        if session.in_private:
            self.statusBar().showMessage("Pictures wait until the private scene ends.", 6000)
            return
        if session.config.images() is None:
            self._warn_plain("No endpoint", "Set up an image endpoint in Settings first.")
            return
        shown = session.pictures.read(subject.file)
        dialog = PictureVariantDialog(
            session,
            subject,
            edit=True,
            catalog=self.image_catalog,
            browse=self._browse_pictures,
            parent=self._dialog_parent(),
        )
        try:
            if not dialog.exec():
                return
            request = dialog.request()
        finally:
            dialog.deleteLater()  # a child of the window, holding the story, otherwise
        if session is not self.session:
            return
        if subject.background and session.story.background_image != subject.file:
            shown = None
        if shown is None or session.pictures.read(subject.file) != shown:
            # The background's file is reused: one drawn meanwhile (a new
            # background landing while this was open) would have been sent
            # in place of the one shown and paid for.
            self._warn_plain(
                "The picture changed",
                "The picture changed while the edit was open, so nothing was sent. Open "
                "Edit… again on the picture as it is now.",
            )
            return
        client = session.image_client()
        if client is None:
            return
        self.image_jobs.start(request, client, session.pictures)
        self.statusBar().showMessage(
            "Drawing the edited picture in the background"
            + (": it goes behind the story when done." if subject.background else "."),
            8000,
        )
        self._refresh_image_views()

    def _show_whole(self, title: str, data: bytes | None) -> None:
        if not data:
            return
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        layout.addWidget(FitPicture(pixmap_of(data)))
        dialog.resize(1000, 760)
        dialog.exec()

    def _added_picture_action(self, action: str, ref_id: str) -> None:
        """A picture added from disk to no card, in Media: see it whole or save it."""
        session = self.session
        ref = next((r for r in session.story.reference_images if r.id == ref_id), None)
        if session is None or ref is None:
            return
        data = session.pictures.read(ref.file)
        if action == "save":
            save_picture_as(self, data, file_name(ref.file))
        elif action in (CHARACTER, PLACE):
            self._use_picture(data, action)
        else:
            self._show_whole(ref.caption or "Added picture", data)

    # --- closing -------------------------------------------------------------

    def _images_allow_close(self) -> bool:
        """Ask before closing with pictures still being drawn."""
        pending = self.image_jobs.pending()
        if not pending:
            self.image_catalog.wait()
            return True
        count = len(pending)
        answer = QMessageBox.question(
            self,
            "Pictures still drawing",
            f"{count} picture{'s are' if count != 1 else ' is'} still being drawn. Close "
            "anyway? The image service may still finish and charge for "
            f"{'them' if count != 1 else 'it'}, and the picture won't be kept.",
        )
        if answer != QMessageBox.Yes:
            return False
        self.image_jobs.stop_all()
        self.image_jobs.wait_all()
        self.image_catalog.wait()
        return True


def _slug(name: str) -> str:
    """A name as part of a file name: "John Smith" as john-smith."""
    return "-".join("".join(c if c.isalnum() else " " for c in name).lower().split())


def _waited() -> str:
    return f"{IMAGE_TIMEOUT_SECONDS / 60:.0f} minutes"


def _failed_item(failure: FailedPicture) -> FailedPictureItem:
    request = failure.request
    model = request.model.rsplit("/", 1)[-1]
    if failure.timed_out:
        detail = (
            f"No picture came back within {_waited()}, so SealedLore stopped waiting. "
            "The image service may still finish it and charge for it: look in your "
            "account there before you retry."
        )
    else:
        detail = f"It failed: {failure.message}"
    price = (
        f" (listed at ${request.listed_price:.3f} a picture)"
        if request.listed_price is not None
        else ""
    )
    return FailedPictureItem(
        failure.job_id,
        f"The picture from {model} ({request.size}) didn't come back.",
        detail,
        retry_tip=(
            f"Send the same request again to {model}{price}, on the picture endpoint "
            "Settings → Images has now. It is charged again."
        ),
    )


def _passage_less(picture) -> bool:
    """A picture (or a request for one) drawn for a card or as the background
    with no passage of its own."""
    return picture.anchor_node_id is None and bool(picture.card_id or picture.background)
