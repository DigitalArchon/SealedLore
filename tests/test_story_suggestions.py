"""What the story suggests for its lore and cards (engine/story_suggestions.py,
engine/session_story_suggestions.py): the request, the reply held to what was
shown, applying and turning down, and the reading after archival. Never the
network: the scripted mock provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.session import StorySession
from sealedlore.engine.story_suggestions import (
    MOST_OF_EACH,
    build_suggestion_messages,
    decline_key,
    loses_much,
    parse_story_suggestions,
    with_addition,
)
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story_suggestion import StorySuggestion
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, load_story_bundle
from tests.conftest import make_exchange

KEEP = LoreEntry(id="l-keep", title="Calder Keep", keywords=["keep"], content="An empty fortress.")
INN = LoreEntry(id="l-inn", title="The Grey Goose", keywords=["inn"], content="An inn by the ford.")
# Held back from every model until the author names it (no plot needed).
SECRET = LoreEntry(
    id="l-secret", title="The Hollow Crown", content="The crown is a forgery.", until_mentioned=True
)
SERRIK = Character(id="char-serrik", name="Serrik Vaun", full_description="A veteran duellist.")
MAELA = Character(id="char-maela", name="Maela Orr", summary="A locksmith.")


def reply(**kinds) -> str:
    return json.dumps({"lore_new": [], "lore_change": [], "card_change": [], **kinds})


def text_of(messages) -> str:
    return "\n".join(part.text for message in messages for part in message.parts)


# --- the request -------------------------------------------------------------------


def test_the_reader_sees_titles_named_entries_and_named_cards_only():
    prose = "Serrik rode up to Calder Keep at dusk."
    messages = build_suggestion_messages(prose, lore=[KEEP, INN], cards=[SERRIK, MAELA])
    user = text_of(messages[1:])
    assert "- Calder Keep" in user and "- The Grey Goose" in user  # every title
    assert "An empty fortress." in user  # the entry the prose names, whole
    assert "An inn by the ford." not in user  # not one it doesn't
    assert "### Serrik Vaun\nA veteran duellist." in user
    assert "Maela" not in user  # a card the prose doesn't name
    assert user.rstrip().endswith(prose)
    assert "lore_new" in text_of(messages[:1])  # the rules ask for the shape


def test_what_was_suggested_or_turned_down_is_listed_not_to_be_offered_again():
    waiting = StorySuggestion(kind="lore_new", title="The Ford")
    user = text_of(
        build_suggestion_messages(
            "x", lore=[KEEP], cards=[], pending=[waiting], declined_titles=["the old mill"]
        )[1:]
    )
    assert "ALREADY SUGGESTED" in user and "- The Ford" in user and "- the old mill" in user


# --- the reply ---------------------------------------------------------------------


def test_a_reply_is_held_to_what_was_shown():
    found = parse_story_suggestions(
        reply(
            lore_new=[
                {
                    "title": "The Ford",
                    "keywords": ["ford", "crossing"],
                    "content": "Shallow.",
                    "why": "They crossed it.",
                },
                {"title": "calder keep", "content": "A duplicate of an entry."},
                {"title": "The Hollow Crown", "content": "A new entry under a hidden title."},
                {"title": "", "content": "No title."},
            ],
            lore_change=[
                {
                    "title": "Calder Keep",
                    "content": "A fortress, burned in the siege.",
                    "why": "It burned.",
                },
                {"title": "The Grey Goose", "content": "An inn by the ford."},  # unchanged
                {"title": "Nowhere", "content": "No such entry."},
            ],
            card_change=[
                {"name": "Serrik", "add": "He lost two fingers at the keep.", "why": "The siege."},
                {"name": "Nobody", "add": "Not a card."},
                {
                    "name": "Maela Orr",
                    "add": " ".join(["word"] * 200),
                },  # far past a sentence or two
            ],
        ),
        lore=[KEEP, INN],
        cards=[SERRIK, MAELA],
        reserved_names=["The Hollow Crown"],
        node_ids=["a1"],
    )
    kinds = [(s.kind, s.title) for s in found]
    assert kinds == [
        ("lore_new", "The Ford"),
        ("lore_change", "Calder Keep"),
        ("card_change", "Serrik Vaun"),
    ]
    new, change, card = found
    assert new.keywords == ["ford", "crossing"] and new.why == "They crossed it."
    assert change.target_id == "l-keep" and card.target_id == "char-serrik"
    assert all(s.node_ids == ["a1"] for s in found)


def test_what_was_turned_down_or_is_waiting_is_never_offered_again():
    ford = StorySuggestion(kind="lore_new", title="The Ford", content="x")
    card = StorySuggestion(
        kind="card_change",
        target_id="char-serrik",
        title="Serrik Vaun",
        content="He lost two fingers.",
    )
    found = parse_story_suggestions(
        reply(
            lore_new=[{"title": "the ford", "content": "Again."}],
            card_change=[{"name": "Serrik Vaun", "add": "He lost two fingers."}],
        ),
        lore=[],
        cards=[SERRIK],
        pending=[ford],
        declined=[decline_key(card)],
    )
    assert found == []


def test_an_addition_already_on_the_card_is_not_suggested():
    found = parse_story_suggestions(
        reply(card_change=[{"name": "Serrik Vaun", "add": "A veteran duellist."}]),
        lore=[],
        cards=[SERRIK],
    )
    assert found == []


def test_at_most_a_few_of_each_and_keywords_kept_short():
    many = [
        {"title": f"Place {i}", "content": "x", "keywords": [f"k{j}" for j in range(20)]}
        for i in range(MOST_OF_EACH + 4)
    ]
    found = parse_story_suggestions(reply(lore_new=many), lore=[], cards=[])
    assert len(found) == MOST_OF_EACH and all(len(s.keywords) == 8 for s in found)


def test_a_reply_with_no_object_is_said():
    with pytest.raises(ValueError):
        parse_story_suggestions("Nothing to add.", lore=[], cards=[])


def test_a_change_that_keeps_little_of_an_entry_is_pointed_out():
    old = "The keep stands on a hill above the river, its walls grey and its gates long gone."
    assert loses_much(old, "Burned.")
    assert not loses_much(old, old + " Burned in the siege.")
    assert (
        with_addition("A duellist.", " He lost two fingers. ")
        == "A duellist.\n\nHe lost two fingers."
    )
    assert with_addition("", "He lost two fingers.") == "He lost two fingers."


# --- the session ---------------------------------------------------------------------


def session_with(story, cast, tmp_path: Path, responses: list[str], **config) -> StorySession:
    story.active_leaf_id = "a3"
    bundle = StoryBundle(
        story=story,
        cast=[c.model_copy(deep=True) for c in cast]
        + [Character(id="char-ghost", name="Ghostly Wren", plot_hidden=True)],
        nodes=make_exchange(4),
        lore=[KEEP.model_copy(), INN.model_copy(), SECRET.model_copy()],
    )
    for node in bundle.nodes:
        if node.kind == "assistant":
            node.content += " Serrik Vaun walked the walls of Calder Keep."
    session = StorySession(
        bundle,
        Config(**config),
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    # As a plot keeps someone it hasn't brought in from every model.
    session.hidden_ids = lambda path=None, *, pending=False: {"char-ghost"}
    return session


def test_suggestions_are_read_kept_applied_and_turned_down(story, cast, tmp_path):
    session = session_with(
        story,
        cast,
        tmp_path,
        [
            reply(
                lore_new=[
                    {
                        "title": "The Ford",
                        "keywords": ["ford"],
                        "content": "Shallow.",
                        "why": "They crossed it.",
                    }
                ],
                lore_change=[{"title": "Calder Keep", "content": "A burned fortress."}],
                card_change=[{"name": "Serrik Vaun", "add": "He lost two fingers."}],
            )
        ],
    )
    events = list(session.suggest_from_recent())
    assert "1 for the cards" in events[-1].text and "2 for the lore" in events[-1].text
    request = session.provider.requests[-1]
    assert request.model == session.summarization_model
    sent = text_of(request.messages)
    # Never a plot-hidden entry or card, title or text.
    assert "Hollow Crown" not in sent and "forgery" not in sent and "Ghostly" not in sent
    new, change, card = session.story_suggestions
    # Kept with the story.
    again = load_story_bundle(session.story.id, root=tmp_path)
    assert [s.id for s in again.story.story_suggestions] == [new.id, change.id, card.id]

    said = session.apply_story_suggestion(new.id, title="The Ford at Calder", keywords=["ford"])
    assert said == "Added the lore entry The Ford at Calder."
    added = session.bundle.lore[-1]
    assert (added.title, added.content, added.came_from) == (
        "The Ford at Calder",
        "Shallow.",
        "the story (a suggestion)",
    )
    session.apply_story_suggestion(change.id, content="A burned fortress, held by no one.")
    assert next(e for e in session.bundle.lore if e.id == "l-keep").content.endswith("no one.")
    session.decline_story_suggestion(card.id)
    assert session.story_suggestions == []
    assert decline_key(card) in session.story.suggestions_declined
    assert "lost two fingers" not in (session.cast[0].full_description or "")


def test_a_card_addition_goes_on_as_its_own_paragraph(story, cast, tmp_path):
    session = session_with(
        story,
        cast,
        tmp_path,
        [reply(card_change=[{"name": "Serrik Vaun", "add": "He lost two fingers."}])],
    )
    serrik = next(c for c in session.cast if c.id == "char-serrik")
    serrik.full_description = "A veteran duellist."
    list(session.suggest_from_recent())
    (card,) = session.story_suggestions
    assert session.apply_story_suggestion(card.id) == "Added to Serrik Vaun's card."
    assert serrik.full_description == "A veteran duellist.\n\nHe lost two fingers."


def test_after_archival_inline_never_costs_the_turn(story, cast, tmp_path):
    session = session_with(story, cast, tmp_path, ["not json at all"])
    events = list(session.suggest_after_archive(session.path()))
    assert "Couldn't read the archived turns for lore and card changes" in events[-1].text
    assert session.story_suggestions == []
    # Switched off, nothing is asked.
    quiet = session_with(story, cast, tmp_path / "off", [reply()], suggest_story_changes=False)
    assert list(quiet.suggest_after_archive(quiet.path())) == []
    assert quiet.provider.requests == []


def test_a_chat_is_never_read_for_suggestions(story, cast, tmp_path):
    session = session_with(story, cast, tmp_path, [reply()])
    session.story.mode = "chat"
    assert not session.suggests_changes()
    assert list(session.suggest_after_archive(session.path())) == []


def test_one_change_to_an_entry_or_card_a_reading():
    """Live, the same card was suggested twice in one stretch, a little
    differently each time."""
    waiting = StorySuggestion(
        kind="card_change",
        target_id="char-maela",
        title="Maela Orr",
        content="She keeps the keys now.",
    )
    found = parse_story_suggestions(
        reply(
            lore_change=[
                {"title": "Calder Keep", "content": "A burned fortress."},
                {"title": "Calder Keep", "content": "A fortress, burned and abandoned."},
            ],
            card_change=[
                {"name": "Serrik Vaun", "add": "He lost two fingers."},
                {"name": "Serrik Vaun", "add": "He fights left-handed now."},
                {"name": "Maela Orr", "add": "She owes the guild."},  # one already waiting
            ],
        ),
        lore=[KEEP],
        cards=[SERRIK, MAELA],
        pending=[waiting],
    )
    assert [(s.title, s.content) for s in found] == [
        ("Calder Keep", "A burned fortress."),
        ("Serrik Vaun", "He lost two fingers."),
    ]
