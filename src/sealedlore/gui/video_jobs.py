"""Videos being made, each followed on its own thread, and the video models.

A video takes minutes, so its job never holds the window: it runs beside the
story's own worker, as a picture's does, and never touches the session. The
job sends the approved request and says `accepted` at once, and the window
writes the pending record then (a video is paid for from that moment); then
it asks after it until it is done, and says `finished`.

A pending record can be followed again (`follow`) after Stop waiting, and
when its story is opened again after the app was closed.

`VideoCatalog` keeps the endpoint's video models, as `ImageCatalog` keeps its
picture models: in config for a day, fetched on a thread the window owns,
held in memory only while a memory-only chat is open. WaveSpeed prices a
request itself; `VideoCatalog.price` asks it on the same kind of thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from sealedlore.engine.videos import VideoRequest, follow_video, submit_video
from sealedlore.gui.image_jobs import listing_is_stale
from sealedlore.gui.worker import GenerationWorker
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.config import Config
from sealedlore.models.video import GeneratedVideo
from sealedlore.providers import wavespeed
from sealedlore.providers.base import StreamCancelled
from sealedlore.providers.media_http import MediaHttp
from sealedlore.providers.videos import (
    VideoClient,
    VideoModelInfo,
    VideoParam,
    list_video_models,
)
from sealedlore.storage.picture_store import PictureStore
from sealedlore.storage.repository import save_config


def model_entry(info: VideoModelInfo) -> dict:
    """A model as config keeps it."""
    return asdict(info)


def model_from_entry(entry: dict) -> VideoModelInfo | None:
    try:
        params = tuple(
            VideoParam(**{**p, "options": tuple(tuple(o) for o in p.get("options") or ())})
            for p in entry.get("params") or ()
        )
        return VideoModelInfo(
            **{
                **entry,
                "params": params,
                "fixed": tuple(tuple(f) for f in entry.get("fixed") or ()),
            }
        )
    except (TypeError, ValueError):
        return None


class VideoCatalog(QObject):
    changed = Signal()
    # (model id, price or None, what it rests on): WaveSpeed's own quote.
    priced = Signal(str, object, str)

    def __init__(
        self,
        config: Config,
        root: Path | None,
        parent: QObject | None = None,
        *,
        in_memory: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.root = root
        self._in_memory = in_memory
        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._held: tuple[list[dict], str] | None = None
        self._price_thread: QThread | None = None
        self._price_worker: GenerationWorker | None = None

    def _listing(self) -> tuple[list[dict], str | None]:
        if self._held is not None and self._in_memory():
            return self._held
        media = self.config.videos()
        source = self.config.video_models_source
        if media is not None and source is not None and source != media.base_url:
            return [], None
        return self.config.video_models, self.config.video_models_fetched_at

    @property
    def models(self) -> list[VideoModelInfo]:
        found = (model_from_entry(entry) for entry in self._listing()[0])
        return [info for info in found if info is not None]

    def model(self, model_id: str) -> VideoModelInfo | None:
        return next((info for info in self.models if info.id == model_id), None)

    @property
    def loading(self) -> bool:
        return self._thread is not None

    def ensure(self, *, force: bool = False) -> None:
        """Fetch the listing if it's a day old (or `force`, the picker's Refresh)."""
        media = self.config.videos()
        if self.loading or media is None:
            return
        if not force and not listing_is_stale(self._listing()[1]):
            return
        found: list[VideoModelInfo] = []

        def job():
            found.extend(list_video_models(media.base_url, api=media.api, api_key=media.api_key))
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._on_finished(found, media.base_url))
        self._worker, self._thread = worker, thread
        thread.start()
        self.changed.emit()

    def _on_finished(self, found: list[VideoModelInfo], source: str) -> None:
        self._worker, self._thread = None, None
        if not found:
            self.changed.emit()
            return
        entries = [model_entry(info) for info in found]
        if self._in_memory():
            self._held = (entries, utc_now_iso())
            self.changed.emit()
            return
        self.config.video_models = entries
        self.config.video_models_fetched_at = utc_now_iso()
        self.config.video_models_source = source
        try:
            save_config(self.config, root=self.root)
        except OSError:
            pass
        self.changed.emit()

    def price(self, model_id: str, inputs: dict[str, Any]) -> None:
        """Ask WaveSpeed what a request costs; `priced` says, with None if it
        wouldn't. One at a time: a newer ask waits for the one running."""
        media = self.config.videos()
        if media is None or self._price_thread is not None:
            return
        answer: list[Any] = [None, ""]

        def job():
            http = MediaHttp(media.base_url, media.api_key, timeout_seconds=30.0)
            try:
                answer[0] = wavespeed.price(http, model_id, inputs)
                answer[1] = "WaveSpeed's own quote for these settings"
            except Exception as exc:  # shown in the dialog, never lost
                answer[1] = f"WaveSpeed wouldn't say ({exc})"
            finally:
                http.close()
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._on_priced(model_id, answer))
        self._price_worker, self._price_thread = worker, thread
        thread.start()

    def _on_priced(self, model_id: str, answer: list[Any]) -> None:
        self._price_worker, self._price_thread = None, None
        self.priced.emit(model_id, answer[0], answer[1])

    def wait(self) -> None:
        for thread in (self._thread, self._price_thread):
            if thread is not None:
                thread.wait(35_000)


@dataclass
class VideoJob:
    id: str
    story_id: str
    client: VideoClient
    store: PictureStore
    # The approved request, until it is accepted; then the record followed.
    request: VideoRequest | None = None
    record: GeneratedVideo | None = None
    thread: QThread | None = None
    worker: GenerationWorker | None = None
    error: str | None = None
    stopped: bool = False
    progress: list[str] = field(default_factory=list)


class VideoJobs(QObject):
    # (story id, the pending record, the story's store): paid for, write it now.
    accepted = Signal(str, object, object)
    # (video id, what the endpoint says about it)
    progress = Signal(str, str)
    # (story id, the record as it now stands: done or failed, the store)
    finished = Signal(str, object, object)
    # (story id, message): nothing was made (refused, a frame missing…).
    failed = Signal(str, str)
    # (story id, video id): stopped waiting; the record stays pending.
    stopped = Signal(str, str)
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.jobs: dict[str, VideoJob] = {}

    def start(self, request: VideoRequest, client: VideoClient, store: PictureStore) -> str:
        job = VideoJob(new_id(), request.story_id, client, store, request=request)
        return self._run(job)

    def follow(self, story_id: str, record: GeneratedVideo, client: VideoClient, store) -> str:
        """Ask after a pending video again. Already followed: nothing new."""
        running = self.running_ids()
        if record.id in running:
            return running[record.id]
        job = VideoJob(new_id(), story_id, client, store, record=record)
        return self._run(job)

    def _run(self, job: VideoJob) -> str:
        def run():
            try:
                if job.record is None:
                    job.record = submit_video(job.client, job.request, job.store)
                    self.accepted.emit(job.story_id, job.record, job.store)
                job.record = follow_video(
                    job.client,
                    job.record,
                    job.store,
                    progress=lambda text: self.progress.emit(job.record.id, text),
                )
            except StreamCancelled:
                job.stopped = True
            except Exception as exc:  # shown to the author, never lost
                job.error = str(exc) or type(exc).__name__
            finally:
                job.client.close()
            return
            yield

        worker = GenerationWorker(run)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        # Cleanup on the thread, never the worker's own finished (CONTRIBUTING.md).
        thread.finished.connect(lambda: self._on_finished(job.id))
        job.thread, job.worker = thread, worker
        self.jobs[job.id] = job
        thread.start()
        self.changed.emit()
        return job.id

    def running_ids(self) -> dict[str, str]:
        """Video id → job id, for the videos being asked after now."""
        return {job.record.id: job.id for job in self.jobs.values() if job.record is not None}

    def pending(self, story_id: str | None = None) -> list[VideoJob]:
        return [job for job in self.jobs.values() if story_id is None or job.story_id == story_id]

    def unsent(self, story_id: str) -> list[VideoJob]:
        """Jobs still sending: no record yet."""
        return [job for job in self.pending(story_id) if job.record is None]

    def stop(self, video_id: str) -> None:
        """Stop waiting for a video (by its record's id)."""
        for job in self.jobs.values():
            if job.record is not None and job.record.id == video_id:
                job.stopped = True
                job.client.cancel()

    def stop_all(self) -> None:
        for job in self.jobs.values():
            job.stopped = True
            job.client.cancel()

    def wait_all(self, msecs: int = 5000) -> None:
        for job in list(self.jobs.values()):
            if job.thread is not None:
                job.thread.wait(msecs)

    def _on_finished(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return
        if job.record is not None and job.record.status != "pending":
            self.finished.emit(job.story_id, job.record, job.store)
        elif job.record is not None and (job.stopped or job.error is None):
            self.stopped.emit(job.story_id, job.record.id)
        elif job.record is not None:
            # Accepted, but asking after it failed (the network, a restart of
            # the service): the record stays pending, to be asked after again.
            self.stopped.emit(job.story_id, job.record.id)
            self.failed.emit(job.story_id, f"couldn't ask after the video: {job.error}")
        elif job.stopped:
            self.failed.emit(job.story_id, "")
        else:
            self.failed.emit(job.story_id, job.error or "the video wasn't accepted")
        job.thread, job.worker = None, None
        self.changed.emit()
