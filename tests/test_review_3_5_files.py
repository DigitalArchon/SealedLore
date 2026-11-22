"""What the review of 3.5.0b1 found in files from strangers (Nov 22 2026),
one test a fault: each fails on 0f289da, but those that hold a rewritten
reader to what it read before. Every hostile file is made here, small, from
invented names."""

from __future__ import annotations

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
from sealedlore.engine.plot_md import parse_plot_markdown, read_clause
from sealedlore.engine.usage import usage_report
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.models.image import ImageRef
from sealedlore.models.plot import EventDef, Plot
from sealedlore.models.story import BUDGET_MOST, StoryDefaults
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage import collection, collection_backup
from sealedlore.storage.collection_backup import BackupError, open_backup, restore
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import DiskPictures
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
        FTYP
        + _box(
            b"moov", _box(b"trak", _box(b"mdia", _box(b"stsd", bytes(8) + _box(b"abcd") * 60_000)))
        ),
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
