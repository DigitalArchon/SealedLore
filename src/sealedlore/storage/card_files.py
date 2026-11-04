"""A card's other files put to use (models/tavern.py CardAsset): its
background behind a chat or story made from it, and the pictures a greeting
shows, with that greeting. Pure.

A card says which picture a greeting shows in text, as the app it was made
in reads it. RisuAI writes `{{img::name}}` (and image, asset, emotion, bg,
raw and path inside an `<img>`), matching names whatever their case; others
write an `<img>` or a Markdown picture whose address is the file's place in
the card (`embeded://…`, `__asset:…`). Each is taken out of the text, which
the model reads as written: the picture shows under the greeting instead.
What SealedLore doesn't play (a sound, a video, background music) is taken
out the same way; it stays with the card in Your characters.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import CardAsset

# RisuAI's (its parser's `assetRegex`, read Nov 4 2026).
_MACRO = re.compile(
    r"\{\{(raw|path|img|image|video|audio|bgm|bg|emotion|asset|video-img|source)::(.+?)\}\}",
    re.IGNORECASE | re.DOTALL,
)
_HTML_IMG = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?P<quote>[\"'])(?P<src>.*?)(?P=quote)[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_SHOWN = ("img", "image", "asset", "emotion", "raw", "path")
_ANY = re.compile(
    f"(?P<tag>{_HTML_IMG.pattern})|(?P<markdown>{_MARKDOWN_IMG.pattern})|(?P<macro>{_MACRO.pattern})",
    re.IGNORECASE | re.DOTALL,
)


def background_of(assets: Sequence[CardAsset]) -> CardAsset | None:
    """The card's main background, as the spec has it: the one named "main",
    else the first; only one the card holds, as a picture."""
    backgrounds = [a for a in assets if a.type == "background" and a.held and a.picture]
    return next(
        (a for a in backgrounds if a.name == "main"), backgrounds[0] if backgrounds else None
    )


@dataclass
class GreetingPictures:
    """A greeting as the model reads it, and what it shows."""

    text: str
    # The card's files it shows, in order, each once (CardAsset), or the
    # character's own expression pictures (ImageRef) for `{{emotion::}}`.
    pictures: list[CardAsset | ImageRef] = field(default_factory=list)
    # A background it asks for (`{{bg::name}}`).
    background: CardAsset | None = None


def _key(name: str) -> str:
    return name.strip().casefold()


def _stem(name: str) -> str:
    return _key(PurePosixPath(name.strip()).stem) if "." in name else _key(name)


def greeting_pictures(
    text: str, assets: Sequence[CardAsset], expressions: Sequence[ImageRef] = ()
) -> GreetingPictures:
    """The pictures `text` shows from the card's files (and the character's
    expression pictures), taken out of it."""
    held = [a for a in assets if a.held]
    by_name: dict[str, CardAsset] = {}
    for asset in held:
        by_name.setdefault(_key(asset.name), asset)
    for asset in held:
        by_name.setdefault(_stem(asset.name), asset)
    by_path = {asset.path: asset for asset in held if asset.path}
    found = GreetingPictures(text="")

    def named(name: str) -> CardAsset | None:
        return by_name.get(_key(name)) or by_name.get(_stem(name))

    def show(picture: CardAsset | ImageRef | None) -> None:
        if picture is None:
            return
        if isinstance(picture, CardAsset) and not picture.picture:
            return
        if all(picture is not other for other in found.pictures):
            found.pictures.append(picture)

    def address(src: str) -> CardAsset | None:
        """A file named by its place in the card, or by a macro in an
        attribute (`{{raw::name}}`)."""
        src = src.strip()
        macro = _MACRO.fullmatch(src)
        if macro is not None:
            return named(macro.group(2))
        for scheme in ("embeded://", "embedded://", "__asset:"):
            if src.startswith(scheme):
                return by_path.get(src.removeprefix(scheme))
        return None

    def tag(match: re.Match[str]) -> str:
        asset = address(match.group("src"))
        if asset is None:
            return match.group(0)  # a link, or nothing of the card's: as written
        show(asset)
        return ""

    def markdown(match: re.Match[str]) -> str:
        asset = address(match.group(1))
        if asset is None:
            return match.group(0)
        show(asset)
        return ""

    def macro(match: re.Match[str]) -> str:
        kind, name = match.group(1).lower(), match.group(2)
        if kind == "emotion":
            wanted = _key(name)
            show(next((r for r in expressions if _key(r.expression) == wanted), None))
        elif kind == "bg":
            asset = named(name)
            if asset is not None and asset.picture:
                found.background = asset
        elif kind in _SHOWN:
            show(named(name))
        return ""

    def each(match: re.Match[str]) -> str:
        """In the order the greeting has them: the pictures show so."""
        if match.group("tag") is not None:
            return tag(_HTML_IMG.fullmatch(match.group(0)))
        if match.group("markdown") is not None:
            return markdown(_MARKDOWN_IMG.fullmatch(match.group(0)))
        return macro(_MACRO.fullmatch(match.group(0)))

    text = _ANY.sub(each, text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    found.text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return found
