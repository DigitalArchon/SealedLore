"""The plot's clock and facts: a read after each passage, snapshots on the nodes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.chronicle import (
    advance,
    chronicle_at,
    clock_minutes,
    fitted,
    format_clock,
    format_duration,
    initial_chronicle,
)
from sealedlore.engine.chronicle_read import (
    MAX_UNQUOTED_MINUTES,
    _names_day,
    _stated_span,
    parse_read,
)
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config
from sealedlore.models.plot import FactDef, Plot
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario
from sealedlore.tree import path_to

PLOT_FILE = """\
---
title: Test
start: day 1, 16:00
---
# World
A cold place.

# Characters
## Ann
- role: player
- present: yes

# Facts
- town: standing (standing, fallen) [watch] — the state of the town
- visited: no [watch] — yes once Ann has been inside the town
"""

PASSAGE = "Ann walked through the gate of the town as the light failed. She slept by the stove."


def read(minutes: float = 20, unit: str = "minutes", quote=None, ends_at=None, facts=()) -> str:
    return json.dumps(
        {
            "elapsed": {"amount": minutes, "unit": unit, "quote": quote},
            "ends_at": ends_at,
            "facts": list(facts),
        }
    )


def plot() -> Plot:
    return parse_plot_markdown(PLOT_FILE).scenario.plot


# --- the clock ---------------------------------------------------------------


def test_clock_formatting():
    assert format_clock(clock_minutes(35, 22, 10)) == "Day 35 · 22:10"
    assert format_duration(8 * 60 + 30) == "8h 30m"
    assert format_duration(3 * 1440 + 4 * 60 + 5) == "3 days 4h"


def test_waking_at_dawn_is_the_next_dawn():
    night = clock_minutes(35, 22, 0)
    assert advance(night, 8 * 60, 6 * 60 + 30) == clock_minutes(36, 6, 30)
    # Whole days first, then on to the hour.
    assert advance(clock_minutes(1, 15, 30), 21 * 1440, 7 * 60) == clock_minutes(23, 7, 0)
    assert advance(night, 15, None) == night + 15


# --- parsing and holding the read to account -----------------------------------


def test_parse_read_units_and_times():
    delta = parse_read(read(3, "weeks", quote="Three weeks passed.", ends_at="07:15"))
    assert delta.elapsed == 21 * 1440
    assert delta.elapsed_quote == "Three weeks passed."
    assert delta.ends_at == 7 * 60 + 15
    assert parse_read(read("2", "hrs")).elapsed == 120
    assert parse_read('```json\n{"elapsed": null}\n```').elapsed == 0
    with pytest.raises(ValueError):
        parse_read("No change, I think.")


def test_an_unquoted_jump_is_cut_back():
    before = initial_chronicle(plot())
    delta = parse_read(read(3, "weeks"))
    after = delta.applied_to(before, plot(), passage=PASSAGE, author_turn="")
    assert after.elapsed == MAX_UNQUOTED_MINUTES
    assert delta.refused


def test_a_quoted_jump_stands_even_from_the_author_turn():
    before = initial_chronicle(plot())
    delta = parse_read(read(3, "weeks", quote="Three weeks pass."))
    after = delta.applied_to(before, plot(), passage=PASSAGE, author_turn="Three weeks pass.")
    assert after.elapsed == 21 * 1440
    assert after.source == "story"


def test_a_misread_hour_does_not_skip_a_day():
    before = initial_chronicle(plot())  # 16:00
    delta = parse_read(read(10, ends_at="15:50"))
    after = delta.applied_to(before, plot(), passage=PASSAGE, author_turn="")
    assert after.minutes == before.minutes + 10


def test_facts_need_a_sentence_in_the_passage_and_an_allowed_value():
    before = initial_chronicle(plot())
    delta = parse_read(
        read(
            facts=[
                {"fact": "visited", "value": "yes", "quote": "Ann walked through the gate"},
                {"fact": "town", "value": "fallen", "quote": "The town burned."},
                {"fact": "town", "value": "razed", "quote": "She slept by the stove."},
                {"fact": "weather", "value": "cold", "quote": "She slept by the stove."},
            ]
        )
    )
    after = delta.applied_to(before, plot(), passage=PASSAGE, author_turn="")
    assert after.facts == {"town": "standing", "visited": "yes"}
    assert [(c.fact, c.old, c.new) for c in after.changes] == [("visited", "no", "yes")]
    assert len(delta.refused) == 3


def test_a_reimported_plot_fits_old_snapshots():
    old = initial_chronicle(plot())
    old.facts["town"] = "fallen"
    new_plot = plot().model_copy(deep=True)
    new_plot.facts = [
        FactDef(name="town", initial="whole", values=["whole", "ruined"]),
        FactDef(name="seen_ann", initial="no", values=["yes", "no"]),
    ]
    assert fitted(old, new_plot).facts == {"town": "whole", "seen_ann": "no"}


# --- in a session --------------------------------------------------------------


def make_session(tmp_path: Path, responses: list[str], **config) -> StorySession:
    bundle = bundle_from_scenario(parse_plot_markdown(PLOT_FILE).scenario)
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        Config(scene_reads="manual", **config),
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    session.story.held_character_id = session.cast[0].id
    return session


def turn(session: StorySession, text: str = "I go in.") -> TurnRequest:
    ann = session.cast[0].id
    return TurnRequest(speaker_id=ann, user_text=text, controlled_character_id=ann)


def test_a_turn_reads_the_clock_and_facts(tmp_path: Path):
    visited = {"fact": "visited", "value": "yes", "quote": "Ann walked through the gate"}
    session = make_session(tmp_path, [PASSAGE, read(9, "hours", ends_at="06:30", facts=[visited])])

    events = list(session.send(turn(session)))

    chronicle = session.chronicle
    assert format_clock(chronicle.minutes) == "Day 2 · 06:30"
    assert chronicle.facts["visited"] == "yes"
    assert session.chronicle_changed_here()
    notices = [event.text for event in events if isinstance(event, SessionNotice)]
    assert "Plot: +14h 30m, now Day 2 · 06:30; visited: no → yes" in notices
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert "chronicle_request" in kinds and "chronicle_response" in kinds
    # Its cost is counted with the other background calls.
    assert session.unreported_usage


def test_every_branch_keeps_its_own_clock(tmp_path: Path):
    session = make_session(tmp_path, [PASSAGE, read(1, "hours"), "Another take.", read(5, "hours")])
    list(session.send(turn(session)))
    first = session.path()[-1]
    list(session.regenerate())
    assert session.chronicle.minutes == clock_minutes(1, 21, 0)

    session.switch_to(first.id)
    assert session.chronicle.minutes == clock_minutes(1, 17, 0)
    # Deleting the passage steps back to the clock before it.
    session.delete_from(first.id)
    assert session.chronicle.minutes == clock_minutes(1, 16, 0)


def test_undo_takes_the_read_back(tmp_path: Path):
    session = make_session(tmp_path, [PASSAGE, read(2, "hours")])
    list(session.send(turn(session)))
    assert session.undo_chronicle_update()
    assert session.chronicle.minutes == clock_minutes(1, 16, 0)
    assert not session.chronicle_changed_here()


def test_a_failed_read_keeps_the_turn(tmp_path: Path):
    session = make_session(tmp_path, [PASSAGE, "I can't tell."])
    events = list(session.send(turn(session)))
    assert session.path()[-1].content == PASSAGE
    assert session.path()[-1].meta.chronicle is None
    assert any("Couldn't read the clock" in e.text for e in events if isinstance(e, SessionNotice))


def test_no_read_without_a_plot_or_with_reads_off(tmp_path: Path, story, cast):
    session = make_session(tmp_path, [PASSAGE], plot_reads=False)
    list(session.send(turn(session)))
    assert len(session.provider.requests) == 1

    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    plain = StorySession(
        bundle,
        Config(scene_reads="manual"),
        MockChatProvider([PASSAGE]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(plain.send(TurnRequest(speaker_id="char-serrik", user_text="Go.")))
    assert len(plain.provider.requests) == 1
    assert plain.chronicle is None


def test_the_author_corrects_the_clock_and_facts(tmp_path: Path):
    session = make_session(tmp_path, [PASSAGE, read(1, "hours")])
    # Before the first passage, setting the clock moves the start.
    session.set_clock(clock_minutes(3, 9, 0))
    assert session.story.plot.start_minutes == clock_minutes(3, 9, 0)

    list(session.send(turn(session)))
    session.set_clock(clock_minutes(40, 20, 0))
    session.set_fact("town", "fallen")
    chronicle = session.path()[-1].meta.chronicle
    assert chronicle.source == "author"
    assert chronicle.minutes == clock_minutes(40, 20, 0)
    assert chronicle.facts["town"] == "fallen"
    # An author's correction isn't the story's, so there's nothing to undo.
    assert not session.chronicle_changed_here()
    with pytest.raises(ValueError):
        session.set_fact("town", "razed")

    # The next read counts from the correction.
    session.provider.responses = ["Later.", read(30)]
    session.provider._call_count = 0
    list(session.send(turn(session, "I wait.")))
    assert session.chronicle.minutes == clock_minutes(40, 20, 30)
    assert session.chronicle.facts["town"] == "fallen"


def test_attaching_a_plot_mid_story_starts_it_at_the_leaf(tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        Config(scene_reads="manual"),
        MockChatProvider([PASSAGE, read(10)]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.send(TurnRequest(speaker_id="char-serrik", user_text="Go.")))
    session.attach_plot(plot())
    leaf = session.path()[-1]
    assert leaf.meta.chronicle is not None
    assert chronicle_at(path_to(session.nodes, leaf.id), session.story.plot).minutes == (
        clock_minutes(1, 16, 0)
    )


def test_a_day_number_needs_a_quote_that_names_it():
    before = initial_chronicle(plot())
    delta = parse_read(read(5, "weeks", quote="Five weeks pass.")[:-1] + ', "day": 70}')
    after = delta.applied_to(before, plot(), passage=PASSAGE, author_turn="Five weeks pass.")
    assert after.elapsed == 35 * 1440
    assert any("day 70" in r for r in delta.refused)
    delta = parse_read(read(2, "days", quote="Day three dawns cold.")[:-1] + ', "day": 3}')
    after = delta.applied_to(before, plot(), passage="Day three dawns cold.", author_turn="")
    assert after.minutes // 1440 == 2


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("Twenty-one days later.", 21),
        ("Forty days pass.", 40),
        ("A few more days pass.", 3),
        ("A hundred days pass.", 100),
        ("One hundred and twenty days later.", 120),
        ("A week and a half goes by.", 10.5),
        ("Two and a half weeks pass.", 17.5),
        ("Another day passes.", 1),
        ("a couple of nights", 2),
        ("It took 3 days.", 3),
    ],
)
def test_a_stated_span_reads_its_number_whole(text, days):
    # Live, "fifty-one days" was read as "one day", and "thirty days" not at all.
    span = _stated_span(text)
    assert span is not None and span[0] == round(days * 1440)


def test_a_count_of_days_is_not_a_day_number():
    # Live, the storyteller's "fifty-one days into a new life" set the story to day 51.
    assert not _names_day("fifty-one days into a new life", 51)
    assert not _names_day("thirty days", 30)
    assert not _names_day("the thirty-first day", 1)
    assert not _names_day("day thirty one", 30)
    assert _names_day("On day 58, the Cortina docked.", 58)
    assert _names_day("Day thirty-one dawned grey.", 31)
    assert _names_day("the thirty-eighth evening", 38)
    assert _names_day("the 31st morning", 31)


def test_the_authors_skip_and_day_stand_whatever_else_the_turn_names():
    # The branch run of Between Stars, day 51: the read said thirty days and
    # day 81, and both were refused (the turn's "fifty-one days" read as one
    # day, the day not in the read's quote). The clock ended 29 days short.
    before = initial_chronicle(plot())
    turn = (
        "The next thirty days blur into a rhythm of station life. Andar and Myla learn each "
        "other again across the distance fifty-one days carved between them. By day 81, the "
        "tension has become almost ordinary."
    )
    passage = "Thirty days settled into a rhythm that had no right to be called one."
    delta = parse_read(read(30, "days", quote="Thirty days settled")[:-1] + ', "day": 81}')
    after = delta.applied_to(
        before, plot(), passage=passage, author_turn=turn, author_states_facts=True
    )
    assert after.minutes // 1440 + 1 == 81
    assert not delta.refused
    # An in-character turn is not the author's statement of the day.
    delta = parse_read(read(30, "days", quote="Thirty days settled")[:-1] + ', "day": 81}')
    after = delta.applied_to(before, plot(), passage=passage, author_turn=turn)
    assert after.elapsed == 30 * 1440 and any("day 81" in r for r in delta.refused)


def test_the_authors_own_skip_needs_no_quote():
    before = initial_chronicle(plot())
    turn = "Five weeks pass. Ann settles into the town."
    delta = parse_read(read(5, "weeks"))
    after = delta.applied_to(
        before, plot(), passage=PASSAGE, author_turn=turn, author_states_facts=True
    )
    assert after.elapsed == 35 * 1440 and after.elapsed_quote == "Five weeks pass."
    # Held to the span the turn states.
    delta = parse_read(read(5, "months"))
    after = delta.applied_to(
        before, plot(), passage=PASSAGE, author_turn=turn, author_states_facts=True
    )
    assert after.elapsed == 35 * 1440 and delta.refused
    # Only a Director or Narration turn is the author's statement.
    delta = parse_read(read(5, "weeks"))
    after = delta.applied_to(before, plot(), passage=PASSAGE, author_turn=turn)
    assert after.elapsed == MAX_UNQUOTED_MINUTES
