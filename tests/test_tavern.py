"""SillyTavern character cards and lorebooks, read into SealedLore and written
from it (storage/tavern_cards.py, storage/tavern_lore.py, storage/png_text.py)."""

from __future__ import annotations

import base64
import io
import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from sealedlore.engine.rules import render_cast_sheet
from sealedlore.models.character import Character, Competence
from sealedlore.models.lore import LoreEntry
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage import collection, png_text, tavern_cards
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import DiskPictures
from sealedlore.storage.png_text import text_chunks, with_text
from sealedlore.storage.repository import save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario
from sealedlore.storage.tavern_cards import (
    CardError,
    TavernCard,
    card_data,
    card_png,
    character_from_card,
    expression_of,
    expressions_from_zip,
    is_card_file,
    macro_filler,
    read_card,
    scenario_from_card,
    tavern_from_file,
    tavern_kind,
    write_card,
)
from sealedlore.storage.tavern_lore import (
    LorebookError,
    book_from_lore,
    character_book_from_lore,
    is_pattern,
    looks_like_lorebook,
    lore_from_book,
    read_lorebook,
    write_lorebook,
)


def tiny_png() -> bytes:
    """A 1x1 PNG, made by hand: storage has no Qt."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")

    header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


ELENA = {
    "name": "Elena",
    "description": "{{char}} keeps the farmhouse north of Hellsville. She trusts {{user}} slowly.",
    "personality": "wary, dry, loyal once won",
    "scenario": "Four winters after the end of the world, {{user}} reaches the farmhouse.",
    "first_mes": '{{char}} lowers the rifle an inch. "Who sent you, {{user}}?"',
    "mes_example": "<START>\n{{user}}: Hello?\n{{char}}: Hands where I can see them.\n<START>\n"
    "{{char}}: Cellar's full of the dead.",
    "creator_notes": "A slow-burn survival story.",
    "system_prompt": "You are {{char}}. Never break character.",
    "post_history_instructions": "",
    "alternate_greetings": ["The door is barred. {{char}} speaks through it.", "  "],
    "tags": ["survival"],
    "creator": "someone",
    "character_version": "1",
    "extensions": {"depth_prompt": {"prompt": "{{char}} never says her surname.", "depth": 4}},
    "character_book": {
        "entries": [
            {
                "keys": ["Hellsville", "the town"],
                "content": "Hellsville is walled; {{user}} is not welcome there.",
                "enabled": True,
                "insertion_order": 100,
                "comment": "Hellsville",
                "extensions": {},
            }
        ]
    },
}


def v2(fields: dict | None = None) -> dict:
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": dict(fields or ELENA)}


def png_card(tmp_path: Path, chunks: dict[str, dict]) -> Path:
    encoded = {
        key: base64.b64encode(json.dumps(value).encode()).decode() for key, value in chunks.items()
    }
    path = tmp_path / "card.png"
    path.write_bytes(with_text(tiny_png(), encoded))
    return path


# --- PNG text -----------------------------------------------------------------------


def test_text_goes_into_a_png_and_comes_back_out():
    data = with_text(tiny_png(), {"chara": "abc", "other": "x"})
    assert text_chunks(data) == {"chara": "abc", "other": "x"}
    # In place of the old one, not beside it.
    again = with_text(data, {"chara": "def"})
    assert text_chunks(again) == {"other": "x", "chara": "def"}


def png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")


def with_chunks(png: bytes, *chunks: bytes) -> bytes:
    """`png` with `chunks` just before its end (its last twelve bytes)."""
    return png[:-12] + b"".join(chunks) + png[-12:]


# A megabyte of text in a kilobyte, as a compressed chunk.
INFLATES = zlib.compress(b"A" * 1024 * 1024, 9)


def test_a_png_cards_other_text_is_never_inflated(tmp_path: Path, monkeypatch):
    """Twenty small chunks of other text, each inflating to a megabyte (to
    sixteen, as one could be made): each was inflated, and a 300 KB card
    took 385 MB to read."""
    inflated: list[bytes] = []
    real = png_text._inflated
    monkeypatch.setattr(png_text, "_inflated", lambda body: inflated.append(body) or real(body))
    notes = [png_chunk(b"zTXt", f"note{n}".encode() + b"\0\0" + INFLATES) for n in range(20)]
    path = tmp_path / "elena.png"
    path.write_bytes(with_chunks(png_card(tmp_path, {"chara": v2()}).read_bytes(), *notes))
    assert read_card(path).name == "Elena"
    assert inflated == []


def test_a_png_cards_own_text_is_held_to_a_total(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(png_text, "MAX_TEXT", 3 * 1024 * 1024)
    files = [
        png_chunk(b"zTXt", f"chara-ext-asset_:{n}".encode() + b"\0\0" + INFLATES) for n in range(4)
    ]
    path = tmp_path / "elena.png"
    path.write_bytes(with_chunks(png_card(tmp_path, {"chara": v2()}).read_bytes(), *files))
    with pytest.raises(CardError, match="its text comes to more than 3 MB"):
        read_card(path)
    # Within it, the card's own compressed text is read as before.
    path.write_bytes(with_chunks(png_card(tmp_path, {"chara": v2()}).read_bytes(), files[0]))
    assert text_chunks(path.read_bytes())["chara-ext-asset_:0"] == "A" * 1024 * 1024


# --- reading cards ------------------------------------------------------------------


def test_a_png_card_is_read_and_its_picture_kept_without_the_card(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))

    assert card.name == "Elena" and card.spec == "chara_card_v2"
    assert card.picture is not None
    assert text_chunks(card.picture) == {}


def test_v3_is_read_before_v2(tmp_path: Path):
    v3 = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {**ELENA, "name": "Elena V3"}}
    card = read_card(png_card(tmp_path, {"chara": v2(), "ccv3": v3}))
    assert card.name == "Elena V3"


@pytest.mark.parametrize(
    "data",
    [
        v2(),
        {k: ELENA[k] for k in ("name", "description", "personality", "first_mes", "mes_example")},
        {"char_name": "Elena", "char_persona": ELENA["description"], "char_greeting": "Hi."},
    ],
    ids=["v2", "v1", "pygmalion"],
)
def test_a_json_card_of_any_version_is_read(tmp_path: Path, data: dict):
    path = tmp_path / "card.json"
    path.write_text(json.dumps(data))
    card = read_card(path)
    assert card.name == "Elena"
    assert "farmhouse" in card.text("description")


def test_a_charx_card_brings_its_picture(tmp_path: Path):
    fields = {
        **ELENA,
        "assets": [
            {"type": "icon", "uri": "embeded://assets/icon/images/main.png", "name": "main"}
        ],
    }
    path = tmp_path / "card.charx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("card.json", json.dumps({"spec": "chara_card_v3", "data": fields}))
        archive.writestr("assets/icon/images/main.png", tiny_png())
    card = read_card(path)
    assert card.name == "Elena" and card.picture is not None


def test_a_large_charx_is_read_in_place(tmp_path: Path, monkeypatch):
    # Its pictures are never read: it is held to MAX_ARCHIVE_BYTES, not to
    # what a card read whole may be.
    monkeypatch.setattr(tavern_cards, "MAX_BYTES", 600)
    fields = {**ELENA, "assets": [{"type": "icon", "uri": "embeded://assets/main.png"}]}
    path = tmp_path / "card.charx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("card.json", json.dumps({"spec": "chara_card_v3", "data": fields}))
        archive.writestr("assets/main.png", tiny_png())
        archive.writestr("assets/expressions/happy.png", b"\0" * 4000)
    assert path.stat().st_size > 600
    assert read_card(path).name == "Elena"
    big = tmp_path / "big.png"
    big.write_bytes(tiny_png() + b"\0" * 1000)
    with pytest.raises(CardError, match="too large"):
        read_card(big)


def test_bytes_say_what_they_are_whatever_the_name(tmp_path: Path):
    png = png_card(tmp_path, {"chara": v2()}).read_bytes()
    assert tavern_from_file(io.BytesIO(png), "download").name == "Elena"
    card = tavern_from_file(io.BytesIO(json.dumps(v2()).encode()), "x", picture=tiny_png())
    assert isinstance(card, TavernCard) and card.picture is not None
    book = book_from_lore([LoreEntry(title="The ridge", content="Cold.")])
    found = tavern_from_file(io.BytesIO(json.dumps(book).encode()), "x")
    assert not isinstance(found, TavernCard) and looks_like_lorebook(found)
    # A source that said "lorebook" isn't taken for a card.
    with pytest.raises(CardError, match="neither a character card nor a lorebook"):
        tavern_from_file(io.BytesIO(json.dumps(v2()).encode()), "x", lorebook=True)
    with pytest.raises(CardError, match="neither a character card nor a lorebook"):
        tavern_from_file(io.BytesIO(b"<html>a page</html>"), "x")


@pytest.mark.parametrize(
    ("name", "content", "said"),
    [
        ("plain.png", tiny_png(), "no character card in it"),
        ("x.json", b'{"entries": {}}', "isn't a character card"),
        ("x.json", b"not json", "isn't valid JSON"),
        ("x.txt", b"hello", "a card is a .png, .json or .charx"),
        ("x.charx", b"PK nonsense", "isn't a CHARX"),
    ],
)
def test_what_isnt_a_card_is_said_plainly(tmp_path: Path, name: str, content: bytes, said: str):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(CardError, match=said):
        read_card(path)
    assert not is_card_file(path)


# --- damaged cards --------------------------------------------------------------------

SOUND = b"ID3\x03" + bytes(4000)


def charx_with_a_sound(tmp_path: Path, *, stored: bool = False) -> Path:
    """Elena with her picture, a barn and a sound: deflated, or stored, so a
    byte changed in it is a bad CRC rather than a broken stream."""
    data = {
        **ELENA,
        "assets": [
            {"type": "icon", "uri": "embeded://assets/icon/main.png", "name": "main"},
            {"type": "x-risu-asset", "uri": "embeded://assets/other/wind.mp3", "name": "wind"},
            {"type": "background", "uri": "embeded://assets/other/barn.png", "name": "barn"},
        ],
    }
    path = tmp_path / "elena.charx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("card.json", json.dumps({"spec": "chara_card_v3", "data": data}))
        archive.writestr("assets/icon/main.png", tiny_png())
        method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
        archive.writestr("assets/other/wind.mp3", SOUND, compress_type=method)
        archive.writestr("assets/other/barn.png", tiny_png())
    return path


def damaged(data: bytes, member: str, how: str) -> bytes:
    """A zip with one member damaged as a bad download or another tool
    leaves one: in its local header, its central entry, or its data."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        central = archive.start_dir
        for info in archive.infolist():
            if info.filename == member:
                break
            central += 46 + len(info.filename.encode()) + len(info.extra) + len(info.comment)
    local = info.header_offset
    raw = bytearray(data)
    name_length, extra_length = struct.unpack_from("<HH", raw, local + 26)
    body = local + 30 + name_length + extra_length
    if how == "broken stream":
        raw[body : body + 4] = b"\xff\xff\xff\xff"
    elif how == "bad CRC":
        raw[body + 100] ^= 0x01
    elif how in ("deflate64", "AES"):
        method = 9 if how == "deflate64" else 99
        struct.pack_into("<H", raw, local + 8, method)
        struct.pack_into("<H", raw, central + 10, method)
    elif how == "encrypted":
        for flags in (local + 6, central + 8):
            struct.pack_into("<H", raw, flags, struct.unpack_from("<H", raw, flags)[0] | 1)
    elif how == "cut short":
        struct.pack_into("<I", raw, central + 20, 10_000_000)
    return bytes(raw)


@pytest.mark.parametrize(
    "how", ["broken stream", "bad CRC", "deflate64", "AES", "encrypted", "cut short"]
)
def test_a_damaged_file_in_a_charx_is_left_out_and_said(tmp_path: Path, how: str):
    """Each of these escaped as zlib's, zipfile's or Python's own error,
    which the window doesn't catch, and the card failed with nothing said;
    one cut short failed the whole card as "not a CHARX"."""
    path = charx_with_a_sound(tmp_path, stored=how == "bad CRC")
    path.write_bytes(damaged(path.read_bytes(), "assets/other/wind.mp3", how))
    by_link = tavern_from_file(io.BytesIO(path.read_bytes()), "elena.charx")
    for card in (read_card(path), by_link):
        assert card.name == "Elena" and card.picture is not None
        assert [f.name for f in card.files] == ["barn"]
        assert any("couldn't be read" in note for note in card.expression_notes)


def test_a_charx_whose_card_is_damaged_is_refused_in_words(tmp_path: Path):
    path = charx_with_a_sound(tmp_path)
    path.write_bytes(damaged(path.read_bytes(), "card.json", "broken stream"))
    with pytest.raises(CardError, match="card.json is damaged"):
        read_card(path)
    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("elena/joy.png", tiny_png() + bytes(400))
        archive.writestr("elena/anger.png", tiny_png())
    pack.write_bytes(damaged(pack.read_bytes(), "elena/joy.png", "broken stream"))
    found, notes = expressions_from_zip(pack)
    assert [name for name, _data in found] == ["anger"] and notes


@pytest.mark.parametrize("depth", [150, 200_000])
def test_a_card_nested_past_any_real_one_is_refused_in_words(tmp_path: Path, depth: int):
    """Two hundred thousand brackets escaped as a RecursionError; a hundred
    and fifty were read, and copying the character later recursed too far."""
    fields = '{"name": "Elena", "description": "x", "extensions": {"deep": '
    text = '{"spec": "chara_card_v2", "data": ' + fields + "[" * depth + "]" * depth + "}}}"
    path = tmp_path / "elena.json"
    path.write_text(text)
    with pytest.raises(CardError, match="nested far deeper than any card"):
        read_card(path)
    with pytest.raises(CardError, match="nested far deeper than any card"):
        tavern_from_file(io.BytesIO(text.encode()), "elena.json")
    charx = tmp_path / "elena.charx"
    with zipfile.ZipFile(charx, "w") as archive:
        archive.writestr("card.json", text)
    with pytest.raises(CardError, match="nested far deeper than any card"):
        read_card(charx)
    if depth > 1000:
        assert tavern_kind(path) is None


def test_half_a_character_in_a_cards_json_is_mended_so_it_can_be_kept(tmp_path: Path):
    """A broken escape ("\\ud800") read as half a UTF-16 pair: no file holds
    one in UTF-8, so every save of Your characters failed after it came in."""
    half = chr(0xD800)
    data = {**ELENA, "description": f"Keeps the farm {half} north.", "tags": [half]}
    path = tmp_path / "elena.json"
    path.write_text(json.dumps({"spec": "chara_card_v3", "data": {**data, "x" + half: 1}}))
    card = read_card(path)
    assert card.text("description") == "Keeps the farm \N{REPLACEMENT CHARACTER} north."
    root = tmp_path / "data"
    elena, _notes = collection.kept_from_card(card, DiskPictures(COLLECTION_ID, root))
    bundle = collection.load_collection(root)
    bundle.cast.append(elena)
    save_story_bundle(bundle, root=root)
    assert collection.load_collection(root).cast[0].tavern.tags == ["\N{REPLACEMENT CHARACTER}"]


# --- what a card holds, within limits ---------------------------------------------------


def test_a_file_the_card_names_many_times_is_read_once_within_the_limits(
    tmp_path: Path, monkeypatch
):
    """A 31 KB CHARX naming one 31 MB member forty times read it forty times,
    keeping copies to the limit: 2.9 s and 524 MB."""
    assets = [
        {"type": "x-risu-asset", "uri": "embeded://assets/other/big.bin", "name": f"big{n}"}
        for n in range(40)
    ]
    path = tmp_path / "elena.charx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "card.json", json.dumps({"spec": "chara_card_v3", "data": {**ELENA, "assets": assets}})
        )
        archive.writestr("assets/other/big.bin", bytes(1000))
    monkeypatch.setattr(tavern_cards, "MAX_FILE_BYTES", 3500)
    reads: list[str] = []
    real = zipfile.ZipFile.read

    def read(archive, name, pwd=None):
        reads.append(getattr(name, "filename", name))
        return real(archive, name, pwd)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    card = read_card(path)
    assert [f.name for f in card.files] == ["big0", "big1", "big2"]
    assert reads.count("assets/other/big.bin") == 1
    assert any("37 of its files went past" in note for note in card.expression_notes)


def test_links_count_among_a_cards_files(tmp_path: Path, monkeypatch):
    """Links had no limit: a card of a hundred thousand kept them all."""
    monkeypatch.setattr(tavern_cards, "MAX_FILES", 5)
    assets = [
        {"type": "x-risu-asset", "uri": f"https://example.com/{n}.png", "name": f"far{n}"}
        for n in range(12)
    ]
    path = tmp_path / "elena.json"
    path.write_text(json.dumps({"spec": "chara_card_v3", "data": {**ELENA, "assets": assets}}))
    card = read_card(path)
    assert [f.name for f in card.files] == [f"far{n}" for n in range(5)]
    assert any("7 of its files went past" in note for note in card.expression_notes)


# --- into SealedLore -----------------------------------------------------------------


def test_the_names_go_in_for_the_macros():
    fill = macro_filler("Elena", "John")
    assert fill("{{char}} and {{ USER }}, <BOT> and <USER>") == "Elena and John, Elena and John"
    assert macro_filler("Elena", None)("{{user}}") == "{{user}}"


def test_a_card_becomes_a_cast_card(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    character = character_from_card(card, macro_filler("Elena", "John"))

    assert character.name == "Elena"
    assert character.summary == "Elena keeps the farmhouse north of Hellsville."
    assert "She trusts John slowly." in character.full_description
    assert "Elena never says her surname." in character.full_description
    # Its own field, told to the storyteller after the description as the
    # import once wrote it into the description.
    assert character.personality == "wary, dry, loyal once won"
    sheet = render_cast_sheet(character, include_full_description=True)
    assert sheet.endswith("Elena never says her surname.\n\nPersonality: wary, dry, loyal once won")
    assert character.voice_notes == (
        "John: Hello?\nElena: Hands where I can see them.\n\nElena: Cellar's full of the dead."
    )
    # The rest of the card rides along, the names in.
    tavern = character.tavern
    assert (
        tavern.first_mes
        == '{{char}} lowers the rifle an inch. "Who sent you, {{user}}?"'.replace(
            "{{char}}", "Elena"
        ).replace("{{user}}", "John")
    )
    assert tavern.greetings[1] == "The door is barred. Elena speaks through it."
    assert tavern.system_prompt == "You are Elena. Never break character."
    assert (tavern.creator, tavern.character_version, tavern.tags) == ("someone", "1", ["survival"])
    assert tavern.book_entries == 1
    # The note at a depth is the description's, never kept twice.
    assert "depth_prompt" not in tavern.extensions


def test_kept_apart_from_a_story_the_examples_are_not_the_voice(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    character = character_from_card(card, lambda text: text, for_story=False)

    assert character.voice_notes is None
    assert character.tavern.mes_example == ELENA["mes_example"]
    assert "{{char}} keeps the farmhouse" in character.full_description


def test_a_cards_own_fields_go_back_out_as_they_came(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    character = character_from_card(card, lambda text: text, for_story=False)
    first, third = card_data(character)
    data = third["data"]

    for key in (
        "personality",
        "scenario",
        "first_mes",
        "mes_example",
        "system_prompt",
        "creator_notes",
        "creator",
        "character_version",
        "tags",
        "alternate_greetings",
        "character_book",
    ):
        assert data[key] == ELENA[key], key
    # The description took the note SillyTavern would have put at a depth.
    assert data["description"] == ELENA["description"] + "\n\n{{char}} never says her surname."
    assert first["data"]["personality"] == ELENA["personality"]


def test_a_story_cards_voice_from_its_examples_is_not_written_twice(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    character = character_from_card(card, macro_filler("Elena", "John"))
    _first, third = card_data(character)
    assert "Voice:" not in third["data"]["description"]
    assert third["data"]["mes_example"].startswith("<START>\nJohn: Hello?")


def test_a_new_story_from_a_card(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    imported = scenario_from_card(card, player="John", about_player="A drifter.", opening=1)
    scenario = imported.scenario

    elena, john = scenario.cast
    assert (elena.name, john.name, john.summary) == ("Elena", "John", "A drifter.")
    assert scenario.suggested_character_id == john.id
    assert scenario.starting_scene.present_character_ids == [elena.id, john.id]
    assert scenario.world_bible == (
        "Four winters after the end of the world, John reaches the farmhouse."
    )
    assert scenario.opening_mode == "as_written"
    assert scenario.opening_text == "The door is barred. Elena speaks through it."
    assert scenario.description == "A slow-burn survival story."
    assert [entry.title for entry in scenario.lore] == ["Hellsville"]
    assert scenario.lore[0].content == "Hellsville is walled; John is not welcome there."
    assert elena.reference_images and elena.reference_images[0].file in scenario.reference_files
    notes = " ".join(imported.notes)
    assert "1 other greeting on the card was not used" in notes
    assert "own instructions" in notes and "tags" in notes
    # It makes a story like any scenario, its picture with it.
    bundle = bundle_from_scenario(scenario)
    assert elena.reference_images[0].file in bundle.pending_files


def test_the_cards_instructions_can_go_into_the_styles_notes(tmp_path: Path):
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    imported = scenario_from_card(card, player="John", instructions=True)
    assert imported.scenario.style.notes == "You are Elena. Never break character."


def test_a_world_card_makes_the_world_and_its_picture_the_background(tmp_path: Path):
    world = {**ELENA, "name": "The Wastes", "description": "A frozen county."}
    card = read_card(png_card(tmp_path, {"chara": v2(world)}))
    imported = scenario_from_card(card, player="John", world=True, opening=None)
    scenario = imported.scenario

    assert [character.name for character in scenario.cast] == ["John"]
    assert scenario.world_bible.startswith("A frozen county.")
    assert scenario.background_image in scenario.reference_files
    assert scenario.opening_text == "" and scenario.opening_mode == "generate"
    assert any("you write the first turn" in note for note in imported.notes)


def test_other_macros_are_said(tmp_path: Path):
    fields = {**ELENA, "description": "{{char}} rolls {{roll:d20}} at {{time}}."}
    card = read_card(png_card(tmp_path, {"chara": v2(fields)}))
    notes = " ".join(scenario_from_card(card, player="John").notes)
    assert "2 macros other than {{char}} and {{user}} stay as written" in notes


# --- out of SealedLore -----------------------------------------------------------------


def test_a_character_goes_out_and_comes_back_whole(tmp_path: Path):
    character = Character(
        name="Gus",
        aliases=["Old Gus"],
        summary="Runs the bar.",
        full_description="Gus runs the bar in Hellsville and hears everything.",
        voice_notes="Slow, kind, never swears.",
        competence=Competence(tiers={"brawling": "skilled"}, notes="Bad knee."),
        is_player_available=False,
        canon=None,
    )
    lore = [LoreEntry(title="The bar", content="Smoky.", keywords=["bar"])]
    first, third = card_data(character, story_title="Doomsville", lore=lore, picture=True)
    path = tmp_path / "gus.png"
    write_card(path, first, third, tiny_png())

    card = read_card(path)
    back = character_from_card(card, macro_filler(card.name, None))

    assert card.spec == "chara_card_v3"
    assert back.model_dump(exclude={"id", "tavern"}) == character.model_dump(
        exclude={"id", "tavern"}
    )
    # What the card said of where it came from, and its lorebook, ride along.
    assert back.tavern.creator_notes == "Exported from SealedLore, from the story Doomsville."
    assert back.tavern.book_entries == 1
    # Something any reader can use, too.
    description = card.text("description")
    assert "Voice: Slow, kind, never swears." in description
    assert "Skills: brawling (skilled); Bad knee." in description
    assert card.fields["nickname"] == "Old Gus"
    assert [entry["content"] for entry in card.book["entries"]] == ["Smoky."]
    assert json.loads(base64.b64decode(text_chunks(path.read_bytes())["chara"]))["name"] == "Gus"


def test_a_json_card_is_v3_with_v1_on_top_and_only_its_links(tmp_path: Path):
    """Written as V2, a JSON card lost what only V3 has a place for, which a
    PNG or a CHARX of the same character kept."""
    gus = Character(name="Gus", aliases=["Old Gus"], summary="Runs the bar.")
    gus.tavern = TavernFields(
        group_only_greetings=["Evening, all."],
        source=["https://example.com/gus"],
        creator_notes_multilingual={"de": "Der Wirt."},
        creation_date=1_700_000_000,
        assets=[
            CardAsset(type="background", name="bar", ext="png", uri="https://example.com/bar.png"),
            CardAsset(type="x-risu-asset", name="song", ext="mp3", file="images/assets/g/song.mp3"),
        ],
    )
    first, third = card_data(gus)
    path = tmp_path / "gus.json"
    write_card(path, first, third, None)
    data = json.loads(path.read_text())
    assert (data["spec"], data["spec_version"]) == ("chara_card_v3", "3.0")
    # V1's fields on top too, for readers that know no other.
    assert data["name"] == "Gus" and data["description"] == "Runs the bar."
    inner = data["data"]
    assert inner["nickname"] == "Old Gus" and inner["group_only_greetings"] == ["Evening, all."]
    assert inner["source"] == ["https://example.com/gus"]
    assert inner["creator_notes_multilingual"] == {"de": "Der Wirt."}
    assert inner["creation_date"] == 1_700_000_000
    # A JSON card holds no files: its links go, the song stays behind.
    assert [a["uri"] for a in inner["assets"]] == ["https://example.com/bar.png"]
    assert read_card(path).fields["group_only_greetings"] == ["Evening, all."]
    with pytest.raises(CardError, match="needs the character's picture"):
        write_card(tmp_path / "gus.png", first, third, None)


def test_a_png_card_takes_the_card_in_place_of_any_text():
    picture = with_text(tiny_png(), {"chara": "old", "Comment": "keep"})
    first, third = card_data(Character(name="Gus"))
    texts = text_chunks(card_png(picture, first, third))
    assert set(texts) == {"Comment", "chara", "ccv3"}


# --- lorebooks ------------------------------------------------------------------------

WORLD_INFO = {
    "entries": {
        "0": {
            "uid": 0,
            "key": ["Hellsville"],
            "keysecondary": ["gate"],
            "selective": True,
            "comment": "The town",
            "content": "Walled, and wary of {{user}}.",
            "constant": False,
            "disable": False,
            "probability": 50,
            "useProbability": True,
            "displayIndex": 1,
        },
        "1": {
            "uid": 1,
            "key": ["/hidden\\s+caves?/i", "caves"],
            "comment": "",
            "content": "Below the ridge.",
            "constant": True,
            "disable": True,
            "sticky": 2,
            "group": "places",
            "position": 4,
            "displayIndex": 0,
        },
        "2": {"uid": 2, "key": ["empty"], "content": "  ", "displayIndex": 2},
    }
}


def test_a_lorebook_becomes_lore_and_says_what_it_left_out():
    imported = lore_from_book(WORLD_INFO, macro_filler("Narrator", "John"))
    caves, town = imported.entries

    assert (caves.title, caves.keywords, caves.always_on, caves.enabled) == (
        "caves",
        ["caves"],
        True,
        False,
    )
    assert (town.title, town.keywords, town.content) == (
        "The town",
        ["Hellsville"],
        "Walled, and wary of John.",
    )
    notes = " ".join(imported.notes())
    for said in (
        "second keywords",
        "regular expressions",
        "a chance below 100%",
        "sticky, cooldown and delay",
        "inclusion groups",
        "where in the prompt",
        "no content",
    ):
        assert said in notes


def test_a_cards_own_lorebook_reads_the_same():
    entries = lore_from_book(ELENA["character_book"]).entries
    assert [(e.title, e.keywords) for e in entries] == [("Hellsville", ["Hellsville", "the town"])]


def card_entry(**fields) -> dict:
    """A card's entry as SillyTavern writes one (convertWorldInfoToCharacterBook)."""
    return {
        "id": 0,
        "keys": ["farm"],
        "secondary_keys": [],
        "comment": "The farm",
        "content": "The farm is cold.",
        "constant": False,
        "selective": True,
        "insertion_order": 100,
        "enabled": True,
        "position": "before_char",
        "use_regex": True,
        "extensions": {"position": 0, "probability": 100, "useProbability": True},
        **fields,
    }


def test_only_a_key_written_like_a_pattern_is_one():
    """SillyTavern puts the names in, then reads a key as a pattern only if it
    is written /like this/ whole (parseRegexFromString), whatever `use_regex`
    says: told by their signs, these keys of a card it wrote were lost."""
    keys = [
        "{{char}}",
        "{{user}}'s farm",
        "(the) king",
        "Who goes there?",
        "C++",
        "/howl/i",
        "/path/to",
        "/wolf/x",
        "/twice/ii",
        None,
        "Hellsville",
    ]
    read = lore_from_book({"entries": [card_entry(keys=keys)]}, macro_filler("Elena", "John"))
    assert read.entries[0].keywords == [
        "Elena",
        "John's farm",
        "(the) king",
        "Who goes there?",
        "C++",
        "/path/to",
        "/wolf/x",
        "/twice/ii",
        "Hellsville",
    ], "a slash inside, or flags JavaScript refuses, make it words; a null is no keyword"
    assert read.left_out == {"regex": 1}, "only the pattern is said to be left out"
    assert lore_from_book({"entries": [card_entry()]}).left_out == {}


def test_a_pattern_is_told_as_sillytavern_tells_one():
    for pattern in ("/howl/i", r"/a\/b/", "/a,b/gimsuy", "/x/", "/(?:hidden)\\s+caves?/"):
        assert is_pattern(pattern), pattern
    for words in ("/path/to", "/usr/bin/", "and/or", "//", "/a/I", "/a/x", "/a/gg", "/a/ "):
        assert not is_pattern(words), words


def test_the_switches_at_the_head_of_an_entrys_text_are_read_as_sillytavern_reads_them():
    """parseDecorators: @@activate sends the entry whatever is said,
    @@dont_activate never, @@activate first; a @@@ line is a fallback for
    one it didn't know; only whole lines count, and the lines go."""
    texts = [
        "@@activate\nThe farm is haunted.",
        "@@dont_activate\nThe cellar.",
        "@@dont_activate\n@@activate\nBoth.",
        "@@@activate\nA fallback with nothing to fall back from.",
        "@@remember\n@@@dont_activate\nFallen back.",
        "@@activate now\nNot a whole line.",
        "The farm.\n@@activate",
        "@@activate",
    ]
    book = {"entries": [card_entry(content=text, constant=False) for text in texts]}
    read = lore_from_book(book)
    assert [(e.content, e.always_on, e.enabled) for e in read.entries] == [
        ("The farm is haunted.", True, True),
        ("The cellar.", False, False),
        ("Both.", True, True),
        ("A fallback with nothing to fall back from.", False, True),
        ("Fallen back.", False, False),
        ("Not a whole line.", False, True),
        ("The farm.\n@@activate", False, True),
        ("@@activate", True, True),
    ], "a text of nothing but switches keeps them, as there"
    assert [entry["content"] for entry in book["entries"]] == texts, "the card keeps its text"


def test_a_cards_entry_is_placed_by_its_extensions():
    """convertCharacterBook reads a card's entry by its extensions first: its
    own `position` only says before or after the character."""
    at_depth = card_entry(position="after_char", extensions={"position": 4, "depth": 2})
    after = card_entry(position="after_char", extensions={"position": 1})
    unset = card_entry(position="after_char", extensions={"position": None, "probability": 40})
    read = lore_from_book({"entries": [at_depth, after, unset]})
    assert read.left_out == {"placement": 1, "probability": 1}
    # SillyTavern's own entry by its own fields first.
    world = {"entries": {"0": {"uid": 0, "key": ["farm"], "content": "c", "position": 0}}}
    world["entries"]["0"]["extensions"] = {"position": 4}
    assert lore_from_book(world).left_out == {}


def test_second_keywords_count_where_each_shape_says_they_do():
    """A card's entry is selective only if it says so (convertCharacterBook's
    `selective || false`); SillyTavern's own is unless it says not."""
    card = [
        {"keys": ["farm"], "secondary_keys": ["door"], "content": "a"},
        {"keys": ["farm"], "secondary_keys": ["door"], "content": "b", "selective": True},
    ]
    assert lore_from_book({"entries": card}).left_out == {"secondary": 1}
    own = {
        "0": {"uid": 0, "key": ["farm"], "keysecondary": ["door"], "content": "a"},
        "1": {"uid": 1, "key": ["farm"], "keysecondary": ["door"], "content": "b"},
    }
    own["1"]["selective"] = False
    assert lore_from_book({"entries": own}).left_out == {"secondary": 1}


def test_a_story_takes_the_fields_the_lorebook_tab_edits():
    """An entry with a field under both names gives a story the one the
    Lorebook tab edits: the spec's in a card's book, SillyTavern's in its own."""
    card = {"keys": ["new"], "key": ["old"], "content": "c", "enabled": False, "disable": False}
    [entry] = lore_from_book({"entries": [card]}).entries
    assert (entry.keywords, entry.enabled) == (["new"], False)
    own = {"uid": 0, "keys": ["old"], "key": ["new"], "content": "c", "enabled": False}
    own["disable"] = False
    [entry] = lore_from_book({"entries": {"0": own}}).entries
    assert (entry.keywords, entry.enabled) == (["new"], True)


def test_lore_goes_out_and_comes_back(tmp_path: Path):
    lore = [
        LoreEntry(title="The bar", content="Smoky.", keywords=["bar", "Gus's"], always_on=True),
        LoreEntry(title="The ridge", content="Cold.", enabled=False),
    ]
    path = tmp_path / "lore.json"
    write_lorebook(path, lore)

    back = lore_from_book(read_lorebook(path))
    assert back.left_out == {}
    assert [
        e.model_dump(include={"title", "content", "keywords", "always_on", "enabled"})
        for e in back.entries
    ] == [
        e.model_dump(include={"title", "content", "keywords", "always_on", "enabled"}) for e in lore
    ]
    embedded = lore_from_book(character_book_from_lore(lore)).entries
    assert [e.title for e in embedded] == ["The bar", "The ridge"]


def test_a_lorebook_is_told_from_a_card_and_from_other_files(tmp_path: Path):
    assert looks_like_lorebook(WORLD_INFO)
    assert looks_like_lorebook(book_from_lore([LoreEntry(title="A", content="B")]))
    assert not looks_like_lorebook(v2())
    assert not looks_like_lorebook({"format": "sealedlore-scenario", "entries": []})
    path = tmp_path / "card.json"
    path.write_text(json.dumps(v2()))
    with pytest.raises(LorebookError, match="isn't a SillyTavern lorebook"):
        read_lorebook(path)


# --- expression pictures ---------------------------------------------------------------


def charx_with_expressions(tmp_path: Path, names: list[str], *, linked: int = 0) -> Path:
    assets = [{"type": "icon", "uri": "embeded://assets/icon/main.png", "name": "main"}]
    assets += [
        {"type": "emotion", "uri": f"embeded://assets/emotion/{name}.png", "name": name}
        for name in names
    ]
    assets += [
        {"type": "emotion", "uri": f"https://example.com/{n}.png", "name": f"far{n}"}
        for n in range(linked)
    ]
    path = tmp_path / "card.charx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "card.json", json.dumps({"spec": "chara_card_v3", "data": {**ELENA, "assets": assets}})
        )
        archive.writestr("assets/icon/main.png", tiny_png())
        for name in names:
            archive.writestr(f"assets/emotion/{name}.png", tiny_png())
    return path


def test_a_charx_brings_its_expression_pictures_labelled(tmp_path: Path):
    card = read_card(charx_with_expressions(tmp_path, ["joy", "anger"], linked=1))
    assert [name for name, _data in card.expressions] == ["joy", "anger"]
    assert all(text_chunks(data) == {} for _name, data in card.expressions)
    # One named by a link is never fetched: kept as the card's link, so the
    # card goes out whole.
    [far] = card.files
    assert (far.type, far.name, far.uri, far.data) == (
        "emotion",
        "far0",
        "https://example.com/0.png",
        None,
    )


def test_expression_pictures_past_the_limit_stay_with_the_card(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tavern_cards, "MAX_EXPRESSIONS", 2)
    card = read_card(charx_with_expressions(tmp_path, ["joy", "anger", "fear"]))
    assert len(card.expressions) == 2
    # The third is one of the card's files, held, to go out with it again.
    [fear] = card.files
    assert (fear.type, fear.name, fear.data is not None) == ("emotion", "fear", True)


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("joy.png", "joy"),
        ("joy-2.png", "joy"),
        ("sprites/anger_1.webp", "anger"),
        ("combat_stance.png", "combat stance"),
        ("Half-smile.jpg", "Half-smile"),
    ],
)
def test_an_expression_picture_is_labelled_by_its_name(name: str, label: str):
    assert expression_of(name) == label


def test_a_sillytavern_expression_pack_is_read(tmp_path: Path):
    path = tmp_path / "pack.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("elena/joy.png", tiny_png())
        archive.writestr("elena/joy-2.png", tiny_png())
        archive.writestr("elena/readme.txt", "not a picture")
        archive.writestr("elena/broken.png", b"not a picture either")
    found, notes = expressions_from_zip(path)
    assert [name for name, _data in found] == ["joy", "joy-2"]
    assert notes and "couldn't be read" in notes[0]
    (tmp_path / "plain.zip").write_bytes(b"no zip")
    with pytest.raises(CardError, match="isn't a zip of pictures"):
        expressions_from_zip(tmp_path / "plain.zip")


def test_a_new_story_keeps_the_expressions_on_its_character(tmp_path: Path):
    card = read_card(charx_with_expressions(tmp_path, ["joy", "anger"]))
    scenario = scenario_from_card(card, player="John").scenario
    elena = scenario.cast[0]
    assert [ref.expression for ref in elena.reference_images] == ["", "joy", "anger"]
    assert elena.reference_images[0].caption == "portrait"
    assert all(ref.file in scenario.reference_files for ref in elena.reference_images)
    left = scenario_from_card(card, player="John", expressions=False)
    assert len(left.scenario.cast[0].reference_images) == 1
    assert any("expression pictures were left out" in note for note in left.notes)
    world = scenario_from_card(card, player="John", world=True)
    assert any("belong to one character" in note for note in world.notes)
