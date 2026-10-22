"""Fill with lore characters: cards for the people of a named world
(engine/lore_cast.py, StorySession.fill_lore_cast). Never the network: the
scripted mock answers, and the world is the tests' own invented one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.lore_cast import (
    MOST_KEPT,
    build_lore_cast_messages,
    generic_alias,
    parse_lore_cast,
)
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from tests.test_session import make_config

WORLD = "The Calder Keep chronicles"


def card(name: str, **fields) -> dict:
    return {
        "name": name,
        "canon": WORLD,
        "summary": f"{name}, of the Keep.",
        "description": "Tall, grey-cloaked.",
        **fields,
    }


REPLY = json.dumps(
    {
        "cast": [
            card("Maren Holt", skills={"swordplay": "expert", "tact": "poor"}, playable=True),
            card("Serrik Vaun"),  # already on file
        ],
        "supporting": [
            card("Old Tobin", skills={"brewing": "expert"}, playable=True),
            card("Wolf of the Keep", aliases=["the Grey Wolf"]),  # Serrik's alias
            card("Mrs. Holt"),  # shares a surname, not a name: kept
        ],
        "notes": "The chronicles never name the gatekeeper.",
    }
)


def test_the_request_names_the_world_the_wanted_the_setting_and_who_is_on_file():
    messages = build_lore_cast_messages(
        WORLD, wanted="Maren Holt, Old Tobin", setting="A hundred years on.", known=["Serrik"]
    )
    system, user = messages[0].text, messages[1].text
    assert '"cast"' in system and '"supporting"' in system and "16 cards" in system
    assert '"expert"' in system  # the tiers the parser accepts
    assert WORLD in user and "Maren Holt, Old Tobin" in user
    assert "A hundred years on." in user and "Serrik" in user
    with pytest.raises(ValueError):
        build_lore_cast_messages("  ")


def test_the_reply_becomes_cards_and_nobody_on_file_is_added_twice(cast):
    result = parse_lore_cast(REPLY, cast)
    assert [c.name for c in result.cast] == ["Maren Holt"]
    assert [c.name for c in result.supporting] == ["Old Tobin", "Mrs. Holt"]
    assert sorted(result.skipped) == ["Serrik Vaun", "Wolf of the Keep"]
    maren = result.cast[0]
    assert maren.canon == WORLD and maren.is_player_available
    assert maren.competence.tiers == {"swordplay": "expert", "tact": "poor"}
    tobin = result.supporting[0]
    # A supporting card is the storyteller's, with no skills, whatever it says.
    assert not tobin.is_player_available and tobin.competence.tiers == {}
    assert all(c.origin == "model" for c in (*result.cast, *result.supporting))
    assert "gatekeeper" in result.notes


def test_a_reply_with_no_cards_is_an_error_and_one_too_long_is_cut():
    with pytest.raises(ValueError):
        parse_lore_cast('{"notes": "I don\'t know that world."}')
    many = json.dumps({"supporting": [card(f"Guard {i}") for i in range(MOST_KEPT + 3)]})
    result = parse_lore_cast(many)
    assert result.added == MOST_KEPT and len(result.warnings) == 3


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider([REPLY]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )


def test_the_session_adds_the_cards_logs_the_call_and_counts_its_cost(
    session: StorySession, tmp_path: Path
):
    result = session.fill_lore_cast(WORLD, "Maren Holt")
    assert result.added == 3
    assert [c.name for c in session.cast][-1] == "Maren Holt"
    assert [c.name for c in session.supporting] == ["Old Tobin", "Mrs. Holt"]
    assert session.unreported_usage
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert "Maren Holt" in [c.name for c in saved.cast]
    request = session.provider.requests[-1]
    assert session.story.world_bible in request.messages[-1].text
    assert "Serrik Vaun" in request.messages[-1].text


def test_a_plot_hidden_card_is_never_named_to_the_model_nor_added_again(
    session: StorySession, monkeypatch
):
    hidden = Character(id="char-maren", name="Maren Holt")
    session.cast.append(hidden)
    monkeypatch.setattr(session, "hidden_ids", lambda *a, **k: {"char-maren"})
    result = session.fill_lore_cast(WORLD)
    assert "Maren Holt" not in session.provider.requests[-1].messages[-1].text
    assert "Maren Holt" in result.skipped
    assert [c.name for c in session.cast].count("Maren Holt") == 1


def test_a_chat_has_no_lore_characters(session: StorySession):
    session.story.mode = "chat"
    with pytest.raises(ValueError, match="simple chat"):
        session.fill_lore_cast(WORLD)


def test_an_alias_that_would_fit_others_is_dropped():
    """Live, GLM 5.3 gave Claudius "the king" and the ghost "the spirit"."""
    for alias in ("the king", "the spirit", "a sexton", "sexton"):
        assert generic_alias(alias), alias
    for alias in ("the Grey Wolf", "Prince Hamlet", "First Clown", "Barnardo"):
        assert not generic_alias(alias), alias
    reply = json.dumps({"supporting": [card("Claudius", aliases=["King Claudius", "the king"])]})
    assert parse_lore_cast(reply).supporting[0].aliases == ["King Claudius"]
    # Live, Sonnet 4.6 gave Morgan le Fay the Lady of the Lake's name, with a note.
    reply = json.dumps(
        {
            "cast": [card("Morgan le Fay", aliases=["Morgana", "Lady of the Lake"])],
            "supporting": [
                card("Lady of the Lake", aliases=["the Lady of the Lake in some traditions"])
            ],
        }
    )
    result = parse_lore_cast(reply)
    assert result.cast[0].aliases == ["Morgana"] and result.supporting[0].aliases == []
