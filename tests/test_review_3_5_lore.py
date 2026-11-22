"""What the review of 3.5.0b1 found in lore and placeholders (Nov 22 2026),
one test a fault: each fails on 0f289da but those that hold ordinary
patterns to what they did. The scripted mock provider and made-up lorebooks:
no network."""

from __future__ import annotations

import random
import time

import pytest

from sealedlore.engine import tavern_firing
from sealedlore.engine.tavern_firing import (
    FireEntry,
    TavernSettings,
    Timed,
    fire,
    fire_entry,
)
from sealedlore.storage.tavern_lore import lore_from_book

WORDS = lambda text: len(text.split())  # noqa: E731 - a token a word


def entry(id: str, keys=(), **fields) -> FireEntry:  # noqa: A002
    return FireEntry(
        id=id, title=id, content=fields.pop("content", f"About {id}."), keys=tuple(keys), **fields
    )


def run(entries, messages, *, settings=None, context=1000, **kw):
    return fire(
        entries,
        messages,
        settings=settings or TavernSettings(),
        context=context,
        count_tokens=WORDS,
        timed=Timed(),
        length=len(messages),
        rng=random.Random(1),
        **kw,
    )


def prose(chars: int, seed: int = 1) -> str:
    """Ordinary words, as a story's messages and a lorebook's entries are."""
    words = (
        "the farm was quiet when John came back from Hellsville with the cart "
        "and two sacks of grain"
    ).split()
    rng = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < chars:
        out.append(rng.choice(words))
        size += len(out[-1]) + 1
    return " ".join(out)[:chars]


# --- L1: patterns the screen let through ------------------------------------------

# Each read all it was given: 15 s on 80,000 characters for the first three
# (a look-ahead reading to the end of the text from every place in it),
# seconds for counts inside counts, and for ways-times-length 12 s on 4,900.
SLOW_ON_PROSE = [
    r"/(?![\s\S]*)/",
    r"/(?=[\s\S]*)§/",
    r"/(?=[\w\s]*)§/",
    r"/(?:(?:(?:[^§]{16}){16}){16}){16}§/",
    r"/(?:(?:(?:a{16}){16}){16}){16}b/",
    "/" + "(?:.{16}|.{15})" * 12 + ".{16}" * 150 + "§/s",
    "/" + "(?:.{16}|.{15})" * 6 + ".{16}" * 150 + "§/s",
    "/(?=" + "(?:.{16}|.{15})" * 12 + ".{16}" * 150 + "§)x/s",
]


@pytest.mark.parametrize("key", SLOW_ON_PROSE)
def test_a_pattern_that_reads_far_from_every_place_reads_only_so_much(key: str):
    for text in (prose(80_000), "a" * 80_000):
        began = time.perf_counter()
        result = run([entry("slow", [key])], [text])
        assert time.perf_counter() - began < 3.0, key
        assert not result.fired


def test_optional_parts_inside_a_look_ahead_are_counted_too():
    """Forty optional letters are 2^40 ways wherever they stand: inside a
    look-ahead they weren't counted, and forty letters hung the window."""
    key = "/(?=" + "a?" * 40 + "a" * 40 + ")x/"
    began = time.perf_counter()
    result = run([entry("hang", [key])], ["a" * 40 + " and more"])
    assert time.perf_counter() - began < 1.0
    assert "is never run" in result.not_fired["hang"]


def test_a_new_story_with_a_long_constant_entry_opens_at_once():
    """The reviewer's case: one message, a constant entry of 80,000
    characters read by recursion, and a pattern that took 16.5 s on it."""
    book = {
        "entries": {
            "0": {
                "uid": 0,
                "comment": "The valley",
                "content": prose(80_000, seed=3),
                "key": [],
                "constant": True,
                "order": 100,
            },
            "1": {
                "uid": 1,
                "comment": "The Hollow",
                "content": "A sinkhole.",
                "key": [r"/(?![\s\S]*)/"],
                "constant": False,
                "order": 90,
            },
        }
    }
    entries = [
        fire_entry(
            id=e.id,
            title=e.title,
            content=e.content,
            keywords=e.keywords,
            always_on=e.always_on,
            enabled=e.enabled,
            raw=e.tavern.fields,
            numbered=e.tavern.numbered,
            decorators=e.tavern.decorators,
        )
        for e in lore_from_book(book).entries
    ]
    began = time.perf_counter()
    result = fire(
        entries,
        ["You: I open the door."],
        settings=TavernSettings(),
        context=60000,
        count_tokens=lambda text: len(text) // 4,
        timed=Timed(),
        length=1,
        free={entries[0].id},
    )
    assert time.perf_counter() - began < 3.0
    assert "read only the newest 8,000 characters" in result.not_fired[entries[1].id]


@pytest.mark.parametrize(
    "key",
    [
        r"/wol(f|ves)/i",
        r"/\bHollow\b/",
        r"/drag\w*/i",
        r"/(?<what>wolves|wyrms) of \w+/i",
        r"/(?:king|queen|prince|princess|duke|duchess) of (?:the )?(?:north|south|east|west)/i",
        r"/\b(?:sword|blade|dagger|knife|axe|spear|bow|arrow|shield|helm|helmet)s?\b/i",
        r"/Hollow(?= road)/",
        r"/(?<!the )Hollow\b/",
        r"/\d{4}-\d{2}-\d{2}/",
    ],
)
def test_ordinary_patterns_read_all_a_story_scans(key: str):
    """Whatever they are looking for 100,000 characters back is found, and
    nothing is said of them."""
    found = "Wolves of Hollow road, 2026-11-22: the duke of the north drew a dragon dagger."
    result = run([entry("d", [key])], [prose(100_000) + " " + found])
    assert result.ids == ["d"], key
    assert not result.not_fired
    assert not run([entry("d", [key])], [prose(100_000)]).fired


@pytest.mark.parametrize(
    "key", [r"/wolves.*road/i", r"/duke [\s\S]*? dagger/", r"/Hollow(?=.* dagger)/"]
)
def test_patterns_with_a_repeat_in_the_middle_fire_as_before(key: str):
    """/dragon.*fire/ and its like: the newest 8,000 characters, as they did."""
    found = "Wolves of Hollow road: the duke of the north drew a dagger."
    pattern = tavern_firing._pattern(key)
    assert pattern is not None and pattern.cap == tavern_firing.SCAN_CAPS[1]
    assert run([entry("d", [key])], [found + " " + prose(20_000)]).ids == ["d"]
    far = run([entry("d", [key])], [prose(20_000) + " " + found])
    assert "read only the newest 8,000 characters" in far.not_fired["d"]


def test_every_pattern_reads_only_so_much():
    """No pattern reads without limit, whatever the screen makes of it: the
    simplest stop at SCAN_MOST, far past what a story scans."""
    for key in (r"/wol(f|ves)/i", r"/\bHollow\b/", r"/drag\w*/i"):
        pattern = tavern_firing._pattern(key)
        assert pattern is not None and pattern.cap == tavern_firing.SCAN_MOST
    text = "x" * tavern_firing.SCAN_MOST + " the Hollow"
    began = time.perf_counter()
    result = run([entry("far", [r"/\bHollow\b/"])], [text])
    assert time.perf_counter() - began < 1.0
    assert f"read only the newest {tavern_firing.SCAN_MOST:,} characters" in result.not_fired["far"]
