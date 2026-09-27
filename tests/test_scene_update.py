"""Reading the scene back out of the prose (engine/scene_update.py)."""

from __future__ import annotations

import json

import pytest

from sealedlore.engine.scene_update import build_scene_messages, parse_scene
from sealedlore.models.character import Character
from sealedlore.models.scene import OffstageCharacter, SceneState

CAST = [
    Character(id="char-jane", name="Jane Moss", aliases=["Moss"]),
    Character(id="char-john", name="John Carver"),
]


def reply(**fields) -> str:
    return json.dumps(fields)


def test_a_proposal_resolves_names_to_cast_ids():
    proposal = parse_scene(
        reply(
            location="The clinic in Hellsville",
            time_of_day="late evening",
            situation="Jane is recovering; John has just arrived and is meditating.",
            present=["Jane Moss", "John Carver"],
        ),
        cast=CAST,
    )

    assert proposal.location == "The clinic in Hellsville"
    assert proposal.present_character_ids == ["char-jane", "char-john"]
    assert proposal.present_others == []


def test_an_alias_still_resolves():
    proposal = parse_scene(reply(present=["Moss"]), cast=CAST)
    assert proposal.present_character_ids == ["char-jane"]


def test_a_name_no_cast_member_answers_to_is_kept_as_someone_else_here():
    """The story's own people are in the room too. Dropping them is how the
    roster used to govern one character while the station came and went."""
    proposal = parse_scene(reply(present=["Jane Moss", "Doctor Ruiz"]), cast=CAST)

    assert proposal.present_character_ids == ["char-jane"]
    assert proposal.present_others == ["Doctor Ruiz"]
    # Never guessed into the cast.
    assert "Doctor Ruiz" not in [c.name for c in CAST]


def test_a_card_on_file_resolves_to_its_own_name():
    ruiz = Character(id="card-ruiz", name="Dr. Elena Ruiz", aliases=["Ruiz"])
    proposal = parse_scene(reply(present=["Elena", "Gus"]), cast=CAST, supporting=[ruiz])
    assert proposal.present_others == ["Dr. Elena Ruiz", "Gus"]


def test_applying_a_full_read_starts_tracking_everyone():
    proposal = parse_scene(reply(present=["Jane Moss", "Hollis"]), cast=CAST)
    updated = proposal.applied_to(SceneState(), leaf_id="n1")
    assert updated.tracked
    assert updated.present_others == ["Hollis"]


def test_missing_fields_are_none_rather_than_guesses():
    proposal = parse_scene(reply(location="The main street", time_of_day=None), cast=CAST)
    assert proposal.location == "The main street"
    assert proposal.time_of_day is None
    assert proposal.situation is None


def test_a_reply_with_no_json_is_an_error():
    with pytest.raises(ValueError):
        parse_scene("I couldn't tell where they are.", cast=CAST)


def test_applying_keeps_what_the_proposal_did_not_establish():
    scene = SceneState(
        location="The river crossing",
        time_of_day="mid-morning",
        situation="Her ship has just been ejected.",
        present_character_ids=["char-jane"],
    )
    proposal = parse_scene(
        reply(location="The infirmary", present=["Jane Moss", "John Carver"]), cast=CAST
    )

    updated = proposal.applied_to(scene, leaf_id="node-9")

    assert updated.location == "The infirmary"
    assert updated.time_of_day == "mid-morning"  # not established, so kept
    assert updated.present_character_ids == ["char-jane", "char-john"]
    assert updated.set_at_node_id == "node-9"
    # The original is untouched until the author accepts.
    assert scene.location == "The river crossing"


def test_applying_clears_a_where_they_are_note_for_someone_now_present():
    scene = SceneState(
        present_character_ids=["char-jane"],
        offstage_but_nearby=[
            OffstageCharacter(character_id="char-john", note="still at the Northern Camp")
        ],
    )
    proposal = parse_scene(reply(present=["Jane Moss", "John Carver"]), cast=CAST)

    updated = proposal.applied_to(scene, leaf_id=None)

    assert updated.offstage_but_nearby == []


def test_the_prompt_names_the_cast_and_carries_the_prose():
    messages = build_scene_messages("She woke in the infirmary.", cast=CAST)
    assert messages[0].role == "system"
    assert "scene card" in messages[0].text
    assert "Jane Moss, John Carver" in messages[1].text
    assert "She woke in the infirmary." in messages[1].text


# --- the per-turn read: what one passage changed ----------------------------

from sealedlore.engine.scene_update import (  # noqa: E402
    build_read_messages,
    cut_to,
    parse_delta,
    resolve_name,
)

HOLLIS = Character(id="card-hollis", name="Constable Hollis", aliases=["Hollis"])
RUIZ = Character(id="card-ruiz", name="Dr. Elena Ruiz", aliases=["Ruiz"])
ON_FILE = [HOLLIS, RUIZ]


def read(**fields):
    return parse_delta(reply(**fields), cast=CAST, supporting=ON_FILE)


def tracked(**fields) -> SceneState:
    return SceneState(tracked=True, **fields)


def test_names_resolve_to_the_cast_a_card_or_as_written():
    assert resolve_name("Moss", cast=CAST, supporting=ON_FILE).character_id == "char-jane"
    assert resolve_name("Hollis", cast=CAST, supporting=ON_FILE).name == "Constable Hollis"
    assert resolve_name("Rosa", cast=CAST, supporting=ON_FILE).name == "Rosa"
    assert resolve_name("  ", cast=CAST, supporting=ON_FILE) is None


def test_an_arrival_joins_the_scene_and_says_how():
    before = tracked(present_character_ids=["char-jane"])
    delta = read(arrived=[{"name": "Hollis", "how": "came in from the security office"}])

    after = delta.applied_to(before, cast=CAST, held_id="char-jane", node_id="n2")

    assert after.present_others == ["Constable Hollis"]
    assert [(c.kind, c.name, c.detail) for c in after.changes] == [
        ("arrived", "Constable Hollis", "came in from the security office")
    ]
    assert after.source == "story"
    assert after.set_at_node_id == "n2"
    assert before.present_others == []  # the card it read is untouched


def test_someone_who_leaves_is_gone_but_the_authors_character_never_is():
    before = tracked(present_character_ids=["char-jane", "char-john"], present_others=["Rosa"])
    delta = read(left=[{"name": "John Carver"}, {"name": "Rosa"}, {"name": "Jane Moss"}])

    after = delta.applied_to(before, cast=CAST, held_id="char-jane", node_id="n2")

    assert after.present_character_ids == ["char-jane"]
    assert after.present_others == []


def test_a_passage_that_changes_nothing_leaves_the_card_alone():
    before = tracked(
        location="The infirmary",
        situation="Jane is recovering.",
        present_character_ids=["char-jane"],
        present_others=["Constable Hollis"],
        privacy="private",
    )
    delta = read(location=None, time_of_day="null", situation=None, arrived=[], left=[])

    after = delta.applied_to(before, cast=CAST, held_id="char-jane", node_id="n2")

    assert after.location == "The infirmary"
    assert after.present_others == ["Constable Hollis"]
    assert after.privacy == "private"
    assert after.changes == []


def test_a_closed_scene_forgets_the_old_rooms_situation_and_privacy():
    before = tracked(
        location="Hollis's office",
        situation="A private talk.",
        privacy="private",
        present_character_ids=["char-jane"],
        present_others=["Constable Hollis"],
    )
    delta = read(
        location="The main street",
        left=[{"name": "Hollis", "how": "stayed in his office"}],
        scene_closed={"gist": "Jane told Hollis about the relic."},
    )

    after = delta.applied_to(before, cast=CAST, held_id="char-jane", node_id="n2")

    assert delta.closed and delta.gist == "Jane told Hollis about the relic."
    assert after.location == "The main street"
    assert after.situation is None
    assert after.privacy is None
    assert after.present_others == []


def test_the_first_read_of_a_card_records_who_is_already_here():
    before = SceneState(present_character_ids=["char-jane"])  # untracked
    delta = read(already_here=["Jane Moss", "Hollis", "Rosa"], arrived=[])

    after = delta.applied_to(before, cast=CAST, held_id="char-jane", node_id="n2")

    assert after.tracked
    assert after.present_character_ids == ["char-jane"]
    assert after.present_others == ["Constable Hollis", "Rosa"]


def test_shortcuts_need_a_known_kind_and_a_quote():
    delta = read(
        shortcuts=[
            {"name": "Gus", "kind": "perceived_from_outside", "quote": "Gus had heard it all."},
            {"name": "Rosa", "kind": "rude", "quote": "Rosa laughed."},
            {"name": "Tess", "kind": "already_there", "quote": ""},
        ]
    )
    assert [(s.name, s.kind) for s in delta.shortcuts] == [("Gus", "perceived_from_outside")]


def test_a_read_with_no_json_is_an_error():
    with pytest.raises(ValueError):
        parse_delta("Nothing much changed.", cast=CAST)


def test_the_read_sees_the_card_the_people_on_file_and_the_passage():
    held = CAST[0]
    messages = build_read_messages(
        scene=tracked(location="The infirmary", present_character_ids=["char-jane"]),
        cast=CAST,
        supporting=ON_FILE,
        held=held,
        author_turn="[Jane Moss]\nI sit up.",
        passage="Hollis came in from the corridor.",
        earlier="An earlier passage.",
    )
    system, user = messages[0].text, messages[1].text
    assert "wherever Jane Moss, the author's character, is" in system
    assert "already_here" not in system  # tracked: no seeding
    assert "Jane Moss (the author's character)" in user
    assert "Constable Hollis (also: Hollis)" in user
    assert "Hollis came in from the corridor." in user
    assert "An earlier passage." not in user


def test_an_untracked_card_is_read_with_earlier_passages_to_start_the_record():
    messages = build_read_messages(
        scene=SceneState(present_character_ids=["char-jane"]),
        cast=CAST,
        supporting=ON_FILE,
        held=None,
        author_turn="",
        passage="Hollis nodded.",
        earlier="Hollis had been standing by the door all along.",
    )
    assert "already_here" in messages[0].text
    assert "Hollis had been standing by the door all along." in messages[1].text


def test_cutting_to_a_character_moves_the_scene_to_them():
    before = tracked(
        location="The infirmary",
        present_character_ids=["char-jane"],
        present_others=["Constable Hollis"],
        offstage_but_nearby=[
            OffstageCharacter(character_id="char-john", note="at the Northern Camp")
        ],
    )

    after = cut_to(before, CAST[1], cast=CAST)

    assert after.location == "at the Northern Camp"
    assert after.present_character_ids == ["char-john"]
    assert after.present_others == []
    assert not after.tracked  # who else is there is for the next read to find
    assert after.offstage_but_nearby[0].character_id == "char-jane"
    assert after.offstage_but_nearby[0].note == "at the infirmary"


# --- which shortcuts stand --------------------------------------------------
# Each case is one the live read got wrong on the 216-passage story.

from sealedlore.engine.scene_update import looks_like_a_name  # noqa: E402


def shortcut(name: str, kind: str, quote: str) -> dict:
    return {"name": name, "kind": kind, "quote": quote}


def test_people_found_where_the_scene_moved_to_were_already_there():
    delta = read(
        location="The shelter below the main street",
        shortcuts=[
            shortcut("Lucy", "already_there", "Lucy with young Tom pressed against her side")
        ],
    )
    assert delta.credible_shortcuts(tracked(location="Gus's trading post")) == []


def test_a_sentence_that_shows_the_arrival_is_not_already_there():
    delta = read(
        shortcuts=[
            shortcut(
                "Hollis", "already_there", "Hollis appeared from the security office at a dead run"
            ),
            shortcut("Rosa", "already_there", "It was Rosa who came to find her an hour later"),
        ]
    )
    assert delta.credible_shortcuts(tracked()) == []


def test_a_card_that_never_kept_the_room_cannot_say_who_was_already_there():
    delta = read(shortcuts=[shortcut("Ruiz", "already_there", "Ruiz looked up from the cot.")])
    assert delta.credible_shortcuts(SceneState()) == []
    # Listening in from outside is still judged on such a card.
    delta = read(
        shortcuts=[
            shortcut("Rosa", "perceived_from_outside", "Rosa had been listening from the junction.")
        ]
    )
    assert [s.name for s in delta.credible_shortcuts(SceneState())] == ["Rosa"]


def test_the_unnamed_are_never_shortcuts_and_never_arrivals():
    delta = read(
        arrived=[
            "Docking crew (unnamed)",
            "Young guard",
            "Pack mule",
            "Private Kim",
            "Wade",
        ],
        shortcuts=[shortcut("two docking crew", "perceived_from_outside", "Two crew watched.")],
    )
    assert [person.name for person in delta.arrived] == ["Private Kim", "Wade"]
    assert delta.credible_shortcuts(tracked()) == []


def test_a_real_shortcut_stands():
    delta = read(
        shortcuts=[shortcut("Gus", "already_there", "Gus leaned on the table between them.")]
    )
    assert [s.name for s in delta.credible_shortcuts(tracked(location="Hollis's office"))] == [
        "Gus"
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Wade", True),
        ("Rosa Delgado", True),
        ("Private Kim", True),
        ("the Grey Wolf", False),
        ("Docking bay officer", False),
        ("Docking crew (unnamed)", False),
        ("two security officers", False),
        ("A travelling trader", False),
    ],
)
def test_telling_a_name_from_a_description(name, expected):
    assert looks_like_a_name(name) is expected


def test_someone_heard_over_a_channel_has_not_arrived():
    """Every model tried did this, and said so in its own note."""
    delta = read(
        arrived=[
            {"name": "Rosa", "how": "over the security channel"},
            {"name": "Tess", "how": "in the radio room tracking the fence sensors"},
            {"name": "Hollis", "how": "ran in from the security office"},
        ]
    )
    assert [person.name for person in delta.arrived] == ["Constable Hollis"]


# --- the perspective decides what counts as reaching in ---------------------
# The author's ruling: following the whole story, people watching or hearing
# what their work gives them are the story being told; staying with the
# character, anything the character can't perceive is out.

RADIO_ROOM = "In the radio room, Tess was tracking the figure's progress on the fence sensors."
SECURITY = 'Rosa\'s voice came through on the security channel: "What just happened?"'
EAVESDROP = "Gus, listening in on the open comm, grinned."


def reaching_in(*quotes: str):
    return read(
        shortcuts=[
            shortcut(name, "perceived_from_outside", quote)
            for name, quote in zip(["Tess", "Rosa", "Gus"], quotes, strict=False)
        ]
    )


def test_following_the_whole_story_people_may_watch_by_their_own_means():
    delta = reaching_in(RADIO_ROOM, SECURITY, EAVESDROP)
    kept = delta.credible_shortcuts(tracked(), strict=False)
    # Eavesdropping is still the excess, whatever the channel.
    assert [s.name for s in kept] == ["Gus"]


def test_staying_with_the_character_nothing_from_outside_is_allowed():
    delta = reaching_in(RADIO_ROOM, SECURITY, EAVESDROP)
    kept = delta.credible_shortcuts(tracked(), strict=True)
    assert [s.name for s in kept] == ["Tess", "Rosa", "Gus"]


def read_prompt(perspective: str, held: Character | None) -> str:
    return build_read_messages(
        scene=tracked(present_character_ids=["char-jane"]),
        cast=CAST,
        supporting=ON_FILE,
        held=held,
        author_turn="",
        passage="Jane waited.",
        perspective=perspective,
    )[0].text


def test_the_read_is_told_which_perspective_it_is_judging():
    whole = read_prompt("whole_story", CAST[0])
    strict = read_prompt("with_character", CAST[0])

    assert "security on its own channel" in whole and "None of that is a shortcut" in whole
    assert "even if it is\ntheir job" in strict or "even if it is their job" in " ".join(
        strict.split()
    )
    assert "Jane Moss" in strict.split("Shortcuts")[1]
    assert "{" + "channels}" not in whole and "{" + "cutaway}" not in strict


def test_staying_with_nobody_is_following_the_scene():
    """With no character held, "stay with my character" has nobody to stay with."""
    assert "None of that is a shortcut" in read_prompt("with_character", None)


def test_an_arrival_the_text_never_names_is_dropped():
    from sealedlore.engine.scene_update import Resolved, SceneDelta

    elena = Character(id="elena", name="Elena Ruiz", aliases=["Elena"])
    delta = SceneDelta(arrived=[Resolved("Elena Ruiz", "elena"), Resolved("Marcus Webb")])
    passage = "One eye appeared in the gap. A girl, maybe sixteen. Marcus Webb stood behind her."
    dropped = delta.unnamed_arrivals((passage, "I call out."), people=[elena])
    assert [p.name for p in dropped] == ["Elena Ruiz"]
    assert [p.name for p in delta.arrived] == ["Marcus Webb"]
    # Named in the author's turn is named.
    delta = SceneDelta(arrived=[Resolved("Elena Ruiz", "elena")])
    assert delta.unnamed_arrivals((passage, "I let Elena in."), people=[elena]) == []
