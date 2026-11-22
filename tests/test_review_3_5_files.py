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
import zipfile
import zlib
from pathlib import Path

import pytest

from sealedlore.engine import plot_md
from sealedlore.engine.plot_md import parse_plot_markdown, read_clause
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage import collection
from sealedlore.storage.collection_backup import open_backup, restore
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import DiskPictures

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
