"""The shapes the app's pictures come in, for cropping a picture of one's own
to fit (gui/crop_dialog.py; the author, Oct 2026: bring in whatever you like,
cut to the part you want or to the exact shape the app uses).

Each fixed shape is the size the configured model draws, read from the image
model listing the config keeps, so a picture brought in matches one drawn;
the background's is the story area as the window is now, since the window
scales the background to cover it. Pure: no Qt, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd

from sealedlore.engine.portraits import portrait_size
from sealedlore.engine.world_pictures import picture_size
from sealedlore.models.config import Config
from sealedlore.providers.images import parse_image_models

FREE = "free"
ORIGINAL = "original"
PORTRAIT = "portrait"
PICTURE = "picture"
BACKGROUND = "background"
ISOMETRIC = "isometric"
SQUARE = "square"

# When a model's sizes can't be read ("1k", "auto"): the shapes it most often draws.
FALLBACK = {PORTRAIT: (3, 4), PICTURE: (3, 2), ISOMETRIC: (4, 3), BACKGROUND: (16, 9)}


@dataclass(frozen=True)
class PictureShape:
    key: str
    label: str
    # Width and height in proportion; None for a free crop or the picture's
    # own shape (ORIGINAL, worked out from the picture).
    width: int | None = None
    height: int | None = None

    @property
    def ratio(self) -> float | None:
        if not self.width or not self.height:
            return None
        return self.width / self.height


def size_ratio(size: str) -> tuple[int, int] | None:
    """ "1024x1536" or "1024*1536" in lowest terms, or None."""
    parts = size.replace("*", "x").split("x")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    width, height = int(parts[0]), int(parts[1])
    if not width or not height:
        return None
    common = gcd(width, height)
    return width // common, height // common


def _said(width: int, height: int) -> str:
    """3:2, or the size itself when its lowest terms aren't a shape anyone
    would name (a window's 935×660 is 187:132)."""
    return f"{width}:{height}" if max(width, height) <= 32 else f"{width} × {height}"


def picture_shapes(
    config: Config | None = None, *, background: tuple[int, int] | None = None
) -> list[PictureShape]:
    """Every shape a picture may be cropped to, free first. `background` is
    the story area's size in pixels, when the window knows it."""
    listing = parse_image_models({"data": config.image_models}) if config is not None else []
    infos = {info.id: info for info in listing}
    portrait_model = infos.get(config.portrait_model) if config is not None else None
    iso_model = infos.get(config.map_isometric_model) if config is not None else None
    portrait = size_ratio(portrait_size(portrait_model)) or FALLBACK[PORTRAIT]
    picture = size_ratio(picture_size(portrait_model, landscape=True)) or FALLBACK[PICTURE]
    isometric = size_ratio(picture_size(iso_model, landscape=True)) or FALLBACK[ISOMETRIC]
    if background is not None and background[0] > 0 and background[1] > 0:
        common = gcd(*background)
        back = (background[0] // common, background[1] // common)
        back_label = f"Background ({_said(*background)}, the story area as it is now)"
    else:
        back = FALLBACK[BACKGROUND]
        back_label = f"Background ({_said(*back)})"
    return [
        PictureShape(FREE, "Free: any part of the picture"),
        PictureShape(ORIGINAL, "The picture's own shape"),
        PictureShape(PORTRAIT, f"Portrait ({_said(*portrait)}, as portraits are drawn)", *portrait),
        PictureShape(
            PICTURE, f"Place or lore picture ({_said(*picture)}, as they are drawn)", *picture
        ),
        PictureShape(BACKGROUND, back_label, *back),
        PictureShape(ISOMETRIC, f"Isometric view ({_said(*isometric)})", *isometric),
        PictureShape(SQUARE, "Square (1:1)", 1, 1),
    ]
