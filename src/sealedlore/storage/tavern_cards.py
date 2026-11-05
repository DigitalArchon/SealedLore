"""SillyTavern character cards: read into SealedLore, and written from it.

A card is the Character Card V2 or V3 spec's JSON (or V1's flat fields): as a
.json file, inside a PNG (base64 in a text chunk, "chara" for V2 and "ccv3"
for V3, the PNG being the character's picture), or in a CHARX zip
(card.json and its pictures). Chub, JanitorAI and the rest share cards in
these shapes.

A card is one character the model plays for the user. Read into SealedLore
it becomes a cast card (or, for a card that is a world or a narrator for
several people, the story's world), with `{{char}}` and `{{user}}` written
out as names. As a new story it also brings its scenario, a greeting as the
opening, its lorebook and its picture (`scenario_from_card`). What a card
holds that SealedLore has nothing for (the card's own system prompt, tags,
other macros) is said, not silently dropped.

Written from SealedLore, a card carries the character's own fields under
`extensions.sealedlore` too, so it comes back whole. Conversions are pure;
`read_card` and `write_card` touch disk.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import struct
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import unquote_to_bytes

from pydantic import ValidationError

from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import OpeningPicture, StyleDirectives
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage.atomic import atomic_write_json
from sealedlore.storage.card_files import background_of, greeting_pictures
from sealedlore.storage.image_meta import (
    MetadataError,
    picture_kind,
    raster_kind,
    strip_metadata,
    stripped_or_same,
)
from sealedlore.storage.images import IMAGES_DIR, REFS_DIR, write_bytes_atomic
from sealedlore.storage.png_text import PngTextError, text_chunks, with_text
from sealedlore.storage.tavern_lore import (
    LorebookError,
    LoreImport,
    character_book_from_lore,
    json_of,
    looks_like_lorebook,
    lore_from_book,
    lorebook_in_picture,
    other_formats,
    read_json,
)

try:
    from lzma import LZMAError
except ImportError:  # a Python built without it reads no LZMA member either
    LZMAError = zipfile.BadZipFile  # type: ignore[misc,assignment]

CARD_SUFFIXES = (".png", ".json", ".charx")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Far past any card (a big PNG card is a few megabytes): something else.
MAX_BYTES = 64 * 1024 * 1024
# A CHARX may carry a whole set of expression pictures (RisuRealm's run past
# MAX_BYTES): only its card.json and its icon are read, from the file as it is.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
# A CHARX's card.json and its picture may not inflate past this.
MAX_INFLATED = 32 * 1024 * 1024
# A card's expression pictures (a CHARX's "emotion" assets, a SillyTavern
# expression pack) are kept to this many, and this much in all: past it the
# rest are counted and said, not read.
MAX_EXPRESSIONS = 120
MAX_EXPRESSION_BYTES = 96 * 1024 * 1024
# A card's other files (backgrounds, pictures its greetings show, sounds):
# RisuAI's run to hundreds, and its CHARX files to tens of megabytes. The
# links among them count too.
MAX_FILES = 1000
MAX_FILE_BYTES = 384 * 1024 * 1024
# Deeper than any card's JSON nests (a lorebook entry's extensions sit a few
# levels down): past it, copying or saving the character recursed too far.
MAX_NESTING = 100
# Half of a UTF-16 pair, as a broken escape in a card's JSON ("\ud800")
# leaves it: no file holds one in UTF-8, so every save after it failed.
_HALF_PAIR = re.compile("[\ud800-\udfff]")
# What reading a damaged zip raises besides BadZipFile: a broken deflate
# stream (zlib.error), a member cut short (EOFError), a way of compressing
# zipfile can't read, deflate64 or AES (NotImplementedError, a
# RuntimeError), an encrypted member (RuntimeError), and what its other
# decompressors and its header readers raise.
_DAMAGED: tuple[type[Exception], ...] = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    EOFError,
    RuntimeError,
    OSError,
    ValueError,
    struct.error,
    LZMAError,
)
SPEC_V2 = "chara_card_v2"
SPEC_V3 = "chara_card_v3"
PORTRAIT_CAPTION = "portrait"
# SillyTavern's expression names (its Character Expressions' classifier),
# which its sprite packs are named by: "joy.png", or "joy-2.png" for another.
SILLYTAVERN_EXPRESSIONS = (
    "admiration",
    "amusement",
    "anger",
    "annoyance",
    "approval",
    "caring",
    "confusion",
    "curiosity",
    "desire",
    "disappointment",
    "disapproval",
    "disgust",
    "embarrassment",
    "excitement",
    "fear",
    "gratitude",
    "grief",
    "joy",
    "love",
    "nervousness",
    "neutral",
    "optimism",
    "pride",
    "realization",
    "relief",
    "remorse",
    "sadness",
    "surprise",
)
# Pygmalion's old field names, and V1's.
_PYGMALION = {
    "char_name": "name",
    "char_persona": "description",
    "world_scenario": "scenario",
    "char_greeting": "first_mes",
    "example_dialogue": "mes_example",
}
_V1 = ("name", "description", "personality", "scenario", "first_mes", "mes_example")
_MACRO = re.compile(r"\{\{[^{}]*\}\}")
# The fields of a SealedLore card that ride in `extensions.sealedlore`.
_OWN = (
    "aliases",
    "summary",
    "full_description",
    "voice_notes",
    "competence",
    "is_player_available",
    "author_only",
    "canon",
    # Their sheet's own rows and columns (engine/look_sheet.py).
    "sheet_looks",
    "sheet_expressions",
)


class CardError(ValueError):
    """A file that isn't a card this version can read, said in plain words."""


@dataclass
class CardFile:
    """One of a card's files (a V3 `assets` entry) other than its main
    picture and the expressions taken: its data when the card holds it
    (stripped, where it is a picture or a video that can be), else the
    card's own `uri` for it, a link never fetched."""

    type: str
    name: str
    ext: str
    path: str = ""
    uri: str = ""
    data: bytes | None = None


@dataclass
class TavernCard:
    """A card as read: its fields (V3's and V2's names; V1's put under them)
    and its picture, with its hidden data removed where that can be done."""

    fields: dict[str, Any]
    picture: bytes | None = None
    spec: str = SPEC_V2
    # Its expression pictures as read (name, bytes), each stripped of hidden
    # data (engine/looks.py labels them); every other file it has (CardFile);
    # and what of its files couldn't come, in words.
    expressions: list[tuple[str, bytes]] = field(default_factory=list)
    files: list[CardFile] = field(default_factory=list)
    expression_notes: list[str] = field(default_factory=list)

    def text(self, key: str) -> str:
        value = self.fields.get(key)
        return value if isinstance(value, str) else ""

    @property
    def name(self) -> str:
        return self.text("name").strip() or "Unnamed"

    @property
    def extensions(self) -> Mapping[str, Any]:
        value = self.fields.get("extensions")
        return value if isinstance(value, Mapping) else {}

    @property
    def picture_labels(self) -> dict[str, tuple[str, str, str, str]]:
        """A SealedLore card's own labels for its pictures, by asset name:
        (look, expression, caption, framing). Another app's card has none."""
        own = self.extensions.get("sealedlore")
        rows = own.get("pictures") if isinstance(own, Mapping) else None
        labels: dict[str, tuple[str, str, str, str]] = {}
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, Mapping) and isinstance(row.get("name"), str):
                labels[row["name"]] = tuple(  # type: ignore[assignment]
                    str(row.get(key) or "") for key in ("look", "expression", "caption", "framing")
                )
        return labels

    @property
    def book(self) -> Mapping[str, Any] | None:
        value = self.fields.get("character_book")
        return value if isinstance(value, Mapping) and value.get("entries") else None

    @property
    def greetings(self) -> list[str]:
        """The first message, then the alternate greetings, the empty left out."""
        others = self.fields.get("alternate_greetings")
        found = [self.text("first_mes")]
        found += (
            [text for text in others if isinstance(text, str)] if isinstance(others, list) else []
        )
        return [text for text in found if text.strip()]

    @property
    def instructions(self) -> str:
        """The card's own system prompt and post-history instructions."""
        return "\n\n".join(
            text.strip()
            for text in (self.text("system_prompt"), self.text("post_history_instructions"))
            if text.strip()
        )


# --- reading ------------------------------------------------------------------------


def card_fields(data: Any) -> tuple[dict[str, Any], str] | None:
    """A card's fields and spec from its JSON, whichever version it is."""
    if not isinstance(data, Mapping):
        return None
    if data.get("spec") in (SPEC_V2, SPEC_V3) and isinstance(data.get("data"), Mapping):
        return dict(data["data"]), str(data["spec"])
    if "char_name" in data:
        return {new: data[old] for old, new in _PYGMALION.items() if old in data}, "v1"
    if isinstance(data.get("name"), str) and any(key in data for key in _V1[1:]):
        return {key: data[key] for key in (*_V1, "tags") if key in data}, "v1"
    return None


def looks_like_card(data: Any) -> bool:
    return card_fields(data) is not None


def _card_of(
    data: Any,
    name: str,
    picture: bytes | None = None,
    member: Callable[[str], bytes | None] | None = None,
    size: Callable[[str], int | None] | None = None,
) -> TavernCard:
    """A card from its fields, its files sized through `size` and read
    through `member` (a CHARX's archive, a PNG's asset chunks; none for
    JSON, whose files can only be data: URLs or links)."""
    data = _mended(data, name)
    found = card_fields(data)
    if found is None:
        raise CardError(f"{name} isn't a character card.")
    fields, spec = found
    card = TavernCard(fields=fields, picture=picture, spec=spec)
    _read_assets(card, _Held(member or (lambda _path: None), size or (lambda _path: None)))
    return card


def _mended(data: Any, name: str) -> Any:
    """A card's (or a lorebook's) JSON as SealedLore can keep it: every half
    of a UTF-16 pair in its text made a replacement character, and refused
    (CardError) when it nests deeper than MAX_NESTING. Walked, never
    recursed into, so a deep one is said rather than crashed on."""

    def text(value: str) -> str:
        return _HALF_PAIR.sub("\N{REPLACEMENT CHARACTER}", value)

    if isinstance(data, str):
        return text(data)
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_NESTING:
            raise _too_deep(name)
        if isinstance(item, dict):
            if any(isinstance(key, str) and _HALF_PAIR.search(key) for key in item):
                mended = {(text(k) if isinstance(k, str) else k): v for k, v in item.items()}
                item.clear()
                item.update(mended)
            children = list(item.items())
        elif isinstance(item, list):
            children = list(enumerate(item))
        else:
            continue
        for key, value in children:
            if isinstance(value, dict | list):
                stack.append((value, depth + 1))
            elif isinstance(value, str) and _HALF_PAIR.search(value):
                item[key] = text(value)
    return data


def _decoded(text: str, name: str) -> Any:
    try:
        return json.loads(base64.b64decode(text.strip(), validate=False).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"{name}'s card data can't be read.") from exc
    except RecursionError as exc:
        raise _too_deep(name) from exc


def _too_deep(name: str) -> CardError:
    return CardError(
        f"{name} is nested far deeper than any card or lorebook is, so it can't be read."
    )


def _card_text(keyword: str) -> bool:
    """A PNG card's own text: the card (V3's and V2's) and the files it
    holds. Nothing else in the picture is inflated or read."""
    return keyword in ("ccv3", "chara") or keyword.startswith("chara-ext-asset_")


def _png_card(data: bytes, name: str) -> TavernCard:
    try:
        texts = text_chunks(data, _card_text)
    except PngTextError as exc:
        raise CardError(f"{name}: {exc}.") from exc

    def chunk(path: str) -> str | None:
        # The spec's "chara-ext-asset_:{path}"; RisuAI has written it
        # without the colon too.
        for keyword in (f"chara-ext-asset_:{path}", f"chara-ext-asset_{path}"):
            if keyword in texts:
                return texts[keyword]
        return None

    def member(path: str) -> bytes | None:
        text = chunk(path)
        try:
            return base64.b64decode(text.strip(), validate=False) if text is not None else None
        except (binascii.Error, ValueError):
            return None

    def size(path: str) -> int | None:
        text = chunk(path)
        return len(text) * 3 // 4 if text is not None else None

    # V3 first, as SillyTavern reads it: a card written by both has both.
    for keyword in ("ccv3", "chara"):
        if keyword in texts:
            parsed = _decoded(texts[keyword], name)
            return _card_of(parsed, name, stripped_or_same(data), member, size)
    raise CardError(f"{name} is a picture with no character card in it.")


def _charx_card(source: BinaryIO, name: str) -> TavernCard:
    try:
        archive = zipfile.ZipFile(source)
    except _DAMAGED as exc:
        raise CardError(f"{name} isn't a CHARX file.") from exc
    with archive:
        card = _member(archive, "card.json", name)
        if card is None:
            raise CardError(f"{name} has no card.json in it.")
        try:
            parsed = json.loads(card.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CardError(f"{name}'s card.json isn't valid JSON.") from exc
        except RecursionError as exc:
            raise _too_deep(name) from exc

        def member(path: str) -> bytes | None:
            try:
                return _member(archive, path, name)
            except CardError:
                return None  # too large or damaged: said with the files not read

        def size(path: str) -> int | None:
            try:
                return archive.getinfo(path).file_size
            except KeyError:
                return None

        return _card_of(parsed, name, member=member, size=size)


def _member(archive: zipfile.ZipFile, member: str, name: str) -> bytes | None:
    """A member's bytes, None when the archive has none by that name;
    CardError when it is too large to read or too damaged to."""
    try:
        info = archive.getinfo(member)
    except KeyError:
        return None
    if info.file_size > MAX_INFLATED:
        raise CardError(f"{name}: {member} is too large.")
    try:
        # zipfile reads no more than the size it was told, and checks it.
        return archive.read(info)
    except _DAMAGED as exc:
        raise CardError(f"{name}: {member} is damaged and can't be read.") from exc


def _embedded(uri: str) -> str | None:
    """The archive path an asset's uri names, or None for one outside it."""
    for scheme in ("embeded://", "embedded://", "__asset:"):
        if uri.startswith(scheme):
            return uri.removeprefix(scheme)
    return None


class _Held:
    """The files a card holds, as its assets name them (`uri`): a path in the
    card, sized by `size` and read by `read` (a CHARX's members, a PNG's
    chunks), or a data: URL. Each is sized before it is read, so a file past
    a limit is never read at all, and read once however many name it: a
    small CHARX naming one large member forty times took seconds and half a
    gigabyte. A link (https://…, ccdefault:) isn't held: never fetched."""

    def __init__(
        self, read: Callable[[str], bytes | None], size: Callable[[str], int | None]
    ) -> None:
        self._read, self._size = read, size
        self._data: dict[tuple[str, str], bytes | None] = {}

    @staticmethod
    def holds(uri: str) -> bool:
        return _embedded(uri) is not None or uri.startswith("data:")

    def size(self, uri: str) -> int | None:
        """How large it is, before it is read; None when the card hasn't it."""
        path = _embedded(uri)
        if path is not None:
            return self._size(path)
        header, _comma, body = uri.partition(",")
        return len(body) * 3 // 4 if ";base64" in header else len(body)

    def data(self, uri: str) -> bytes | None:
        """Its bytes, their hidden data removed where that can be done; None
        when it can't be read."""
        path = _embedded(uri)
        key = ("path", path) if path is not None else ("data", uri)
        if key not in self._data:
            raw = self._read(path) if path is not None else _data_url(uri)
            self._data[key] = stripped_or_same(raw) if raw else None
        return self._data[key]


def _data_url(uri: str) -> bytes | None:
    header, _comma, body = uri.partition(",")
    try:
        if ";base64" in header:
            return base64.b64decode(body, validate=False)
        return unquote_to_bytes(body)
    except (binascii.Error, ValueError):
        return None


def _extension(value: object, path: str = "", data: bytes | None = None) -> str:
    """A file's extension as the spec writes it: lowercase, no dot."""
    ext = str(value or "").strip().lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,10}", ext) or ext == "unknown":
        ext = PurePosixPath(path).suffix.lower().lstrip(".")
    if data is not None and picture_kind(data):
        ext = (picture_kind(data) or "").lstrip(".").replace("jpeg", "jpg")
    return ext if re.fullmatch(r"[a-z0-9]{1,10}", ext) else "bin"


def _read_assets(card: TavernCard, held: _Held) -> None:
    """Every file the card's `assets` names, where SealedLore keeps it: the
    main icon its picture (a PNG card's is the PNG itself), "emotion" files its
    expressions (MAX_EXPRESSIONS, MAX_EXPRESSION_BYTES), everything else (and
    expressions past those) its files (MAX_FILES, MAX_FILE_BYTES), with their
    data where the card holds it and as links where it doesn't (never
    fetched). Each limit is held to before a file is read. The spec asks for
    every file to be kept so the card can go out whole, and for the user to
    be told of any that couldn't be."""
    assets = card.fields.get("assets")
    entries = [a for a in assets if isinstance(a, Mapping)] if isinstance(assets, list) else []
    icons = [a for a in entries if a.get("type") == "icon"]
    main = next((a for a in icons if a.get("name") == "main"), icons[0] if icons else None)
    expression_bytes = file_bytes = unread = left = 0
    linked_picture = False
    for asset in entries:
        kind = str(asset.get("type") or "").strip() or "other"
        label = str(asset.get("name") or "").strip()
        uri = str(asset.get("uri") or "").strip()
        if asset is main:
            if not held.holds(uri):
                # ccdefault: (a PNG card's own picture) or a link.
                linked_picture = card.picture is None and uri != "ccdefault:"
                continue
            if card.picture is None:
                card.picture = held.data(uri)
                if card.picture is None:
                    unread += 1
            continue
        if not held.holds(uri):
            # A link, kept as the card's own and never fetched.
            if len(card.files) >= MAX_FILES:
                left += 1
                continue
            card.files.append(
                CardFile(type=kind, name=label, ext=_extension(asset.get("ext"), uri), uri=uri)
            )
            continue
        size = held.size(uri)
        if not size:
            unread += 1
            continue
        path = _embedded(uri) or ""
        if (
            kind == "emotion"
            and len(card.expressions) < MAX_EXPRESSIONS
            and expression_bytes + size <= MAX_EXPRESSION_BYTES
        ):
            data = held.data(uri)
            if data is None:
                unread += 1
                continue
            expression_bytes += size
            card.expressions.append((label or PurePosixPath(path).stem, data))
            continue
        if len(card.files) >= MAX_FILES or file_bytes + size > MAX_FILE_BYTES:
            left += 1
            continue
        data = held.data(uri)
        if data is None:
            unread += 1
            continue
        file_bytes += size
        card.files.append(
            CardFile(
                type=kind,
                name=label,
                ext=_extension(asset.get("ext"), path or uri, data),
                path=path,
                data=data,
            )
        )
    card.expression_notes = []
    if linked_picture:
        card.expression_notes.append(
            "Its picture is a link, not in the card: nothing is fetched for it."
        )
    if unread:
        card.expression_notes.append(
            f"{unread} of its files couldn't be read, or {'were' if unread > 1 else 'was'} "
            f"larger than {MAX_INFLATED // (1024 * 1024)} MB: "
            f"{'they' if unread > 1 else 'it'} won't go out with the card again."
        )
    if left:
        card.expression_notes.append(
            f"{left} of its files went past what SealedLore keeps of a card ({MAX_FILES} "
            f"files, {MAX_FILE_BYTES // (1024 * 1024)} MB): "
            f"{'they' if left > 1 else 'it'} won't go out with the card again."
        )


def _expression_notes(left: int, linked: int) -> list[str]:
    notes = []
    if left:
        notes.append(
            f"{left} expression picture{'s' if left > 1 else ''} couldn't be read or went past "
            f"what SealedLore takes ({MAX_EXPRESSIONS} pictures, "
            f"{MAX_EXPRESSION_BYTES // (1024 * 1024)} MB)."
        )
    if linked:
        notes.append(
            f"{linked} expression picture{'s are' if linked > 1 else ' is'} a link, not in the "
            "card: nothing is fetched for it."
        )
    return notes


def expressions_from_zip(path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    """A SillyTavern expression pack: a zip of pictures named by expression
    ("joy.png", "joy-2.png"), at any depth. (name, bytes) each, stripped of
    hidden data, within the same limits as a card's; and what was left out.
    CardError when it isn't a zip at all."""
    found: list[tuple[str, bytes]] = []
    total = left = 0
    try:
        archive = zipfile.ZipFile(path)
    except _DAMAGED as exc:
        raise CardError(f"{Path(path).name} isn't a zip of pictures: {exc}") from exc
    with archive:
        for info in archive.infolist():
            if info.is_dir() or Path(info.filename).suffix.lower() not in (
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            ):
                continue
            if (
                len(found) >= MAX_EXPRESSIONS
                or info.file_size > MAX_INFLATED
                or total + info.file_size > MAX_EXPRESSION_BYTES
            ):
                left += 1
                continue
            try:
                data = archive.read(info)
            except _DAMAGED:
                left += 1  # damaged: the rest of the pack is read
                continue
            if picture_kind(data) is None:
                left += 1
                continue
            total += len(data)
            found.append((Path(info.filename).stem, stripped_or_same(data)))
    return found, _expression_notes(left, 0)


def read_card(path: Path) -> TavernCard:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            parsed = read_json(path)
        except LorebookError as exc:
            raise CardError(str(exc)) from exc
        except RecursionError as exc:
            raise _too_deep(path.name) from exc
        return _card_of(parsed, path.name)
    if suffix not in CARD_SUFFIXES:
        raise CardError(f"{path.name} isn't a card: a card is a .png, .json or .charx file.")
    limit = MAX_BYTES if suffix == ".png" else MAX_ARCHIVE_BYTES
    try:
        if path.stat().st_size > limit:
            raise CardError(f"{path.name} is too large to be a card.")
        if suffix == ".charx":
            with path.open("rb") as file:
                return _charx_card(file, path.name)
        data = path.read_bytes()
    except OSError as exc:
        raise CardError(f"Couldn't read {path}: {exc.strerror or exc}") from exc
    return _png_card(data, path.name)


def tavern_from_file(
    file: BinaryIO, name: str, picture: bytes | None = None, *, lorebook: bool = False
) -> TavernCard | Mapping[str, Any]:
    """What a file holds, told by its bytes rather than a name (a link's may
    say nothing): a card, as a PNG, a CHARX zip (one with a picture before it
    too, as RisuRealm's) or JSON, with `picture` beside a JSON card built from
    a site's fields; or a lorebook, as its JSON. `lorebook` when the source
    said that is what it is. `file` is read from its start and may be large
    only as a CHARX, which is read in place. CardError, in words, for
    anything else."""
    file.seek(0, io.SEEK_END)
    size = file.tell()
    file.seek(0)
    png = file.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    file.seek(0)
    if not lorebook and not png and size <= MAX_ARCHIVE_BYTES and zipfile.is_zipfile(file):
        file.seek(0)
        return _charx_card(file, name)
    if size > MAX_BYTES:
        raise CardError(f"{name} is too large to be a card.")
    file.seek(0)
    data = file.read()
    if not lorebook and png:
        try:
            return _png_card(data, name)
        except CardError as not_a_card:
            # NovelAI keeps a lorebook in a picture (`naidata`): by a link, one
            # was taken for a card that isn't there.
            try:
                found = lorebook_in_picture(data, name)
            except LorebookError:
                raise not_a_card from None
            if not other_formats(found):
                raise
            return _mended(found, name)
    try:
        parsed = json_of(data, name)
    except LorebookError as exc:
        raise CardError(f"{name} is neither a character card nor a lorebook: {exc}") from exc
    except RecursionError as exc:
        raise _too_deep(name) from exc
    if other_formats(parsed):
        # Another app's lorebook, which SillyTavern converts: an Agnai book
        # with a description read as an old card.
        return _mended(parsed, name)
    if not lorebook and looks_like_card(parsed):
        return _card_of(parsed, name, stripped_or_same(picture) if picture else None)
    if looks_like_lorebook(parsed):
        return _mended(parsed, name)
    raise CardError(f"{name} is neither a character card nor a lorebook.")


def tavern_kind(path: Path) -> str | None:
    """ "card" or "lorebook" for a SillyTavern JSON file, else None. (A PNG
    or a CHARX is a card or nothing: `read_card` says which.)"""
    if Path(path).suffix.lower() != ".json":
        return None
    try:
        data = read_json(path)
    except (LorebookError, RecursionError):
        return None
    if looks_like_card(data):
        return "card"
    return "lorebook" if looks_like_lorebook(data) else None


def is_card_file(path: Path) -> bool:
    """Whether a file holds a card, without saying why not."""
    try:
        read_card(path)
    except CardError:
        return False
    return True


# --- into SealedLore ---------------------------------------------------------------


def macro_filler(char: str, user: str | None) -> Callable[[str], str]:
    """`{{char}}` (and the old `<BOT>`) as `char`, `{{user}}` (`<USER>`) as
    `user`; with no `user`, `{{user}}` stays as written."""

    def fill(text: str) -> str:
        text = re.sub(r"\{\{\s*char\s*\}\}|<BOT>", lambda _m: char, text, flags=re.I)
        if user:
            text = re.sub(r"\{\{\s*user\s*\}\}|<USER>", lambda _m: user, text, flags=re.I)
        return text

    return fill


def unread_macros(texts: Sequence[str]) -> int:
    """Macros still in `texts` after the names went in: SealedLore reads none."""
    return sum(len(_MACRO.findall(text)) for text in texts)


def first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    match = re.search(r"(.+?[.!?])(?=\s|$)", text)
    sentence = match.group(1) if match else text
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit].rsplit(" ", 1)[0] + "…"


def example_dialogue(text: str) -> str:
    """A card's example messages as voice notes: the `<START>` markers that
    separate them in SillyTavern taken out, the exchanges kept apart."""
    parts = re.split(r"<START>", text.replace("\r\n", "\n"), flags=re.I)
    return "\n\n".join(part.strip() for part in parts if part.strip())


def _number(value: object) -> int | None:
    """A V3 date (seconds since 1970); None for anything else a card holds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def tavern_fields(card: TavernCard, fill: Callable[[str], str]) -> TavernFields:
    """What the card holds that a story's card has no place for, its text
    with the names in as `fill` puts them. The note SillyTavern puts at a
    depth in the chat is the description's (`character_from_card`), and
    SealedLore's own fields are the character's: neither is kept twice."""
    fields = card.fields

    def strings(key: str) -> list[str]:
        value = fields.get(key)
        return [text for text in value if isinstance(text, str)] if isinstance(value, list) else []

    def texts(key: str) -> list[str]:
        return [fill(text) for text in strings(key)]

    multilingual = fields.get("creator_notes_multilingual")
    book = fields.get("character_book")
    return TavernFields(
        scenario=fill(card.text("scenario")),
        first_mes=fill(card.text("first_mes")),
        alternate_greetings=texts("alternate_greetings"),
        group_only_greetings=texts("group_only_greetings"),
        mes_example=fill(card.text("mes_example")),
        system_prompt=fill(card.text("system_prompt")),
        post_history_instructions=fill(card.text("post_history_instructions")),
        creator_notes=fill(card.text("creator_notes")),
        creator_notes_multilingual=dict(multilingual) if isinstance(multilingual, Mapping) else {},
        creator=card.text("creator"),
        character_version=card.text("character_version"),
        tags=strings("tags"),
        source=strings("source"),
        creation_date=_number(fields.get("creation_date")),
        modification_date=_number(fields.get("modification_date")),
        character_book=dict(book) if isinstance(book, Mapping) else None,
        extensions={
            key: value
            for key, value in card.extensions.items()
            if key not in ("sealedlore", "depth_prompt")
        },
    )


def character_from_card(
    card: TavernCard, fill: Callable[[str], str], *, for_story: bool = True
) -> Character:
    """The card's character as a cast card, with the rest of the card on it
    (`tavern_fields`). A card SealedLore wrote brings its own fields back as
    they were. `for_story`: the example messages are its voice, as a story's
    storyteller reads them; kept in the author's own characters
    (storage/collection.py) they stay where the card has them, and a story
    takes its voice from them when the character joins it."""
    name = fill(card.name).strip() or "Unnamed"
    description = fill(card.text("description")).strip()
    personality = fill(card.text("personality")).strip()
    depth = card.extensions.get("depth_prompt")
    note = fill(str(depth.get("prompt") or "")).strip() if isinstance(depth, Mapping) else ""
    nickname = fill(card.text("nickname")).strip()
    examples = example_dialogue(fill(card.text("mes_example"))) if for_story else ""
    character = Character(
        name=name,
        aliases=[nickname] if nickname and nickname != name else [],
        summary=first_sentence(description or personality) or None,
        full_description="\n\n".join(part for part in (description, note) if part) or None,
        personality=personality or None,
        voice_notes=examples or None,
        tavern=tavern_fields(card, fill),
    )
    own = card.extensions.get("sealedlore")
    if isinstance(own, Mapping):
        try:
            return Character.model_validate(
                {
                    **character.model_dump(),
                    **{key: _filled(own[key], fill) for key in _OWN if key in own},
                    "id": character.id,
                }
            )
        except ValidationError:
            pass  # someone else's edit broke it: the card's own fields stand
    return character


def _filled(value: Any, fill: Callable[[str], str]) -> Any:
    """A SealedLore card's own field with the names in: one written from the
    author's own characters keeps `{{char}}` and `{{user}}` as they are there
    (storage/collection.py)."""
    if isinstance(value, str):
        return fill(value)
    if isinstance(value, list):
        return [_filled(item, fill) for item in value]
    if isinstance(value, Mapping):
        return {key: _filled(item, fill) for key, item in value.items()}
    return value


@dataclass
class CardImport:
    """A card read into SealedLore, and what it held that didn't come along."""

    scenario: Scenario
    notes: list[str] = field(default_factory=list)


def _picture_ref(card: TavernCard, scenario: Scenario, caption: str) -> ImageRef | None:
    """The card's picture among the scenario's files, or None when it has
    none SealedLore keeps (PNG, JPEG and WebP)."""
    data = card.picture
    suffix = picture_kind(data) if data else None
    if data is None or suffix is None:
        return None
    ref_id = new_id()
    ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}", caption=caption)
    scenario.reference_files[ref.file] = base64.b64encode(data).decode("ascii")
    return ref


def expression_of(name: str) -> str:
    """An expression picture's name read as its label: SillyTavern's
    "joy-2" is another "joy"; anything else as written, its underscores
    spaces."""
    stem = name.strip().rsplit("/", 1)[-1]
    stem = re.sub(r"\.(png|jpe?g|webp|gif)$", "", stem, flags=re.I)
    lowered = stem.lower()
    for known in SILLYTAVERN_EXPRESSIONS:
        if lowered == known or re.fullmatch(rf"{known}[-._ ].+", lowered):
            return known
    return " ".join(stem.replace("_", " ").split())


def expression_refs(
    expressions: Sequence[tuple[str, bytes]],
    keep: Callable[[bytes, str, str], ImageRef | None],
    labels: Mapping[str, tuple[str, str, str, str]] | None = None,
) -> tuple[list[ImageRef], int]:
    """Expression pictures as a character's pictures, labelled
    (`expression_of`, or a SealedLore card's own `labels` by name: look,
    expression, caption and framing): `keep(data, suffix, label)` stores one
    and returns its ref, or None when it won't keep it. How many were in a
    format SealedLore doesn't keep, or weren't kept."""
    refs: list[ImageRef] = []
    skipped = 0
    for name, data in expressions:
        suffix = picture_kind(data)
        if suffix is None:
            skipped += 1
            continue
        look, expression, caption, framing = (labels or {}).get(
            name, ("", expression_of(name), "", "")
        )
        ref = keep(data, suffix, expression)
        if ref is None:
            skipped += 1
            continue
        ref.look, ref.framing = look, framing
        if caption:
            ref.caption = caption
        refs.append(ref)
    return refs, skipped


# The CHARX asset a SealedLore card's portrait is (`card_charx`).
ICON_NAME = "main"


def card_charx(
    v3: Mapping[str, Any],
    pictures: Sequence[tuple[ImageRef, bytes]],
    files: Sequence[tuple[CardAsset, bytes | None]] = (),
    *,
    shared: bool = True,
) -> bytes:
    """A character as a CHARX (a zip of card.json and its pictures): the
    first picture its icon, every other one an "emotion" asset named by its
    expression ("angry", "angry-2" for another), as SillyTavern and RisuAI
    read them. SealedLore's own labels (the look too) ride under
    `extensions.sealedlore.pictures` by asset name, so they come back whole.
    Each picture's hidden data is removed first. `files`: the card's other
    files (CardAsset, with its bytes, None for a link), back under their own
    type, name and path, so a card read in goes out whole.

    `shared`: the card leaves this computer (an export), so a picture whose
    hidden data can't be removed stops it (CardError), as it stops a
    scenario file. A copy kept here (the Trash's, or one read back to make a
    story) takes every file as it is."""
    data = json.loads(json.dumps(v3["data"]))
    assets: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    members: list[tuple[str, bytes]] = []
    # The portrait is the icon named "main": no other picture may take its
    # name, or the two would share a row of labels and the portrait come
    # back with the other's.
    used: dict[str, int] = {ICON_NAME: 1}
    for index, (ref, raw) in enumerate(pictures):
        cleaned = _outgoing(raw, f"their picture {index + 1}", shared)
        suffix = picture_kind(cleaned) or ".png"
        ext = suffix.lstrip(".")
        if index == 0:
            name, kind = ICON_NAME, "icon"
        else:
            base = re.sub(r"[^a-z0-9]+", "_", (ref.expression or ref.look or "picture").lower())
            base = base.strip("_") or "picture"
            used[base] = used.get(base, 0) + 1
            name, kind = (base if used[base] == 1 else f"{base}-{used[base]}"), "emotion"
        member = f"assets/{kind}/images/{index + 1}{suffix}"
        assets.append({"type": kind, "uri": f"embeded://{member}", "name": name, "ext": ext})
        rows.append(
            {
                "name": name,
                "look": ref.look,
                "expression": ref.expression,
                "caption": ref.caption,
                "framing": ref.framing,
            }
        )
        members.append((member, cleaned))
    taken = {member for member, _raw in members} | {"card.json"}
    for asset, raw in files:
        entry = {"type": asset.type, "name": asset.name, "ext": asset.ext}
        if raw is None:
            if not asset.uri:
                continue  # its file was lost
            assets.append({**entry, "uri": asset.uri})
            continue
        # Its own path wherever that is safe, so what names it by its place
        # (a greeting's `<img src="embeded://barn.png">`) still finds it in
        # the card read back.
        if _safe_member(asset.path) and asset.path not in taken:
            member = asset.path
        else:
            member = _new_member(asset, taken)
        taken.add(member)
        assets.append({**entry, "uri": f"embeded://{member}"})
        members.append((member, _outgoing(raw, f"the file {asset.name or asset.type}", shared)))
    data["assets"] = assets
    extensions = dict(data.get("extensions") or {})
    own = dict(extensions.get("sealedlore") or {})
    own["pictures"] = rows
    extensions["sealedlore"] = own
    data["extensions"] = extensions
    card = {"spec": SPEC_V3, "spec_version": "3.0", "data": data}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("card.json", json.dumps(card, ensure_ascii=False, indent=1))
        for member, raw in members:
            archive.writestr(member, raw, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


def _plural(count: int, what: str) -> str:
    return f"{count} {what}{'' if count == 1 else 's'}"


def _outgoing(data: bytes, what: str, shared: bool) -> bytes:
    """One of a card's files as it goes out in a CHARX: a picture with its
    hidden data removed (told by its bytes; CardError when that can't be
    done and the card is `shared`), a video blanked, anything else as it
    is."""
    if shared and raster_kind(data) is not None:
        try:
            return strip_metadata(data)
        except MetadataError as exc:
            raise CardError(
                f"{what[:1].upper()}{what[1:]} couldn't have its hidden data removed ({exc}), "
                "so nothing was written. Take it off the card, or put on another copy of it."
            ) from exc
    return stripped_or_same(data)


def _safe_member(path: str) -> bool:
    """A path a card's file may be written back under in a CHARX: relative,
    inside the archive, every part of it one any file system can hold
    (extracted, nothing climbs out or lands elsewhere), never card.json. At
    the top or in any script, as the card had it."""
    if not path or path == "card.json" or path.startswith("/") or "\\" in path:
        return False
    return all(_safe_part(part) for part in path.split("/"))


def _safe_part(part: str) -> bool:
    try:
        size = len(part.encode("utf-8"))
    except UnicodeEncodeError:  # half a UTF-16 pair: no zip can name it
        return False
    return (
        0 < size <= 255
        and part not in (".", "..")
        and not part.endswith((".", " "))
        and not any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)
    )


def _new_member(asset: CardAsset, taken: set[str]) -> str:
    """A place in a CHARX for a file that has no safe one of its own (one
    added here, a JSON card's data: URL): under its type and name, every
    part made of letters, digits, "_" and "-" alone, numbered only when
    another file has the place, so it is the same from one export to the
    next."""

    def part(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:64]

    kind = part(asset.type) or "other"
    ext = re.sub(r"[^a-z0-9]+", "", asset.ext.lower())[:10] or "bin"
    folder = "images" if ext in ("png", "jpg", "jpeg", "webp", "gif") else "other"
    stem = part(asset.name) or "file"
    member, number = f"assets/{kind}/{folder}/{stem}.{ext}", 2
    while member in taken:
        member, number = f"assets/{kind}/{folder}/{stem}-{number}.{ext}", number + 1
    return member


def scenario_from_card(
    card: TavernCard,
    *,
    player: str,
    about_player: str = "",
    opening: int | None = 0,
    world: bool = False,
    instructions: bool = False,
    expressions: bool = True,
) -> CardImport:
    """A new story from a card: `player` is the author's character (the
    card's `{{user}}`), `opening` which greeting opens it (None: the author
    writes the first turn), `world` that the card is a world or a narrator
    for several people rather than one character, `instructions` that the
    card's own system prompt goes into the style's notes, `expressions` that
    its expression pictures come too (labelled, for its character's looks)."""
    fill = macro_filler(card.name, player)
    notes: list[str] = []
    you = Character(name=player, summary=about_player.strip() or None)
    scenario = Scenario(
        title=card.name,
        description=fill(card.text("creator_notes")).strip() or None,
        style=StyleDirectives(),
    )
    setting = fill(card.text("scenario")).strip()
    if world:
        about = "\n\n".join(
            part
            for part in (
                fill(card.text("description")).strip(),
                fill(card.text("personality")).strip(),
                setting,
            )
            if part
        )
        scenario.world_bible = about or None
        scenario.cast = [you]
        background = _picture_ref(card, scenario, "")
        if background is not None:
            # A world's picture is its cover: behind the story, not a face.
            scenario.background_image = background.file
        if card.text("mes_example").strip():
            notes.append("The card's example messages were left out: they belong to one character.")
        if card.expressions:
            notes.append(
                f"The card's {len(card.expressions)} expression pictures were left out: they "
                "belong to one character."
            )
    else:
        character = character_from_card(card, fill)
        portrait = _picture_ref(card, scenario, PORTRAIT_CAPTION)
        if portrait is not None:
            look, expression, caption, framing = card.picture_labels.get(
                ICON_NAME, ("", "", "", "")
            )
            portrait.look, portrait.expression, portrait.framing = look, expression, framing
            portrait.caption = caption or portrait.caption
            character.reference_images.append(portrait)
        if card.expressions and expressions:

            def keep(data: bytes, suffix: str, label: str) -> ImageRef:
                ref_id = new_id()
                ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}", expression=label)
                scenario.reference_files[ref.file] = base64.b64encode(data).decode("ascii")
                return ref

            refs, skipped = expression_refs(card.expressions, keep, card.picture_labels)
            character.reference_images += refs
            if skipped:
                notes.append(
                    f"{skipped} expression picture{'s were' if skipped > 1 else ' was'} in a "
                    "format SealedLore doesn't keep."
                )
        elif card.expressions:
            notes.append(f"The card's {len(card.expressions)} expression pictures were left out.")
        notes += card.expression_notes
        scenario.world_bible = setting or None
        scenario.cast = [character, you]
    if (
        card.picture is not None
        and not any(
            ref.caption == PORTRAIT_CAPTION for c in scenario.cast for ref in c.reference_images
        )
        and not scenario.background_image
    ):
        notes.append(
            "The card's picture is in a format SealedLore doesn't keep, so it was left out."
        )
    scenario.starting_scene = SceneState(
        present_character_ids=[character.id for character in scenario.cast]
    )
    scenario.suggested_character_id = you.id

    # The card's other files: its background behind the story, and the
    # pictures its greeting shows under the opening (storage/card_files.py).
    held: dict[str, bytes] = {}
    assets: list[CardAsset] = []
    for found in card.files:
        if found.data is not None:
            asset = CardAsset(type=found.type, name=found.name, ext=found.ext, path=found.path)
            asset.file = f"{IMAGES_DIR}/{new_id()}.{found.ext or 'bin'}"
            held[asset.file] = found.data
            assets.append(asset)
    background = None
    greetings = [fill(text).strip() for text in card.greetings]
    if opening is not None and 0 <= opening < len(greetings):
        expressions_shown = [] if world else scenario.cast[0].reference_images
        shown = greeting_pictures(greetings[opening], assets, expressions_shown)
        scenario.opening_text = shown.text
        background = shown.background
        for picture in shown.pictures:
            if isinstance(picture, CardAsset):
                scenario.reference_files[picture.file] = base64.b64encode(
                    held[picture.file]
                ).decode("ascii")
            scenario.opening_pictures.append(
                OpeningPicture(
                    file=picture.file,
                    from_card=card.name,
                    card_file=picture.name
                    if isinstance(picture, CardAsset)
                    else picture.expression,
                )
            )
        if scenario.opening_pictures:
            count = len(scenario.opening_pictures)
            notes.append(
                f"The {_plural(count, 'picture')} its greeting shows "
                f"{'are' if count > 1 else 'is'} under the opening."
            )
        scenario.opening_mode = "as_written"
        if len(greetings) > 1:
            others = len(greetings) - 1
            notes.append(
                f"{others} other greeting{'s' if others > 1 else ''} on the card "
                f"{'were' if others > 1 else 'was'} not used."
            )
    elif greetings:
        notes.append("The card's greetings were not used: you write the first turn.")

    background = background or background_of(assets)
    if background is not None and not scenario.background_image:
        scenario.background_image = f"{IMAGES_DIR}/background.{background.ext}"
        scenario.reference_files[scenario.background_image] = base64.b64encode(
            held[background.file]
        ).decode("ascii")
        notes.append("The card's background is behind the story.")
    if card.files:
        notes.append("Its other files stay with the card in Your characters.")

    if card.instructions:
        if instructions:
            scenario.style.notes = fill(card.instructions)
        else:
            notes.append(
                "The card's own instructions (its system prompt) were left out: SealedLore's "
                "storyteller rules stand."
            )
    if card.book is not None:
        imported = lore_from_book(card.book, fill)
        scenario.lore = imported.entries
        notes += [f"Lorebook: left out {note}." for note in imported.notes()]
    tags = card.fields.get("tags")
    if isinstance(tags, list) and tags:
        notes.append("The card's tags were left out: SealedLore keeps none.")
    left = unread_macros(
        [
            scenario.world_bible or "",
            scenario.opening_text,
            *(c.full_description or "" for c in scenario.cast),
            *(c.personality or "" for c in scenario.cast),
            *(c.voice_notes or "" for c in scenario.cast),
            *(entry.content for entry in scenario.lore),
        ]
    )
    if left:
        notes.append(
            f"{left} macro{'s' if left > 1 else ''} other than {{{{char}}}} and {{{{user}}}} "
            "stay as written: SealedLore doesn't read them."
        )
    return CardImport(scenario=scenario, notes=notes)


def lore_from_card(card: TavernCard, fill: Callable[[str], str]) -> LoreImport:
    """A card's own lorebook as lore entries (none when it has none)."""
    return lore_from_book(card.book, fill) if card.book is not None else LoreImport()


# --- out of SealedLore -------------------------------------------------------------


def _skills(character: Character) -> str:
    tiers = ", ".join(f"{skill} ({tier})" for skill, tier in character.competence.tiers.items())
    parts = [part for part in (tiers, character.competence.notes.strip()) if part]
    return "Skills: " + "; ".join(parts) if parts else ""


def _seconds(when: str) -> int | None:
    """An ISO time as the V3 spec's seconds since 1970."""
    try:
        return int(datetime.fromisoformat(when).timestamp())
    except (TypeError, ValueError):
        return None


def card_data(
    character: Character,
    *,
    story_title: str = "",
    lore: Sequence[LoreEntry] = (),
    picture: bool = False,
    origin: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A character as a card: (V2, V3). Its description carries what the
    cast sheet says (the card, the voice, the skills); SealedLore's own
    fields ride under `extensions.sealedlore`, and the rest of a card it
    came from (`Character.tavern`) goes back where the card had it. `lore`
    becomes the card's own lorebook (else the card's own book, if it had
    one); `picture` says the card's PNG is its picture (V3's asset).
    `origin`: where it was exported from, in the card's notes when it has
    none of its own ("a story", or the story's title, by default)."""
    tavern = character.tavern or TavernFields()
    voice = (character.voice_notes or "").strip()
    if voice and voice == example_dialogue(tavern.mes_example):
        voice = ""  # read from the card's own examples, which go back as they were
    description = "\n\n".join(
        part
        for part in (
            (character.full_description or character.summary or "").strip(),
            f"Voice: {voice}" if voice else "",
            _skills(character),
        )
        if part
    )
    origin = origin or (f"the story {story_title}" if story_title else "a story")
    own = character.model_dump(include=set(_OWN))
    data: dict[str, Any] = {
        "name": character.name,
        "description": description,
        "personality": (character.personality or "").strip(),
        "scenario": tavern.scenario,
        "first_mes": tavern.first_mes,
        "mes_example": tavern.mes_example,
        "creator_notes": tavern.creator_notes or f"Exported from SealedLore, from {origin}.",
        "system_prompt": tavern.system_prompt,
        "post_history_instructions": tavern.post_history_instructions,
        "alternate_greetings": list(tavern.alternate_greetings),
        "tags": list(tavern.tags),
        "creator": tavern.creator,
        "character_version": tavern.character_version,
        "extensions": {**tavern.extensions, "sealedlore": own},
    }
    if lore:
        data["character_book"] = character_book_from_lore(lore, name=story_title)
    elif tavern.character_book:
        data["character_book"] = tavern.character_book
    v2 = {
        # V1's fields at the top too, as SillyTavern writes them, for readers
        # that know no other.
        **{key: data[key] for key in _V1},
        "tags": list(tavern.tags),
        "spec": SPEC_V2,
        "spec_version": "2.0",
        "data": data,
    }
    v3_data = {
        **data,
        "nickname": character.aliases[0] if character.aliases else "",
        "group_only_greetings": list(tavern.group_only_greetings),
        "creator_notes_multilingual": dict(tavern.creator_notes_multilingual),
        "source": list(tavern.source),
    }
    for key in ("creation_date", "modification_date"):
        if getattr(tavern, key) is not None:
            v3_data[key] = getattr(tavern, key)
    kept = character.collection
    if kept is not None:
        # Changed in Your characters since it was kept: the card says when,
        # so whoever it is shared with can tell it from the one it came from.
        added, changed = _seconds(kept.added_at), _seconds(kept.changed_at)
        if tavern.creation_date is None and not kept.came_from and added is not None:
            v3_data["creation_date"] = added
        if changed is not None and added is not None and changed > added:
            v3_data["modification_date"] = max(changed, tavern.modification_date or 0)
    links = [
        {"type": a.type, "uri": a.uri, "name": a.name, "ext": a.ext}
        for a in tavern.assets
        if not a.held and a.uri
    ]
    if picture:
        v3_data["assets"] = [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}]
        v3_data["assets"] += links
    elif links:
        v3_data["assets"] = links
    v3 = {"spec": SPEC_V3, "spec_version": "3.0", "data": v3_data}
    return v2, v3


def card_png(png: bytes, v2: Mapping[str, Any], v3: Mapping[str, Any]) -> bytes:
    """The picture (a PNG, its hidden data already removed) with the card in
    it, both versions, as SillyTavern writes them."""

    def encoded(data: Mapping[str, Any]) -> str:
        return base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode(
            "ascii"
        )

    return with_text(png, {"chara": encoded(v2), "ccv3": encoded(v3)})


def json_card(v3: Mapping[str, Any]) -> dict[str, Any]:
    """A card as a .json file: V3, as a CHARX's card.json and a PNG's ccv3
    chunk have it (V2 had no place for a card's group greetings, its sources,
    its notes in other languages, its dates or its nickname, and they were
    lost), with only its links among its assets, since a JSON card holds no
    files. V1's fields ride at the top too, as SillyTavern writes them, for
    readers that know no other."""
    data = dict(v3["data"])

    def linked(asset: Any) -> bool:
        uri = str(asset.get("uri") or "") if isinstance(asset, Mapping) else ""
        return bool(uri) and not _Held.holds(uri) and uri != "ccdefault:"

    links = [asset for asset in data.pop("assets", None) or [] if linked(asset)]
    if links:
        data["assets"] = links
    return {
        **{key: data.get(key, "") for key in _V1},
        "tags": list(data.get("tags") or []),
        "spec": SPEC_V3,
        "spec_version": "3.0",
        "data": data,
    }


def write_card(path: Path, v2: Mapping[str, Any], v3: Mapping[str, Any], png: bytes | None) -> None:
    """A .png card with the picture (its hidden data removed, or CardError),
    or a .json card (`json_card`)."""
    path = Path(path)
    if path.suffix.lower() == ".png":
        if png is None:
            raise CardError("a PNG card needs the character's picture")
        try:
            png = strip_metadata(png)
        except MetadataError as exc:
            raise CardError(f"their picture couldn't have its hidden data removed ({exc})") from exc
        if picture_kind(png) != ".png":
            raise CardError("a PNG card's picture must be a PNG")
        write_bytes_atomic(path, card_png(png, v2, v3))
    else:
        atomic_write_json(path, json_card(v3))
