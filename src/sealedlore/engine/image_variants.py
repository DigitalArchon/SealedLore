"""A picture model as the author chooses it, one model whatever versions the
endpoint lists it in (the author, Oct 18 2026: "the user doesn't have to
select between Edit or Normal mode for any of them, just select the model and
the UI should show if it supports adding an image or not, and if it does, it
should auto select the edit version if an image is included").

Endpoints list a model's text-to-image and edit versions as separate ids
("blackforestlabs/flux-3/klein/text-to-image" and ".../edit-image",
"bytedance/seedream-v5.0-pro" and ".../edit", "nano-banana-pro" and
"nano-banana-pro-edit"), and their listings don't say which needs a picture:
both FLUX.3 Klein ids list ten input pictures. Measured live (SealedLoreDocs
design/images.md, "FLUX.3 Klein", Oct 18 2026): its edit id refuses a request
with no picture (HTTP 400, nothing charged); its text id takes a picture and
uses it, but two blind judges ranked its edits below the edit id's on all
three edits tried. So the versions are paired by their ids into one
`ImageFamily`, chosen and kept by its text version's id, and a request goes
to the version what is sent calls for (`fitting_model`):

- a picture with the prompt, and an edit version listed: the edit version;
- no picture, and an edit version chosen (a saved setting, a typed id): its
  text version.

Pure: ids and listings in, ids and families out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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
_BARE_EDITS = ("/edit", "-edit", "-image-to-image", "/edit-image", "/image-to-image")


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


@dataclass(frozen=True)
class ImageFamily:
    """One model as the author chooses it: its listing (the text version's,
    or the only one) and its separate edit version when one is listed."""

    info: ImageModelInfo
    edit: ImageModelInfo | None = None

    @property
    def id(self) -> str:
        """The id it is chosen and kept by."""
        return self.info.id

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def needs_picture(self) -> bool:
        """An edit version with no text version: it only changes a picture."""
        return self.edit is None and edit_only(self.info.id)

    @property
    def takes_pictures(self) -> bool:
        return self.edit is not None or self.info.accepts_images

    @property
    def max_pictures(self) -> int:
        return (self.edit or self.info).max_inputs

    @property
    def ids(self) -> tuple[str, ...]:
        return (self.info.id, self.edit.id) if self.edit is not None else (self.info.id,)


def _edit_of(model: str, known: dict[str, ImageModelInfo]) -> ImageModelInfo | None:
    return next(
        (known[m] for m in edit_versions(model) if m in known and known[m].accepts_images), None
    )


def families(listing: Iterable[ImageModelInfo]) -> list[ImageFamily]:
    """The listing with each model's versions as one, in its order: an edit
    version whose text version is listed is folded into it."""
    listing = list(listing)
    known = {info.id: info for info in listing}
    out: list[ImageFamily] = []
    for info in listing:
        if edit_only(info.id):
            text = next((known[t] for t in text_versions(info.id) if t in known), None)
            if text is not None and _edit_of(text.id, known) is info:
                continue  # one row: its text version's
            out.append(ImageFamily(info))
            continue
        out.append(ImageFamily(info, _edit_of(info.id, known)))
    return out


def family_of(model: str, listing: Iterable[ImageModelInfo]) -> ImageFamily | None:
    """The family a model id belongs to (either version's id), or None."""
    model = model.strip()
    for family in families(listing):
        if model in family.ids:
            return family
    return None


def family_id(model: str, listing: Iterable[ImageModelInfo]) -> str:
    """The id a model is chosen and kept by: an edit version's text version
    where one is listed, else the id as it is."""
    found = family_of(model, listing)
    return found.id if found is not None else model.strip()


def fitting_model(model: str, pictures: int, listing: Iterable[ImageModelInfo]) -> str:
    """The version of `model` to send a request with `pictures` pictures to.
    With pictures: its listed edit version that takes them, else itself (the
    dialogs say when it can't be given a picture). With none: an edit
    version goes to its text version, the listed one, or the likeliest when
    the listing knows neither (it is older than the model: the edit version
    would fail for certain)."""
    model = model.strip()
    if not model:
        return model
    known = {info.id: info for info in listing}
    if pictures > 0:
        if edit_only(model):
            return model
        edit = _edit_of(model, known)
        return edit.id if edit is not None else model
    if not edit_only(model):
        return model
    found = next((m for m in text_versions(model) if m in known), None)
    if found is not None:
        return found
    if model not in known:
        versions = text_versions(model)
        return versions[0] if versions else model
    return model


def version_words(sent: str, pictures: int) -> str:
    """What the dialogs say when the request goes to the other version."""
    if pictures > 0:
        return f"sent to {sent}, its edit version, since pictures go with the prompt"
    return f"sent to {sent}, its text-to-image version, since no picture goes with the prompt"


def picture_support(model: str, listing: Iterable[ImageModelInfo]) -> tuple[str, str]:
    """(a few words beside the model, the whole of it for a tooltip) about
    whether the model can be given a picture; ("", "") for one the listing
    doesn't know."""
    found = family_of(model, listing)
    if found is None:
        return "", ""
    count = found.max_pictures
    up_to = f"up to {count} picture{'s' if count != 1 else ''}" if count else "pictures"
    if found.needs_picture:
        return (
            "edits a picture only",
            f"It only changes a picture it is given ({up_to}); it can't draw from words alone.",
        )
    if found.edit is not None:
        return (
            "takes pictures",
            f"It can be given {up_to}: when a picture goes with the prompt, its edit version "
            f"({found.edit.id}) is sent for you.",
        )
    if found.takes_pictures:
        return "takes pictures", f"It can be given {up_to} with the prompt."
    return "words only", "It draws from words alone: it can't be given a picture."


def takes_pictures(model: str, listing: Iterable[ImageModelInfo]) -> bool:
    """The model (either version) can be given a picture; False for one the
    listing doesn't know."""
    found = family_of(model, listing)
    return found is not None and found.takes_pictures
