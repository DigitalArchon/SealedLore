"""Condensing the cards of a cast too large for its room, on the session's
side (engine/session_cast.py). Never the network: the scripted mock answers
(it has no `detached`, so the job runs inline), and the cards are Doomsville
people with long descriptions."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.card_condense import fingerprint
from sealedlore.engine.prompt import SECTION_CAST, TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import KIND_LABELS
from sealedlore.models.character import Character
from sealedlore.providers.base import ProviderError
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from tests.test_session import make_config

JANE = "Jane keeps the farmhouse stove going through the long winter. " * 90
ELENA = "Elena patches wounds in Hellsville's clinic and trusts nobody new. " * 70


def big_cast() -> list[Character]:
    return [
        Character(id="char-john", name="John Jacobs", summary="A survivor who walks alone."),
        Character(
            id="char-jane", name="Jane Jacobs", summary="John's wife.", full_description=JANE
        ),
        Character(id="char-elena", name="Elena Ruiz", summary="A medic.", full_description=ELENA),
    ]


def make_session(tmp_path: Path, story, replies, **config) -> StorySession:
    settings = make_config()
    settings.cast_limit = "tokens"
    settings.cast_token_cap = config.pop("cap", 1_000)
    for key, value in config.items():
        setattr(settings, key, value)
    story.held_character_id = "char-john"
    story.scene.present_character_ids = ["char-john", "char-jane", "char-elena"]
    bundle = StoryBundle(story=story, cast=big_cast())
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        settings,
        MockChatProvider(replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def by_id(session: StorySession, cid: str) -> Character:
    return next(c for c in session.cast if c.id == cid)


def test_waiting_cards_are_condensed_adopted_logged_and_counted(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["Condensed: a farm wife."])
    assert {c.id for c in session.cards_to_condense()} == {"char-jane", "char-elena"}
    assert session.start_condensing()
    assert session.condensing_ready()
    notices = session.adopt_condensed()
    assert notices and notices[0].startswith("Condensed the cards of")
    jane = by_id(session, "char-jane")
    assert jane.condensed == "Condensed: a farm wife."
    assert jane.condensed_from == fingerprint(jane)
    assert session.cards_to_condense() == []
    assert session.unreported_usage
    # The author's character is named to the condenser.
    assert "the author plays John Jacobs" in session.provider.requests[0].messages[0].text
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds.count("condense_request") == kinds.count("condense_response") == 2
    assert "condense_response" in KIND_LABELS
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert next(c for c in saved.cast if c.id == "char-jane").condensed


def test_the_next_turn_sends_them_condensed(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["Condensed card."])
    session.start_condensing()
    session.adopt_condensed()
    prompt = session.assemble(TurnRequest(speaker_id="char-john", user_text="Hello."))
    cast_text = prompt.section(SECTION_CAST).text
    assert "Condensed card." in cast_text
    assert "farmhouse stove" not in cast_text and "Hellsville's clinic" not in cast_text
    assert "A survivor who walks alone." in cast_text


def test_a_card_edited_while_it_was_condensed_keeps_waiting(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["Condensed card."])
    session.start_condensing()
    by_id(session, "char-jane").full_description += " She keeps a shotgun by the door."
    session.adopt_condensed()
    assert by_id(session, "char-jane").condensed is None
    assert [c.id for c in session.cards_to_condense()] == ["char-jane"]
    entries = read_api_log(session.story.id, root=tmp_path)
    assert any("changed while it was written" in (e.get("dropped") or "") for e in entries)


def test_a_failure_is_said_and_not_asked_again_until_the_card_changes(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["unused"])
    session.provider.error = ProviderError("HTTP 503")
    session.start_condensing()
    notices = session.adopt_condensed()
    assert any("Couldn't condense" in text for text in notices)
    assert set(session.condense_failures()) == {"char-jane", "char-elena"}
    assert session.cards_to_condense() == []
    # Asked for by name, it is tried again.
    session.provider.error = None
    assert session.start_condensing([by_id(session, "char-jane")])
    session.adopt_condensed()
    assert by_id(session, "char-jane").condensed
    # A card that changes may be tried again on its own.
    by_id(session, "char-elena").summary = "A medic in Hellsville."
    assert [c.id for c in session.cards_to_condense()] == ["char-elena"]


def test_a_turn_adopts_them_and_starts_the_next_after_its_passage(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["Condensed card."])
    session.start_condensing()
    turn = TurnRequest(
        speaker_id="char-john", user_text="John knocks.", controlled_character_id="char-john"
    )
    events = list(session.send(turn))
    assert any("Condensed the cards" in getattr(e, "text", "") for e in events)
    # The passage's request went out with them condensed.
    story_request = next(
        r for r in session.provider.requests if "John knocks." in r.messages[-1].text
    )
    assert "farmhouse stove" not in story_request.messages[0].text


def test_a_card_the_author_writes_is_kept_and_current(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["unused"])
    jane = by_id(session, "char-jane")
    session.set_condensed(jane, "Jane, John's wife: steady, practical, a crack shot.")
    assert jane.condensed_from == fingerprint(jane)
    assert [c.id for c in session.cards_to_condense()] == ["char-elena"]
    session.set_condensed(jane, "  ")
    assert jane.condensed is None and jane.condensed_from is None


def test_nothing_is_condensed_in_a_chat_or_for_a_hidden_card(tmp_path: Path, story, monkeypatch):
    session = make_session(tmp_path, story, ["unused"])
    monkeypatch.setattr(session, "hidden_ids", lambda *a, **k: {"char-jane"})
    assert [c.id for c in session.cards_to_condense()] == ["char-elena"]
    session.story.mode = "chat"
    assert session.cards_to_condense() == []
    assert not session.start_condensing()
    with pytest.raises(ValueError):
        session.condense_card(by_id(session, "char-elena"))


def test_a_private_model_set_up_condenses_for_the_scenes_room_too(tmp_path: Path, story):
    from sealedlore.models.config import ProviderConfig

    session = make_session(tmp_path, story, ["Condensed."], cap=10**6)
    assert session.cards_to_condense() == []
    session.config.private_provider = ProviderConfig(
        name="private", base_url="https://nano-gpt.com/api/v1", model="TEE/glm-5.3-flash"
    )
    session.config.cast_limit = "share"
    session.config.cast_share = 0.15
    session.config.cast_token_cap = 10**6
    session.config.private_budget = 7_000
    session.story.defaults.context_token_budget = 10**7
    # The story's room holds them whole; a scene's 1,050 tokens doesn't.
    assert not session.cast_fit().over
    assert {c.id for c in session.cards_to_condense()} == {"char-jane", "char-elena"}


def test_closing_logs_a_job_not_adopted(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["Condensed."])
    session.start_condensing()
    session.close(wait_seconds=0)
    entries = read_api_log(session.story.id, root=tmp_path)
    dropped = [e for e in entries if e["kind"] == "condense_response"]
    assert dropped and all("closed" in e.get("dropped", "") for e in dropped)


def test_a_long_supporting_card_is_condensed_short(tmp_path: Path, story):
    session = make_session(tmp_path, story, ["Condensed."], cap=10**6)
    rival = Character(
        id="sup-marcus", name="Marcus Hale", summary="A raider.", full_description=ELENA
    )
    session.supporting.append(rival)
    assert [c.id for c in session.cards_to_condense()] == ["sup-marcus"]
    session.start_condensing()
    assert "at most 150 words" in session.provider.requests[-1].messages[0].text
    session.adopt_condensed()
    assert rival.condensed == "Condensed."
    assert session.cards_to_condense() == []
