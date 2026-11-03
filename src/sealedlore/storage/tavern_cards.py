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
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import ValidationError

from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import StyleDirectives
from sealedlore.storage.atomic import atomic_write_json
from sealedlore.storage.image_meta import picture_kind, stripped_or_same
from sealedlore.storage.images import REFS_DIR, write_bytes_atomic
from sealedlore.storage.png_text import PngTextError, text_chunks, with_text
from sealedlore.storage.tavern_lore import (
    LorebookError,
    LoreImport,
    character_book_from_lore,
    json_of,
    looks_like_lorebook,
    lore_from_book,
    read_json,
)

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
class TavernCard:
    """A card as read: its fields (V3's and V2's names; V1's put under them)
    and its picture, with its hidden data removed where that can be done."""

    fields: dict[str, Any]
    picture: bytes | None = None
    spec: str = SPEC_V2
    # Its expression pictures as read (name, bytes), each stripped of hidden
    # data; and what of them couldn't come (engine/looks.py labels them).
    expressions: list[tuple[str, bytes]] = field(default_factory=list)
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
    def picture_labels(self) -> dict[str, tuple[str, str, str]]:
        """A SealedLore card's own labels for its pictures, by asset name:
        (look, expression, caption). Another app's card has none."""
        own = self.extensions.get("sealedlore")
        rows = own.get("pictures") if isinstance(own, Mapping) else None
        labels: dict[str, tuple[str, str, str]] = {}
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, Mapping) and isinstance(row.get("name"), str):
                labels[row["name"]] = tuple(  # type: ignore[assignment]
                    str(row.get(key) or "") for key in ("look", "expression", "caption")
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


def _card_of(data: Any, name: str, picture: bytes | None = None) -> TavernCard:
    found = card_fields(data)
    if found is None:
        raise CardError(f"{name} isn't a character card.")
    fields, spec = found
    card = TavernCard(fields=fields, picture=picture, spec=spec)
    # Outside a CHARX an expression picture can only be named, never held.
    assets = fields.get("assets")
    named = sum(
        1
        for asset in (assets if isinstance(assets, list) else ())
        if isinstance(asset, Mapping) and asset.get("type") == "emotion"
    )
    card.expression_notes = _expression_notes(0, named)
    return card


def _decoded(text: str, name: str) -> Any:
    try:
        return json.loads(base64.b64decode(text.strip(), validate=False).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"{name}'s card data can't be read.") from exc


def _png_card(data: bytes, name: str) -> TavernCard:
    try:
        texts = text_chunks(data)
    except PngTextError as exc:
        raise CardError(f"{name}: {exc}.") from exc
    # V3 first, as SillyTavern reads it: a card written by both has both.
    for keyword in ("ccv3", "chara"):
        if keyword in texts:
            return _card_of(_decoded(texts[keyword], name), name, stripped_or_same(data))
    raise CardError(f"{name} is a picture with no character card in it.")


def _charx_card(source: BinaryIO, name: str) -> TavernCard:
    try:
        with zipfile.ZipFile(source) as archive:
            card = _member(archive, "card.json", name)
            if card is None:
                raise CardError(f"{name} has no card.json in it.")
            try:
                parsed = json.loads(card.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CardError(f"{name}'s card.json isn't valid JSON.") from exc
            result = _card_of(parsed, name)
            result.picture = _charx_icon(archive, result.fields, name)
            result.expressions, result.expression_notes = _charx_expressions(
                archive, result.fields, name
            )
            return result
    except zipfile.BadZipFile as exc:
        raise CardError(f"{name} isn't a CHARX file.") from exc


def _member(archive: zipfile.ZipFile, member: str, name: str) -> bytes | None:
    try:
        info = archive.getinfo(member)
    except KeyError:
        return None
    if info.file_size > MAX_INFLATED:
        raise CardError(f"{name}: {member} is too large.")
    return archive.read(info)


def _embedded(uri: str) -> str | None:
    """The archive path an asset's uri names, or None for one outside it."""
    for scheme in ("embeded://", "embedded://", "__asset:"):
        if uri.startswith(scheme):
            return uri.removeprefix(scheme)
    return None


def _charx_expressions(
    archive: zipfile.ZipFile, fields: Mapping[str, Any], name: str
) -> tuple[list[tuple[str, bytes]], list[str]]:
    """The card's expression pictures (the spec's "emotion" assets) in the
    archive, within MAX_EXPRESSIONS and MAX_EXPRESSION_BYTES; one named by a
    link is never fetched."""
    assets = fields.get("assets")
    found: list[tuple[str, bytes]] = []
    total = left = linked = 0
    for asset in assets if isinstance(assets, list) else ():
        if not isinstance(asset, Mapping) or asset.get("type") != "emotion":
            continue
        member = _embedded(str(asset.get("uri") or ""))
        if member is None:
            linked += 1
            continue
        try:
            info = archive.getinfo(member)
        except KeyError:
            left += 1
            continue
        if len(found) >= MAX_EXPRESSIONS or total + info.file_size > MAX_EXPRESSION_BYTES:
            left += 1
            continue
        data = _member(archive, member, name)
        if not data:
            left += 1
            continue
        total += len(data)
        label = str(asset.get("name") or "").strip() or Path(member).stem
        found.append((label, stripped_or_same(data)))
    return found, _expression_notes(left, linked)


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
        with zipfile.ZipFile(path) as archive:
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
                data = archive.read(info)
                if picture_kind(data) is None:
                    left += 1
                    continue
                total += len(data)
                found.append((Path(info.filename).stem, stripped_or_same(data)))
    except (OSError, zipfile.BadZipFile) as exc:
        raise CardError(f"{Path(path).name} isn't a zip of pictures: {exc}") from exc
    return found, _expression_notes(left, 0)


def _charx_icon(archive: zipfile.ZipFile, fields: Mapping[str, Any], name: str) -> bytes | None:
    """The card's main picture: its "icon" asset (the spec writes
    "embeded://" for a file in the archive), else none."""
    assets = fields.get("assets")
    for asset in assets if isinstance(assets, list) else ():
        if not isinstance(asset, Mapping) or asset.get("type") != "icon":
            continue
        member = _embedded(str(asset.get("uri") or ""))
        if member is not None:
            data = _member(archive, member, name)
            return stripped_or_same(data) if data else None
    return None


def read_card(path: Path) -> TavernCard:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return _card_of(read_json(path), path.name)
        except LorebookError as exc:
            raise CardError(str(exc)) from exc
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
        return _png_card(data, name)
    try:
        parsed = json_of(data, name)
    except LorebookError as exc:
        raise CardError(f"{name} is neither a character card nor a lorebook: {exc}") from exc
    if not lorebook and looks_like_card(parsed):
        return _card_of(parsed, name, stripped_or_same(picture) if picture else None)
    if looks_like_lorebook(parsed):
        return parsed
    raise CardError(f"{name} is neither a character card nor a lorebook.")


def tavern_kind(path: Path) -> str | None:
    """ "card" or "lorebook" for a SillyTavern JSON file, else None. (A PNG
    or a CHARX is a card or nothing: `read_card` says which.)"""
    if Path(path).suffix.lower() != ".json":
        return None
    try:
        data = read_json(path)
    except LorebookError:
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


def character_from_card(card: TavernCard, fill: Callable[[str], str]) -> Character:
    """The card's character as a cast card. A card SealedLore wrote brings
    its own fields back as they were."""
    name = fill(card.name).strip() or "Unnamed"
    description = fill(card.text("description")).strip()
    personality = fill(card.text("personality")).strip()
    depth = card.extensions.get("depth_prompt")
    note = fill(str(depth.get("prompt") or "")).strip() if isinstance(depth, Mapping) else ""
    nickname = fill(card.text("nickname")).strip()
    character = Character(
        name=name,
        aliases=[nickname] if nickname and nickname != name else [],
        summary=first_sentence(description or personality) or None,
        full_description="\n\n".join(
            part
            for part in (description, f"Personality: {personality}" if personality else "", note)
            if part
        )
        or None,
        voice_notes=example_dialogue(fill(card.text("mes_example"))) or None,
    )
    own = card.extensions.get("sealedlore")
    if isinstance(own, Mapping):
        try:
            return Character.model_validate(
                {
                    **character.model_dump(),
                    **{key: own[key] for key in _OWN if key in own},
                    "id": character.id,
                }
            )
        except ValidationError:
            pass  # someone else's edit broke it: the card's own fields stand
    return character


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
    labels: Mapping[str, tuple[str, str, str]] | None = None,
) -> tuple[list[ImageRef], int]:
    """Expression pictures as a character's pictures, labelled
    (`expression_of`, or a SealedLore card's own `labels` by name: look,
    expression and caption): `keep(data, suffix, label)` stores one and
    returns its ref. How many were in a format SealedLore doesn't keep."""
    refs: list[ImageRef] = []
    skipped = 0
    for name, data in expressions:
        suffix = picture_kind(data)
        if suffix is None:
            skipped += 1
            continue
        look, expression, caption = (labels or {}).get(name, ("", expression_of(name), ""))
        ref = keep(data, suffix, expression)
        if ref is not None:
            ref.look = look
            if caption:
                ref.caption = caption
            refs.append(ref)
    return refs, skipped


# The CHARX asset a SealedLore card's portrait is (`card_charx`).
ICON_NAME = "main"


def card_charx(v3: Mapping[str, Any], pictures: Sequence[tuple[ImageRef, bytes]]) -> bytes:
    """A character as a CHARX (a zip of card.json and its pictures): the
    first picture its icon, every other one an "emotion" asset named by its
    expression ("angry", "angry-2" for another), as SillyTavern and RisuAI
    read them. SealedLore's own labels (the look too) ride under
    `extensions.sealedlore.pictures` by asset name, so they come back whole.
    Each picture's hidden data is removed first."""
    data = json.loads(json.dumps(v3["data"]))
    assets: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    members: list[tuple[str, bytes]] = []
    used: dict[str, int] = {}
    for index, (ref, raw) in enumerate(pictures):
        cleaned = stripped_or_same(raw)
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
            {"name": name, "look": ref.look, "expression": ref.expression, "caption": ref.caption}
        )
        members.append((member, cleaned))
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
            look, expression, caption = card.picture_labels.get(ICON_NAME, ("", "", ""))
            portrait.look, portrait.expression = look, expression
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

    greetings = [fill(text).strip() for text in card.greetings]
    if opening is not None and 0 <= opening < len(greetings):
        scenario.opening_text = greetings[opening]
        scenario.opening_mode = "as_written"
        if len(greetings) > 1:
            others = len(greetings) - 1
            notes.append(
                f"{others} other greeting{'s' if others > 1 else ''} on the card "
                f"{'were' if others > 1 else 'was'} not used."
            )
    elif greetings:
        notes.append("The card's greetings were not used: you write the first turn.")

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


def card_data(
    character: Character,
    *,
    story_title: str = "",
    lore: Sequence[LoreEntry] = (),
    picture: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A character as a card: (V2, V3). Its description carries what the
    cast sheet says (the card, the voice, the skills); SealedLore's own
    fields ride under `extensions.sealedlore`. `lore` becomes the card's own
    lorebook; `picture` says the card's PNG is its picture (V3's asset)."""
    description = "\n\n".join(
        part
        for part in (
            (character.full_description or character.summary or "").strip(),
            f"Voice: {character.voice_notes.strip()}" if character.voice_notes else "",
            _skills(character),
        )
        if part
    )
    origin = f"the story {story_title}" if story_title else "a story"
    own = character.model_dump(include=set(_OWN))
    data: dict[str, Any] = {
        "name": character.name,
        "description": description,
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "mes_example": "",
        "creator_notes": f"Exported from SealedLore, from {origin}.",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "tags": [],
        "creator": "",
        "character_version": "",
        "extensions": {"sealedlore": own},
    }
    if lore:
        data["character_book"] = character_book_from_lore(lore, name=story_title)
    v2 = {
        # V1's fields at the top too, as SillyTavern writes them, for readers
        # that know no other.
        **{key: data[key] for key in _V1},
        "tags": [],
        "spec": SPEC_V2,
        "spec_version": "2.0",
        "data": data,
    }
    v3_data = {
        **data,
        "nickname": character.aliases[0] if character.aliases else "",
        "group_only_greetings": [],
        "creator_notes_multilingual": {},
        "source": [],
    }
    if picture:
        v3_data["assets"] = [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}]
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


def write_card(path: Path, v2: Mapping[str, Any], v3: Mapping[str, Any], png: bytes | None) -> None:
    """A .png card with the picture, or a .json card (V2, which every
    reader takes)."""
    path = Path(path)
    if path.suffix.lower() == ".png":
        if png is None:
            raise CardError("a PNG card needs the character's picture")
        write_bytes_atomic(path, card_png(png, v2, v3))
    else:
        atomic_write_json(path, v2)
