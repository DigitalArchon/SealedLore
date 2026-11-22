"""What the review of 3.5.0b1 found in files from strangers (Nov 22 2026),
one test a fault: each fails on 0f289da, but those that hold a rewritten
reader to what it read before. Every hostile file is made here, small, from
invented names."""

from __future__ import annotations

import random
import re
import time

import pytest

from sealedlore.engine import plot_md
from sealedlore.engine.plot_md import parse_plot_markdown, read_clause

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
