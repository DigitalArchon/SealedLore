"""Plot files as documents: what the editor writes is what the importer reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.plot_file import (
    CHARACTER,
    EVENT,
    FACT,
    STORYTELLER,
    check_plot_document,
    describe_where,
    read_plot_document,
    render_plot_markdown,
)
from sealedlore.engine.plot_md import parse_plot_markdown, picture_line
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


@pytest.mark.parametrize(
    "path",
    [
        SAMPLES / "Doomsville.md",
        EXTENDED,
        # Variants that only gate (no tell of their own).
        SAMPLES / "The Ninth Furnace.md",
        SAMPLES / "Brasshaven.md",
    ],
    ids=["sample", "extended", "furnace", "brasshaven"],
)
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


def test_pictures_are_read_written_and_handed_to_whoever_imports():
    """A picture line is a file beside the plot file. The parser reads no
    files: it checks the path and hands the list on. A field the reader
    doesn't carry is a field the editor silently deletes."""
    text = EXTENDED.read_text(encoding="utf-8")
    parsed = parse_plot_markdown(text)
    assert parsed.ok
    found = [(p.owner_kind, p.owner_name, p.source, p.caption) for p in parsed.pictures]
    assert found == [
        ("character", "Jane Moss", "pictures/jane-moss.png", "in her army coat, rifle slung"),
        ("lore", "Hellsville", "pictures/hellsville-gate.png", "the east gate and its watchtower"),
        # More than one to an item, a caption or none.
        ("lore", "Hellsville", "pictures/hellsville-gate.png", ""),
        ("lore", "Jane's truck", "pictures/janes-truck.png", "from the front, one headlight gone"),
        # The picture behind the transcript, from the front matter.
        ("background", "The background", "pictures/hellsville-gate.png", ""),
    ]
    jane = next(c for c in parsed.scenario.cast if c.name == "Jane Moss")
    assert parsed.pictures[0].owner_id == jane.id
    # A character's picture carries its look and expression after the caption.
    assert (parsed.pictures[0].look, parsed.pictures[0].expression) == ("army coat", "watchful")
    assert all(not (p.look or p.expression) for p in parsed.pictures[1:])
    assert jane.reference_images == []  # until the files are read
    assert jane.summary.startswith("A thirty-year-old")  # the lines around it still read

    document = read_plot_document(text).document
    assert [(p.file, p.caption, p.look, p.expression) for p in document.characters[0].pictures] == [
        ("pictures/jane-moss.png", "in her army coat, rifle slung", "army coat", "watchful")
    ]
    assert len(document.places[0].pictures) == 2
    assert document.background == "pictures/hellsville-gate.png"
    rendered = render_plot_markdown(document).text
    assert (
        "- picture: pictures/jane-moss.png | in her army coat, rifle slung"
        " | look: army coat | expression: watchful\n"
    ) in rendered
    again = parse_plot_markdown(rendered)
    assert [(p.owner_name, p.source, p.caption) for p in again.pictures] == [
        row[1:] for row in found
    ]
    assert (again.pictures[0].look, again.pictures[0].expression) == ("army coat", "watchful")


@pytest.mark.parametrize(
    ("value", "read"),
    [
        ("pictures/a.png", ("pictures/a.png", "", "", "")),
        ("pictures/a.png | front view", ("pictures/a.png", "front view", "", "")),
        # Labels in either order, the key in any case, the value's spaces tidied.
        (
            "pictures/a.png | Expression:  in a   fighting stance | LOOK: armour",
            ("pictures/a.png", "", "armour", "in a fighting stance"),
        ),
        # A caption keeps its own | and colons: only a label is a label.
        (
            "pictures/a.png | east | west: the gate | look: armour",
            ("pictures/a.png", "east | west: the gate", "armour", ""),
        ),
        ("pictures/a.png | look:", ("pictures/a.png", "", "", "")),
    ],
)
def test_a_picture_line_reads_its_caption_and_labels(value: str, read: tuple):
    line = picture_line(value)
    assert (line.source, line.caption, line.look, line.expression) == read


def test_picture_labels_are_a_characters_and_said_once():
    """A label twice is noted and the first kept; a place's picture has no
    look, so its labels are noted and left off: parsing never guesses."""
    text = (
        "# World\n\nA town.\n\n# Characters\n\n## Jane\n- role: player\n"
        "- picture: pictures/jane.png | look: coat | look: armour | expression: angry\n\n"
        "A medic.\n\n# Places\n\n## Hellsville\n"
        "- picture: pictures/town.png | the gate | look: winter\n\nA town.\n"
    )
    parsed = parse_plot_markdown(text)
    assert parsed.ok
    jane, town = parsed.pictures
    assert (jane.look, jane.expression) == ("coat", "angry")
    assert (town.caption, town.look, town.expression) == ("the gate", "", "")
    notes = [(p.line, p.message) for p in parsed.problems]
    assert (9, "The picture line names its look twice; the first is kept.") in notes
    assert any(line == 16 and "Only a character's pictures" in m for line, m in notes)
    # The editor keeps what is written and says what it couldn't keep.
    read = read_plot_document(text)
    assert [(p.look, p.expression) for p in read.document.characters[0].pictures] == [
        ("coat", "angry")
    ]
    assert any("naming its look twice" in p.message for p in read.problems)


def test_a_background_must_be_a_picture_inside_the_files_own_folder():
    for value in ("/etc/passwd.png", "../away.jpg", "notes.txt"):
        parsed = parse_plot_markdown(f"---\nbackground: {value}\n---\n\n# World\n\nA town.\n")
        assert not parsed.ok and [p.line for p in parsed.problems if p.error] == [2]
        assert parsed.pictures == []


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("- picture: /etc/passwd.png", "own folder"),
        ("- picture: ../elsewhere/jane.png", "own folder"),
        ("- picture: pictures/../../jane.png", "own folder"),
        ("- picture: C:\\Users\\jane.png", "own folder"),
        ("- picture: ~/jane.png", "own folder"),
        ("- picture: pictures/notes.txt", "isn't a picture"),
        ("- picture: | just a caption", "needs a file"),
    ],
)
def test_a_picture_must_be_a_picture_inside_the_files_own_folder(line: str, message: str):
    """A plot file may be someone else's: its lines must not reach outside."""
    text = f"# World\n\nA town.\n\n# Characters\n\n## Jane\n- role: player\n{line}\n\nA medic.\n"
    parsed = parse_plot_markdown(text)
    errors = [p for p in parsed.problems if p.error]
    assert not parsed.ok and len(errors) == 1
    assert errors[0].line == 9 and message in errors[0].message
    assert parsed.pictures == []

    # The editor keeps the line as written and shows the importer's verdict
    # against the character it belongs to.
    read = read_plot_document(text)
    if "needs a file" in message:
        # Nothing to keep, so it says so before the file is opened.
        assert [p.line for p in read.problems] == [9]
        return
    _rendered, problems = check_plot_document(read.document)
    assert [(p.where, p.error) for p in problems if p.error] == [((CHARACTER, 0, -1), True)]


def test_a_windows_path_is_read_with_forward_slashes():
    text = "# Characters\n\n## Jane\n- picture: pictures\\jane.png | front\n\nA medic.\n"
    assert [p.source for p in parse_plot_markdown(text).pictures] == ["pictures/jane.png"]


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


# --- the storyteller's notes ---------------------------------------------------------

NOTED = """\
# World

A harbour town. Old Bram keeps the light.

# Characters

## Mara Quill
- role: player
- present: yes

A boatwright.

## Teodor Vask
- hidden: yes

A customs captain.

## Old Bram
- hidden: yes

The lighthouse keeper.

# Places

## The Drowned Chapel
- keywords: the sea cave, the cave
- hidden: yes

A chapel in a sea cave.

# Storyteller's notes

{notes}

# Events

## The captain comes
- brings: Teodor Vask, Old Bram
- reveals: The Drowned Chapel

A ship puts in.
"""


def test_the_storytellers_notes_are_a_section_of_prose_imported_unread():
    """`# Storyteller's notes`: what only the storyteller is told. Taken as
    written, sub-headings included, and whoever imports the file has not
    read them (Setup keeps them unread until asked)."""
    written = (
        "The harbour master is in the smugglers' pay,\nand has been for years.\n\n"
        "## What they hide\n\nMara Quill owes him money."
    )
    parsed = parse_plot_markdown(NOTED.format(notes=written))
    assert parsed.ok and parsed.problems == []
    scenario = parsed.scenario
    # As prose is read anywhere in a file: a wrapped line joins the one before.
    assert scenario.storyteller_notes == (
        "The harbour master is in the smugglers' pay, and has been for years.\n\n"
        "## What they hide\nMara Quill owes him money."
    )
    assert scenario.storyteller_notes_by == "model"
    # Never lore: nothing to reveal, nowhere to be, no keywords to wake it.
    assert [entry.title for entry in scenario.lore] == ["The Drowned Chapel"]
    assert [place.name for place in scenario.plot.places] == ["The Drowned Chapel"]
    assert "harbour master" not in scenario.model_dump_json(exclude={"storyteller_notes"})


def test_the_notes_are_read_anywhere_among_the_sections_and_under_either_name():
    plain = "The harbour master is in the smugglers' pay.\n\nHe hides it badly."
    for heading in ("Storyteller's notes", "Storyteller’s notes", "Notes for the storyteller"):
        first = f"# {heading}\n\n{plain}\n\n# World\n\nA harbour town.\n"
        last = f"# World\n\nA harbour town.\n\n# {heading}\n\n{plain}\n"
        for text in (first, last):
            parsed = parse_plot_markdown(text)
            assert parsed.ok and parsed.problems == []
            assert parsed.scenario.storyteller_notes == plain
    # Once: a second section is an error, as two of any section are.
    twice = (
        "# World\n\nA town.\n\n# Storyteller's notes\n\nOne.\n\n"
        "# Notes for the storyteller\n\nTwo.\n"
    )
    parsed = parse_plot_markdown(twice)
    assert [(p.line, p.error) for p in parsed.problems] == [(9, True)]


def test_an_empty_notes_section_is_no_notes():
    parsed = parse_plot_markdown("# World\n\nA town.\n\n# Storyteller's notes\n\n# Facts\n")
    assert parsed.ok
    assert parsed.scenario.storyteller_notes is None
    assert parsed.scenario.storyteller_notes_by == "author"
    # As a file with no such section.
    plain = parse_plot_markdown("# World\n\nA town.\n").scenario
    assert plain.storyteller_notes is None and plain.storyteller_notes_by == "author"


def test_notes_keep_lines_that_look_like_settings_as_prose():
    text = (
        "# World\n\nA town.\n\n# Storyteller's notes\n\n"
        "- hidden: the ledger, under the floor\n- keywords: none of these are settings\n"
    )
    parsed = parse_plot_markdown(text)
    assert parsed.problems == []
    assert parsed.scenario.storyteller_notes == (
        "- hidden: the ledger, under the floor\n- keywords: none of these are settings"
    )


def test_notes_that_name_someone_hidden_are_noted_never_refused():
    """The storyteller has the notes from the first passage: a hidden person
    or place named there is known to it before the event that brings them in.
    A note, since the author may mean it."""
    secret = "Teodor Vask is paid by the smugglers. The crates wait in the sea cave."
    parsed = parse_plot_markdown(NOTED.format(notes=secret))
    assert parsed.ok
    found = [p.message for p in parsed.problems]
    assert found == [
        "The storyteller's notes name Teodor Vask, who is hidden: the storyteller would learn "
        "of them before the event that brings them in.",
        "The storyteller's notes name The Drowned Chapel, which is hidden: the storyteller "
        "would learn of it before the event that reveals it.",
    ]
    heading = NOTED.split("\n").index("# Storyteller's notes") + 1
    assert {p.line for p in parsed.problems} == {heading}, "the section's heading"
    # By any part of a name, as the story finds people; a one-word keyword is
    # the story's everyday word, not the secret.
    partly = parse_plot_markdown(NOTED.format(notes="Vask is paid. They wait in the cave."))
    assert ["Teodor Vask" in p.message for p in partly.problems] == [True]
    # A name the world already uses is no secret, and notes that name nobody
    # hidden say nothing.
    assert parse_plot_markdown(NOTED.format(notes="Old Bram is paid too.")).problems == []
    assert parse_plot_markdown(NOTED.format(notes="Mara Quill owes money.")).problems == []


def test_a_document_carries_the_notes_and_writes_them_between_lore_and_facts():
    """A field the reader doesn't carry is a field the editor silently
    deletes: the fixture's notes are read, written after `# Lore` and before
    `# Facts`, and imported the same again."""
    text = EXTENDED.read_text(encoding="utf-8")
    notes = parse_plot_markdown(text).scenario.storyteller_notes
    assert notes.startswith("The Northern Camp's council has known for two winters")
    assert "## What they hide" in notes and notes.endswith("has not said so.")

    document = read_plot_document(text).document
    assert document.storyteller_notes == notes
    assert document.notes.startswith("Two strands"), "the author's own comment is another thing"
    rendered = render_plot_markdown(document).text
    lore, section, facts = (
        rendered.index("\n# Lore\n"),
        rendered.index("\n# Storyteller's notes\n"),
        rendered.index("\n# Facts\n"),
    )
    assert lore < section < facts
    assert parse_plot_markdown(rendered).scenario.storyteller_notes == notes
    # A document with none writes no section.
    assert "Storyteller" not in render_plot_markdown(PlotDocument(world="A town.")).text


def test_the_reader_takes_the_notes_wherever_and_however_they_are_headed():
    plain = "The harbour master is in the smugglers' pay.\n\nHe hides it badly."
    for heading in ("Storyteller's notes", "Notes for the storyteller"):
        text = f"# {heading}\n\n{plain}\n\n# World\n\nA harbour town.\n"
        read = read_plot_document(text)
        assert read.problems == [] and read.document.storyteller_notes == plain
        # Written back under the one heading, in its place.
        assert render_plot_markdown(read.document).text.endswith(
            f"# Storyteller's notes\n\n{plain}\n"
        )
    twice = (
        "# World\n\nA town.\n\n# Storyteller's notes\n\nOne.\n\n"
        "# Notes for the storyteller\n\nTwo.\n"
    )
    read = read_plot_document(twice)
    assert read.document.storyteller_notes == "One."
    assert any("second such section" in p.message for p in read.problems)
    # Lines shaped like settings are the notes' own prose, there and back.
    listed = "- hidden: the ledger, under the floor\n- keywords: none of these are settings"
    document = read_plot_document(f"# World\n\nA town.\n\n# Storyteller's notes\n\n{listed}\n")
    again = parse_plot_markdown(render_plot_markdown(document.document).text)
    assert again.scenario.storyteller_notes == listed and again.problems == []


def test_a_hidden_name_in_the_notes_is_a_problem_of_the_notes_own():
    """The editor shows the importer's note against the notes' page."""
    secret = "Teodor Vask is paid by the smugglers. The crates wait in the sea cave."
    document = read_plot_document(NOTED.format(notes=secret)).document
    document.title = "Salt and Tide"
    _rendered, problems = check_plot_document(document)
    assert [(p.where, p.error) for p in problems] == [((STORYTELLER, 0, -1), False)] * 2
    assert describe_where(document, problems[0].where) == "Storyteller's notes"
