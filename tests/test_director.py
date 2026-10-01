"""The director: code decides what it can, a small model decides the moment."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from sealedlore.engine.chronicle import (
    UNCONFIRMED_LIMIT,
    armed,
    chronicle_at,
    clock_minutes,
    holds,
    initial_chronicle,
    resolve_due,
    schedule,
    settle_unconfirmed,
)
from sealedlore.engine.director import (
    automatic_directions,
    awaits_answer,
    held_for_answer,
    parse_director,
    render_direction_block,
)
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.prompt import (
    SECTION_DIRECTION,
    SECTION_REMINDER,
    SECTION_STORY_TIME,
    TurnRequest,
)
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config
from sealedlore.models.plot import Condition, Plot
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import read_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario

PLOT_FILE = """\
---
title: T
start: day 3, 21:00
---
# World
Cold.

# Characters
## Ann
- role: player
- present: yes

# Facts
- town: standing (standing, under attack, fallen) [watch] — the state of the town
- visited: yes — yes once Ann has been in the town
- at_town: yes — yes while Ann is in the town

# Timeline
## The fall
- when: day 3–5
- lead-in: yes
- aftermath: The town burned, and the dead walk its streets.

The dead attack the town.

### In town
- requires: at_town = yes
- tell: The horde breaks the gate.
- sets: town = under attack

### Never been
- requires: visited = no
- offscreen: yes
- sets: town = fallen
"""

AWAY = PLOT_FILE.replace("- visited: yes", "- visited: no").replace(
    "- at_town: yes", "- at_town: no"
)

PASSAGE = "Ann banked the fire. Then the horde broke the gate, and the bells began."


def plot(text: str = PLOT_FILE) -> Plot:
    return parse_plot_markdown(text).scenario.plot


def director(*directions: dict) -> str:
    return json.dumps({"directions": list(directions)})


def read(minutes: int = 30, happened=(), facts=()) -> str:
    return json.dumps(
        {
            "elapsed": {"amount": minutes, "unit": "minutes"},
            "facts": list(facts),
            "happened": list(happened),
        }
    )


# --- pure ---------------------------------------------------------------------


def test_conditions_are_checked_in_code():
    chronicle = initial_chronicle(plot())
    assert holds([Condition(fact="town", value="standing")], chronicle)
    assert not holds([Condition(fact="town", op="!=", value="standing")], chronicle)
    assert holds([Condition(event_id="the_fall", happened=False)], chronicle)
    chronicle.events["the_fall"].state = "happened"
    assert holds([Condition(event_id="the_fall")], chronicle)


def test_each_event_gets_a_day_in_its_window_once():
    days = schedule(plot(), {}, random.Random(4))
    assert 3 <= days["the_fall"] <= 5
    assert schedule(plot(), days, random.Random(99)) == days
    # A kept day outside a changed window is drawn again.
    assert 3 <= schedule(plot(), {"the_fall": 40}, random.Random(1))["the_fall"] <= 5


def test_offscreen_happens_in_code_when_nothing_else_fits():
    chronicle = initial_chronicle(plot(AWAY))
    result = resolve_due(plot(AWAY), chronicle, {"the_fall": 3}, node_id="n1")
    assert [event.id for event in result.happened] == ["the_fall"] and result.changed
    status = chronicle.events["the_fall"]
    assert status.state == "happened" and not status.revealed
    assert chronicle.facts["town"] == "fallen"
    # What it left behind can now be revealed, when the director judges it time.
    assert [(a.event.id, a.kinds) for a in armed(plot(AWAY), chronicle, {"the_fall": 3})] == [
        ("the_fall", ("reveal",))
    ]


def test_not_before_its_day_except_as_a_lead_in():
    chronicle = initial_chronicle(plot())  # day 3
    assert armed(plot(), chronicle, {"the_fall": 7}) == []
    [item] = armed(plot(), chronicle, {"the_fall": 5})
    assert item.kinds == ("lead_in",)
    [item] = armed(plot(), chronicle, {"the_fall": 3})
    assert item.kinds == ("begin",) and [v.title for v in item.variants] == ["In town"]


def test_a_closed_window_makes_it_overdue_or_missed():
    chronicle = initial_chronicle(plot())
    chronicle.minutes = clock_minutes(6, 9, 0)
    resolve_due(plot(), chronicle, {"the_fall": 4}, node_id=None)
    assert chronicle.events["the_fall"].overdue
    [forced] = automatic_directions(armed(plot(), chronicle, {"the_fall": 4}))
    assert forced.kind == "begin" and forced.variant == "In town"

    # Visited but away: no variant fits. It must happen (no event-level
    # `requires`), so the plot model is asked to choose.
    chronicle = initial_chronicle(plot())
    chronicle.minutes = clock_minutes(6, 9, 0)
    chronicle.facts["at_town"] = "no"
    result = resolve_due(plot(), chronicle, {"the_fall": 4}, node_id=None)
    assert [event.id for event in result.undecided] == ["the_fall"]
    assert chronicle.events["the_fall"].state == "pending"

    # An event that needn't happen is simply missed.
    optional = plot(PLOT_FILE.replace("- lead-in: yes", "- lead-in: yes\n- must happen: no"))
    chronicle = initial_chronicle(optional)
    chronicle.minutes = clock_minutes(6, 9, 0)
    chronicle.facts["at_town"] = "no"
    result = resolve_due(optional, chronicle, {"the_fall": 4}, node_id=None)
    assert chronicle.events["the_fall"].state == "missed" and result.changed


def test_the_reply_is_held_to_what_is_armed():
    items = armed(plot(), initial_chronicle(plot()), {"the_fall": 3})
    reply = parse_director(
        director(
            {"event": "the_fall", "kind": "begin", "variant": None, "why": "she is asleep"},
            {"event": "the_fall", "kind": "reveal"},
            {"event": "the_caves", "kind": "begin"},
        ),
        items,
    )
    [begin] = reply.directions
    # The one fitting variant is taken when the reply names none.
    assert begin.variant == "In town"
    assert "The horde breaks the gate." in begin.text
    assert len(reply.refused) == 2

    wrong = parse_director(director({"event": "the_fall", "kind": "begin", "variant": "X"}), items)
    # A single fitting variant is used even when the reply misnames it.
    assert wrong.directions[0].variant == "In town"
    with pytest.raises(ValueError):
        parse_director("Nothing yet.", items)


def test_nothing_quiet_begins_while_the_author_waits_on_an_answer():
    assert awaits_answer('I listen. "How many on the wall tonight?"')
    assert awaits_answer("Where does this road go?")
    assert not awaits_answer("I find the bunk and sleep.")
    assert not awaits_answer('"Nobody asked you," I say. I walk off.')

    items = armed(plot(), initial_chronicle(plot()), {"the_fall": 3})
    assert held_for_answer(items, '"How many on the wall?"') == []
    assert held_for_answer(items, "I sleep.") == items

    chronicle = initial_chronicle(plot())
    chronicle.events["the_fall"].overdue = True
    overdue = armed(plot(), chronicle, {"the_fall": 3})
    assert held_for_answer(overdue, '"How many?"') == overdue


def test_the_storyteller_is_told_to_leave_the_held_character_alone():
    items = armed(plot(), initial_chronicle(plot()), {"the_fall": 3})
    reply = parse_director(director({"event": "the_fall", "kind": "begin"}), items)
    block = render_direction_block(reply.directions, "Ann")
    assert block.startswith("# STORY DIRECTION")
    assert "Never decide what Ann does" in block
    assert "The author has not seen this" in block


# --- in a session ---------------------------------------------------------------


def make_session(tmp_path: Path, responses: list[str], text: str = PLOT_FILE, **config):
    bundle = bundle_from_scenario(parse_plot_markdown(text).scenario)
    bundle.story.plot_schedule = {"the_fall": 3}
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        Config(scene_reads="manual", min_cacheable_tokens=1, **config),
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    session.story.held_character_id = session.cast[0].id
    return session


def turn(session: StorySession, text: str = "I bank the fire and sleep.") -> TurnRequest:
    ann = session.cast[0].id
    return TurnRequest(speaker_id=ann, user_text=text, controlled_character_id=ann)


def section(session: StorySession, name: str) -> str:
    """A tail section's text; an empty one is left out of the prompt."""
    found = session.last_prompt.section(name)
    return found.text if found is not None else ""


def kinds(session: StorySession, tmp_path: Path) -> list[str]:
    return [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]


def test_a_directed_event_reaches_the_tail_and_happens(tmp_path: Path):
    begin = {"event": "the_fall", "kind": "begin", "variant": "In town", "why": "asleep"}
    seen = {"event": "the_fall", "quote": "the horde broke the gate"}
    session = make_session(
        tmp_path,
        [
            director(begin),
            PASSAGE,
            read(happened=[seen], facts=[{"fact": "town", "value": "fallen", "quote": PASSAGE}]),
        ],
    )
    events = list(session.send(turn(session)))

    prompt = session.last_prompt
    assert "The horde breaks the gate." in prompt.section(SECTION_DIRECTION).text
    assert "Begin what the story direction sets out" in prompt.section(SECTION_REMINDER).text
    # The storyteller isn't told the day and time (measured, Oct 2026).
    # Nothing known yet, so no story-time section at all.
    assert prompt.section(SECTION_STORY_TIME) is None
    user_node = session.path()[-2]
    assert [d.kind for d in user_node.meta.direction] == ["begin"]

    status = session.chronicle.events["the_fall"]
    assert status.state == "happened" and status.revealed
    # The event set "under attack"; the passage showed it fall.
    assert session.chronicle.facts["town"] == "fallen"
    assert "director_request" in kinds(session, tmp_path)
    # Spoilers off by default: the notice doesn't name it.
    assert "Plot: the director was consulted." in [
        e.text for e in events if isinstance(e, SessionNotice)
    ]


def test_the_system_block_does_not_change_when_an_event_is_directed(tmp_path: Path):
    session = make_session(
        tmp_path,
        [
            director(),
            "Quiet.",
            read(),
            director({"event": "the_fall", "kind": "begin"}),
            PASSAGE,
            read(),
        ],
    )
    list(session.send(turn(session, "I look around.")))
    first = session.last_prompt.messages[0]
    list(session.send(turn(session)))
    assert session.last_prompt.messages[0] == first
    assert session.last_prompt.section(SECTION_DIRECTION).text


def test_regenerate_reuses_the_direction_without_asking_again(tmp_path: Path):
    session = make_session(
        tmp_path,
        [
            director({"event": "the_fall", "kind": "begin"}),
            PASSAGE,
            read(),
            "Another take.",
            read(),
        ],
    )
    list(session.send(turn(session)))
    asked = kinds(session, tmp_path).count("director_request")
    list(session.regenerate())
    assert kinds(session, tmp_path).count("director_request") == asked
    assert "The horde breaks the gate." in session.last_prompt.section(SECTION_DIRECTION).text


def test_nothing_armed_means_no_director_call(tmp_path: Path):
    early = PLOT_FILE.replace("day 3, 21:00", "day 1, 09:00").replace("- lead-in: yes\n", "")
    session = make_session(tmp_path, ["Quiet.", read()], text=early)
    list(session.send(turn(session)))
    assert "director_request" not in kinds(session, tmp_path)
    assert section(session, SECTION_DIRECTION) == ""


def test_offscreen_needs_no_call_and_is_revealed_later(tmp_path: Path):
    session = make_session(
        tmp_path,
        [
            # Nothing is armed until the offscreen event has happened, and
            # then only its reveal: the director is asked about that.
            director(),
            "Snow.",
            read(),
            # Declined, the reveal rests a turn: no call.
            "Wind.",
            read(),
            director({"event": "the_fall", "kind": "reveal", "why": "she walks to the town"}),
            "Smoke on the horizon.",
            read(),
        ],
        text=AWAY,
    )
    list(session.send(turn(session, "I rest.")))
    status = session.chronicle.events["the_fall"]
    assert status.state == "happened" and not status.revealed
    assert session.chronicle.facts["town"] == "fallen"
    list(session.send(turn(session, "I rest some more.")))
    assert not session.chronicle.events["the_fall"].revealed

    list(session.send(turn(session, "I walk north toward the town.")))
    assert session.chronicle.events["the_fall"].revealed
    assert "The town burned" in session.last_prompt.section(SECTION_DIRECTION).text
    assert "The fall" in session.last_prompt.section(SECTION_STORY_TIME).text


def test_a_failed_director_call_keeps_the_turn(tmp_path: Path):
    session = make_session(tmp_path, ["not json", PASSAGE, read()])
    events = list(session.send(turn(session)))
    assert session.path()[-1].content == PASSAGE
    assert section(session, SECTION_DIRECTION) == ""
    assert any(
        "Couldn't consult the director" in e.text for e in events if isinstance(e, SessionNotice)
    )


def test_spoilers_on_names_the_event(tmp_path: Path):
    session = make_session(
        tmp_path,
        [director({"event": "the_fall", "kind": "begin"}), PASSAGE, read()],
        show_plot_spoilers=True,
    )
    events = list(session.send(turn(session)))
    assert "Plot: The fall (In town) begins" in [
        e.text for e in events if isinstance(e, SessionNotice)
    ]


def test_the_author_marks_an_event(tmp_path: Path):
    session = make_session(tmp_path, [director(), "Quiet.", read()])
    list(session.send(turn(session, "I look around.")))
    session.mark_event("the_fall", "happened")
    assert session.chronicle.events["the_fall"].state == "happened"
    assert session.chronicle.facts["town"] == "under attack"
    session.mark_event("the_fall", "missed")
    assert session.chronicle.events["the_fall"].state == "missed"
    assert chronicle_at(session.path()[:-1], session.story.plot).events["the_fall"].state == (
        "pending"
    )


def test_an_event_sent_again_keeps_counting_and_is_settled(tmp_path: Path):
    # Overdue: the window (days 3-5) has closed, so it is sent every turn
    # without asking. Live, each send reset the count, so it never ran out.
    overdue = PLOT_FILE.replace("day 3, 21:00", "day 6, 10:00")
    session = make_session(tmp_path, ["Quiet.", read()] * UNCONFIRMED_LIMIT, text=overdue)
    first = None
    for sent in range(1, UNCONFIRMED_LIMIT + 1):
        list(session.send(turn(session, "I look around.")))
        assert "Begin this in the passage" in section(session, SECTION_DIRECTION)
        status = session.chronicle.events["the_fall"]
        if sent < UNCONFIRMED_LIMIT:
            assert status.state == "directed"
            assert status.unconfirmed_passages == sent
            first = first or status.node_id
            assert status.node_id == first
    assert session.chronicle.events["the_fall"].state == "happened"
    assert "director_request" not in kinds(session, tmp_path)


def test_an_event_the_director_began_and_no_passage_showed_waits_again():
    # The window is still open and the event has a quiet-moment pacing: no
    # count takes it as happened. Live (Between Stars), one such event was
    # never told, the director answered nothing for eight turns, and it could
    # neither happen nor begin again; after the limit it goes back to waiting.
    from sealedlore.engine.chronicle import release_unconfirmed

    p = plot()
    chronicle = initial_chronicle(p)
    chronicle.events["the_fall"].state = "directed"
    chronicle.events["the_fall"].variant = "In town"
    for _ in range(UNCONFIRMED_LIMIT - 1):
        assert settle_unconfirmed(p, chronicle, [], node_id="n") == []
        assert release_unconfirmed(p, chronicle) == []
    assert chronicle.events["the_fall"].state == "directed"
    assert settle_unconfirmed(p, chronicle, [], node_id="n") == []
    assert [e.id for e in release_unconfirmed(p, chronicle)] == ["the_fall"]
    status = chronicle.events["the_fall"]
    assert status.state == "pending" and status.variant is None
    assert status.unconfirmed_passages == 0
