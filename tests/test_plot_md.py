"""Plot files: Markdown in, a scenario with facts and events out, problems by line."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config
from sealedlore.models.plot import Assignment, Condition
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import save_story_bundle
from sealedlore.storage.scenario import (
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    write_scenario,
)

SAMPLE = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"

MINIMAL = """\
---
title: Test
start: day 2, 08:30
---
# World
A cold place.

# Characters
## Ann
- role: player
- choose: start
## Bo
- role: player
- choose: start
## Cy
- present: yes

# Facts
- town: standing (standing, fallen) — the state of the town
- visited: no — yes once Ann has been there

# Timeline
## Attack
- when: day 3–5
- sets: town = fallen
### Here
- requires: visited = yes
- tell: The dead come.
### Away
- requires: not visited
- offscreen: yes

# Events
## Greeting
- requires: town = standing, attack not happened
- trigger: Ann arrives.
The mayor greets her.
"""


def problems(text: str) -> list[str]:
    return [problem.describe() for problem in parse_plot_markdown(text).problems]


def test_the_sample_parses_without_errors():
    parsed = parse_plot_markdown(SAMPLE.read_text(encoding="utf-8"))

    assert parsed.ok, [problem.describe() for problem in parsed.problems]
    scenario = parsed.scenario
    assert scenario.title == "Doomsville"
    assert scenario.world_bible and "four years after" in scenario.world_bible
    assert scenario.starting_scene.location.startswith("A seemingly abandoned farmhouse")
    names = {character.name: character for character in scenario.cast}
    assert set(names) == {"John", "Jane", "Ben", "Evan Stevens", "Marcus Webb", "Elena Ruiz"}
    assert scenario.choose_one_of == [names["John"].id, names["Jane"].id]
    assert not names["Marcus Webb"].is_player_available
    assert names["Ben"].is_player_available
    assert [entry.title for entry in scenario.lore] == ["Hellsville", "The Hidden Caves"]

    plot = scenario.plot
    assert plot is not None
    assert plot.start_minutes == 15 * 60 + 30
    assert plot.fact("hellsville").values == ["standing", "under attack", "saved", "fallen"]
    fall = plot.event("the_fall_of_hellsville")
    assert fall.kind == "timed" and fall.window == (30, 50) and fall.lead_in
    assert [variant.offscreen for variant in fall.variants] == [False, False, True]
    assert fall.variants[2].sets == [
        Assignment(fact="hellsville", value="fallen"),
        Assignment(fact="elena", value="fleeing east"),
    ]
    assert plot.event("the_caves").kind == "optional"


def test_the_sample_has_nothing_to_note():
    assert problems(SAMPLE.read_text(encoding="utf-8")) == []


def test_shared_name_parts_are_pointed_out():
    text = MINIMAL.replace("## Cy\n", "## Ann Smith\n## Cy\n")
    assert any("Ann and Ann Smith both answer to “Ann”" in line for line in problems(text))


def test_events_fields_variants_and_conditions():
    parsed = parse_plot_markdown(MINIMAL)
    assert parsed.ok, problems(MINIMAL)
    plot = parsed.scenario.plot
    assert plot.start_minutes == 1440 + 8 * 60 + 30

    attack = plot.event("attack")
    # An event-level `sets` applies to every variant.
    assert all(v.sets == [Assignment(fact="town", value="fallen")] for v in attack.variants)
    assert attack.variants[1].requires == [Condition(fact="visited", value="no")]
    assert attack.variants[1].offscreen

    greeting = plot.event("greeting")
    assert greeting.requires == [
        Condition(fact="town", value="standing"),
        Condition(event_id="attack", happened=False),
    ]
    # Prose after the fields is what the storyteller is told.
    assert greeting.tell == "The mayor greets her."
    assert greeting.pacing == "quiet"


def test_present_and_choose():
    scenario = parse_plot_markdown(MINIMAL).scenario
    ids = {character.name: character.id for character in scenario.cast}
    assert scenario.starting_scene.present_character_ids == [ids["Cy"]]
    assert scenario.choose_one_of == [ids["Ann"], ids["Bo"]]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("- requires: visited = yes", "- requires: seen = yes"), "line 27: No fact called `seen`"),
        (("- requires: visited = yes", "- requires: town = razed"), "line 27: `town` can't be"),
        (("- when: day 3–5", "- when: soon"), "line 24: Can't read the days"),
        (("- when: day 3–5\n", ""), "is on the timeline but has no `when`"),
        (("attack not happened", "siege not happened"), "No event called “siege”"),
        (("- offscreen: yes", "- offscreen: maybe"), "should be yes or no"),
    ],
)
def test_problems_are_errors_with_line_numbers(change: tuple[str, str], expected: str):
    text = MINIMAL.replace(*change)
    parsed = parse_plot_markdown(text)
    assert not parsed.ok
    assert any(expected in line for line in problems(text)), problems(text)


def test_a_bullet_list_in_a_description_stays_prose():
    text = MINIMAL.replace("- present: yes\n", "- present: yes\n\n- Strength: great\n")
    cy = next(c for c in parse_plot_markdown(text).scenario.cast if c.name == "Cy")
    assert cy.full_description == "- Strength: great"


def test_a_field_in_the_wrong_place_is_pointed_out():
    text = MINIMAL.replace("## Cy\n", "## Cy\n- when: day 3\n")
    assert any("`when` isn't used under “Cy”" in line for line in problems(text))


def test_comments_are_dropped_and_lines_still_count():
    text = MINIMAL.replace("# World\n", "# World\n<!-- a note\nover two lines -->\n")
    parsed = parse_plot_markdown(text.replace("- requires: visited = yes", "- requires: x = y"))
    assert parsed.scenario.world_bible == "A cold place."
    assert any("line 29" in problem.describe() for problem in parsed.problems)


def test_indented_lines_continue_a_field():
    text = MINIMAL.replace("- tell: The dead come.", "- tell: The dead come.\n  All of them.")
    attack = parse_plot_markdown(text).scenario.plot.event("attack")
    assert attack.variants[0].tell == "The dead come. All of them."


def test_a_file_with_no_facts_or_events_has_no_plot():
    parsed = parse_plot_markdown("# World\nSomewhere.\n", fallback_title="Plain")
    assert parsed.ok
    assert parsed.scenario.title == "Plain"
    assert parsed.scenario.plot is None


def test_the_plot_survives_a_scenario_file(tmp_path: Path):
    scenario = parse_plot_markdown(SAMPLE.read_text(encoding="utf-8")).scenario
    path = tmp_path / "doomsville.sealedlore-scenario.json"
    write_scenario(path, scenario)
    assert read_scenario(path) == scenario

    bundle = bundle_from_scenario(read_scenario(path))
    assert bundle.story.plot == scenario.plot
    assert bundle.story.setup.choose_one_of == scenario.choose_one_of


def make_session(tmp_path: Path) -> StorySession:
    bundle = bundle_from_scenario(parse_plot_markdown(MINIMAL).scenario)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        Config(scene_reads="manual"),
        MockChatProvider(["It begins."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_whoever_is_not_picked_never_enters_the_story(tmp_path: Path):
    session = make_session(tmp_path)
    ann = session.character_by_name("Ann")
    list(session.begin(ann.id))

    assert [character.name for character in session.cast] == ["Ann", "Cy"]
    assert [character.name for character in session.story.setup.set_aside] == ["Bo"]
    # A restart offers the choice again.
    again = fresh_playthrough(session.bundle, "Again")
    assert {character.name for character in again.cast} == {"Ann", "Bo", "Cy"}
    assert len(again.story.setup.choose_one_of) == 2
    assert again.story.setup.set_aside == []


def test_directing_keeps_everyone(tmp_path: Path):
    session = make_session(tmp_path)
    list(session.begin(None))
    assert [character.name for character in session.cast] == ["Ann", "Bo", "Cy"]


def test_lore_is_revealable_but_never_a_place():
    text = MINIMAL + (
        "\n# Places\n## The Mill\nAn old mill.\n\n"
        "# Lore\n## The Old Feud\n- hidden: yes\nWho burned the mill.\n"
    )
    text = text.replace("- tell:", "- reveals: The Old Feud\n- tell:", 1)
    parsed = parse_plot_markdown(text)
    assert parsed.ok, problems(text)
    assert [entry.title for entry in parsed.scenario.lore] == ["The Mill", "The Old Feud"]
    assert [place.name for place in parsed.scenario.plot.places] == ["The Mill"]
    feud = parsed.scenario.lore[1]
    assert any(
        feud.id in holder.reveals
        for event in parsed.scenario.plot.events
        for holder in (event, *event.variants)
    )
    # Lore is not somewhere to be.
    bad = MINIMAL.replace("- requires:", "- requires: at = The Old Feud,", 1) + (
        "\n# Lore\n## The Old Feud\nWho burned the mill.\n"
    )
    assert any("No place called “The Old Feud”" in line for line in problems(bad))


@pytest.mark.parametrize(
    "spelling",
    [
        "attack not happened",
        "attack has not happened",
        "attack hasn't happened",
        "attack hasn’t happened",
    ],
)
def test_every_spelling_of_not_happened_is_a_negation(spelling: str):
    # "hasn't happened" was read as "has happened": the check looked for "not".
    text = MINIMAL.replace("attack not happened", spelling)
    greeting = next(e for e in parse_plot_markdown(text).scenario.plot.events if e.id == "greeting")
    assert [(c.event_id, c.happened) for c in greeting.requires if c.event_id] == [
        ("attack", False)
    ]
