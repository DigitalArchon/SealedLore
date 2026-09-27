"""Sending an approved image request, off the GUI thread and outside the session.

A picture takes from seconds to minutes, and the author goes on playing, so
the job touches nothing the session owns: it reads its reference files,
calls the image model, writes each picture and its API log entries into the
story's own folder (by story id, so switching stories meanwhile is fine) and
returns the records. The window then adds them to images.json on the GUI
thread (`storage.images.add_generated_images`).

`ImageRequest` is built only from what the author approved in the dialog.
Nothing between here and the wire changes its prompt.

Reference pictures go with their hidden data removed (`storage.image_meta`),
or not at all; pictures that come back are kept with theirs removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.image import GeneratedImage, RefUse
from sealedlore.providers.base import ProviderError
from sealedlore.providers.images import (
    ImageClient,
    build_image_payload,
    data_url,
    loggable_payload,
)
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import IMAGES_DIR, MEDIA_TYPES, read_story_file, write_story_file
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import append_api_log


@dataclass(frozen=True)
class ImageRequest:
    story_id: str
    model: str
    # Exactly as approved: never stripped, never edited.
    prompt: str
    size: str
    n: int
    references: tuple[RefUse, ...]
    anchor_node_id: str | None
    direction: str = ""
    # USD per picture from the listing, for when the reply reports no cost.
    listed_price: float | None = None
    # The private scene it was asked for in, if any.
    private_span: str | None = None
    # False in a memory-only scene: the author's direction describes the
    # scene, so it reaches neither images.json nor the API log. The approved
    # prompt is kept either way: it was sent to the image model regardless.
    direction_kept: bool = True


class MissingReference(ValueError):
    """A reference picture's file is gone; nothing was sent."""


class UnstrippableReference(ValueError):
    """A reference picture whose hidden data can't be removed; nothing was sent."""


def _log(story_id: str, root: Path | None, kind: str, body: dict) -> str:
    entry_id = new_id()
    append_api_log(story_id, {"id": entry_id, "kind": kind, "at": utc_now_iso(), **body}, root)
    return entry_id


def run_image_request(
    client: ImageClient, request: ImageRequest, root: Path | None = None
) -> list[GeneratedImage]:
    """Send the request and keep what comes back. Raises ProviderError on failure."""
    references: list[str] = []
    for use in request.references:
        data = read_story_file(request.story_id, use.file, root)
        if data is None:
            raise MissingReference(f"the picture for {use.owner_name} is missing ({use.file})")
        try:
            data = strip_metadata(data)
        except MetadataError as exc:
            raise UnstrippableReference(
                f"the picture for {use.owner_name} ({use.file}) couldn't have its hidden "
                f"data removed ({exc}), so nothing was sent. Replace it with another copy "
                "of the picture."
            ) from exc
        references.append(data_url(data, MEDIA_TYPES[picture_kind(data)]))
    payload = build_image_payload(
        request.model, request.prompt, request.size, request.n, references
    )
    names = [f"{use.owner_name}: {use.file}" for use in request.references]
    direction = request.direction if request.direction_kept else ""
    log_ref = _log(
        request.story_id,
        root,
        "image_request",
        {
            "node_id": request.anchor_node_id,
            "direction": direction,
            "payload": loggable_payload(payload, names),
        },
    )
    try:
        reply = client.generate(payload)
    except Exception as exc:
        _log(
            request.story_id,
            root,
            # Not a response kind: a failed call isn't counted as an unpriced one.
            "image_error",
            {"request_log_ref": log_ref, "error": str(exc)},
        )
        raise

    if not (story_dir(request.story_id, root) / "story.json").is_file():
        # Deleted while the picture was drawn: don't bring its folder back.
        raise ProviderError("the story was deleted while its picture was being drawn")
    cost, reported = reply.cost, reply.cost is not None
    usage: dict = dict(reply.raw)
    if cost is None and request.listed_price is not None:
        cost = request.listed_price * len(reply.images)
        usage = {**usage, "cost": cost, "cost_estimated": True}
    records: list[GeneratedImage] = []
    # Paid for, so kept either way; one that can't be stripped is refused
    # if it is ever sent on.
    unstripped: list[str] = []
    for data in reply.images:
        image_id = new_id()
        relative = f"{IMAGES_DIR}/{image_id}{picture_kind(data) or '.png'}"
        try:
            data = strip_metadata(data)
        except MetadataError:
            unstripped.append(relative)
        write_story_file(request.story_id, relative, data, root)
        records.append(
            GeneratedImage(
                id=image_id,
                anchor_node_id=request.anchor_node_id,
                file=relative,
                prompt=request.prompt,
                direction=direction,
                model=request.model,
                size=request.size,
                references=list(request.references),
                cost=cost / len(reply.images) if cost is not None else None,
                cost_reported=reported,
                request_log_ref=log_ref,
                private_span=request.private_span,
            )
        )
    _log(
        request.story_id,
        root,
        "image_response",
        {
            "request_log_ref": log_ref,
            "files": [record.file for record in records],
            "usage": usage,
            **({"metadata_kept": unstripped} if unstripped else {}),
        },
    )
    return records
