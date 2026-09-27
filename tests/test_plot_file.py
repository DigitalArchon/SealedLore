"""Plot files as documents: what the editor writes is what the importer reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.plot_file import (
    CHARACTER,
    EVENT,
    FACT,
    check_plot_document,
    describe_where,
    read_plot_document,
    render_plot_markdown,
)
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.models.plot_file import (
    AssignmentDoc,
    CharacterDoc,
    ConditionDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    PlotDocument,
    VariantDoc,
)
from sealedlore.models.scenario import Scenario

SAMPLES = Path(__file__).resolve().parents[1] / "SampleStories"
# Doomsville with every part of the format the sample leaves out.
EXTENDED = Path(__file__).resolve().parent / "data" / "doomsville_extended.md"


def normalised(scenario: Scenario) -> dict:
    """The scenario with every generated id replaced by the name it stands for."""
    names = {c.id: c.name for c in (*scenario.cast, *scenario.supporting)}
    names.update({entry.id: entry.title for entry in scenario.lore})

    def walk(value):
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items() if k not in ("id", "lore_id")}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return names.get(value, value) if isinstance(value, str) else value

    return walk(scenario.model_dump())


@pytest.mark.parametrize("path", [SAMPLES / "Doomsville.md", EXTENDED], ids=["sample", "extended"])
def test_a_sample_survives_reading_and_writing(path: Path):
    text = path.read_text(encoding="utf-8")
    read = read_plot_document(text)
    assert read.problems == []

    rendered, problems = check_plot_document(read.document)
    assert problems == []
    assert normalised(parse_plot_markdown(rendered.text).scenario) == normalised(
        parse_plot_markdown(text).scenario
    )
    # Writing what was read back gives the same file again.
    again = render_plot_markdown(read_plot_document(rendered.text).document)
    assert again.text == rendered.text


def test_an_optional_events_days_are_kept():
    text = (SAMPLES / "Doomsville.md").read_text(encoding="utf-8")
    bandits = next(e for e in read_plot_document(text).document.events if "bandit" in e.title)
    assert not bandits.timeline and bandits.when == (2, 60)


def test_problems_point_at_the_item_that_caused_them():
    document = PlotDocument(
        title="Test",
        characters=[CharacterDoc(name="Ann", role="player")],
        places=[EntryDoc(name="Town", description="A town.")],
        facts=[FactDoc(name="town", initial="standing", values=["standing", "fallen"])],
        events=[
            EventDoc(
                title="The fall",
                timeline=True,
                when=(3, 5),
                tell="It falls.",
                requires=[ConditionDoc(kind="fact", fact="twon", value="standing")],
                variants=[
                    VariantDoc(
                        title="Away",
                        requires=[ConditionDoc(kind="at", negated=True, place="City")],
                        sets=[AssignmentDoc(fact="town", value="burnt")],
                    )
                ],
            ),
        ],
    )
    _, problems = check_plot_document(document)
    by_where = {(p.where, p.message) for p in problems}
    assert ((EVENT, 0, -1), "No fact called `twon` (the Facts section defines them).") in by_where
    assert ((EVENT, 0, 0), "No place called “City” (the Places section).") in by_where
    assert ((EVENT, 0, 0), "`town` can't be “burnt”; it is one of: standing, fallen.") in by_where
    assert all(p.error for p in problems if p.where == (EVENT, 0, 0))
    assert describe_where(document, (EVENT, 0, 0)) == "Event “The fall”, variant “Away”"
    assert describe_where(document, None) == "File"


def test_the_document_raises_what_the_renderer_would_hide():
    document = PlotDocument(
        title="Test",
        start_time="9",
        characters=[CharacterDoc(name="")],
        facts=[FactDoc(name="Town State", initial="a, b")],
        events=[EventDoc(title="Fire and ice", timeline=True, tell="x")],
    )
    _, problems = check_plot_document(document)
    messages = {(p.where, p.message) for p in problems if p.error}
    assert (("story", 0, -1), "The start time “9” isn't HH:MM.") in messages
    assert ((CHARACTER, 0, -1), "This character needs a name.") in messages
    assert any(where == (FACT, 0, -1) and "lower-case" in text for where, text in messages)
    assert any(where == (FACT, 0, -1) and "can't be a value" in text for where, text in messages)
    assert ((EVENT, 0, -1), "A timeline event needs its days.") in messages


def test_a_name_the_line_would_cut_in_two_is_named():
    document = PlotDocument(
        title="Test",
        events=[
            EventDoc(title="Fire and ice", tell="x"),
            EventDoc(
                title="After",
                tell="y",
                requires=[ConditionDoc(kind="happened", event="Fire and ice")],
            ),
        ],
    )
    _, problems = check_plot_document(document)
    assert any(
        p.where == (EVENT, 1, -1) and "“Fire and ice” can't be named here" in p.message
        for p in problems
    )


def test_reading_keeps_what_it_cannot_read_and_reports_what_it_drops():
    text = """---
title: Odd
flavour: strong
---

# World
Rules.

# Weather
Rainy.

# Places

## Town
A town.

# Facts
- town: standing (standing, fallen)

# Events

## Storm
- requires: town = standing, the moon rises!, at = Harbour
- brings: Nobody
- offscreen: maybe
A storm.
"""
    read = read_plot_document(text)
    document = read.document
    assert document.title == "Odd" and document.world == "Rules."
    storm = document.events[0]
    assert [c.kind for c in storm.requires] == ["fact", "text", "at"]
    assert storm.requires[1].text == "the moon rises!"
    # An unknown place is kept by name for the importer to point at.
    assert storm.requires[2].place == "Harbour"
    assert storm.brings == ["Nobody"]
    messages = [p.describe() for p in read.problems]
    assert any("`flavour: strong` isn't a setting" in m for m in messages)
    assert any("“# Weather” isn't a section" in m for m in messages)
    assert any("`offscreen` should be yes or no" in m for m in messages)
    # And the rendered file carries the clause as written, so the importer flags it.
    _, problems = check_plot_document(document)
    assert any("Can't read the condition “the moon rises!”" in p.message for p in problems)
    assert any("No place called “Harbour”" in p.message for p in problems)


def test_comments_become_notes_and_are_written_back():
    text = "<!-- my notes -->\n\n# World\nRules.\n"
    document = read_plot_document(text).document
    assert document.notes == "my notes"
    rendered = render_plot_markdown(document).text
    assert rendered.startswith("<!--\nmy notes\n-->")
    assert parse_plot_markdown(rendered).scenario.world_bible == "Rules."


def test_sub_headings_in_a_description_are_kept():
    text = (
        "# Characters\n\n## Ann\n- role: player\n\nA woman.\n\n### Skills\n- Strength: legendary\n"
    )
    parsed = parse_plot_markdown(text)
    assert parsed.scenario.cast[0].full_description == (
        "A woman.\n\n### Skills\n\n- Strength: legendary"
    )
    document = read_plot_document(text).document
    assert "### Skills" in document.characters[0].description
    rendered, problems = check_plot_document(document)
    assert not any(p.error for p in problems)
    assert "### Skills" in rendered.text


def test_renaming_follows_every_reference():
    document = read_plot_document((SAMPLES / "Doomsville.md").read_text(encoding="utf-8")).document
    document.rename_fact("hellsville", "town")
    document.rename_place("Hellsville", "Hell's Ville")
    document.rename_event("The caves", "Cave country")
    document.rename_character("Elena Ruiz", "Elena Cruz")
    _, problems = check_plot_document(document)
    assert problems == []
    fall = next(e for e in document.events if e.title == "The fall of Hellsville")
    assert fall.requires[0].fact == "town"
    assert fall.variants[0].at == "Hell's Ville" and fall.variants[0].brings[1] == "Elena Cruz"
    ben = next(e for e in document.events if e.title == "Ben in the caves")
    assert ben.requires[0].event == "Cave country"


def test_an_empty_document_renders_nothing_alarming():
    rendered, problems = check_plot_document(PlotDocument())
    assert rendered.text.strip() == ""
    assert all(not p.error for p in problems)
