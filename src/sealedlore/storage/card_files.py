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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import CardAsset

# The patterns below each stop where what they read must end: a macro at the
# end of its line, a tag at a bracket or a blank line, a Markdown picture at
# a bracket. A card's text is as its maker left it: read on to the next "}}"
# or ">" anywhere, as a browser would, one left unclosed took the prose after
# it out of the greeting, and a greeting of a few hundred unclosed tags took
# seconds.

# RisuAI's (its parser's `assetRegex`, read Nov 4 2026), its name on one
# line and holding no brace.
_MACRO = re.compile(
    r"\{\{(raw|path|img|image|video|audio|bgm|bg|emotion|asset|video-img|source)::([^{}\r\n]+)\}\}",
    re.IGNORECASE,
)
# Space inside a tag: never a blank line, where a broken tag's paragraph ends.
_GAP = r"(?:[ \t\f\r]|\n(?![ \t\f\r]*\n))"
# One of a tag's attributes: a name as HTML's are written, and a value,
# quoted or not, that never holds a bracket. A tag that isn't made of them
# (its ">" written "&gt;") is no tag: it stays in the text, and only a macro
# in it shows its picture.
_ATTRIBUTE = rf"""{_GAP}+[a-z_:][\w:.-]*(?:{_GAP}*={_GAP}*(?:"[^"<>]*"|'[^'<>]*'|[^\s"'<>=`]+))?"""
# The picture's own attribute: `src`, its address quoted, holding neither
# quote nor bracket. The first such one in the tag is the tag's picture.
_SRC = rf"""{_GAP}+src{_GAP}*={_GAP}*(?:"[^"'<>]*"|'[^"'<>]*')"""
# Each attribute is read once and never again (`*+`): the attributes before
# the first `src`, the `src`, the rest. A tag's attributes read only one way,
# so this is the tag a lazy run of attributes before `src` found, but that
# tried every way of splitting them when the tag never closed, and a
# greeting of 64 KB of ` src="x"` after one `<img` took 20 seconds.
_HTML_IMG = re.compile(
    rf"""<img(?:(?!{_SRC}){_ATTRIBUTE})*+{_GAP}+src{_GAP}*={_GAP}*(?P<quote>["'])"""
    rf"""(?P<src>[^"'<>]*)(?P=quote)(?:{_ATTRIBUTE})*+{_GAP}*/?>""",
    re.IGNORECASE,
)
# Its words never hold a bracket, nor its address a parenthesis or a space.
_MARKDOWN_IMG = re.compile(r"""!\[[^\[\]]*\]\(\s*<?([^()\s<>]+)>?(?:\s+"[^"\r\n]*")?\s*\)""")
_SHOWN = ("img", "image", "asset", "emotion", "raw", "path")
_ANY = re.compile(
    f"(?P<tag>{_HTML_IMG.pattern})|(?P<markdown>{_MARKDOWN_IMG.pattern})|(?P<macro>{_MACRO.pattern})",
    re.IGNORECASE,
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


def _lookup(names: Sequence[str]) -> Callable[[str], int | None]:
    """Which of the card's files (by their `names`, in the card's order) a
    macro means by a name: the file named so in any case, else one named
    so but for its extension ("snow" for snow.png);
    failing both, the same for the name without its own extension
    ("snow.jpg" for snow.png). The first file to answer wins."""
    by_name: dict[str, int] = {}
    for index, name in enumerate(names):
        by_name.setdefault(_key(name), index)
    for index, name in enumerate(names):
        by_name.setdefault(_stem(name), index)

    def find(name: str) -> int | None:
        found = by_name.get(_key(name))
        return found if found is not None else by_name.get(_stem(name))

    return find


def greeting_pictures(
    text: str, assets: Sequence[CardAsset], expressions: Sequence[ImageRef] = ()
) -> GreetingPictures:
    """The pictures `text` shows from the card's files (and the character's
    expression pictures), taken out of it."""
    held = [a for a in assets if a.held]
    find = _lookup([asset.name for asset in held])
    by_path = {asset.path: asset for asset in held if asset.path}
    found = GreetingPictures(text="")

    def named(name: str) -> CardAsset | None:
        index = find(name)
        return held[index] if index is not None else None

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
    # Line by line: a pattern for the spaces before a line's end tried again
    # at every space of a long run.
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    found.text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return found


def renamed_in(
    text: str,
    old: str,
    new: str,
    assets: Sequence[CardAsset] = (),
    renamed: CardAsset | None = None,
) -> tuple[str, int]:
    """`text` with RisuAI's macros that name the file `old` naming `new`
    instead, and how many did: a file renamed on the card is still shown by
    the greetings that showed it. A macro names it as `greeting_pictures`
    reads one (any case, with or without its extension). Given the card's
    files (`assets`, `renamed` among them under its new name), only the
    macros that meant that one: "snow" means snow.png, not snow.mp3, when
    the picture comes first."""
    held = [asset for asset in assets if asset.held]
    if renamed is not None and any(asset is renamed for asset in held):
        names = [old if asset is renamed else asset.name for asset in held]
        target = next(index for index, asset in enumerate(held) if asset is renamed)
    else:
        names, target = [old], 0
    find = _lookup(names)
    count = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal count
        if find(match.group(2)) != target:
            return match.group(0)
        count += 1
        return "{{" + match.group(1) + "::" + new + "}}"

    return _MACRO.sub(swap, text), count
