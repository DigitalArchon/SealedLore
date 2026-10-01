"""Plots v2: code decides what each model sees.

Hidden characters and places until an event brings them in, facts set by
events unless watched, places kept by the program, timed events that always
resolve, and the fixes from the Doomsville playtest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.chronicle import (
    UNCONFIRMED_LIMIT,
    clock_minutes,
    initial_chronicle,
    settle_unconfirmed,
)
from sealedlore.engine.chronicle_read import parse_read
from sealedlore.engine.director import render_story_time_block
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.prompt import SECTION_DIRECTION, TurnRequest
from sealedlore.engine.prompt_texts import PromptTexts
from sealedlore.engine.scene_update import SceneDelta
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.models.plot import Condition, Plot
from sealedlore.models.scene import SceneShortcut, SceneState
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import read_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario

PLOT_FILE = """\
---
title: V2
start: day 3, 18:00
---
# World
Cold.

# Characters
## Ann
- role: player
- present: yes

## Mara
- hidden: yes

A trader with a limp and a quick laugh.

# Places
## The Mill
A ruined mill.

## The Cellar
- hidden: yes

A cellar under the mill, dry and hidden.

# Facts
- mood: calm (calm, tense) [watch] — tense once raiders are near
- raided: no — whether the mill was raided

# Timeline
## The raid
- when: day 3–4
- aftermath: The mill was stripped bare; the raiders went north with the grain.

Raiders hit the mill.

### Ann at the mill
- requires: at = The Mill
- brings: Mara
- tell: Raiders come; Mara the trader limps in to warn Ann.
- sets: raided = yes

### Ann away
- requires: visited The Mill, at != The Mill
- offscreen: yes
- sets: raided = yes

### Nobody there
- requires: not visited The Mill
- offscreen: yes
- sets: raided = yes

# Events
## The cellar
- requires: visited The Mill
- reveals: The Cellar
- trigger: Ann searches the mill.
- tell: A trapdoor under the millstone.
"""

PASSAGE = "Ann pushed through the mill door. Dust hung in the grey light."
RAID = "Mara limped through the door, shouting. Raiders were on the road."


def plot(text: str = PLOT_FILE) -> Plot:
    parsed = parse_plot_markdown(text)
    assert parsed.ok, [p.describe() for p in parsed.problems]
    return parsed.scenario.plot


def read(minutes=10, place=None, quote=None, happened=(), facts=(), ends_at=None, day=None):
    data = {
        "elapsed": {"amount": minutes, "unit": "minutes", "quote": quote},
        "ends_at": ends_at,
        "day": day,
        "facts": list(facts),
        "happened": list(happened),
    }
    if place is not None:
        data["place"] = {"name": place[0], "quote": place[1]}
    return json.dumps(data)


def director(*directions) -> str:
    return json.dumps({"directions": list(directions)})


# --- the file ----------------------------------------------------------------------


def test_the_file_reads_hidden_brings_watch_and_places():
    parsed = parse_plot_markdown(PLOT_FILE)
    assert parsed.ok and not parsed.problems
    scenario = parsed.scenario
    mara = next(c for c in scenario.cast if c.name == "Mara")
    cellar = next(entry for entry in scenario.lore if entry.title == "The Cellar")
    assert mara.plot_hidden and cellar.plot_hidden
    raid = scenario.plot.event("the_raid")
    assert raid.variants[0].brings == [mara.id]
    assert raid.variants[0].requires == [Condition(place="The Mill", place_test="at")]
    assert raid.variants[1].requires == [
        Condition(place="The Mill", place_test="visited"),
        Condition(place="The Mill", place_test="at", op="!="),
    ]
    assert raid.guaranteed
    assert scenario.plot.event("the_cellar").reveals == [cellar.id]
    assert [f.name for f in scenario.plot.facts if f.watched] == ["mood"]
    assert [p.name for p in scenario.plot.places] == ["The Mill", "The Cellar"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("- brings: Mara", "- brings: Nobody"), "`brings` names “Nobody”"),
        (("at = The Mill\n- brings", "at = The Barn\n- brings"), "No place called “The Barn”"),
        (("- brings: Mara\n", ""), "Mara is hidden, but no event brings them in."),
    ],
)
def test_problems_with_the_new_fields(change, expected):
    text = PLOT_FILE.replace(*change)
    lines = [p.describe() for p in parse_plot_markdown(text).problems]
    assert any(expected in line for line in lines), lines


def test_an_event_with_its_own_requires_is_conditional():
    text = PLOT_FILE.replace("- when: day 3–4\n", "- when: day 3–4\n- requires: mood = tense\n")
    assert not plot(text).event("the_raid").guaranteed
    text = PLOT_FILE.replace("- when: day 3–4\n", "- when: day 3–4\n- must happen: no\n")
    assert not plot(text).event("the_raid").guaranteed


# --- in a session ----------------------------------------------------------------


def make_session(
    tmp_path: Path, responses: list[str], text: str = PLOT_FILE, raid_day: int = 4, **config
) -> StorySession:
    bundle = bundle_from_scenario(parse_plot_markdown(text).scenario)
    bundle.story.plot_schedule = {"the_raid": raid_day}
    save_story_bundle(bundle, root=tmp_path)
    settings = {"scene_reads": "manual", "min_cacheable_tokens": 1, **config}
    session = StorySession(
        bundle,
        Config(**settings),
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    session.story.held_character_id = session.cast[0].id
    return session


def turn(session: StorySession, text: str) -> TurnRequest:
    ann = session.cast[0].id
    return TurnRequest(speaker_id=ann, user_text=text, controlled_character_id=ann)


def system_text(session: StorySession) -> str:
    message = session.last_prompt.messages[0]
    return "\n".join(part.text for part in message.parts)


def test_a_hidden_character_joins_only_when_the_event_happens(tmp_path: Path):
    session = make_session(
        tmp_path,
        [
            # Turn 1: nothing is armed yet (Ann hasn't been to the mill), so
            # there is no director call.
            PASSAGE,
            read(place=("The Mill", "Ann pushed through the mill door.")),
            # Turn 2: the raid can begin at the mill, and the cellar is armed.
            director({"event": "the_raid", "kind": "begin", "variant": "Ann at the mill"}),
            RAID,
            read(happened=[{"event": "the_raid", "quote": "Mara limped through the door"}]),
            # Turn 3: the cellar is still armed.
            director(),
            "Ann barred the door.",
            read(),
        ],
    )
    mara = session.character_by_name("Mara")

    list(session.send(turn(session, "I go into the mill.")))
    first_leaf = session.path()[-1].id
    # The raid is due tomorrow.
    session.set_clock(clock_minutes(4, 8, 0))
    assert "Mara" not in system_text(session)
    assert mara not in session.visible_cast()
    assert session.chronicle.place == "The Mill"
    assert session.chronicle.visited == ["The Mill"]
    assert [e.title for e in session.visible_lore()] == ["The Mill"]

    list(session.send(turn(session, "I sit by the millstone and rest.")))
    # The passage that introduces her carries her card in the direction,
    # not in the cached system block.
    direction = session.last_prompt.section(SECTION_DIRECTION).text
    assert "A trader with a limp" in direction
    assert "Mara" not in system_text(session)
    assert session.chronicle.events["the_raid"].state == "happened"
    assert session.chronicle.facts["raided"] == "yes"
    assert mara in session.visible_cast()

    list(session.send(turn(session, "I bar the door.")))
    assert "A trader with a limp" in system_text(session)

    # A branch from before the raid never met her.
    session.branch_from(first_leaf)
    assert mara not in session.visible_cast()
    assert session.hidden_ids() >= {mara.id}


def test_the_system_block_holds_until_someone_is_brought_in(tmp_path: Path):
    session = make_session(tmp_path, ["One.", read(), "Two.", read(minutes=5)])
    session.story.plot_schedule = {"the_raid": 4}
    list(session.send(turn(session, "I wait.")))
    first = session.last_prompt.messages[0]
    list(session.send(turn(session, "I wait more.")))
    assert session.last_prompt.messages[0] == first


def test_hidden_people_stay_out_of_the_reads(tmp_path: Path):
    session = make_session(tmp_path, ["One.", read()], scene_reads="every_turn")
    session.provider.responses = ["One.", "{}", read()]
    list(session.send(turn(session, "I wait.")))
    for request in session.provider.requests:
        text = "\n".join(part.text for message in request.messages for part in message.parts)
        assert "Mara" not in text and "The Cellar" not in text


def test_a_guaranteed_event_with_nothing_fitting_is_chosen_by_the_plot_model(tmp_path: Path):
    # Without "Nobody there", a character who never went to the mill fits no
    # way of the raid; it must happen, so the plot model picks.
    text = (
        PLOT_FILE[: PLOT_FILE.index("### Nobody there")] + PLOT_FILE[PLOT_FILE.index("# Events") :]
    )
    text = text.replace("start: day 3, 18:00", "start: day 5, 09:00")
    session = make_session(
        tmp_path,
        [json.dumps({"variant": "Ann away"}), "The day passes.", read()],
        text=text,
        raid_day=3,
    )
    list(session.send(turn(session, "I walk the fields.")))
    status = session.chronicle.events["the_raid"]
    assert status.state == "happened" and status.variant == "Ann away"
    assert session.chronicle.facts["raided"] == "yes"
    # Offscreen: nobody was met, so Mara stays out.
    assert session.character_by_name("Mara") not in session.visible_cast()
    # Dated to its own day, not the day it was found.
    assert status.at_minutes == clock_minutes(3, 22, 0)


def test_an_overdue_unconfirmed_event_is_taken_as_happened():
    p = plot()
    chronicle = initial_chronicle(p)
    chronicle.minutes = clock_minutes(6, 9, 0)
    chronicle.events["the_raid"].state = "directed"
    chronicle.events["the_raid"].variant = "Ann at the mill"
    for _passage in range(UNCONFIRMED_LIMIT - 1):
        assert settle_unconfirmed(p, chronicle, [], node_id="n") == []
    [settled] = settle_unconfirmed(p, chronicle, [], node_id="n")
    assert settled.id == "the_raid"
    assert chronicle.facts["raided"] == "yes"
    assert chronicle.events["the_raid"].state == "happened"


def test_missed_and_overdue_are_saved_on_the_turn(tmp_path: Path):
    text = PLOT_FILE.replace("- when: day 3–4\n", "- when: day 3–4\n- must happen: no\n")
    bundle = bundle_from_scenario(parse_plot_markdown(text).scenario)
    bundle.story.plot_schedule = {"the_raid": 3}
    bundle.story.plot.start_minutes = clock_minutes(6, 9, 0)
    # No variant fits a character who has never been anywhere, and it needn't
    # happen: missed, and saved even though nothing else changed.
    for event in bundle.story.plot.events:
        for variant in event.variants:
            variant.requires = [Condition(place="The Mill", place_test="at")]
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        Config(scene_reads="manual"),
        MockChatProvider(["Quiet.", read()]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.send(turn(session, "I wait.")))
    user_node = session.path()[-2]
    assert user_node.meta.chronicle is not None
    assert user_node.meta.chronicle.events["the_raid"].state == "missed"


def test_revealed_aftermath_stays_in_story_time():
    p = plot()
    chronicle = initial_chronicle(p)
    status = chronicle.events["the_raid"]
    status.state, status.revealed, status.at_minutes = "happened", True, clock_minutes(3, 22, 0)
    block = render_story_time_block(p, chronicle)
    assert "- The raid: The mill was stripped bare" in block
    # No clock and no dates unless the text asks for the clock ({now}).
    assert "Day 3" not in block and "It is now" not in block
    clocked = PromptTexts({"plot.story_time": "# STORY TIME\n\nIt is now {now} of the story."})
    block = render_story_time_block(p, chronicle, texts=clocked)
    assert "It is now Day 3 · 18:00 of the story." in block
    assert "The raid (Day 3 · 22:00): The mill was stripped bare" in block


def test_no_story_time_block_when_nothing_is_known():
    # Measured (Oct 2026): "It is now Day N" every turn made GLM 5.3 work out
    # days and hours wrongly, and Sonnet 4.6 no better; it is off by default.
    p = plot()
    assert render_story_time_block(p, initial_chronicle(p)) == ""


# --- the read ------------------------------------------------------------------


def test_the_read_moves_the_place_only_with_a_quote_and_a_known_place():
    p = plot()
    before = initial_chronicle(p)
    places = ["The Mill"]
    moved = parse_read(read(place=("the mill", "Ann pushed through the mill door.")))
    after = moved.applied_to(before, p, passage=PASSAGE, author_turn="", places=places)
    assert after.place == "The Mill" and after.visited == ["The Mill"]

    unquoted = parse_read(read(place=("The Mill", "She saw it from the ridge.")))
    after = unquoted.applied_to(before, p, passage=PASSAGE, author_turn="", places=places)
    assert after.place is None and unquoted.refused

    hidden = parse_read(read(place=("The Cellar", "Ann pushed through the mill door.")))
    after = hidden.applied_to(before, p, passage=PASSAGE, author_turn="", places=places)
    assert after.place is None and hidden.refused


def test_unwatched_facts_are_not_the_reads_to_change():
    p = plot()
    before = initial_chronicle(p)
    delta = parse_read(
        read(facts=[{"fact": "raided", "value": "yes", "quote": "Ann pushed through"}])
    )
    after = delta.applied_to(before, p, passage=PASSAGE, author_turn="")
    assert after.facts["raided"] == "no"
    assert "isn't the read's to change" in delta.refused[0]


def test_a_week_then_morning_lands_in_the_morning():
    p = plot()
    before = initial_chronicle(p)  # day 3, 18:00
    delta = parse_read(read(minutes=7 * 1440, quote="A week passes.", ends_at="08:00"))
    after = delta.applied_to(before, p, passage="", author_turn="A week passes.")
    assert after.minutes == clock_minutes(11, 8, 0)


def test_a_named_day_wins_when_quoted_and_not_past():
    p = plot()
    before = initial_chronicle(p)  # day 3
    delta = parse_read(read(minutes=21 * 1440, quote="On day 31, a knock.", day=31))
    after = delta.applied_to(before, p, passage="", author_turn="On day 31, a knock.")
    assert after.minutes // 1440 + 1 == 31

    past = parse_read(read(minutes=5, quote="Day 1 was long ago.", day=1))
    after = past.applied_to(before, p, passage="Day 1 was long ago.", author_turn="")
    assert after.minutes == before.minutes + 5 and past.refused


# --- shortcut flags from the playtest ---------------------------------------------

MARCUS = Character(id="marcus", name="Marcus Webb")
TRACKED = SceneState(location="The gate", tracked=True, present_character_ids=["john"])


@pytest.mark.parametrize(
    ("shortcut", "how"),
    [
        # A mere mention.
        (
            SceneShortcut(
                name="Marcus Webb",
                character_id="marcus",
                kind="already_there",
                quote="Marcus decides who settles",
            ),
            {"marcus webb": "mentioned as the one who decides who settles"},
        ),
        # Watching her own yard from her kitchen window.
        (
            SceneShortcut(
                name="Ruth",
                kind="perceived_from_outside",
                quote="Ruth saw the bowl from the kitchen window",
            ),
            {},
        ),
        # Backstory in past perfect.
        (
            SceneShortcut(
                name="Elena Ruiz",
                kind="already_there",
                quote="She'd checked the cot building once in the night",
            ),
            {},
        ),
        # A guess at who: the sentence names nobody.
        (
            SceneShortcut(
                name="Marcus Webb",
                character_id="marcus",
                kind="perceived_from_outside",
                quote="some silent exchange passing between them",
            ),
            {},
        ),
    ],
)
def test_the_playtest_false_shortcuts_are_dropped(shortcut, how):
    delta = SceneDelta(shortcuts=[shortcut], how=how)
    assert delta.credible_shortcuts(TRACKED, cast=[MARCUS]) == []


def test_a_real_eavesdropper_still_counts():
    rosa = Character(id="rosa", name="Rosa Delgado", aliases=["Rosa"])
    shortcut = SceneShortcut(
        name="Rosa Delgado",
        character_id="rosa",
        kind="perceived_from_outside",
        quote="Rosa had been listening from the junction",
    )
    delta = SceneDelta(shortcuts=[shortcut])
    assert delta.credible_shortcuts(TRACKED, cast=[rosa]) == [shortcut]


def test_a_mentioned_arrival_does_not_join_the_scene():
    from sealedlore.engine.scene_update import Resolved

    delta = SceneDelta(
        arrived=[Resolved(name="Marcus Webb", character_id="marcus")],
        how={"marcus webb": "mentioned as the one who decides who settles"},
    )
    after = delta.applied_to(TRACKED, cast=[MARCUS], held_id="john", node_id="n")
    assert "marcus" not in after.present_character_ids


def test_a_conditional_event_waits_for_its_trigger_after_the_window():
    text = PLOT_FILE.replace("- when: day 3–4\n", "- when: day 3–4\n- requires: mood = calm\n")
    p = plot(text)
    chronicle = initial_chronicle(p)
    chronicle.minutes = clock_minutes(9, 9, 0)
    chronicle.place = "The Mill"
    chronicle.visited = ["The Mill"]
    from sealedlore.engine.chronicle import armed, resolve_due

    result = resolve_due(p, chronicle, {"the_raid": 3}, node_id=None)
    status = chronicle.events["the_raid"]
    assert not status.overdue and status.state == "pending" and not result.undecided
    [item] = [a for a in armed(p, chronicle, {"the_raid": 3}) if a.event.id == "the_raid"]
    assert item.kinds == ("begin",) and not item.automatic


def test_a_director_turn_is_evidence_for_where_they_went():
    p = plot()
    before = initial_chronicle(p)
    author = "[Director]\nFive weeks pass. Ann settles in at the mill."
    delta = parse_read(read(place=("The Mill", "Ann settles in at the mill.")))
    after = delta.applied_to(
        before,
        p,
        passage="The weeks go by.",
        author_turn=author,
        places=["The Mill"],
        author_states_facts=True,
    )
    assert after.place == "The Mill"
    # The held character's own turn is only an attempt.
    delta = parse_read(read(place=("The Mill", "Ann settles in at the mill.")))
    after = delta.applied_to(
        before, p, passage="The weeks go by.", author_turn=author, places=["The Mill"]
    )
    assert after.place is None


def test_an_event_seen_to_happen_somewhere_puts_them_there():
    from sealedlore.engine.chronicle import happen

    text = PLOT_FILE.replace("### Ann at the mill\n", "### Ann at the mill\n- at: The Mill\n")
    p = plot(text)
    event = p.event("the_raid")
    assert event.variants[0].place == "The Mill"
    chronicle = initial_chronicle(p)
    happen(chronicle, event, event.variants[0], node_id="n", revealed=True)
    assert chronicle.place == "The Mill" and chronicle.visited == ["The Mill"]
    # Offscreen puts nobody anywhere.
    other = initial_chronicle(p)
    happen(other, event, event.variants[2], node_id="n", revealed=False)
    assert other.place is None and other.visited == []


# The second playtest's false shortcuts, each from a real passage.

JOHN = Character(id="john", name="John")
JANE = Character(id="jane", name="Jane")
ELENA = Character(id="elena", name="Elena Ruiz", aliases=["Elena"])
EVAN = Character(id="evan", name="Evan Stevens")


def shortcuts_kept(passage, shortcut, scene, cast, **kw):
    delta = SceneDelta(shortcuts=[shortcut])
    return delta.credible_shortcuts(scene, cast=cast, passage=passage, **kw)


def test_someone_on_the_roster_is_never_already_there():
    passage = (
        'The boy steadies himself. "Two days ago she found it."\n\n'
        "Behind John, the farmhouse door opened. Mara stood in the frame, reading the "
        "boy's face, then John's back.\n\n"
        '"You should have told me he might come," she said quietly.'
    )
    shortcut = SceneShortcut(
        name="Mara",
        kind="already_there",
        quote="Behind John, the farmhouse door opened. Mara stood in the frame, reading "
        "the boy's face, then John's back.",
    )
    scene = SceneState(
        location="the farmhouse",
        tracked=True,
        present_character_ids=["john"],
        present_others=["Mara"],
    )
    assert shortcuts_kept(passage, shortcut, scene, [JOHN]) == []
    # Not on the roster, the same sentence still counts.
    scene.present_others = []
    shortcut.quote = "Mara stood in the frame, reading the boy's face, then John's back."
    assert shortcuts_kept(passage, shortcut, scene, [JOHN]) == [shortcut]


def test_someone_only_talked_about_is_not_in_the_scene():
    passage = (
        'Cal looks up from the catalogue. "Two days," he says. "Maybe less if the snow\'s '
        'packed." He glances at Sarah, then back. "There was a woman there. Elena. She was '
        'a doctor."'
    )
    shortcut = SceneShortcut(
        name="Elena Ruiz",
        character_id="elena",
        kind="already_there",
        quote="There was a woman there. Elena. She was a doctor.",
    )
    scene = SceneState(location="the farmhouse", tracked=True, present_character_ids=["jane"])
    assert shortcuts_kept(passage, shortcut, scene, [JANE, ELENA]) == []


def test_a_messenger_naming_who_sent_him_is_not_them_reaching_in():
    passage = '"You John?" he called down, breath ragged. "Marcus Webb sent me."'
    shortcut = SceneShortcut(
        name="Marcus Webb",
        character_id="marcus",
        kind="perceived_from_outside",
        quote="Marcus Webb sent me.",
    )
    assert shortcuts_kept(passage, shortcut, TRACKED, [JOHN, MARCUS]) == []


def test_a_voice_heard_earlier_is_backstory():
    passage = (
        '"I\'ve been putting off the planting map," she admits. She glances toward the door, '
        "the direction Cal's voice had come from earlier."
    )
    shortcut = SceneShortcut(
        name="Cal",
        kind="already_there",
        quote="She glances toward the door, the direction Cal's voice had come from earlier.",
    )
    assert shortcuts_kept(passage, shortcut, TRACKED, [JANE]) == []


def test_a_stranger_giving_their_name_was_already_there():
    passage = 'He glances at the others. "You can walk with us. Name\'s Evan."'
    shortcut = SceneShortcut(
        name="Evan Stevens",
        character_id="evan",
        kind="already_there",
        quote="You can walk with us. Name's Evan.",
    )
    scene = SceneState(location="The road north", tracked=True, present_character_ids=["jane"])
    assert shortcuts_kept(passage, shortcut, scene, [JANE, EVAN]) == []


def test_whoever_the_event_brings_in_is_meant_to_turn_up():
    passage = "Evan Stevens leaned on the rail of the bridge, rifle across his knees."
    shortcut = SceneShortcut(
        name="Evan Stevens", character_id="evan", kind="already_there", quote=passage
    )
    scene = SceneState(location="The road north", tracked=True, present_character_ids=["jane"])
    assert shortcuts_kept(passage, shortcut, scene, [JANE, EVAN], introduced=[EVAN]) == []
    assert shortcuts_kept(passage, shortcut, scene, [JANE, EVAN]) == [shortcut]


def test_a_cutaway_is_the_story_being_told_following_the_whole_story():
    passage = (
        '"Voss had six men that we counted."\n\n'
        "Five miles north, Marcus Webb stood at the gate with his second rider already "
        "saddled, watching the road and doing the same arithmetic John was doing."
    )
    shortcut = SceneShortcut(
        name="Marcus Webb",
        character_id="marcus",
        kind="perceived_from_outside",
        quote="Five miles north, Marcus Webb stood at the gate with his second rider already "
        "saddled, watching the road and doing the same arithmetic John was doing.",
    )
    assert shortcuts_kept(passage, shortcut, TRACKED, [JOHN, MARCUS]) == []
    # Staying with the character, it is still reaching in.
    assert shortcuts_kept(passage, shortcut, TRACKED, [JOHN, MARCUS], strict=True) == [shortcut]


def test_real_shortcuts_survive_the_new_filters():
    rosa = Character(id="rosa", name="Rosa Delgado", aliases=["Rosa"])
    gus = Character(id="gus", name="Gus")
    jane = Character(id="jane", name="Jane")
    scene = SceneState(location="the radio room", tracked=True, present_character_ids=["jane"])
    cast = [jane, rosa, gus]
    # Narration, not speech.
    passage = "Jane turned. Rosa had been listening from the junction."
    eavesdrop = SceneShortcut(
        name="Rosa Delgado",
        character_id="rosa",
        kind="perceived_from_outside",
        quote="Rosa had been listening from the junction.",
    )
    assert shortcuts_kept(passage, eavesdrop, scene, cast) == [eavesdrop]
    # Naming himself on a channel is still reaching in.
    passage = '"This is Gus. I couldn\'t help hearing what you said to Hollis."'
    channel = SceneShortcut(
        name="Gus",
        character_id="gus",
        kind="perceived_from_outside",
        quote="This is Gus. I couldn't help hearing what you said to Hollis.",
    )
    assert shortcuts_kept(passage, channel, scene, cast) == [channel]
    # A place phrase alone doesn't make a cutaway of a paragraph about the scene.
    passage = "In the doorway, Rosa stood watching Jane, arms folded."
    doorway = SceneShortcut(
        name="Rosa Delgado", character_id="rosa", kind="already_there", quote=passage
    )
    assert shortcuts_kept(passage, doorway, scene, cast) == [doorway]


def test_single_quoted_speech_counts_as_speech_too():
    passage = (
        "Cal looks up from the catalogue. 'Two days,' he says. 'There was a woman there. "
        "Elena. She was a doctor.'"
    )
    shortcut = SceneShortcut(
        name="Elena Ruiz",
        character_id="elena",
        kind="already_there",
        quote="There was a woman there. Elena. She was a doctor.",
    )
    scene = SceneState(location="the farmhouse", tracked=True, present_character_ids=["jane"])
    assert shortcuts_kept(passage, shortcut, scene, [JANE, ELENA]) == []
    passage = "'You can walk with us,' he says. 'Name's Evan.'"
    shortcut = SceneShortcut(
        name="Evan Stevens",
        character_id="evan",
        kind="already_there",
        quote="Name's Evan.",
    )
    assert shortcuts_kept(passage, shortcut, scene, [JANE, EVAN]) == []


def test_apostrophes_in_the_narration_are_not_speech():
    rosa = Character(id="rosa", name="Rosa Delgado", aliases=["Rosa"])
    scene = SceneState(location="the radio room", tracked=True, present_character_ids=["jane"])
    for passage in (
        "He'd seen 'em coming from the Calloways' yard. Rosa had been listening from the junction.",
        "'Stay here,' Jane said. Rosa had been listening from the junction.",
    ):
        shortcut = SceneShortcut(
            name="Rosa Delgado",
            character_id="rosa",
            kind="perceived_from_outside",
            quote="Rosa had been listening from the junction.",
        )
        assert shortcuts_kept(passage, shortcut, scene, [JANE, rosa]) == [shortcut], passage


EXTENDED = Path(__file__).resolve().parent / "data" / "doomsville_extended.md"


def test_the_extended_story_parses_and_keeps_its_hidden_cast_out_of_the_prompt(tmp_path: Path):
    text = EXTENDED.read_text(encoding="utf-8")
    parsed = parse_plot_markdown(text)
    assert parsed.problems == [], [problem.describe() for problem in parsed.problems]
    plot = parsed.scenario.plot
    assert "The old frequency" not in [place.name for place in plot.places]
    assert {event.id for event in plot.events if event.guaranteed} >= {
        "the_camp_s_answer",
        "a_guide_across_the_belt",
        "the_relay_tower",
        "across_the_river",
    }
    session = make_session(tmp_path, [], text=text)
    session.assemble(turn(session, "I look around."))
    prompt = "\n".join(
        part.text for message in session.last_prompt.messages for part in message.parts
    )
    for hidden in ("Voss", "Vance", "Brennan", "Okafor", "Stevens", "Relay Tower", "Fort Garrow"):
        assert hidden not in prompt, hidden
    assert "Jane Moss" in prompt


def test_each_character_keeps_their_own_place_across_a_switch():
    from sealedlore.engine.chronicle import take_viewpoint

    p = plot()
    chronicle = initial_chronicle(p)
    assert take_viewpoint(chronicle, "ann")  # the story's start: nothing to keep
    chronicle.place, chronicle.visited = "The Mill", ["The Mill"]

    # Taking up someone never held: nowhere known, and the read's answer is
    # news, with no sentence of a move to ask for.
    assert take_viewpoint(chronicle, "bo")
    assert chronicle.place is None and chronicle.visited == [] and not chronicle.place_known
    delta = parse_read(read(place=("elsewhere", None)))
    after = delta.applied_to(chronicle, p, passage=PASSAGE, author_turn="", places=["The Mill"])
    assert after.place is None and after.place_known and not delta.refused
    # From then on a move needs its sentence again.
    delta = parse_read(read(place=("The Mill", None)))
    after = delta.applied_to(after, p, passage=PASSAGE, author_turn="", places=["The Mill"])
    assert after.place is None and delta.refused

    # And back: Ann is where she was.
    assert take_viewpoint(after, "ann")
    assert after.place == "The Mill" and after.visited == ["The Mill"] and after.place_known
    assert after.places == {"bo": None}
    assert not take_viewpoint(after, "ann")


def test_a_condition_can_be_about_a_named_character():
    from sealedlore.engine.chronicle import holds, take_viewpoint

    text = PLOT_FILE.replace(
        "## The cellar\n- requires: visited The Mill\n",
        "## The cellar\n- requires: Ann visited The Mill, Ann not at The Mill\n",
    ).replace("## Mara\n", "## Bo\n- role: player\n\nA second player.\n\n## Mara\n")
    parsed = parse_plot_markdown(text)
    assert parsed.ok, [problem.describe() for problem in parsed.problems]
    ann = parsed.scenario.cast[0].id
    requires = parsed.scenario.plot.event("the_cellar").requires
    assert [(c.who, c.place, c.place_test, c.op) for c in requires] == [
        (ann, "The Mill", "visited", "="),
        (ann, "The Mill", "at", "!="),
    ]
    chronicle = initial_chronicle(parsed.scenario.plot)
    take_viewpoint(chronicle, ann)
    chronicle.place, chronicle.visited = "The Mill", ["The Mill"]
    assert not holds(requires, chronicle)  # she is still there
    take_viewpoint(chronicle, "bo")  # holding Bo: it is still about Ann
    assert not holds(requires, chronicle)
    chronicle.places[ann] = None
    assert holds(requires, chronicle)


def test_the_read_is_told_whose_place_it_is():
    from sealedlore.engine.chronicle_read import build_read_messages

    p = plot()
    messages = build_read_messages(
        plot=p, chronicle=initial_chronicle(p), author_turn="", passage="x", held="John Carver"
    )
    assert "THE AUTHOR'S CHARACTER: John Carver" in messages[1].parts[0].text


# --- must happen, but the story moved past it; skipped over; revealed ---------------

MUST_RAID = PLOT_FILE.replace(
    "## The raid\n- when: day 3–4\n",
    "## The raid\n- when: day 3–4\n- requires: raided = no\n- must happen: yes\n",
)


def test_a_must_happen_event_whose_own_conditions_fail_is_missed_not_forced():
    from sealedlore.engine.chronicle import resolve_due

    p = plot(MUST_RAID)
    chronicle = initial_chronicle(p)
    chronicle.facts["raided"] = "yes"  # the author's story got there first
    chronicle.minutes = clock_minutes(6, 9, 0)
    result = resolve_due(p, chronicle, {"the_raid": 4}, node_id=None)
    assert chronicle.events["the_raid"].state == "missed"
    assert not result.undecided and not result.gap


def test_an_event_well_past_its_window_is_a_gap_not_overdue():
    from sealedlore.engine.chronicle import GAP_DAYS, resolve_due

    p = plot()
    chronicle = initial_chronicle(p)
    chronicle.place, chronicle.visited = "The Mill", ["The Mill"]
    chronicle.minutes = clock_minutes(4 + GAP_DAYS, 9, 0)  # just overdue
    result = resolve_due(p, chronicle, {"the_raid": 4}, node_id=None)
    assert chronicle.events["the_raid"].overdue and not result.gap

    chronicle.minutes = clock_minutes(4 + GAP_DAYS + 1, 9, 0)
    result = resolve_due(p, chronicle, {"the_raid": 4}, node_id=None)
    assert [(event.id, variant.title) for event, variant in result.gap] == [
        ("the_raid", "Ann at the mill")
    ]


GAP_ACCOUNT = (
    "Raiders hit the mill on the fourth night. Mara, a trader passing through, "
    "limped in to warn Ann, and the grain went north with them."
)


def gap_session(tmp_path: Path, gap_reply: str) -> StorySession:
    session = make_session(
        tmp_path,
        [
            # Turn 1: into the mill, before the raid's day.
            PASSAGE,
            read(place=("The Mill", "Ann pushed through the mill door.")),
            # Turn 2, days after the raid's window closed.
            gap_reply,
            director(),
            "Ann looked at the empty sacks.",
            read(),
        ],
    )
    list(session.send(turn(session, "I go into the mill.")))
    session.set_clock(clock_minutes(9, 10, 0))
    return session


def test_a_skipped_over_event_is_fitted_in_by_the_storyteller_and_told_as_past(tmp_path):
    reply = json.dumps({"accounts": [{"event": "the_raid", "account": GAP_ACCOUNT}]})
    session = gap_session(tmp_path, reply)
    notices = [
        e.text for e in session.send(turn(session, "I sweep the floor.")) if hasattr(e, "text")
    ]
    assert any("time that passed" in n for n in notices)
    status = session.chronicle.events["the_raid"]
    assert status.state == "happened" and status.revealed and status.account == GAP_ACCOUNT
    # Dated to its own day, not today.
    assert status.at_minutes is not None and status.at_minutes < clock_minutes(9, 0, 0)
    assert session.character_by_name("Mara").id in session.chronicle.brought_in
    # The side call rode the story's own prefix: same system block as the turn.
    log = [e for e in read_api_log(session.story.id, root=tmp_path) if e["kind"] == "gap_request"]
    assert log and "not from the author" in json.dumps(log[0]["payload"]).lower()
    prompt = "\n".join(p.text for m in session.last_prompt.messages for p in m.parts)
    assert "This happened in the time that has just passed: " + GAP_ACCOUNT in prompt
    assert "Mara" in system_text(session)
    assert session.story.plot_schedule["the_raid"] == 4


def test_a_failed_gap_call_still_settles_the_event_in_the_plots_words(tmp_path):
    session = gap_session(tmp_path, "I'd rather not.")
    notices = [
        e.text for e in session.send(turn(session, "I sweep the floor.")) if hasattr(e, "text")
    ]
    assert any("Couldn't ask how the skipped-over events went" in n for n in notices)
    status = session.chronicle.events["the_raid"]
    assert status.state == "happened"
    assert status.account.startswith("Raiders come; Mara the trader limps in")


def test_a_revealed_aftermath_brings_in_whoever_it_names(tmp_path):
    text = PLOT_FILE.replace(
        "- aftermath: The mill was stripped bare; the raiders went north with the grain.",
        "- aftermath: The mill was stripped bare. Mara the trader got away and went south.",
    )
    session = make_session(
        tmp_path,
        [
            director({"event": "the_raid", "kind": "reveal"}),
            "A carter tells Ann the mill was stripped.",
            read(),
        ],
        text=text,
    )
    session.set_clock(clock_minutes(5, 10, 0))  # the raid went offscreen: never at the mill
    list(session.send(turn(session, "Any news from the mill?")))
    chronicle = session.chronicle
    assert chronicle.events["the_raid"].revealed
    assert session.character_by_name("Mara").id in chronicle.brought_in


def test_an_event_the_story_leaves_no_room_for_begins_now_instead(tmp_path):
    session = make_session(
        tmp_path,
        [
            PASSAGE,
            read(place=("The Mill", "Ann pushed through the mill door.")),
            json.dumps({"accounts": [{"event": "the_raid", "account": "", "not_yet": True}]}),
            # Overdue, it begins by itself: no director call.
            RAID,
            read(),
        ],
    )
    list(session.send(turn(session, "I go into the mill.")))
    session.set_clock(clock_minutes(9, 10, 0))
    list(session.send(turn(session, "I sweep the floor.")))
    user = session.path()[-2]
    assert [(d.event_id, d.kind) for d in user.meta.direction] == [("the_raid", "begin")]
    status = session.chronicle.events["the_raid"]
    assert status.not_in_gap and status.state == "directed" and not status.account


# --- the director isn't asked about a place nobody is near -------------------------

GATED = PLOT_FILE.replace(
    "## The cellar\n- requires: visited The Mill\n",
    "## The cellar\n- at: The Mill\n",
)


def test_a_place_bound_optional_event_is_armed_only_there_or_when_the_place_comes_up():
    from sealedlore.engine.chronicle import armed

    p = plot(GATED)
    chronicle = initial_chronicle(p)
    chronicle.minutes = clock_minutes(1, 9, 0)  # before the raid's lead-in days
    days = {"the_raid": 4}

    def ids(**kw):
        return [item.event.id for item in armed(p, chronicle, days, **kw)]

    assert ids() == ["the_cellar"]  # ungated: the Plot tab's view
    assert ids(mentioned=set()) == []
    assert ids(mentioned={"The Mill"}) == ["the_cellar"]
    chronicle.place = "The Mill"
    assert ids(mentioned=set()) == ["the_cellar"]
    # Once directed it stays armed wherever they go, until it is seen to happen.
    chronicle.place = None
    chronicle.events["the_cellar"].state = "directed"
    assert ids(mentioned=set()) == ["the_cellar"]


def test_no_director_call_while_the_place_is_far_away(tmp_path):
    session = make_session(
        tmp_path,
        ["Ann walked the ridge.", read(), director(), "The mill stood below.", read()],
        text=GATED,
    )
    first = [e.text for e in session.send(turn(session, "I walk the ridge.")) if hasattr(e, "text")]
    assert not any("Consulting the director" in n for n in first)
    second = [
        e.text
        for e in session.send(turn(session, "I head down to the mill."))
        if hasattr(e, "text")
    ]
    assert any("Consulting the director" in n for n in second)


# --- the two reads after a passage, made side by side ------------------------------


class TwoLines(MockChatProvider):
    """A scripted provider that can make a second call alongside its own."""

    def __init__(self, responses, side_responses):
        super().__init__(responses)
        self.side = MockChatProvider(side_responses)

    def sibling(self):
        return self.side


def test_the_chronicle_read_goes_out_alongside_the_scene_read(tmp_path):
    session = make_session(tmp_path, [], scene_reads="every_turn")
    scene_reply = json.dumps({"location": "The mill", "arrived": [], "left": [], "shortcuts": []})
    chronicle_reply = read(minutes=20, place=("The Mill", "Ann pushed through the mill door."))
    session.provider = TwoLines([PASSAGE, scene_reply], [chronicle_reply])
    notices = [
        e.text for e in session.send(turn(session, "I go into the mill.")) if hasattr(e, "text")
    ]
    leaf = session.path()[-1]
    assert leaf.meta.scene is not None and leaf.meta.scene.location == "The mill"
    assert session.chronicle.place == "The Mill" and session.chronicle.elapsed == 20
    assert session.provider.side._call_count == 1  # the side line made the chronicle's call
    assert any(n.startswith("Scene:") for n in notices) and any(
        n.startswith("Plot: +20m") for n in notices
    )
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds.count("chronicle_request") == 1 and kinds.count("chronicle_response") == 1


def test_a_failed_side_call_is_a_notice_not_a_failed_turn(tmp_path):
    session = make_session(tmp_path, [], scene_reads="every_turn")
    scene_reply = json.dumps({"location": "The mill", "arrived": [], "left": [], "shortcuts": []})
    session.provider = TwoLines([PASSAGE, scene_reply], ["not json at all"])
    notices = [
        e.text for e in session.send(turn(session, "I go into the mill.")) if hasattr(e, "text")
    ]
    assert any("Couldn't read the clock and facts" in n for n in notices)
    assert session.path()[-1].content == PASSAGE


def test_speech_that_wraps_over_a_line_is_still_speech():
    passage = (
        'Cal looks up. "Two days," he says. "There was a woman there.\nElena. She was a doctor."'
    )
    shortcut = SceneShortcut(
        name="Elena Ruiz",
        character_id="elena",
        kind="already_there",
        quote="Elena. She was a doctor.",
    )
    scene = SceneState(location="the farmhouse", tracked=True, present_character_ids=["jane"])
    assert shortcuts_kept(passage, shortcut, scene, [JANE, ELENA]) == []


def test_regenerating_a_gap_turn_reuses_its_account_without_asking_again(tmp_path):
    reply = json.dumps({"accounts": [{"event": "the_raid", "account": GAP_ACCOUNT}]})
    session = gap_session(tmp_path, reply)
    list(session.send(turn(session, "I sweep the floor.")))
    session.provider = MockChatProvider(["Ann counted the empty sacks again.", read()])
    list(session.regenerate())
    log = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert log.count("gap_request") == 1
    prompt = "\n".join(p.text for m in session.last_prompt.messages for p in m.parts)
    assert "This happened in the time that has just passed: " + GAP_ACCOUNT in prompt
    assert session.chronicle.events["the_raid"].account == GAP_ACCOUNT


def test_switching_there_and_back_in_a_story_restores_each_place(tmp_path):
    text = PLOT_FILE.replace(
        "## Mara\n", "## Bo\n- role: player\n\nA second player, far away.\n\n## Mara\n"
    )
    session = make_session(
        tmp_path,
        [
            PASSAGE,
            read(place=("The Mill", "Ann pushed through the mill door.")),
            # No director call for Bo: the cellar needs a visit to the mill,
            # and that is asked of whoever is held.
            "Bo watched the river, a day's ride south.",
            read(place=("elsewhere", None)),
            director(),
            "Ann swept the mill floor.",
            read(),
        ],
        text=text,
    )
    ann, bo = session.cast[0], session.character_by_name("Bo")
    list(session.send(turn(session, "I go into the mill.")))
    assert session.chronicle.place == "The Mill"

    session.hold(bo.id)
    request = TurnRequest(speaker_id=bo.id, user_text="I watch.", controlled_character_id=bo.id)
    list(session.send(request))
    chronicle = session.chronicle
    assert chronicle.held_id == bo.id and chronicle.place is None and chronicle.place_known
    assert chronicle.places == {ann.id: "The Mill"}

    session.hold(ann.id)
    list(session.send(turn(session, "I sweep.")))
    chronicle = session.chronicle
    assert chronicle.held_id == ann.id and chronicle.place == "The Mill"
    assert chronicle.visited == ["The Mill"] and chronicle.places == {bo.id: None}


ANY_TIME = GATED.replace("## The cellar\n- at: The Mill\n", "## The cellar\n- pacing: any time\n")


def test_a_declined_any_time_event_rests_a_little_longer_each_time():
    from sealedlore.engine.chronicle import armed, note_asked, note_skipped, resting

    p = plot(ANY_TIME)
    chronicle = initial_chronicle(p)
    chronicle.minutes = clock_minutes(1, 9, 0)
    items = armed(p, chronicle, {"the_raid": 4})
    assert [item.event.id for item in items] == ["the_cellar"]
    assert not resting(items[0], chronicle, "the ridge")

    note_asked(items, set(), chronicle, "the ridge")
    assert resting(items[0], chronicle, "the ridge")
    note_skipped(items, chronicle)
    assert not resting(items[0], chronicle, "the ridge")  # one turn's rest, the first time
    note_asked(items, set(), chronicle, "the ridge")
    assert chronicle.events["the_cellar"].rest == 2
    # The scene moving, or hours passing, ends a rest at once.
    assert not resting(items[0], chronicle, "the mill yard")
    chronicle.minutes += 6 * 60
    assert not resting(items[0], chronicle, "the ridge")
    # An event it began isn't counted as declined.
    before = chronicle.events["the_cellar"].declined
    note_asked(items, {"the_cellar"}, chronicle, "the ridge")
    assert chronicle.events["the_cellar"].declined == before


def test_a_quiet_moment_event_is_asked_about_every_turn():
    from sealedlore.engine.chronicle import armed, note_asked, resting

    p = plot(GATED.replace("- at: The Mill\n", ""))  # the cellar, quiet moment by default
    chronicle = initial_chronicle(p)
    chronicle.minutes = clock_minutes(1, 9, 0)
    items = armed(p, chronicle, {"the_raid": 4})
    note_asked(items, set(), chronicle, "the ridge")
    assert not resting(items[0], chronicle, "the ridge")


def test_the_director_is_not_asked_again_on_the_very_next_turn(tmp_path):
    session = make_session(
        tmp_path,
        [
            director(),
            "Ann walked the ridge.",
            read(),
            # Turn 2: the cellar is resting, so no director call.
            "Ann kept walking.",
            read(),
            director(),
            "Ann stopped to drink.",
            read(),
        ],
        text=ANY_TIME,
    )

    def consulted(text: str) -> bool:
        notices = [e.text for e in session.send(turn(session, text)) if hasattr(e, "text")]
        return any("Consulting the director" in n for n in notices)

    assert [consulted("I walk."), consulted("I keep on."), consulted("I drink.")] == [
        True,
        False,
        True,
    ]


def test_the_scene_card_vetoes_a_move_to_elsewhere_while_it_names_the_place():
    p = plot()
    before = initial_chronicle(p)
    before.place, before.visited = "The Mill", ["The Mill"]
    passage = "Ann stepped out into the yard."
    delta = parse_read(read(place=("elsewhere", "Ann stepped out into the yard.")))
    after = delta.applied_to(
        before,
        p,
        passage=passage,
        author_turn="",
        places=["The Mill"],
        scene_location="the yard of the mill",
    )
    assert after.place == "The Mill" and "the scene is still there" in delta.refused[0]
    # Once the scene has moved on, the same read is taken.
    delta = parse_read(read(place=("elsewhere", "Ann stepped out into the yard.")))
    after = delta.applied_to(
        before, p, passage=passage, author_turn="", places=["The Mill"], scene_location="the ridge"
    )
    assert after.place is None


def test_a_furtive_listener_is_not_a_cutaway():
    marcus = Character(id="marcus", name="Marcus Webb", aliases=["Marcus"])
    passage = (
        '"Don\'t tell Marcus," Elena said, low. "He counts every body."\n\n'
        "In the corridor outside, Marcus had been standing by the half-open door the whole "
        "time, listening to every word. He did not move away."
    )
    scene = SceneState(
        location="the clinic in Hellsville", tracked=True, present_character_ids=["john", "elena"]
    )
    quote = (
        "In the corridor outside, Marcus had been standing by the half-open door the whole "
        "time, listening to every word."
    )
    for kind in ("perceived_from_outside", "already_there"):
        shortcut = SceneShortcut(name="Marcus Webb", character_id="marcus", kind=kind, quote=quote)
        assert shortcuts_kept(passage, shortcut, scene, [JOHN, ELENA, marcus]) == [shortcut]
    # An ordinary cutaway still isn't a shortcut.
    passage = "Five miles north, Marcus Webb stood at the gate and watched the road."
    shortcut = SceneShortcut(
        name="Marcus Webb", character_id="marcus", kind="perceived_from_outside", quote=passage
    )
    assert shortcuts_kept(passage, shortcut, scene, [JOHN, ELENA, marcus]) == []


def test_a_director_turn_naming_the_place_needs_no_quote():
    p = plot()
    before = initial_chronicle(p)
    places = ["The Mill"]
    author = "[Director]\nFive weeks pass. Ann settles in at The Mill."
    delta = parse_read(read(place=("The Mill", None)))
    after = delta.applied_to(
        before,
        p,
        passage="The weeks go by.",
        author_turn=author,
        places=places,
        author_states_facts=True,
    )
    assert after.place == "The Mill"
    # Named only as where she came from, it isn't a move there.
    author = "[Director]\nFive weeks pass. Ann rides away from The Mill."
    delta = parse_read(read(place=("The Mill", None)))
    after = delta.applied_to(
        before,
        p,
        passage="The weeks go by.",
        author_turn=author,
        places=places,
        author_states_facts=True,
    )
    assert after.place is None


def test_someone_the_previous_passage_showed_is_not_already_there():
    hale = Character(id="", name="Hale")
    scene = SceneState(location="the main street", tracked=True, present_character_ids=["john"])
    passage = "Hale, looking over his shoulder, exhaled through his nose."
    shortcut = SceneShortcut(name="Hale", kind="already_there", quote=passage)
    previous = "Sergeant Hale crossed the main street and crouched by the clipboard."
    assert shortcuts_kept(passage, shortcut, scene, [JOHN], previous=previous) == []
    # Talked about is not shown.
    previous = '"Hale will want to see this," Ruiz said.'
    assert shortcuts_kept(passage, shortcut, scene, [JOHN], previous=previous) == [shortcut]
    assert hale.name == "Hale"


def test_coming_down_from_the_radio_room_in_person_is_an_arrival():
    from sealedlore.engine.scene_update import parse_delta

    reply = (
        '{"arrived": [{"name": "Sergeant Hale",'
        ' "how": "came down from the radio room by the stairs"},'
        ' {"name": "Rosa", "how": "came in over the comm from the radio room"}]}'
    )
    assert [p.name for p in parse_delta(reply, cast=[]).arrived] == ["Sergeant Hale"]


def test_a_lead_in_never_names_who_the_event_will_bring(tmp_path: Path):
    from sealedlore.models.plot import Direction

    session = make_session(tmp_path, [], text=EXTENDED.read_text(encoding="utf-8"))
    lead_in = Direction(
        event_id="the_colonel_s_offer",
        kind="lead_in",
        text="Colonel Rory Vance arrives at Hellsville. Vance's manners are perfect.",
    )
    text = session._unnamed_lead_in(lead_in).text
    assert "Vance" not in text and text.startswith("someone arrives")
    begin = lead_in.model_copy(update={"kind": "begin"})
    assert session._unnamed_lead_in(begin).text == begin.text


def test_a_failed_read_still_confirms_an_event_the_passage_brings_in(tmp_path: Path):
    from sealedlore.models.node import Node

    session = make_session(tmp_path, [], text=EXTENDED.read_text(encoding="utf-8"))
    plot = session.story.plot
    before = initial_chronicle(plot)
    before.events["father_brennan_s_flock"].state = "directed"
    brennan = next(c for c in [*session.cast, *session.supporting] if c.name == "Aaron Brennan")
    node = Node(kind="assistant", speaker_id="__narrator__", content="Father Brennan bowed.")
    assert [e.id for e in session._confirm_by_introductions(node, before, plot)] == [
        "father_brennan_s_flock"
    ]
    assert node.meta.chronicle.events["father_brennan_s_flock"].state == "happened"
    assert brennan.id in node.meta.chronicle.brought_in
    # A passage that brings in nobody the event brings confirms nothing.
    node = Node(kind="assistant", speaker_id="__narrator__", content="Quiet on the main street.")
    assert session._confirm_by_introductions(node, before, plot) == []


def test_a_new_name_in_a_public_crowd_is_not_already_there():
    scene = SceneState(
        location="the mess hall", tracked=True, privacy="public", present_character_ids=["jane"]
    )
    passage = "Reyes, the nearest man, wiped grease from his hands and grinned at her."
    shortcut = SceneShortcut(name="Reyes", kind="already_there", quote=passage)
    assert shortcuts_kept(passage, shortcut, scene, [JANE]) == []
    scene.privacy = "private"
    assert shortcuts_kept(passage, shortcut, scene, [JANE]) == [shortcut]


# --- an event waits for the events it requires ---------------------------------------

CHAIN = """\
---
title: Chain
start: day 1, 09:00
---
# World
Cold.

# Characters
## Ann
- role: player
- present: yes

# Facts
- road: open (open, closed) — whether the road north is open

# Timeline
## The crossing
- when: day 2–3
- must happen: yes

Ann's guide reaches the far side of the pass.

## The arrival
- when: day 4–5
- requires: the crossing happened, road = open
- must happen: yes

The guide arrives at the gate.
"""


def test_a_late_prerequisite_holds_an_event_rather_than_missing_it():
    # Live (Between Stars): Ossareth was passed on and ran late, "Master
    # Soliss arrives" (which requires it) was missed at its window's close,
    # and everything after it in the story could never happen.
    from sealedlore.engine.chronicle import happen, is_late, last_day, resolve_due

    p = plot(CHAIN)
    days = {"the_crossing": 3, "the_arrival": 5}
    chronicle = initial_chronicle(p)
    chronicle.events["the_crossing"].state = "directed"
    chronicle.minutes = clock_minutes(8, 9, 0)  # the arrival's window closed on day 5
    resolve_due(p, chronicle, days, node_id=None)
    assert chronicle.events["the_arrival"].state == "pending"
    assert not chronicle.events["the_arrival"].overdue

    # The crossing happens on day 8: the arrival is due from then, not "late".
    crossing = p.event("the_crossing")
    happen(chronicle, crossing, crossing.variants[0], node_id=None, revealed=True)
    arrival = p.event("the_arrival")
    assert last_day(arrival, chronicle) == 8 and not is_late(arrival, chronicle)
    result = resolve_due(p, chronicle, days, node_id=None)
    assert chronicle.events["the_arrival"].state == "pending" and not result.gap
    chronicle.minutes = clock_minutes(9, 9, 0)
    resolve_due(p, chronicle, days, node_id=None)
    assert chronicle.events["the_arrival"].overdue


def test_a_prerequisite_that_can_no_longer_happen_still_misses_it():
    from sealedlore.engine.chronicle import resolve_due

    p = plot(CHAIN)
    days = {"the_crossing": 3, "the_arrival": 5}
    chronicle = initial_chronicle(p)
    chronicle.events["the_crossing"].state = "missed"
    chronicle.minutes = clock_minutes(8, 9, 0)
    resolve_due(p, chronicle, days, node_id=None)
    assert chronicle.events["the_arrival"].state == "missed"

    # Waiting on an event, but a fact has moved on too: missed, as before.
    chronicle = initial_chronicle(p)
    chronicle.events["the_crossing"].state = "directed"
    chronicle.facts["road"] = "closed"
    chronicle.minutes = clock_minutes(8, 9, 0)
    resolve_due(p, chronicle, days, node_id=None)
    assert chronicle.events["the_arrival"].state == "missed"


# --- the author's own skip moves the clock before the passage ------------------------


def read_api_log_refusals(session: StorySession, tmp_path: Path) -> list[str]:
    log = read_api_log(session.story.id, root=tmp_path)
    return [r for e in log if e["kind"] == "chronicle_refused" for r in e["refused"]]


def test_a_director_skip_settles_the_skipped_events_before_its_passage(tmp_path):
    # Live (Between Stars): "Three weeks pass" was written knowing nothing of
    # the plot, and the next turn fitted five events into weeks the passage
    # had already told without them.
    from sealedlore.models.node import DIRECTOR_SPEAKER_ID

    reply = json.dumps({"accounts": [{"event": "the_raid", "account": GAP_ACCOUNT}]})
    session = make_session(
        tmp_path,
        [
            PASSAGE,
            read(place=("The Mill", "Ann pushed through the mill door.")),
            # The Director turn: the gap first, then the director, then the skip.
            reply,
            director(),
            "Five days went by in sweeping and mending, and in what the raid had left.",
            read(minutes=2 * 1440),  # the read undercounts the author's five days
        ],
    )
    list(session.send(turn(session, "I go into the mill.")))
    start = session.chronicle.minutes  # day 3, 18:10

    ann = session.cast[0].id
    skip = TurnRequest(
        speaker_id=DIRECTOR_SPEAKER_ID,
        user_text="Five days pass at the mill. Ann mends the roof.",
        controlled_character_id=ann,
    )
    list(session.send(skip))
    user_node = session.path()[-2]
    assert user_node.meta.chronicle.minutes == start + 5 * 1440
    assert user_node.meta.chronicle.elapsed_quote == "Five days pass at the mill."
    # The skip's own passage was told what happened in it.
    direction = session.last_prompt.section(SECTION_DIRECTION).text
    assert GAP_ACCOUNT in direction
    status = session.chronicle.events["the_raid"]
    assert status.state == "happened" and status.account == GAP_ACCOUNT
    # The gap request quoted the author's skip as what the story says of it.
    log = [e for e in read_api_log(session.story.id, root=tmp_path) if e["kind"] == "gap_request"]
    assert "Five days pass at the mill." in json.dumps(log[0]["payload"])
    # The read counted from before the skip, and can't end short of it.
    reads = [
        e for e in read_api_log(session.story.id, root=tmp_path) if e["kind"] == "chronicle_request"
    ]
    payload = json.dumps(reads[-1]["payload"], ensure_ascii=False)
    assert "THE CLOCK, before the author's turn: Day 3 · 18:10" in payload
    # Day 8, the day the author's five days reached, at whatever hour.
    assert session.chronicle.minutes == clock_minutes(8, 0, 0)
    assert any("moved the clock" in r for r in read_api_log_refusals(session, tmp_path))


def test_an_in_character_turn_or_a_count_moves_no_clock(tmp_path):
    from sealedlore.engine.chronicle_read import stated_skip

    session = make_session(tmp_path, [PASSAGE, read()])
    list(session.send(turn(session, "Five days pass, I think, as I sit here.")))
    assert session.path()[-2].meta.chronicle is None or (
        session.path()[-2].meta.chronicle.elapsed == 0
    )
    now = clock_minutes(51, 9, 0)
    assert stated_skip("They are fifty-one days apart.", now) is None
    assert stated_skip("The burn takes three hours.", now) is None
    assert stated_skip("On day 1 she arrived.", now) is None
    # A day said as a plan is not where the clock is.
    assert stated_skip("Sisko sets the launch for day 60.", now) is None
    assert stated_skip("They must be ready by day 60.", now) is None
    assert stated_skip("The fleet is expected by day 60.", now) is None
    assert stated_skip("It is now day 65, 14:20.", now)[0] == clock_minutes(65, 9, 0)
    assert stated_skip("Two days pass. By day 58 he has read it. By day 62, nothing.", now)[
        0
    ] == clock_minutes(62, 9, 0)
    assert stated_skip("A few more days pass.", now)[0] == now + 3 * 1440


# --- an event the author writes themselves ------------------------------------------


def test_an_event_the_authors_director_turn_tells_has_happened():
    # Live (the author's own Between Stars story): the author wrote the
    # Jem'Hadar boarding in a Director turn, and the plot would have
    # scheduled it again, since only a passed-on event could be confirmed.
    from sealedlore.engine.chronicle_read import authors_candidates

    p = plot()
    before = initial_chronicle(p)
    before.place, before.visited = "The Mill", ["The Mill"]
    offered = authors_candidates(p, before)
    assert list(offered) == ["the_raid", "the_cellar"]
    turn = "Night falls. Raiders hit the mill, and Mara, a trader, limps in to warn Ann."
    claim = [{"event": "the_raid", "quote": "Raiders hit the mill, and Mara, a trader, limps in"}]
    delta = parse_read(read(happened=claim))
    after = delta.applied_to(
        before,
        p,
        passage="Ann barred the door.",
        author_turn=turn,
        author_states_facts=True,
        authors_events=offered,
    )
    status = after.events["the_raid"]
    assert status.state == "happened" and status.variant == "Ann at the mill"
    assert after.facts["raided"] == "yes"
    assert set(offered["the_raid"].brings) <= set(after.brought_in)

    # The sentence must be the author's, not the storyteller's.
    delta = parse_read(read(happened=claim))
    after = delta.applied_to(
        before,
        p,
        passage="Raiders hit the mill, and Mara, a trader, limps in to warn Ann.",
        author_turn="Night falls.",
        author_states_facts=True,
        authors_events=offered,
    )
    assert after.events["the_raid"].state == "pending" and delta.refused

    # An in-character turn is not the author's statement of an event.
    delta = parse_read(read(happened=claim))
    after = delta.applied_to(
        before, p, passage="Ann barred the door.", author_turn=turn, authors_events=offered
    )
    assert after.events["the_raid"].state == "pending"


def test_only_a_director_turn_offers_the_read_the_plots_events(tmp_path):
    from sealedlore.models.node import DIRECTOR_SPEAKER_ID

    session = make_session(
        tmp_path,
        [
            PASSAGE,
            read(place=("The Mill", "Ann pushed through the mill door.")),
            director(),
            "Ann waited.",
            read(),
        ],
    )
    list(session.send(turn(session, "I go into the mill.")))
    ann = session.cast[0].id
    list(
        session.send(
            TurnRequest(
                speaker_id=DIRECTOR_SPEAKER_ID,
                user_text="An hour passes quietly.",
                controlled_character_id=ann,
            )
        )
    )
    log = read_api_log(session.story.id, root=tmp_path)
    reads = [json.dumps(e["payload"]) for e in log if e["kind"] == "chronicle_request"]
    assert "EVENTS THE AUTHOR'S TURN MAY HAVE TOLD" not in reads[0]
    assert "EVENTS THE AUTHOR'S TURN MAY HAVE TOLD" in reads[1]
    assert "the_raid: The raid." in reads[1]


def test_the_authors_events_have_their_own_field_in_the_reply():
    # Under "happened", whose quotes are the passage's, GLM 5.3 quoted the
    # passage for the author's own events 4 times in 5; in a field of its
    # own it quoted the author's turn.
    from sealedlore.engine.chronicle_read import authors_candidates

    p = plot()
    before = initial_chronicle(p)
    before.place, before.visited = "The Mill", ["The Mill"]
    reply = json.loads(read())
    reply["author_told"] = [{"event": "the_raid", "quote": "Raiders hit the mill."}]
    delta = parse_read(json.dumps(reply))
    assert delta.author_told == [("the_raid", "Raiders hit the mill.")]
    after = delta.applied_to(
        before,
        p,
        passage="Ann barred the door.",
        author_turn="Raiders hit the mill. Ann bars the door.",
        author_states_facts=True,
        authors_events=authors_candidates(p, before),
    )
    assert after.events["the_raid"].state == "happened"
