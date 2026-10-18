"""A picture model's other version, chosen for what is sent (the author, Oct 18
2026: "with Flux Klein there is a text to image mode and an edit image mode,
and if you set the wrong one, the other fails. That should be automatic").

Endpoints list a model's text-to-image and edit versions as separate ids
("blackforestlabs/flux-3/klein/text-to-image" and ".../edit-image",
"bytedance/seedream-v5.0-flash" and ".../edit", "nano-banana-pro" and
"nano-banana-pro-edit"), and their listings don't say which needs a picture:
both FLUX.3 Klein ids list ten input pictures. Measured live (SealedLoreDocs
design/images.md, "FLUX.3 Klein", Oct 18 2026): its edit id refuses a request
with no picture (HTTP 400, nothing charged); its text id takes a picture and
uses it, but two blind judges ranked its edits below the edit id's on all
three edits tried. So the pairs are read from the ids:

- no picture, and an edit version: its text version;
- pictures, and an id that names itself text-to-image: its listed edit
  version (whether or not it takes pictures itself); a plain id ("bytedance/
  seedream-v5.0-pro", which edits as itself and was chosen for keeping
  characters) only when its listing says it takes none.

Pure: ids and listings in, an id out.
"""

from __future__ import annotations

from collections.abc import Iterable

from sealedlore.providers.images import ImageModelInfo

# The endings of an edit version, longest first, each with the endings its
# text version may have in its place ("" is the bare id).
_EDIT_ENDINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/edit-lora", ("/text-to-image-lora",)),
    ("/edit-image", ("/text-to-image", "")),
    ("/image-edit", ("/text-to-image", "")),
    ("/image-to-image", ("/text-to-image", "")),
    ("-image-to-image", ("",)),
    ("/edit", ("/text-to-image", "")),
    ("-edit", ("",)),
)
_TEXT_ENDINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/text-to-image-lora", ("/edit-lora",)),
    ("/text-to-image", ("/edit-image", "/edit", "/image-edit", "/image-to-image")),
)
# A bare id's edit versions.
_BARE_EDITS = ("/edit", "-edit", "-image-to-image", "/edit-image")


def edit_only(model: str) -> bool:
    """An id that names an edit version: it needs a picture to work from."""
    return any(model.endswith(end) for end, _texts in _EDIT_ENDINGS)


def text_versions(model: str) -> list[str]:
    """The ids an edit version's text version may have, likeliest first."""
    for end, texts in _EDIT_ENDINGS:
        if model.endswith(end):
            base = model[: -len(end)]
            return [base + text for text in texts if base]
    return []


def edit_versions(model: str) -> list[str]:
    """The ids a text version's edit version may have, likeliest first."""
    if edit_only(model):
        return []
    for end, edits in _TEXT_ENDINGS:
        if model.endswith(end):
            base = model[: -len(end)]
            return [base + edit for edit in edits]
    return [model + edit for edit in _BARE_EDITS]


def fitting_model(model: str, pictures: int, listing: Iterable[ImageModelInfo]) -> str:
    """The version of `model` to send a request with `pictures` pictures to:
    `model` itself unless that would fail. An edit version asked with no
    picture goes to its text version, the listed one, or the likeliest when
    the listing knows neither (it is older than the model: the edit version
    would fail for certain). Asked with pictures, an id that names itself
    text-to-image, or one the listing says takes none, goes to its listed
    edit version that takes them; with none listed it is left (the dialog
    says when it can't be given a picture)."""
    model = model.strip()
    if not model:
        return model
    known = {info.id: info for info in listing}
    if pictures <= 0 and edit_only(model):
        found = next((m for m in text_versions(model) if m in known), None)
        if found is not None:
            return found
        if model not in known:
            versions = text_versions(model)
            return versions[0] if versions else model
        return model
    info = known.get(model)
    named_text = any(model.endswith(end) for end, _edits in _TEXT_ENDINGS)
    if pictures > 0 and (named_text or (info is not None and not info.accepts_images)):
        found = next(
            (m for m in edit_versions(model) if m in known and known[m].accepts_images), None
        )
        return found or model
    return model


def version_words(sent: str, pictures: int) -> str:
    """What the dialogs say when the request goes to the other version."""
    if pictures > 0:
        return f"sent to {sent}, its edit version, since pictures go with the prompt"
    return f"sent to {sent}, its text-to-image version, since no picture goes with the prompt"
