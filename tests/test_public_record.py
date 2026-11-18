"""The public record for characters played by their own model
(engine/public_record.py, engine/session_record.py): what everyone where the
story is set knows by now, kept after every passage by a cheap model and given
to each of them before the story as they saw it; the keeper's private notes
reach no one else."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.public_record import (
    HARD_FACTS,
    RecordState,
    apply_reply,
    keeper_prompt,
    record_at,
    render_record,
)
from sealedlore.models.node import Node
from sealedlore.models.scene import SceneState
from tests.test_reactions import MAELA_MODEL, a_cast, make_session, play, sent, turn

HEADING = DEFAULT_TEXTS["characters.own.record.heading"]


def record_calls(session):
    return [r for r in session.provider.requests if "THE RECORD NOW" in sent(r)]


def character_calls(session):
    return [r for r in session.provider.requests if r.model == MAELA_MODEL]


# --- pure ---------------------------------------------------------------------


def test_the_reply_is_the_whole_record_and_new_notes() -> None:
    state = RecordState(("The bells rang.", "The gate is shut."), ("An old note.",))
    new = apply_reply(
        '{"facts": ["The bells rang.", "The gate is open again.", "Pilgrims came."],'
        ' "private": ["Mira told Odo a secret."]}',
        state,
    )
    assert new.facts == ("The bells rang.", "The gate is open again.", "Pilgrims came.")
    # Notes are only ever added to.
    assert new.notes == ("An old note.", "Mira told Odo a secret.")


def test_a_reply_that_wont_do_is_refused() -> None:
    state = RecordState(tuple(f"Fact {i}." for i in range(8)))
    assert apply_reply("no json here", state) is None
    assert apply_reply('{"facts": "one"}', state) is None  # the wrong shape
    assert apply_reply('{"private": ["x"]}', state) is None  # no record at all
    assert apply_reply('{"facts": ["Fact 0.", "Fact 1."]}', state) is None  # most gone at once
    assert apply_reply('{"facts": ["A."], "private": "x"}', RecordState()) is None


def test_past_the_ceiling_the_oldest_go_never_the_newest() -> None:
    """Cutting the newest froze a record that a keeper had filled: every later
    event was lost (DeepSeek V4.1 Flash, live)."""
    facts = [f"Fact {i}." for i in range(HARD_FACTS)] + ["The newest."]
    new = apply_reply(json.dumps({"facts": facts}), RecordState(tuple(facts[:-1])))
    assert new.facts[-1] == "The newest." and "Fact 0." not in new.facts
    assert len(new.facts) == HARD_FACTS


def test_the_record_anywhere_is_the_nearest_snapshot_above() -> None:
    first = Node(kind="assistant", speaker_id="__narrator__", content="One.")
    first.meta.public_record = ["Old."]
    second = Node(kind="assistant", speaker_id="__narrator__", content="Two.")
    second.meta.public_record = ["New."]
    second.meta.public_notes = ["A secret."]
    third = Node(kind="assistant", speaker_id="__narrator__", content="Three.")
    assert record_at([first, second, third]) == RecordState(("New.",), ("A secret.",))
    assert record_at([third]) == RecordState()


def test_the_keeper_reads_the_passage_with_its_scene_as_a_hint() -> None:
    node = Node(kind="assistant", speaker_id="__narrator__", content="The Engine faltered.")
    node.meta.scene = SceneState(location="The workshop", privacy="private")
    text = keeper_prompt(RecordState(("The bells rang.",), ("A secret.",)), [node])
    assert "The Engine faltered." in text and "The workshop; scene marked private" in text
    assert "1. The bells rang." in text and "1. A secret." in text
    assert render_record(()) == "" and render_record(["X."]).startswith(HEADING)


# --- the session ----------------------------------------------------------------


def test_the_record_is_kept_after_a_passage_and_given_to_the_round(tmp_path: Path) -> None:
    session = make_session(
        tmp_path,
        [
            "The door gives. Across the town the bells ring noon.",
            '{"facts": ["The town bells rang at noon."]}',
            "I pick the lock.",
        ],
        record=True,
    )
    play(session, turn())
    passage = session.path()[-1]
    assert passage.meta.public_record == ["The town bells rang at noon."]
    [keeper] = record_calls(session)
    assert "the bells ring noon" in sent(keeper)
    [asked] = character_calls(session)
    assert HEADING in sent(asked) and "The town bells rang at noon." in sent(asked)
    # Before the story as they saw it.
    assert sent(asked).index(HEADING) < sent(asked).index(
        DEFAULT_TEXTS["characters.own.history.heading"]
    )


def test_the_keepers_private_notes_reach_no_character(tmp_path: Path) -> None:
    session = make_session(
        tmp_path,
        [
            "The door gives.",
            '{"facts": [], "private": ["SECRET-NOTE: Serrik told Maela of the vault."]}',
            "I pick the lock.",
            "It opens.",
            '{"facts": []}',
            "Hm.",
        ],
        record=True,
    )
    play(session, turn())
    play(session, turn("I go in."))
    assert all("SECRET-NOTE" not in sent(r) for r in character_calls(session))
    # The keeper alone carries them on.
    assert "SECRET-NOTE" in sent(record_calls(session)[-1])


def test_no_call_without_a_character_on_their_own_model_or_with_it_off(tmp_path: Path) -> None:
    cast = a_cast()
    cast[1].own_model = None
    session = make_session(tmp_path / "none", ["The door gives."], cast=cast, record=True)
    play(session, turn())
    assert record_calls(session) == []
    session = make_session(tmp_path / "off", ["The door gives.", "I pick it."], record=False)
    play(session, turn())
    assert record_calls(session) == [] and session.path()[-1].meta.public_record is None


def test_an_unusable_reply_is_carried_forward_not_asked_again(tmp_path: Path) -> None:
    session = make_session(
        tmp_path,
        ["The door gives.", "not json at all", "I pick it.", "It opens.", '{"facts": []}', "Hm."],
        record=True,
    )
    play(session, turn())
    first = session.path()[-1]
    assert first.meta.public_record == []  # carried forward, empty
    play(session, turn("I go in."))
    # One keeper call a passage: the refused one wasn't paid for again.
    assert len(record_calls(session)) == 2


def test_passages_it_missed_are_read_one_at_a_time(tmp_path: Path) -> None:
    """A Stop or a failure leaves passages unread: the next round reads each
    of them, in order, before the characters answer."""
    # The first passage goes by with the record off, as an older story's would.
    session = make_session(
        tmp_path,
        [
            "The door gives.",
            "I pick it.",
            "It opens.",
            '{"facts": ["A door gave."]}',
            '{"facts": ["A door gave.", "A door opened."]}',
            "Hm.",
        ],
        record=False,
    )
    play(session, turn())
    session.config.public_record = True
    play(session, turn("I go in."))
    keepers = record_calls(session)
    assert len(keepers) == 2
    assert "The door gives." in sent(keepers[0]) and "It opens." in sent(keepers[1])
    assert session.path()[-1].meta.public_record == ["A door gave.", "A door opened."]


def test_after_the_turn_the_record_is_kept_before_they_answer(tmp_path: Path) -> None:
    from sealedlore.models.story import Story  # noqa: F401 - the story's order below

    session = make_session(
        tmp_path,
        [
            "The door gives.",
            '{"facts": ["A door gave."]}',
            "I pick the lock.",
            "It turns.",
        ],
        record=True,
    )
    session.story.defaults.round_order = "after"
    play(session, turn())
    play(session, turn("I hold the lamp."))
    asked = character_calls(session)
    assert asked and "A door gave." in sent(asked[-1])
