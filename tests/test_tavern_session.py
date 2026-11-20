"""A story whose lore fires as SillyTavern fires it (engine/session_tavern.py):
an imported lorebook's own settings kept, each turn's lore fired and
recorded, constants cached, timed effects along the path. The scripted mock
provider: no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.tavern_lore import lore_from_book
from tests.test_session import make_config

# A SillyTavern world file, as its World Info editor saves one.
WORLD = {
    "entries": {
        "0": {
            "uid": 0,
            "comment": "The Grey Wolves",
            "key": ["wolf"],
            "content": "The grey wolves hunt in threes and fear fire.",
            "order": 100,
            "sticky": 3,
        },
        "1": {
            "uid": 1,
            "comment": "The Old Road",
            "key": ["road"],
            "content": "The old road runs north to {{user}}'s farm.",
            "order": 50,
        },
        "2": {
            "uid": 2,
            "comment": "The Valley",
            "key": [],
            "constant": True,
            "content": "The valley is cut off by snow all winter.",
            "order": 10,
        },
        "3": {
            "uid": 3,
            "comment": "Hollow King",
            "key": ["fear fire"],
            "content": "Only the Hollow King commands the wolves.",
            "order": 20,
        },
        "4": {
            "uid": 4,
            "comment": "Rare Omen",
            "key": ["wolf"],
            "content": "An omen.",
            "probability": 0,
            "useProbability": True,
        },
    }
}


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    imported = lore_from_book(WORLD, fill=lambda text: text.replace("{{user}}", "Serrik"))
    story.held_character_id = "char-serrik"
    story.defaults.lore_firing = "sillytavern"
    config = make_config()
    config.scene_reads = "manual"
    return StorySession(
        StoryBundle(story=story, cast=cast, lore=imported.entries),
        config,
        MockChatProvider(["A passage."] * 8),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def sent_text(session: StorySession) -> str:
    request = session.provider.requests[-1]
    return "\n".join(part.text for message in request.messages for part in message.parts)


def test_an_imported_entry_keeps_what_sillytavern_reads(session: StorySession):
    wolves = next(e for e in session.bundle.lore if e.title == "The Grey Wolves")
    assert wolves.tavern is not None and wolves.tavern.numbered
    assert wolves.tavern.fields["sticky"] == 3
    road = next(e for e in session.bundle.lore if e.title == "The Old Road")
    assert road.tavern.fields["content"] == "The old road runs north to Serrik's farm."


def test_a_turn_fires_its_lore_and_records_it(session: StorySession):
    list(session.send(turn("A wolf watches from the trees.")))
    report = session.last_retrieval
    assert report.selector == "sillytavern"
    fired = [i.entry.title for i in report.injected]
    # The wolves by their key, and the Hollow King by the wolves' own text
    # (recursion); the omen's 0% chance never comes up; the road isn't named.
    assert fired == ["Hollow King", "The Grey Wolves"]  # SillyTavern's order: lowest first
    assert report.injected[0].describe().startswith("fired as SillyTavern would: the text of")
    not_fired = {entry.title: why for entry, why in report.not_fired}
    assert not_fired["Rare Omen"] == "its 0% chance didn't come up"
    # The constant stands with the world, cached; the rest go with the turn.
    assert [e.title for e in report.standing] == ["The Valley"]
    text = sent_text(session)
    assert "cut off by snow" in text and "hunt in threes" in text and "old road" not in text
    user = session.path()[-2]
    assert user.meta.tavern_length == 1
    assert sorted(user.meta.tavern_fired) == sorted(
        e.id for e in session.bundle.lore if e.title in ("Hollow King", "The Grey Wolves")
    )


def test_a_sticky_entry_keeps_firing_on_the_next_turns(session: StorySession):
    """Sticky counts messages, the storyteller's included, as SillyTavern's
    does: fired at a chat of 1, sticky 3 holds at 3 (the next turn) and is
    over at 5."""
    list(session.send(turn("A wolf watches from the trees.")))
    list(session.send(turn("I walk on.")))
    report = session.last_retrieval
    wolves = next(i for i in report.injected if i.entry.title == "The Grey Wolves")
    assert wolves.why == "sticky (1 more message)"
    list(session.send(turn("I keep walking.")))
    assert "The Grey Wolves" not in [i.entry.title for i in session.last_retrieval.injected]


def test_a_take_fires_afresh_on_its_own_turn(session: StorySession):
    list(session.send(turn("A wolf watches from the trees.")))
    first = list(session.path()[-2].meta.tavern_fired)
    list(session.regenerate())
    assert session.path()[-2].meta.tavern_fired == first  # scanned again, the same here


def test_held_back_lore_never_fires(session: StorySession):
    road = next(e for e in session.bundle.lore if e.title == "The Old Road")
    road.until_mentioned = True
    road.keywords = ["farm"]
    list(session.send(turn("Down the road.")))
    assert "The Old Road" not in [i.entry.title for i in session.last_retrieval.injected]


def test_no_lore_pick_is_asked_for_after_a_passage(session: StorySession):
    session.config.lore_selector = "picker"
    list(session.send(turn("A wolf watches from the trees.")))
    assert len(session.provider.requests) == 1  # the passage alone


def test_sealedlores_own_way_is_untouched(tmp_path: Path, story, cast):
    story.defaults.lore_firing = "sealedlore"
    entry = LoreEntry(title="Wolves", content="They hunt in threes.", keywords=["wolf"])
    session = StorySession(
        StoryBundle(story=story, cast=cast, lore=[entry]),
        make_config(),
        MockChatProvider(["A passage."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.send(turn("A wolf.")))
    assert session.last_retrieval.selector != "sillytavern"
    assert session.path()[-2].meta.tavern_length is None


def test_the_import_says_what_is_kept_for_firing_as_sillytavern_does():
    from sealedlore.storage.tavern_lore import KEPT_NOTE

    notes = lore_from_book(WORLD).notes()
    assert any(note.startswith("sticky, cooldown and delay") for note in notes)
    assert notes[-1] == KEPT_NOTE
    plain = {"entries": {"0": {"uid": 0, "key": ["x"], "content": "y"}}}
    assert KEPT_NOTE not in lore_from_book(plain).notes()
