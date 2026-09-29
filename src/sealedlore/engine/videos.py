"""Making an approved video, off the GUI thread and outside the session.

A video takes minutes and is paid for when it is sent, so the job is in two
parts, and the record between them is what keeps a paid video from being
lost:

1. `submit_video` sends the approved request and, once the endpoint has
   accepted it, returns its record (`status="pending"`, with the job's id).
   The window writes that record at once (videos.json), on the GUI thread.
2. `follow_video` asks after it until it is done or has failed, fetches it,
   removes its hidden data, keeps it, and returns the finished record. It
   can start again from the record alone: after Stop waiting, or when the
   story is opened again after the app was closed.

`VideoRequest` is built only from what the author approved in the dialog;
nothing between here and the wire changes its prompt. Frames go with their
hidden data removed, or not at all.

What it cost: NanoGPT says on accepting a video what it has just charged
(the finished status says nothing), OpenRouter says when it is done, and
WaveSpeed says neither, so its quote stands, marked as an estimate. A video
that fails is refunded by NanoGPT, so nothing is counted for it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sealedlore.ids import utc_now_iso
from sealedlore.models.image import RefUse
from sealedlore.models.video import GeneratedVideo
from sealedlore.providers.base import ProviderError
from sealedlore.providers.images import data_url
from sealedlore.providers.videos import (
    FrameInputs,
    VideoClient,
    VideoJob,
    VideoModelInfo,
    build_video_payload,
    loggable_video_payload,
)
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES, VIDEOS_DIR
from sealedlore.storage.picture_store import PictureStore
from sealedlore.storage.video_meta import strip_video_metadata

# How often a video is asked after. They took three to six minutes live.
POLL_SECONDS = 10.0


@dataclass(frozen=True)
class VideoRequest:
    story_id: str
    model: VideoModelInfo
    # Exactly as approved: never stripped, never edited.
    prompt: str
    # Already cleaned against the model's listing (`VideoModelInfo.clean`).
    settings: dict[str, Any]
    anchor_node_id: str | None
    start_frame: RefUse | None = None
    end_frame: RefUse | None = None
    direction: str = ""
    quote: float | None = None
    private_span: str | None = None
    # False in a memory-only scene: the author's direction describes the scene.
    direction_kept: bool = True


class MissingFrame(ValueError):
    """A frame's picture is gone; nothing was sent."""


class UnstrippableFrame(ValueError):
    """A frame whose hidden data can't be removed; nothing was sent."""


def _frame(store: PictureStore, use: RefUse | None) -> str | None:
    if use is None:
        return None
    data = store.read(use.file)
    if data is None:
        raise MissingFrame(f"the picture for {use.owner_name} is missing ({use.file})")
    try:
        data = strip_metadata(data)
    except MetadataError as exc:
        raise UnstrippableFrame(
            f"the picture for {use.owner_name} ({use.file}) couldn't have its hidden data "
            f"removed ({exc}), so nothing was sent."
        ) from exc
    return data_url(data, MEDIA_TYPES[picture_kind(data)])


def submit_video(client: VideoClient, request: VideoRequest, store: PictureStore) -> GeneratedVideo:
    """Send the request. Returns the pending record once it is accepted;
    raises (MissingFrame, ProviderError…) if nothing was made."""
    frames = FrameInputs(
        start=_frame(store, request.start_frame), end=_frame(store, request.end_frame)
    )
    payload = build_video_payload(request.model, request.prompt, request.settings, frames)
    names = {
        "start": request.start_frame.owner_name if request.start_frame else "",
        "end": request.end_frame.owner_name if request.end_frame else "",
    }
    direction = request.direction if request.direction_kept else ""
    log_ref = store.log(
        "video_request",
        {
            "node_id": request.anchor_node_id,
            "direction": direction,
            "quote": request.quote,
            "payload": loggable_video_payload(payload, names),
        },
    )
    try:
        job = client.submit(payload)
    except Exception as exc:
        store.log("video_error", {"request_log_ref": log_ref, "error": str(exc)})
        raise
    store.log(
        "video_accepted",
        {"request_log_ref": log_ref, "job_id": job.id, "charged": job.charged, "reply": job.raw},
    )
    return GeneratedVideo(
        anchor_node_id=request.anchor_node_id,
        prompt=request.prompt,
        direction=direction,
        model=request.model.id,
        settings=dict(request.settings),
        start_frame=request.start_frame,
        end_frame=request.end_frame,
        api=job.api,
        base_url=job.base_url,
        job_id=job.id,
        poll_url=job.poll_url,
        quote=request.quote,
        charged=job.charged,
        request_log_ref=log_ref,
        private_span=request.private_span,
    )


def follow_video(
    client: VideoClient,
    record: GeneratedVideo,
    store: PictureStore,
    *,
    progress: Callable[[str], None] | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> GeneratedVideo:
    """Ask after a pending video until it is done or has failed, then keep
    it. Returns the record as it now stands. Stop (`client.cancel()`) raises
    StreamCancelled and leaves the record pending, to be followed again."""
    job = VideoJob(
        id=record.job_id, api=record.api, base_url=record.base_url, poll_url=record.poll_url
    )
    while True:
        status = client.status(job)
        if status.state == "done":
            break
        if status.state == "failed":
            store.log(
                "video_error",
                {"request_log_ref": record.request_log_ref, "error": status.error},
            )
            return record.model_copy(
                update={"status": "failed", "error": status.error, "finished_at": utc_now_iso()}
            )
        if progress is not None and status.detail:
            progress(status.detail)
        client.wait(poll_seconds)
    data = client.fetch(status.urls[0])
    if not store.alive():
        raise ProviderError("the story was closed or deleted while its video was made")
    try:
        data = strip_video_metadata(data)
        kept_metadata = False
    except MetadataError:
        kept_metadata = True  # paid for, so kept either way
    relative = f"{VIDEOS_DIR}/{record.id}.mp4"
    store.write(relative, data)
    # The bill: what the endpoint said on accepting it, or when done; else
    # the quote, marked as an estimate.
    cost, reported = record.charged, record.charged is not None
    if status.cost is not None:
        cost, reported = status.cost, True
    usage: dict[str, Any] = {"cost": cost} if cost is not None else {}
    if cost is None and record.quote is not None:
        cost, usage = record.quote, {"cost": record.quote, "cost_estimated": True}
    store.log(
        "video_response",
        {
            "request_log_ref": record.request_log_ref,
            "files": [relative],
            "usage": usage,
            **({"metadata_kept": [relative]} if kept_metadata else {}),
        },
    )
    return record.model_copy(
        update={
            "status": "done",
            "file": relative,
            "cost": cost,
            "cost_reported": reported,
            "finished_at": utc_now_iso(),
            "progress": "",
        }
    )
