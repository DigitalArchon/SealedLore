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

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from sealedlore.gui.image_dialog import ImageDialog, ImageViewer, save_picture_as
from sealedlore.gui.image_jobs import ImageCatalog, ImageJobs
from sealedlore.gui.images_panel import ImagesPanel
from sealedlore.gui.ref_images import PICTURE_FILTER, picture_bytes, picture_bytes_of
from sealedlore.gui.transcript import PendingPicture
from sealedlore.gui.winprivacy import exclude_from_capture
from sealedlore.ids import new_id
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.node import Usage
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.images import IMAGES_DIR, REFS_DIR
from sealedlore.storage.picture_store import PictureStore, file_name
from sealedlore.storage.repository import save_config


class ImagesWindow:
    def _init_images(self) -> None:
        """Called from `_build_ui`: the jobs, the Images tab and the transcript's signals."""
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
        self.transcript.illustrate_requested.connect(
            lambda node_id: self.generate_image(anchor_id=node_id)
        )
        self.transcript.image_action_requested.connect(self._image_action)
        self.transcript.stop_image_requested.connect(self.image_jobs.stop)
        self.composer.image_requested.connect(lambda: self.generate_image())

    def _image_actions(self, story_menu) -> None:
        """Called from `_build_actions`: the Story menu's picture entries."""
        story_menu.addSeparator()
        self.image_action = QAction("&Generate image…", self)
        self.image_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
        self.image_action.setStatusTip(
            "A picture of what is happening; you approve the prompt before anything is sent"
        )
        self.image_action.triggered.connect(lambda: self.generate_image())
        story_menu.addAction(self.image_action)

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
        self.background_action.setEnabled(has_story and not busy)
        self.clear_background_action.setEnabled(
            has_story and not busy and bool(self.session and self.session.story.background_image)
        )
        self.images_panel.generate_button.setEnabled(has_story and not busy)

    # --- the open story's pictures --------------------------------------------

    def _story_pictures(self) -> PictureStore | None:
        """Where the open story keeps its pictures: its folder, or memory."""
        return self.session.pictures if self.session is not None else None

    def _load_images(self) -> None:
        self._images = self.session.generated_images() if self.session is not None else []

    def _pictures_for(self, anchor: str | None) -> list:
        if self.session is None:
            return []
        story_id = self.session.story.id
        store = self.session.pictures
        items: list = [
            (image, store.read(image.file))
            for image in self._images
            if image.anchor_node_id == anchor
        ]
        for job in self.image_jobs.pending(story_id):
            if job.request.anchor_node_id == anchor:
                model = job.request.model.rsplit("/", 1)[-1]
                items.append(
                    PendingPicture(
                        job.id,
                        f"Drawing a picture with {model} ({job.request.size})… "
                        "Play on; it appears here when it's done.",
                    )
                )
        return items + self._videos_for(anchor)

    def refresh_images(self) -> None:
        self._load_images()
        self._load_videos()
        if self.session is None:
            self.images_panel.set_images([], None, set())
            return
        on_path = {node.id for node in self.session.full_path()}
        self.images_panel.set_images(
            self._images, self.session.pictures, on_path, videos=self._videos
        )

    def _refresh_image_views(self) -> None:
        """The Images tab now; the transcript once no passage is streaming into it."""
        self.refresh_images()
        if not self._busy:
            self.reload_transcript()

    # --- generating -------------------------------------------------------------

    def generate_image(
        self, *, anchor_id: str | None = None, prefill: GeneratedImage | None = None
    ) -> None:
        if self.session is None:
            return
        if self._busy:
            self.statusBar().showMessage(
                "A picture can be asked for once the story's current job is done.", 6000
            )
            return
        if self.session.config.images() is None:
            self._warn_plain("No endpoint", "Set up an endpoint in Settings first.")
            return
        if anchor_id is not None and not any(n.id == anchor_id for n in self.session.nodes):
            anchor_id = None
        dialog = ImageDialog(
            self.session,
            anchor_id=anchor_id,
            prefill=prefill,
            catalog=self.image_catalog,
            browse=self._browse_models,
            parent=self,
        )
        if self._shows_private():
            exclude_from_capture(dialog, True)  # the private model's prompt
        accepted = dialog.exec()
        # The writer picked in the dialog sticks, as Settings → Models →
        # Image prompt writer (blank: the story model).
        # (Not in private or a TEE chat: their own model writes, and the
        # setting stays as the author left it for stories. Nor from a chat
        # kept in memory only, which teaches the settings nothing.)
        private = self.session.in_private or self.session.tee_chat or self.session.memory_only
        if not private and dialog.writer_model() != self.config.image_prompt_model:
            self.config.image_prompt_model = dialog.writer_model()
            save_config(self.config, root=self.root)
        # The prompt writer's call, paid whether or not a picture follows.
        for usage in self.session.unreported_usage:
            self.status_strip.add_cost(usage)
        self.session.unreported_usage.clear()
        self._refresh_story_cost()
        if not accepted or dialog.request is None:
            return
        client = self.session.image_client()
        if client is None:
            return
        self.image_jobs.start(dialog.request, client, self.session.pictures)
        self.statusBar().showMessage(
            "Drawing the picture in the background. It appears after its passage when done.",
            8000,
        )
        self._refresh_image_views()

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
        else:
            self.statusBar().showMessage(
                "A picture for another story is ready; it's in that story's Images tab.", 8000
            )

    def _on_image_failed(self, _job_id: str, story_id: str, message: str) -> None:
        if self.session is not None and self.session.story.id == story_id:
            self._refresh_story_cost()
            self._refresh_image_views()
        if not message:
            self.statusBar().showMessage("Stopped waiting for the picture.", 8000)
            return
        self.statusBar().showMessage(f"The picture failed: {message}", 20000)

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
            ImageViewer(image, data, self).exec()
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
        elif action == "reference":
            self._use_as_reference(data)
        elif action == "background" and data is not None:
            self._set_background_data(data, picture_kind(data) or ".png")
        elif action == "again":
            self.generate_image(anchor_id=image.anchor_node_id, prefill=image)
        elif action == "delete":
            answer = QMessageBox.question(
                self, "Delete picture", "Delete this picture? Its file is removed."
            )
            if answer == QMessageBox.Yes:
                store.remove_image(image.id)
                self._refresh_image_views()

    def save_all_pictures(self) -> None:
        """Images → Save all…: every picture of the story into a folder the
        author picks, named in the order they were made. Nothing there is
        overwritten. The way out for a memory-only chat's pictures, and a
        convenience for any story."""
        if self.session is None:
            return
        images = self.session.pictures.images()
        if not images:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Save all pictures", str(Path.home()))
        if not chosen:
            return
        folder = Path(chosen)
        store = self.session.pictures
        written, missing = 0, 0
        try:
            for number, image in enumerate(images, start=1):
                data = store.read(image.file)
                if data is None:
                    missing += 1
                    continue
                name = file_name(image.file)
                target = folder / f"{number:03d}-{name}"
                extra = 2
                while target.exists():
                    target = folder / f"{number:03d}-{extra}-{name}"
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

    def _use_as_reference(self, data: bytes | None) -> None:
        if self.session is None or data is None:
            return
        if self._busy:
            self.statusBar().showMessage("Wait for the story's current job to finish.", 6000)
            return
        owners: list[tuple[str, object]] = [
            (f"Character: {character.name}", character)
            for character in [*self.session.cast, *self.session.supporting]
        ] + [(f"Lore: {entry.title}", entry) for entry in self.session.bundle.lore]
        if not owners:
            return
        labels = [label for label, _ in owners]
        choice, ok = QInputDialog.getItem(
            self, "Use as a reference picture", "A picture of:", labels, 0, False
        )
        if not ok:
            return
        owner = owners[labels.index(choice)][1]
        caption, ok = QInputDialog.getText(self, "Caption", "What the picture shows (optional):")
        if not ok:
            return
        # Prepared as any added picture is: a large one is scaled down.
        ref_id = new_id()
        try:
            prepared, suffix = picture_bytes_of(data, picture_kind(data) or ".png")
            relative = f"{REFS_DIR}/{ref_id}{suffix}"
            self.session.pictures.write(relative, prepared)
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't add the picture", str(exc))
            return
        ref = ImageRef(id=ref_id, file=relative, caption=caption.strip())
        owner.reference_images.append(ref)
        self.session.save()
        self.refresh_panels()
        self.statusBar().showMessage(f"Added to {choice.split(': ', 1)[1]}'s pictures.", 6000)

    # --- the background ----------------------------------------------------------

    def _apply_background(self) -> None:
        story = self.session.story if self.session is not None else None
        if story is None or not story.background_image:
            self.transcript.set_background(None)
            return
        data = self.session.pictures.read(story.background_image)
        # A missing file leaves no background, quietly: nothing to warn about.
        if data is None or not self.transcript.set_background(data):
            self.transcript.set_background(None)

    def _set_background_from(self, source: Path) -> None:
        """A picture file as the background: copied into the story, cleaned."""
        if self.session is None:
            return
        try:
            data, suffix = picture_bytes(source)
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't use that picture", str(exc))
            return
        self._set_background_data(data, suffix)

    def _set_background_data(self, data: bytes, suffix: str) -> None:
        if self.session is None:
            return
        if self._busy:
            self.statusBar().showMessage("Wait for the story's current job to finish.", 6000)
            return
        store = self.session.pictures
        relative = f"{IMAGES_DIR}/background{suffix}"
        story = self.session.story
        old = story.background_image
        try:
            store.write(relative, data)
        except (OSError, ValueError) as exc:
            self._warn_plain("Couldn't use that picture", str(exc))
            return
        if old and old != relative:
            store.remove(old)
        self.session.story.background_image = relative
        self.session.save()
        self._apply_background()
        self._update_controls()

    def choose_background(self) -> None:
        if self.session is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Background picture", str(Path.home()), PICTURE_FILTER
        )
        if path:
            self._set_background_from(Path(path))

    def clear_background(self) -> None:
        if self.session is None or self._busy:
            return
        story = self.session.story
        if story.background_image:
            self.session.pictures.remove(story.background_image)
        story.background_image = None
        self.session.save()
        self._apply_background()
        self._update_controls()

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
