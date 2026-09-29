"""The main window's side of videos. A mixin for `MainWindow`.

Four rules:
- Nothing is sent to a video model without the author seeing its price and
  approving the whole request in `VideoDialog`; what they approve is sent.
- A video is paid for when it is sent, so its record is written the moment
  the endpoint accepts it (`_on_video_accepted`), and a video still being
  made is asked after again whenever its story is opened (`_resume_videos`).
- A video's job never touches the session. Its record reaches the story's
  store (videos.json, or a memory-only chat's memory) here, on the GUI thread.
- Videos outlive their passages: only the author deletes one.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox

from sealedlore.gui.video_dialog import VideoDialog
from sealedlore.gui.video_jobs import VideoCatalog, VideoJobs
from sealedlore.gui.video_view import FrameGrabber, VideoItem, multimedia_available, stop_playback
from sealedlore.gui.winprivacy import exclude_from_capture
from sealedlore.models.config import _same_host
from sealedlore.models.image import RefUse
from sealedlore.models.node import Usage
from sealedlore.models.video import GeneratedVideo
from sealedlore.providers.videos import VideoClient
from sealedlore.storage.images import VIDEOS_DIR, story_path
from sealedlore.storage.picture_store import DiskPictures, PictureStore, file_name
from sealedlore.storage.repository import save_config


class VideosWindow:
    def _init_videos(self) -> None:
        """Called from `_build_ui`, after `_init_images`."""
        self.video_jobs = VideoJobs(self)
        self.video_catalog = VideoCatalog(
            self.config, self.root, self, in_memory=lambda: self.memory_chat
        )
        self.video_jobs.accepted.connect(self._on_video_accepted)
        self.video_jobs.progress.connect(self._on_video_progress)
        self.video_jobs.finished.connect(self._on_video_finished)
        self.video_jobs.failed.connect(self._on_video_failed)
        self.video_jobs.stopped.connect(lambda *_args: self._refresh_image_views())
        self.video_jobs.changed.connect(self._on_video_jobs_changed)
        self.transcript.video_action_requested.connect(self._video_action)
        self.images_panel.video_action_requested.connect(self._video_action)
        self.images_panel.generate_video_requested.connect(lambda: self.generate_video())
        self.transcript.video_requested.connect(
            lambda node_id: self.generate_video(anchor_id=node_id)
        )
        self._videos: list[GeneratedVideo] = []
        # Frames being taken, by video id; and those tried this session.
        self._grabbers: dict[str, FrameGrabber] = {}
        self._grab_tried: set[str] = set()
        self._video_progress: dict[str, str] = {}

    def _video_menu_actions(self, story_menu) -> None:
        """Called from `_build_actions`, after the picture entries."""
        self.video_action = QAction("Generate &video…", self)
        self.video_action.setStatusTip(
            "A few seconds of video of what is happening. Far dearer than a picture: you see "
            "the price and approve everything before it is sent"
        )
        self.video_action.triggered.connect(lambda: self.generate_video())
        story_menu.addAction(self.video_action)

    def _update_video_controls(self) -> None:
        enabled = self.session is not None and not self._busy
        self.video_action.setEnabled(enabled)
        self.images_panel.generate_video_button.setEnabled(enabled)

    # --- what the transcript shows ----------------------------------------------

    def _videos_for(self, anchor: str | None) -> list[VideoItem]:
        if self.session is None:
            return []
        store = self.session.pictures
        running = self.video_jobs.running_ids()
        items: list[VideoItem] = []
        for video in self._videos:
            if video.anchor_node_id != anchor:
                continue
            progress = self._video_progress.get(video.id)
            if progress:
                video = video.model_copy(update={"progress": progress})
            items.append(
                VideoItem(
                    record=video,
                    poster=store.read(video.poster) if video.poster else None,
                    source=lambda v=video: self._video_source(v),
                    running=video.id in running,
                    can_open=isinstance(store, DiskPictures),
                )
            )
            if video.status == "done" and not video.poster:
                self._grab_frames(video)
        return items

    def _load_videos(self) -> None:
        self._videos = self.session.generated_videos() if self.session is not None else []

    def _video_path(self, video: GeneratedVideo) -> Path | None:
        store = self.session.pictures if self.session is not None else None
        if not isinstance(store, DiskPictures) or not video.file:
            return None
        try:
            path = story_path(store.story_id, video.file, store.root)
        except ValueError:
            return None
        return path if path.is_file() else None

    def _video_source(self, video: GeneratedVideo) -> Path | bytes | None:
        """The file to play: its path on disk (streamed), or a memory
        chat's bytes."""
        path = self._video_path(video)
        if path is not None:
            return path
        return self.session.pictures.read(video.file) if self.session and video.file else None

    # --- making one ----------------------------------------------------------------

    def generate_video(
        self,
        *,
        anchor_id: str | None = None,
        prefill: GeneratedVideo | None = None,
        start: RefUse | None = None,
    ) -> None:
        if self.session is None:
            return
        if self._busy:
            self.statusBar().showMessage(
                "A video can be asked for once the story's current job is done.", 6000
            )
            return
        if self.session.config.videos() is None:
            self._no_video_key()
            return
        if anchor_id is not None and not any(n.id == anchor_id for n in self.session.nodes):
            anchor_id = None
        dialog = VideoDialog(
            self.session,
            anchor_id=anchor_id,
            prefill=prefill,
            start=start,
            catalog=self.video_catalog,
            browse=self._browse_models,
            parent=self,
        )
        if self._shows_private():
            exclude_from_capture(dialog, True)
        accepted = dialog.exec()
        private = self.session.in_private or self.session.tee_chat or self.session.memory_only
        # The prompt writer's call, paid whether or not a video follows.
        for usage in self.session.unreported_usage:
            self.status_strip.add_cost(usage)
        self.session.unreported_usage.clear()
        self._refresh_story_cost()
        if not accepted or dialog.request is None:
            return
        if not private:
            # The next video starts from these, as the picture dialog's size does.
            self.config.video_model = dialog.request.model.id
            self.config.video_params = dict(dialog.request.settings)
            if dialog.writer_model() != self.config.image_prompt_model:
                self.config.image_prompt_model = dialog.writer_model()
            save_config(self.config, root=self.root)
        client = self.session.video_client()
        if client is None:
            return
        self.video_jobs.start(dialog.request, client, self.session.pictures)
        self.statusBar().showMessage(
            "Sending the video. It takes a few minutes; play on, it appears after its passage.",
            10000,
        )
        self._refresh_image_views()

    def _no_video_key(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Video is off")
        box.setIcon(QMessageBox.Information)
        box.setText(
            "Video needs an API key of its own: enter one in Settings → Video.\n\nVideo costs "
            "far more than pictures (tens of cents to over $10 for a few seconds), so the app "
            "never uses your chat key for it. Make a key for video alone at the service, with "
            "a daily spending limit set on it: that limit is the one that holds whatever "
            "happens here. The chat key can be entered there too, by hand, but then every "
            "video warns you first."
        )
        settings = box.addButton("Open Settings…", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is settings:
            self.open_settings()

    # --- the jobs ------------------------------------------------------------------

    def _on_video_accepted(
        self, story_id: str, record: GeneratedVideo, store: PictureStore
    ) -> None:
        """Paid for from now: on disk (or in the chat's memory) at once."""
        try:
            store.put_video(record)
        except OSError as exc:
            self._warn_plain("Couldn't keep the video's record", str(exc))
        if self.session is not None and self.session.story.id == story_id:
            charged = f" (charged ${record.charged:.2f})" if record.charged is not None else ""
            self.statusBar().showMessage(
                f"The video was accepted{charged}; it's being made.", 10000
            )
            self._refresh_image_views()

    def _on_video_progress(self, video_id: str, text: str) -> None:
        if self._video_progress.get(video_id) != text:
            self._video_progress[video_id] = text

    def _on_video_finished(
        self, story_id: str, record: GeneratedVideo, store: PictureStore
    ) -> None:
        self._video_progress.pop(record.id, None)
        try:
            store.put_video(record)
        except OSError as exc:
            self._warn_plain("Couldn't keep the video", str(exc))
            return
        open_story = self.session is not None and self.session.story.id == story_id
        if record.status == "failed":
            if open_story:
                self._refresh_image_views()
            self.statusBar().showMessage(f"The video failed: {record.error}", 20000)
            return
        if open_story:
            paid = record.cost
            if paid is not None:
                self.status_strip.add_cost(
                    Usage(
                        cost=paid,
                        cost_reported=record.cost_reported,
                        cost_estimated=not record.cost_reported,
                    )
                )
            self._refresh_story_cost()
            self._load_videos()
            self._grab_frames(record)
            self._refresh_image_views()
            self.statusBar().showMessage("Video ready.", 8000)
            if record.over_quote:
                QMessageBox.warning(
                    self,
                    "It cost more than shown",
                    f"The video cost ${record.charged or record.cost:.2f}, more than the "
                    f"${record.quote:.2f} worked out from the model's listing before it was "
                    "sent. The listing's price for this model may be wrong.",
                )
        else:
            self.statusBar().showMessage(
                "A video for another story is ready; it's in that story's Media tab.", 8000
            )

    def _on_video_failed(self, story_id: str, message: str) -> None:
        if self.session is not None and self.session.story.id == story_id:
            self._refresh_story_cost()
            self._refresh_image_views()
        if not message:
            self.statusBar().showMessage("Stopped before the video was sent.", 8000)
            return
        self._warn_plain("The video wasn't made", message)

    def _on_video_jobs_changed(self) -> None:
        self.status_strip.set_pictures(
            len(self.image_jobs.pending()) + len(self.video_jobs.pending())
        )

    def _client_for(self, video: GeneratedVideo) -> VideoClient | None:
        """A client for the endpoint a video was sent to, on the video key,
        if that is still where the key is for."""
        media = self.config.videos()
        if media is None or not _same_host(media.base_url, video.base_url):
            return None
        return VideoClient(video.base_url, media.api_key, api=video.api)

    def _resume_videos(self) -> None:
        """Ask after every video of the open story that is still being made
        (after the app was closed, or Stop waiting)."""
        if self.session is None:
            return
        for video in self.session.generated_videos():
            if video.status == "pending":
                client = self._client_for(video)
                if client is not None:
                    self.video_jobs.follow(
                        self.session.story.id, video, client, self.session.pictures
                    )

    # --- frames -----------------------------------------------------------------

    def _grab_frames(self, video: GeneratedVideo) -> None:
        """Take a finished video's first and last frames, once."""
        if video.id in self._grab_tried or not multimedia_available():
            return
        source = self._video_source(video)
        if source is None:
            return
        self._grab_tried.add(video.id)
        store = self.session.pictures
        story_id = self.session.story.id
        grabber = FrameGrabber(source, self)
        self._grabbers[video.id] = grabber
        grabber.done.connect(
            lambda first, last, seconds, v=video.id: self._on_frames(
                story_id, store, v, first, last, seconds
            )
        )

    def _on_frames(
        self,
        story_id: str,
        store: PictureStore,
        video_id: str,
        first: bytes,
        last: bytes,
        seconds: float,
    ) -> None:
        grabber = self._grabbers.pop(video_id, None)
        if grabber is not None:
            grabber.deleteLater()
        record = next((v for v in store.videos() if v.id == video_id), None)
        if record is None or not first:
            return
        poster, end = f"{VIDEOS_DIR}/{video_id}-first.jpg", f"{VIDEOS_DIR}/{video_id}-last.jpg"
        try:
            store.write(poster, first)
            store.write(end, last or first)
            store.put_video(
                record.model_copy(
                    update={"poster": poster, "last_frame": end, "duration": seconds or None}
                )
            )
        except (OSError, ValueError):
            return
        if self.session is not None and self.session.story.id == story_id:
            self._load_videos()
            self._refresh_image_views()

    # --- a video's menu -------------------------------------------------------------

    def _video(self, video_id: str) -> GeneratedVideo | None:
        if self.session is None:
            return None
        return next((v for v in self.session.pictures.videos() if v.id == video_id), None)

    def _video_action(self, action: str, video_id: str) -> None:
        video = self._video(video_id)
        if video is None or self.session is None:
            return
        store = self.session.pictures
        if action == "show":
            if video.anchor_node_id is not None:
                self._scroll_to_node(video.anchor_node_id)
        elif action == "stop":
            self.video_jobs.stop(video_id)
            self.statusBar().showMessage(
                "Stopped waiting. The video is still being made and paid for; Check again, or "
                "open the story later, and it is fetched.",
                10000,
            )
        elif action == "check":
            client = self._client_for(video)
            if client is None:
                self._warn_plain(
                    "Can't ask after it",
                    f"The video was sent to {video.base_url}, but Settings → Video has no key for "
                    "that address now. Put it back to fetch the video.",
                )
                return
            self.video_jobs.follow(self.session.story.id, video, client, store)
            self._refresh_image_views()
        elif action == "open":
            path = self._video_path(video)
            if path is not None:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        elif action == "prompt":
            box = QMessageBox(self)
            box.setWindowTitle("Prompt")
            box.setText(video.prompt)
            settings = ", ".join(f"{k}: {v}" for k, v in video.settings.items())
            frames = "; ".join(
                f"{word}: {use.owner_name}"
                for word, use in (("starts on", video.start_frame), ("ends on", video.end_frame))
                if use is not None
            )
            box.setInformativeText(
                "\n".join(line for line in (video.model, settings, frames) if line)
            )
            box.exec()
        elif action == "save":
            data = store.read(video.file) if video.file else None
            if data is None:
                return
            target, _ = QFileDialog.getSaveFileName(
                self, "Save video", str(Path.home() / file_name(video.file)), "Videos (*.mp4)"
            )
            if target:
                try:
                    Path(target).write_bytes(data)
                except OSError as exc:
                    self._warn_plain("Couldn't save", str(exc))
        elif action == "continue" and video.last_frame:
            start = RefUse(
                owner_kind="picture",
                owner_id=video.id,
                owner_name="An earlier video",
                ref_id=f"{video.id}-last",
                file=video.last_frame,
                caption="its last frame",
            )
            self.generate_video(anchor_id=self.session.story.active_leaf_id, start=start)
        elif action == "frame" and video.last_frame:
            self._use_as_reference(store.read(video.last_frame))
        elif action == "again":
            self.generate_video(anchor_id=video.anchor_node_id, prefill=video)
        elif action == "delete":
            pending = video.status == "pending"
            text = (
                "This video is still being made and is already paid for. Deleting its record "
                "means it is never fetched. Delete it?"
                if pending
                else "Delete this video? Its file is removed."
            )
            if QMessageBox.question(self, "Delete video", text) != QMessageBox.Yes:
                return
            self.video_jobs.stop(video_id)
            stop_playback()
            store.remove_video(video_id)
            self._refresh_image_views()

    # --- closing -------------------------------------------------------------------

    def _videos_allow_close(self) -> bool:
        """Videos still being sent are waited for or given up; those being
        made carry on at the service and are fetched next time (not in a
        memory-only chat, which asks)."""
        sending = [job for job in self.video_jobs.pending() if job.record is None]
        if sending:
            answer = QMessageBox.question(
                self,
                "A video is being sent",
                "A video is being sent right now. If you close, it may still be accepted and "
                "charged, and the app won't know to fetch it. Close anyway?",
            )
            if answer != QMessageBox.Yes:
                return False
        self.video_jobs.stop_all()
        self.video_jobs.wait_all()
        self.video_catalog.wait()
        stop_playback()
        return True

    def _video_counts(self, story_id: str) -> int:
        """Videos of a story still being made: lost with a memory-only chat."""
        if self.session is None or self.session.story.id != story_id:
            return 0
        return sum(1 for v in self.session.pictures.videos() if v.status == "pending")
