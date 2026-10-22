"""Pictures being drawn, each on its own thread, while the author plays on.

A picture takes from seconds to a couple of minutes (Seedream 5.0 Pro with
three references: two minutes, live), so a job never holds the window: it
runs beside the story's own worker, like the model catalogue's fetch, and
never touches the session. The job writes its picture and log entries into
its story's folder; the window adds the record to images.json on the GUI
thread when the job finishes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from sealedlore.engine.images import ImageRequest, run_image_request
from sealedlore.gui.worker import GenerationWorker
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.config import Config
from sealedlore.models.image import GeneratedImage
from sealedlore.providers.base import StreamCancelled
from sealedlore.providers.images import (
    ImageClient,
    ImageModelInfo,
    list_image_models,
    parse_image_models,
)
from sealedlore.providers.media_http import MediaTimedOut
from sealedlore.storage.picture_store import DiskPictures, PictureStore
from sealedlore.storage.repository import save_config

LISTING_MAX_AGE = timedelta(days=1)


def listing_is_stale(fetched_at: str | None) -> bool:
    if not fetched_at:
        return True
    try:
        when = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(UTC) - when > LISTING_MAX_AGE


class ImageCatalog(QObject):
    """The endpoint's image models (sizes, reference limits, prices), kept in
    config for a day and fetched on a thread the window owns: a dialog closed
    mid-fetch must not take a running thread down with it.

    While a chat kept in memory only is open (`in_memory`), a listing fetched
    is kept here and not in the config: its fetch time would say when that
    chat's picture dialog was opened, and a memory chat teaches the settings
    nothing."""

    changed = Signal()

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
        # A listing fetched during a memory-only chat: (entries, fetched at).
        self._held: tuple[list[dict], str] | None = None

    def _listing(self) -> tuple[list[dict], str | None]:
        if self._held is not None and self._in_memory():
            return self._held
        media = self.config.images()
        source = self.config.image_models_source
        if media is not None and source is not None and source != media.base_url:
            # Another endpoint's listing: its models may not be this one's.
            return [], None
        return self.config.image_models, self.config.image_models_fetched_at

    @property
    def models(self) -> list[ImageModelInfo]:
        return parse_image_models({"data": self._listing()[0]})

    @property
    def loading(self) -> bool:
        return self._thread is not None

    def ensure(self, *, force: bool = False) -> None:
        """Fetch the listing if it's a day old (or `force`, the picker's Refresh)."""
        media = self.config.images()
        if self.loading or media is None:
            return
        if not force and not listing_is_stale(self._listing()[1]):
            return
        found: list[ImageModelInfo] = []
        wanted = self.config.image_model

        def job():
            found.extend(
                list_image_models(
                    media.base_url, api=media.api, api_key=media.api_key, wanted=wanted
                )
            )
            return
            yield

        worker = GenerationWorker(job)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        thread.finished.connect(lambda: self._on_finished(found))
        self._worker, self._thread = worker, thread
        thread.start()
        self.changed.emit()  # a picker shows "Loading…"

    def _on_finished(self, found: list[ImageModelInfo]) -> None:
        self._worker, self._thread = None, None
        if not found:
            self.changed.emit()  # done loading; the listing kept stands
            return
        entries = [listing_entry(info) for info in found]
        if self._in_memory():
            self._held = (entries, utc_now_iso())
            self.changed.emit()
            return
        self.config.image_models = entries
        self.config.image_models_fetched_at = utc_now_iso()
        media = self.config.images()
        self.config.image_models_source = media.base_url if media is not None else None
        try:
            save_config(self.config, root=self.root)
        except OSError:
            pass
        self.changed.emit()

    def wait(self) -> None:
        """Before the app closes: the fetch gives up by itself within its timeout."""
        if self._thread is not None:
            self._thread.wait(35_000)


def listing_entry(info: ImageModelInfo) -> dict:
    """An `ImageModelInfo` back in the listing's own shape, for config."""
    return {
        "id": info.id,
        "name": info.name,
        "description": info.description,
        "pricing": {"per_image": dict(info.per_image)},
        "supported_parameters": {
            "resolutions": list(info.resolutions),
            "max_input_images": info.max_inputs,
            "max_output_images": info.max_outputs,
        },
    }


@dataclass
class ImageJob:
    id: str
    request: ImageRequest
    client: ImageClient
    # Where its story keeps pictures: written by the job, added to by the window.
    store: PictureStore
    thread: QThread | None = None
    worker: GenerationWorker | None = None
    records: list[GeneratedImage] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    stopped: bool = False


@dataclass
class FailedPicture:
    """A picture that didn't come back, kept until the author retries,
    edits or dismisses it (for this run of the app only: the API log keeps
    the error). Stopping isn't failing: a stopped job keeps nothing."""

    job_id: str
    request: ImageRequest
    store: PictureStore
    message: str
    # Nothing came back in time: the service may still make it and charge.
    timed_out: bool = False


class ImageJobs(QObject):
    # (job id, story id, records, the story's PictureStore)
    finished = Signal(str, str, list, object)
    # (job id, story id, message); a stopped job fails with an empty message.
    failed = Signal(str, str, str)
    changed = Signal()

    def __init__(self, root: Path | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.jobs: dict[str, ImageJob] = {}
        self.failures: dict[str, FailedPicture] = {}

    def start(
        self, request: ImageRequest, client: ImageClient, store: PictureStore | None = None
    ) -> str:
        """`store`: where the story keeps its pictures; by default its folder."""
        if store is None:
            store = DiskPictures(request.story_id, self.root)
        job = ImageJob(id=new_id(), request=request, client=client, store=store)

        def run():
            try:
                job.records = run_image_request(job.client, job.request, store=job.store)
            except StreamCancelled:
                job.stopped = True
            except MediaTimedOut as exc:
                job.error, job.timed_out = str(exc), True
            except Exception as exc:  # shown to the author, never lost
                job.error = str(exc) or type(exc).__name__
            finally:
                job.client.close()
            return
            yield  # a generator, so GenerationWorker can run it

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

    def pending(self, story_id: str | None = None) -> list[ImageJob]:
        return [
            job
            for job in self.jobs.values()
            if story_id is None or job.request.story_id == story_id
        ]

    def failed_for(self, story_id: str) -> list[FailedPicture]:
        """The story's failed pictures still to be dealt with. A memory-only
        chat's go with it."""
        for job_id, failure in list(self.failures.items()):
            if not failure.store.alive():
                del self.failures[job_id]
        return [f for f in self.failures.values() if f.request.story_id == story_id]

    def dismiss(self, job_id: str) -> FailedPicture | None:
        failure = self.failures.pop(job_id, None)
        if failure is not None:
            self.changed.emit()
        return failure

    def stop(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is not None:
            job.stopped = True
            job.client.cancel()

    def stop_all(self) -> None:
        for job_id in list(self.jobs):
            self.stop(job_id)

    def wait_all(self, msecs: int = 5000) -> None:
        for job in list(self.jobs.values()):
            if job.thread is not None:
                job.thread.wait(msecs)

    def _on_finished(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return
        story_id = job.request.story_id
        if job.records:
            # Kept even when stopped: the service finished it and charged for it.
            self.finished.emit(job.id, story_id, job.records, job.store)
        elif job.stopped:
            self.failed.emit(job.id, story_id, "")
        else:
            message = job.error or "no picture came back"
            self.failures[job.id] = FailedPicture(
                job.id, job.request, job.store, message, timed_out=job.timed_out
            )
            self.failed.emit(job.id, story_id, message)
        job.thread, job.worker = None, None
        self.changed.emit()
