"""What the review of 3.5.0b1 found in files from strangers (Nov 22 2026),
one test a fault: each fails on 0f289da, but those that hold a rewritten
reader to what it read before. Every hostile file is made here, small, from
invented names."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import re
import struct
import time
import tracemalloc
import zipfile
import zlib
from pathlib import Path

import pytest

from sealedlore.engine import plot_md
from sealedlore.engine.chronicle import schedule
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.plot_md import parse_plot_markdown, read_clause
from sealedlore.engine.usage import usage_report
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.node import Node, Usage
from sealedlore.models.plot import EventDef, Plot
from sealedlore.models.story import BUDGET_MOST, Story, StoryDefaults
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage import (
    archive,
    collection,
    collection_backup,
    lorebooks,
    png_text,
    tavern_cards,
    tavern_lore,
)
from sealedlore.storage import scenario as scenario_files
from sealedlore.storage.atomic import json_for_others, outside_json
from sealedlore.storage.collection_backup import BackupError, open_backup, restore
from sealedlore.storage.image_meta import PNG_SIGNATURE, MetadataError, strip_metadata
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures
from sealedlore.storage.prompt_sets import PromptSetError, read_prompt_set
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.video_meta import strip_video_metadata

# --- F1: a plot file's lines are read in the time their length takes ---------------

PLOT = """---
title: Doomsville probe
---

# World

A cold valley north of Hellsville.

# Characters

## John Smith{heading}
- role: player

A farmer.

# Facts

- wanted (no, yes)

# Timeline

## The welcome
- when: day 1
{field}

Something happens.
"""


def test_a_heading_with_runs_of_spaces_is_read_at_once():
    spaces = " " * 3000
    text = PLOT.format(heading=f"{spaces}the{spaces}farmer{spaces}", field="- trigger: now")
    began = time.perf_counter()
    parsed = parse_plot_markdown(text, fallback_title="x")
    assert time.perf_counter() - began < 1.0
    assert parsed.ok
    assert [c.name for c in parsed.scenario.cast] == [f"John Smith{spaces}the{spaces}farmer"]


@pytest.mark.parametrize(
    "field",
    [
        "- requires: wanted{spaces}x",
        "- requires: The welcome{spaces}has happenedx",
        "- trigger{spaces}{spaces}{spaces}{spaces}now",
    ],
)
def test_a_field_with_a_run_of_spaces_is_read_at_once(field):
    text = PLOT.format(heading="", field=field.format(spaces=" " * 20000))
    began = time.perf_counter()
    parse_plot_markdown(text, fallback_title="x")
    assert time.perf_counter() - began < 1.0


_OLD_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_OLD_FIELD = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z _-]*?)\s*:\s*(.*)$")
_OLD_HAPPENED = re.compile(
    r"(.+?)\s+(has not happened|hasn['’]t happened|not happened|has happened|happened)",
    re.IGNORECASE,
)
_OLD_COMPARISON = re.compile(r"([A-Za-z][\w ]*?)\s*(!=|=|\bis not\b|\bisn't\b|\bis\b)\s*(.+)")


def _made(rng: random.Random, pieces: list[str], most: int) -> str:
    return "".join(rng.choice(pieces) for _ in range(rng.randint(0, most)))


def test_headings_and_fields_read_as_the_patterns_before_read_them():
    rng = random.Random(5)
    pieces = ["#", "##", " ", "  ", "\t", "a", "John", "Smith", "-", "*", ":", "_", "é", "1", "\r"]
    for _ in range(40000):
        line = _made(rng, pieces, 9)
        old = _OLD_HEADING.match(line)
        assert plot_md._heading(line) == (
            (len(old.group(1)), old.group(2).strip()) if old else None
        ), repr(line)
        old, new = _OLD_FIELD.match(line), plot_md._FIELD.match(line)
        assert (old.groups() if old else None) == (new.groups() if new else None), repr(line)


def _old_fact_clause(clause: str) -> tuple | None:
    """What `read_clause` made of an event or a fact, by the patterns it had."""
    if happened := _OLD_HAPPENED.fullmatch(clause):
        negated = bool(re.search(r"not|n['’]t", happened.group(2), re.IGNORECASE))
        return ("happened", negated, happened.group(1).strip(), "")
    if match := _OLD_COMPARISON.fullmatch(clause):
        negated = match.group(2).strip() not in ("=", "is")
        return ("fact", negated, plot_md._key(match.group(1)), plot_md._norm(match.group(3)))
    return None


def _fact_clause(clause: str) -> tuple | None:
    """The same, by the readers it has now."""
    if happened := plot_md._happened(clause):
        negated = bool(re.search(r"not|n['’]t", happened[1], re.IGNORECASE))
        return ("happened", negated, happened[0].strip(), "")
    if match := plot_md._comparison(clause):
        negated = match[1].strip() not in ("=", "is")
        return ("fact", negated, plot_md._key(match[0]), plot_md._norm(match[2]))
    return None


def test_conditions_read_as_the_patterns_before_read_them():
    rng = random.Random(7)
    pieces = [
        " ", "  ", "\t", "a", "wanted", "The welcome", "is", "is not", "isn't", "not", "=", "!=",
        "has", "happened", "has not happened", "hasn’t happened", "Has Happened", "x", "-", "!",
        "yes", "this", "\n",
    ]  # fmt: skip
    read = 0
    for _ in range(60000):
        clause = _made(rng, pieces, 7)
        old = _old_fact_clause(clause)
        assert _fact_clause(clause) == old, repr(clause)
        read += old is not None
    assert read > 5000
    # And through the door the importer and the plot editor use.
    assert read_clause("The welcome   has not happened", []) == plot_md.Clause(
        "happened", True, "The welcome"
    )
    assert read_clause("wanted \t is not  Yes ", []) == plot_md.Clause(
        "fact", True, "wanted", "yes"
    )


# --- F10: a backup of characters never writes over a file a character here names ----


def _png(shade: int) -> bytes:
    """A clean 1×1 grey PNG: a picture of its own, nothing hidden."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, shade])))
        + chunk(b"IEND", b"")
    )


def _characters_zip(path: Path, characters: list[Character], files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 1})
        )
        archive.writestr(
            "characters.json", json.dumps([c.model_dump(mode="json") for c in characters])
        )
        for name, data in files.items():
            archive.writestr("files/" + name, data)
    return path


def test_a_backup_of_characters_never_writes_over_a_picture_kept_here(tmp_path: Path):
    store = DiskPictures(COLLECTION_ID, tmp_path / "data")
    mine = collection.new_collection()
    elena = collection.kept("Elena Ruiz")
    elena.reference_images = [ImageRef(id="p1", file="images/refs/p1.png")]
    store.write("images/refs/p1.png", _png(10))
    mine.cast.append(elena)
    # The backup: Elena again, and two newcomers whose picture has her
    # picture's name and other bytes.
    marcus, ben = collection.kept("Marcus"), collection.kept("Ben")
    marcus.reference_images = [ImageRef(id="p9", file="images/refs/p1.png")]
    asset = CardAsset(type="other", name="p1", ext="png", file="images/refs/p1.png")
    ben.tavern = TavernFields(assets=[asset])
    path = _characters_zip(
        tmp_path / "theirs.zip", [elena, marcus, ben], {"images/refs/p1.png": _png(200)}
    )
    with open_backup(path) as backup:
        done = restore(backup, mine, store, replace=False)
    assert done.kept == ["Elena Ruiz"] and done.added == ["Marcus", "Ben"] and not done.missing
    assert store.read("images/refs/p1.png") == _png(10), "hers is as it was"
    moved = mine.cast[1].reference_images[0].file
    assert moved != "images/refs/p1.png" and store.read(moved) == _png(200)
    assert mine.cast[2].tavern.assets[0].file == moved, "one file in the backup, one here"
    # The same backup again, with the backup's in place of mine: what is here
    # already and the same is not kept twice.
    with open_backup(path) as backup:
        restore(backup, mine, store, replace=True)
    assert [c.name for c in mine.cast] == ["Elena Ruiz", "Marcus", "Ben"]
    files = {p.name for p in (tmp_path / "data").rglob("*.png")}
    assert files == {Path(moved).name}, "Elena's is the backup's now, her old one gone"
    assert mine.cast[0].reference_images[0].file == moved


# --- F2: a backup of characters takes the room its own size allows, or none --------


def _with_card_files(name: str, files: int) -> tuple[Character, list[str]]:
    character = collection.kept(name)
    names = [f"images/assets/{character.id}/f{i}.bin" for i in range(files)]
    character.tavern = TavernFields(
        assets=[
            CardAsset(type="other", name=f"f{i}", ext="bin", file=n) for i, n in enumerate(names)
        ]
    )
    return character, names


def test_a_backup_claiming_far_more_than_its_size_is_refused_before_anything_is_read(
    tmp_path: Path, monkeypatch
):
    """A megabyte of zeros is a kilobyte in a zip: 786 KB wrote 805 MB."""
    monkeypatch.setattr(collection_backup, "ROOM_SPARE", 1024 * 1024, raising=False)
    elena, names = _with_card_files("Elena Ruiz", 6)
    path = _characters_zip(tmp_path / "bomb.zip", [elena], dict.fromkeys(names, bytes(512 * 1024)))
    assert path.stat().st_size < 64 * 1024
    store = DiskPictures(COLLECTION_ID, tmp_path / "data")
    mine = collection.new_collection()
    with pytest.raises(BackupError, match="more than a backup of its size"), open_backup(path) as b:
        restore(b, mine, store, replace=False)
    assert not mine.cast and not list((tmp_path / "data").rglob("*.bin"))
    # A backup as SealedLore writes one (its files stored as they are) is
    # its own size, whatever it holds.
    for name in names:
        store.write(name, bytes(512 * 1024))
    mine.cast.append(elena)
    path = tmp_path / "real.zip"
    collection_backup.write_backup(path, mine.cast, store)
    there = collection.new_collection()
    with open_backup(path) as b:
        done = restore(b, there, DiskPictures(COLLECTION_ID, tmp_path / "there"), replace=False)
    assert done.added == ["Elena Ruiz"] and not done.missing


class _FullAfter(DiskPictures):
    """A disk with room for so many more files."""

    def __init__(self, root: Path, room: int) -> None:
        super().__init__(COLLECTION_ID, root)
        self.room = room

    def write(self, relative: str, data: bytes) -> None:
        if self.room <= 0:
            raise OSError(28, "No space left on device")
        self.room -= 1
        super().write(relative, data)


def test_a_full_disk_while_restoring_is_said_and_nothing_of_the_backup_is_kept(tmp_path: Path):
    store = _FullAfter(tmp_path / "data", room=1)
    mine = collection.new_collection()
    elena = collection.kept("Elena Ruiz")
    elena.reference_images = [ImageRef(id="p1", file="images/refs/p1.png")]
    store.write("images/refs/p1.png", _png(10))
    mine.cast.append(elena)
    store.room = 3
    # The backup's Elena has another picture; Marcus brings four files.
    theirs = elena.model_copy(deep=True)
    theirs.reference_images = [ImageRef(id="p2", file="images/refs/p2.png")]
    marcus, names = _with_card_files("Marcus", 4)
    files = {"images/refs/p2.png": _png(20)} | {name: name.encode() for name in names}
    path = _characters_zip(tmp_path / "theirs.zip", [theirs, marcus], files)
    with pytest.raises(BackupError, match="No space left") as raised, open_backup(path) as backup:
        restore(backup, mine, store, replace=True)
    assert "Nothing" in str(raised.value)
    assert mine.cast == [elena] and elena.reference_images[0].file == "images/refs/p1.png"
    kept = {
        p.relative_to(tmp_path / "data" / "characters").as_posix()
        for p in (tmp_path / "data").rglob("*")
        if p.is_file()
    }
    assert kept == {"images/refs/p1.png"}, "hers is still here, and nothing of the backup's"


# --- F3: numbers a backup's reader takes, and what reads them after -----------------


def test_a_log_with_numbers_nothing_can_add_is_still_counted():
    entries = [
        {"id": "r1", "kind": "response", "usage": {"prompt_tokens": 100, "cost": 0.25}},
        {"id": "r2", "kind": "response", "usage": {"prompt_tokens": float("nan")}},
        {"id": "r3", "kind": "response", "usage": {"prompt_tokens": float("inf")}},
        {"id": "r4", "kind": "aside", "usage": {"prompt_tokens": 5, "cost": 10**400}},
        {"id": "r5", "kind": "aside", "usage": {"prompt_tokens": 5, "cost": float("nan")}},
        {"id": "r6", "kind": "aside", "usage": {"cost": float("inf"), "cost_estimated": True}},
        {"id": "r7", "kind": "response", "usage": {"prompt_tokens": 7, "cost": 0.5}},
    ]
    total = usage_report(entries).total
    assert total.calls == 7 and total.cost == 0.75 and total.prompt_tokens >= 107
    assert total.unpriced >= 3, "said as unpriced, never as free"


def test_a_storys_budget_and_an_events_window_are_read_as_they_can_be_used():
    defaults = StoryDefaults.model_validate({"context_token_budget": 10**400})
    assert defaults.context_token_budget == BUDGET_MOST
    Config().cast_cap(defaults.context_token_budget)  # a share of it: no overflow
    assert StoryDefaults(context_token_budget=900).context_token_budget == 900
    event = EventDef.model_validate({"id": "e", "title": "E", "kind": "timed", "window": [30, 10]})
    assert event.window == (10, 30)
    days = schedule(Plot(events=[event]), {}, random.Random(1))
    assert 10 <= days["e"] <= 30


# --- F5: stripping a video holds about the video, however many boxes it is ----------


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


FTYP = _box(b"ftyp", b"isom\x00\x00\x02\x00isom")


_ENTRIES = _box(b"stsd", bytes(8) + _box(b"abcd") * 60_000)


def _peak_while(call) -> int:
    tracemalloc.start()
    try:
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize(
    "video",
    [
        FTYP + _box(b"moov", _box(b"mvhd", bytes(100)) + _box(b"abcd") * 60_000) + _box(b"mdat"),
        FTYP + _box(b"moov", _box(b"mvhd", bytes(100))) + _box(b"abcd") * 60_000,
        FTYP + _box(b"moov", _box(b"trak", _box(b"mdia", _box(b"minf")) * 20_000)),
        FTYP + _box(b"moov", _box(b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", _ENTRIES))))),
    ],
    ids=["in the movie header", "at the top", "tracks of nothing", "sample entries"],
)
def test_stripping_a_video_of_tiny_boxes_holds_little_more_than_the_video(video: bytes):
    """A 144 KB card file of 8-byte boxes took 1.1 GB: every box was listed,
    at thirty times its size, before any was looked at."""
    stripped: list[bytes] = []
    peak = _peak_while(lambda: stripped.append(strip_video_metadata(video)))
    assert len(stripped[0]) == len(video)
    # The copy worked on and the copy returned, and little besides.
    assert peak < 3 * len(video)


# --- F9: the strippers keep only what they know, below the top as well --------------

SECRET = b"GPS 47.3769N 8.5417E owner=John Smith serial=SN-0042"


def test_a_videos_boxes_nobody_listed_are_blanked_wherever_they_are():
    """Unknown boxes were blanked at the top of the file only: one inside
    the movie header, a track or its sample table kept whatever it held."""
    stsd = _box(b"stsd", bytes(8) + _box(b"mp4a", bytes(28)))
    stco = _box(b"stco", bytes(4) + struct.pack(">II", 1, 4242))
    stbl = _box(b"stbl", stsd + _box(b"XtrA", SECRET) + stco)
    hdlr = _box(b"hdlr", bytes(8) + b"soun" + bytes(12) + b"h\x00")
    mdia = _box(
        b"mdia", _box(b"mdhd", bytes(24)) + hdlr + _box(b"minf", stbl + _box(b"nope", SECRET))
    )
    trak = _box(b"trak", _box(b"tkhd", bytes(84)) + mdia + _box(b"loci", SECRET))
    nested = _box(b"trak", _box(b"trak", _box(b"stbl", _box(b"stco", SECRET))))
    moov = _box(b"moov", _box(b"mvhd", bytes(100)) + trak + nested + _box(b"gps ", SECRET))
    moof = _box(
        b"moof",
        _box(b"mfhd", bytes(8)) + _box(b"traf", _box(b"tfhd", bytes(8)) + _box(b"zzzz", SECRET)),
    )
    mfra = _box(b"mfra", _box(b"tfra", bytes(16)) + _box(b"hide", SECRET) + _box(b"mfro", bytes(8)))
    video = (
        FTYP + moov + moof + _box(b"mdat", bytes(64)) + mfra + _box(b"trak", _box(b"udta", SECRET))
    )
    stripped = strip_video_metadata(video)
    assert len(stripped) == len(video) and SECRET not in stripped
    for kind in (b"XtrA", b"nope", b"loci", b"gps ", b"zzzz", b"hide"):
        assert kind not in stripped
    # What a player reads is where it was: the sample table's offsets, the
    # fragment's and the index's own boxes.
    for kept in (stco, stsd, b"tkhd", b"mdhd", b"soun", b"mfhd", b"tfhd", b"tfra", b"mfro"):
        assert kept in stripped
    assert stripped.index(stco) == video.index(stco)
    assert strip_video_metadata(stripped) == stripped


def test_a_real_video_is_stripped_as_it_was():
    """tests/data/tiny.mp4 (ffmpeg's, a second of a test pattern) holds
    nothing the lists don't know: stripped, it is byte for byte what it was
    before they were made strict (decoded by ffmpeg to the same frames and
    sound as the file itself, by hand)."""
    video = (Path(__file__).parent / "data" / "tiny.mp4").read_bytes()
    stripped = strip_video_metadata(video)
    assert (
        hashlib.sha256(stripped).hexdigest()
        == "1827a146fc26e07a08bc0e24611dfd1f48415d64b5fc76a8ff95cec4e893e9f7"
    )


def test_a_video_whose_movie_header_is_compressed_cant_be_stripped():
    """QuickTime's `cmov`: the header, zlib-compressed, with whatever it
    names inside. Blanked as unknown, the video would be nothing; kept, its
    metadata. Refused, as any file that can't be cleaned is."""
    video = FTYP + _box(b"moov", _box(b"cmov", _box(b"dcom", b"zlib") + _box(b"cmvd", bytes(40))))
    with pytest.raises(MetadataError, match="compressed"):
        strip_video_metadata(video + _box(b"mdat", bytes(8)))


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def test_a_png_that_isnt_animated_keeps_no_animation_frames():
    """`fdAT` and `fcTL` are an animation's frames; with no `acTL` before the
    picture no decoder reads them, and they held whatever was put there."""
    header = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    picture = _png_chunk(b"IDAT", zlib.compress(b"\x00\x00"))
    hidden = _png_chunk(b"fcTL", SECRET[:26]) + _png_chunk(b"fdAT", bytes(4) + SECRET)
    end = _png_chunk(b"IEND", b"")
    still = PNG_SIGNATURE + header + hidden + picture + hidden + end
    assert strip_metadata(still) == PNG_SIGNATURE + header + picture + end
    # Named an animation only after the picture, it is none either.
    late = PNG_SIGNATURE + header + picture + _png_chunk(b"acTL", bytes(8)) + hidden + end
    assert strip_metadata(late) == PNG_SIGNATURE + header + picture + end
    # A real animation keeps its frames.
    control = _png_chunk(b"acTL", struct.pack(">II", 2, 0))
    frame = _png_chunk(b"fcTL", bytes(26)) + _png_chunk(
        b"fdAT", bytes(4) + zlib.compress(b"\x00\x01")
    )
    moving = (
        PNG_SIGNATURE + header + control + _png_chunk(b"fcTL", bytes(26)) + picture + frame + end
    )
    assert strip_metadata(moving) == moving


# --- F7: a card's summary is taken from its description in one pass -----------------


def test_a_description_with_no_sentence_end_gives_its_summary_at_once():
    text = "[Elena Ruiz: doctor; keeps the farmhouse; " + "likes tea; " * 3_000 + "]"
    began = time.perf_counter()
    summary = tavern_cards.first_sentence(text)
    assert time.perf_counter() - began < 0.5
    assert summary.startswith("[Elena Ruiz: doctor;") and summary.endswith("…")
    assert len(summary) <= 241


def test_a_first_sentence_is_what_the_pattern_before_found():
    old = re.compile(r"(.+?[.!?])(?=\s|$)")
    rng = random.Random(13)
    pieces = ["Elena", " ", ".", "!", "?", "...", "\n", "e.g.", "3.5", " keeps the farm", "\t", ""]
    for _ in range(40_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 9)))
        flat = " ".join(text.split())
        found = old.search(flat)
        assert tavern_cards.first_sentence(text, 10_000) == (found.group(1) if found else flat)


# --- F12: a lorebook entry's keys on one line are split in one pass -----------------


def _old_split_keys(text: str) -> list[str]:
    keys: list[str] = []
    start, opened, closed = 0, False, False
    for index, char in enumerate(text):
        if char == "/" and (index == 0 or text[index - 1] != "\\"):
            if not opened and not text[start:index].strip():
                opened = True
            elif opened:
                closed = True
        elif char == "," and (not opened or closed):
            keys += tavern_lore._finished(text[start:index])
            start, opened, closed = index + 1, False, False
    return keys + tavern_lore._finished(text[start:])


def test_keys_on_one_long_line_are_split_at_once():
    """Every slash had the line so far copied to see whether a keyword had
    begun: a megabyte of them took nine seconds."""
    began = time.perf_counter()
    assert tavern_lore.split_keys("farm" + "/" * 600_000) == ["farm" + "/" * 600_000]
    assert time.perf_counter() - began < 1.0


def test_keys_are_split_as_they_were_split_before():
    rng = random.Random(17)
    pieces = ["/", ",", " ", "\\", "a", "barn", "/i", "\t", " ", "\\/", ", ", "/a,b/i", "x/y"]
    seen: set[int] = set()
    for _ in range(60_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 10)))
        keys = tavern_lore.split_keys(text)
        assert keys == _old_split_keys(text), repr(text)
        seen.add(len(keys))
    assert len(seen) > 4


# --- F8: a number of thousands of digits is unreadable JSON, said as any is ---------

LONG = "1" + "0" * 5000  # past the 4,300 digits Python reads as a whole number


def _long(text: str) -> str:
    """`text` with that number written where it says LONG."""
    return text.replace("LONG", LONG)


def test_a_number_of_thousands_of_digits_is_json_that_cant_be_read():
    with pytest.raises(json.JSONDecodeError, match="digits"):
        outside_json(_long('{"a": LONG}'))
    with pytest.raises(json.JSONDecodeError):
        outside_json((_long('{"a": [LONG]}')).encode())
    assert outside_json(_long('{"a": 1e5000, "b": "LONG"}'))["b"] == LONG, (
        "text, and a float, are read"
    )
    with pytest.raises(UnicodeDecodeError):
        outside_json(b'{"a": "\xff"}')


def test_a_models_reply_with_such_a_number_is_a_reply_that_cant_be_read():
    with pytest.raises(ValueError, match="list of characters wasn't valid JSON"):
        extract_json(_long('[{"name": "Elena", "age": LONG}]'), list, "list of characters")
    # The last whole value before it is still found.
    assert extract_json(_long('{"a": 1} then {"b": LONG}'), dict, "answer") == {"a": 1}


def test_every_reader_says_so_in_its_own_words(tmp_path: Path):
    def file(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    backup = file(
        "big.sealedlore-archive.json", _long('{"format": "sealedlore-archive", "x": LONG}')
    )
    scenario = file(
        "big.sealedlore-scenario.json", _long('{"format": "sealedlore-scenario", "x": LONG}')
    )
    card = file(
        "card.json",
        _long(
            '{"spec": "chara_card_v2", "data": {"name": "Elena Ruiz", "extensions": {"x": LONG}}}'
        ),
    )
    book = file(
        "book.json", _long('{"entries": {"0": {"key": ["a"], "content": "c", "order": LONG}}}')
    )
    prompts = file(
        "big.sealedlore-prompts.json", _long('{"format": "sealedlore-prompts", "x": LONG}')
    )
    assert archive.file_format(backup) is None
    with pytest.raises(archive.ArchiveError, match="digits"):
        archive.read_archive(backup)
    with pytest.raises(scenario_files.ScenarioError, match="digits"):
        scenario_files.read_scenario(scenario)
    assert tavern_cards.tavern_kind(card) is None and not tavern_lore.is_converted_lorebook(book)
    with pytest.raises(tavern_cards.CardError):
        tavern_cards.read_card(card)
    with pytest.raises(tavern_lore.LorebookError, match="digits"):
        tavern_lore.open_lorebook(book)
    with pytest.raises(PromptSetError, match="digits"):
        read_prompt_set(prompts)


def test_a_backups_records_and_log_with_such_a_number_are_passed_over(tmp_path: Path):
    assert not archive._records(_long('[{"id": LONG}]').encode(), GeneratedImage)
    path = tmp_path / "theirs.zip"
    with zipfile.ZipFile(path, "w") as zipped:
        zipped.writestr(
            "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 1})
        )
        zipped.writestr("characters.json", "[]")
        zipped.writestr("api_log.jsonl", _long('{"id": "a", "n": LONG}\n{"id": "b"}\n'))
    with open_backup(path) as backup:
        assert [entry["id"] for entry in collection_backup.new_log_entries(backup, [])] == ["b"]


@pytest.mark.parametrize("memories", [5, True, "abc", {"a": 1}, [5], [[]]])
def test_a_backup_whose_memories_arent_a_list_of_them_is_refused_in_words(tmp_path: Path, memories):
    """`"memories": 5` was a TypeError out of the window's import."""
    story = Story(title="Doomsville")
    data = archive.archive_data(StoryBundle(story=story), [], {})
    data = json.loads(json.dumps(data)) | {"memories": memories}
    path = tmp_path / "theirs.sealedlore-archive.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="invalid content"):
        archive.read_archive(path)


# --- F11: no number that is no number goes out in a card or a lorebook --------------


def _strict(text: str | bytes):
    """Read as JavaScript's JSON.parse reads: the bare words Infinity and NaN
    are no JSON."""

    def refused(word: str):
        raise ValueError(f"not JSON: {word}")

    return json.loads(text, parse_constant=refused)


def test_a_card_and_a_lorebook_go_out_as_json_any_program_reads(tmp_path: Path):
    """`1e999` in a card is read as an infinity, and was written back as the
    bare word Infinity: SillyTavern, and any strict reader, refused the file."""
    source = tmp_path / "elena.json"
    source.write_text(
        '{"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "Elena Ruiz", '
        '"description": "A doctor.", "first_mes": "Hello.", '
        '"extensions": {"talkativeness": 1e999, "depth": [NaN, -Infinity, 2.5]}, '
        '"character_book": {"entries": [{"keys": ["farm"], "content": "The farmhouse.", '
        '"enabled": true, "insertion_order": 1e999, "extensions": {"probability": 1e999}}]}}}',
        encoding="utf-8",
    )
    card = tavern_cards.read_card(source)
    elena, _notes = collection.kept_from_card(card, MemoryPictures(lambda kind, body: "id"))
    v2, v3 = tavern_cards.card_data(elena)
    tavern_cards.write_card(tmp_path / "out.json", v2, v3, None)
    written = _strict((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert written["data"]["extensions"]["talkativeness"] is None
    assert written["data"]["extensions"]["depth"] == [None, None, 2.5]
    assert written["data"]["name"] == "Elena Ruiz"
    # In a picture, and as a CHARX.
    png = tavern_cards.card_png(_png(10), v2, v3)
    for text in png_text.text_chunks(png).values():
        assert _strict(base64.b64decode(text))["data"]["name"] == "Elena Ruiz"
    with zipfile.ZipFile(io.BytesIO(tavern_cards.card_charx(v3, []))) as charx:
        assert _strict(charx.read("card.json"))["data"]["extensions"]["talkativeness"] is None
    # A lorebook, SillyTavern's file and SealedLore's lore as one.
    book = tmp_path / "book.json"
    book.write_text(
        '{"entries": {"0": {"uid": 0, "key": ["farm"], "content": "The farmhouse.", '
        '"order": 1e999, "probability": NaN}}}',
        encoding="utf-8",
    )
    kept = lorebooks.kept_from(tavern_lore.open_lorebook(book), "Farm", "book.json")
    lorebooks.export(kept, tmp_path / "book-out.json")
    assert _strict((tmp_path / "book-out.json").read_text(encoding="utf-8"))["entries"]
    tavern_lore.write_world(tmp_path / "world.json", elena.tavern.character_book)
    assert _strict((tmp_path / "world.json").read_text(encoding="utf-8"))["entries"]


def test_the_apps_own_files_keep_such_a_number_and_load_again(tmp_path: Path):
    """Its own files are read by Python, which takes the words back: written
    as null there, a cost that is no number would be a story that can't load."""
    node = Node(kind="assistant", speaker_id="__narrator__", content="The road is empty.")
    node.meta.usage = Usage(prompt_tokens=5, cost=float("inf"))
    story = Story(title="Doomsville", active_leaf_id=node.id)
    save_story_bundle(StoryBundle(story=story, nodes=[node]), root=tmp_path)
    loaded = load_story_bundle(story.id, root=tmp_path)
    assert loaded.nodes[0].meta.usage.cost == float("inf")
    assert json_for_others({"a": float("nan"), "b": [float("inf"), 1.5]}) == (
        '{"a": null, "b": [null, 1.5]}'
    )
