"""The reaction round: main characters played by their own model.

The author's trial (Nov 2026): after the storyteller's passage lands, the
player and every character whose card names a model answer it at once, none
seeing the others' answers; the storyteller gets all of it with the author's
next turn and works out how the intentions fit together, treating each
character's answer as what they intend to do, never as fact of what they did
(the author's explicit ruling: nothing a character says can move another
out of order — the storyteller resolves it as intention).

These tests drive the round end to end against the mock provider.
"""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle


def make_story() -> Story:
    """The conftest story fixture's value, built directly (a fixture cannot be
    called as a function)."""
    return Story(
        id="story-1",
        title="The Sundering",
        world_bible="Calder Keep has stood empty for a hundred years.",
        scene=SceneState(
            location="The undercroft beneath Calder Keep",
            time_of_day="past midnight",
            situation="Torchlight, and something moving in the dark.",
            present_character_ids=["char-serrik", "char-maela"],
        ),
    )


def make_cast() -> list[Character]:
    return [
        Character(
            id="char-serrik",
            name="Serrik Vaun",
            aliases=["the Grey Wolf"],
            summary="A veteran duellist with a debt he cannot repay.",
        ),
        Character(id="char-maela", name="Maela Orr", summary="A locksmith who talks too much."),
        Character(id="char-idris", name="Captain Idris", summary="Three days' ride north."),
    ]


def make_session(
    tmp_path: Path,
    story: Story,
    cast: list[Character],
    responses: list[str],
) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
    )
    # Every call is the mock's, in order: passage, scene read (its JSON),
    # then the own-model round. No scene read model is set, so the read is
    # a second passage-shaped reply the tests count past.
    config.scene_reads = "manual"
    return StorySession(
        bundle,
        config,
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def own_story() -> Story:
    return make_story()


def own_cast() -> list[Character]:
    """The conftest cast, with Maela played by her own model and Idris not."""
    cast = [c.model_copy(deep=True) for c in make_cast()]
    cast[1].own_model = "nano/maela-tuned"
    return cast


def drain(turns) -> list:
    return list(turns)


def test_no_round_without_an_own_model(tmp_path: Path) -> None:
    """A story that names no character's own model behaves exactly as before:
    the passage lands and no extra call goes out."""
    story = own_story()
    cast = [c.model_copy(deep=True) for c in make_cast()]
    session = make_session(tmp_path, story, cast, ["The world answers."])
    list(session.send(_turn()))
    provider = session.provider
    assert isinstance(provider, MockChatProvider)
    assert len(provider.requests) == 1  # the passage alone


def test_the_round_answers_each_present_character(tmp_path: Path) -> None:
    """After a passage, each character with an own model who is in the scene
    answers it on their own model: two calls after the passage, each blind to
    the other's answer, kept on the passage as their intention."""
    story = own_story()
    cast = own_cast()
    session = make_session(
        tmp_path,
        story,
        cast,
        [
            "The door gives.",  # the passage
            "Maela reaches for her picks.",  # her own model's answer
        ],
    )
    events = drain(session.send(_turn()))
    provider = session.provider
    assert isinstance(provider, MockChatProvider)
    assert len(provider.requests) == 2
    passage = session.path()[-1]
    assert passage.kind == "assistant"
    assert [r.character_id for r in passage.meta.reactions] == ["char-maela"]
    reaction = passage.meta.reactions[0]
    assert reaction.text == "Maela reaches for her picks."
    assert reaction.model == "nano/maela-tuned"
    # The round's notice says the answers ride with the next turn.
    assert any(isinstance(e, SessionNotice) and "answered at once" in e.text for e in events)
    # The history renders the answer as Maela's intention, for every reader.
    from sealedlore.engine.rules import render_history_node

    cast_by_id = {c.id: c for c in cast}
    rendered = render_history_node(passage, cast_by_id)
    assert "Maela reaches for her picks." in rendered
    assert "intend to do" in rendered


def test_each_answer_is_blind_to_the_others(tmp_path: Path) -> None:
    """Each character's prompt holds only the story as they lived it and the
    passage: no other character's answer, and nothing of what the author will
    write. The blindness is in what each is given, built before any call."""
    story = own_story()
    cast = own_cast()
    cast[2].own_model = "nano/idris-tuned"
    # Both answering characters are in the scene.
    story.scene.present_character_ids = ["char-serrik", "char-maela", "char-idris"]
    session = make_session(
        tmp_path,
        story,
        cast,
        [
            "The door gives.",
            "Idris sets his lantern down.",
            "Maela reaches for her picks.",
        ],
    )
    list(session.send(_turn()))
    provider = session.provider
    assert isinstance(provider, MockChatProvider)
    assert len(provider.requests) == 3  # the passage, then two round calls
    idris_call = provider.requests[1]
    maela_call = provider.requests[2]
    idris_text = "\n".join(part.text for m in idris_call.messages for part in m.parts)
    maela_text = "\n".join(part.text for m in maela_call.messages for part in m.parts)
    # Neither holds the other's answer: both prompts were built before either
    # call went out, and neither answer is in the history they read.
    assert "Maela reaches for her picks." not in idris_text
    assert "Idris sets his lantern down." not in maela_text
    # Each holds the passage they answer, and their own card.
    assert "The door gives." in idris_text
    assert "The door gives." in maela_text
    # And neither holds what the author will write next (nothing has been).
    assert "shoulder it again" not in maela_text


def test_a_character_not_in_the_scene_is_not_asked(tmp_path: Path) -> None:
    """Presence decides who answers, by the scene snapshots: a character whose
    card names a model but whom the scene puts elsewhere gets no call."""
    story = own_story()
    cast = own_cast()
    cast[2].own_model = "nano/idris-tuned"
    session = make_session(tmp_path, story, cast, ["The door gives.", "unused"])
    list(session.send(_turn()))
    provider = session.provider
    assert isinstance(provider, MockChatProvider)
    models = [r.model for r in provider.requests]
    assert models == ["anthropic/claude-sonnet-4.5", "nano/maela-tuned"]


def test_direction_turns_get_no_round(tmp_path: Path) -> None:
    """A Director turn is to the storyteller alone (the author's answer 3):
    the passage it lands gets no own-model round."""
    story = own_story()
    cast = own_cast()
    session = make_session(tmp_path, story, cast, ["The world answers."])
    from sealedlore.engine.prompt import TurnRequest

    turn = TurnRequest(
        speaker_id="__director__",
        user_text="The door holds.",
        controlled_character_id=None,
    )
    list(session.send(turn))
    provider = session.provider
    assert isinstance(provider, MockChatProvider)
    assert len(provider.requests) == 1


def test_the_storyteller_gets_the_answers_as_intention(tmp_path: Path) -> None:
    """The answers a passage carries ride into the next turn's history, as that
    character's intention — so the storyteller works them in with the author's
    turn, and a later take or branch renders them exactly as they were."""
    story = own_story()
    cast = own_cast()
    session = make_session(
        tmp_path,
        story,
        cast,
        [
            "The door gives.",
            "Maela reaches for her picks.",
            "The lock turns over.",
        ],
    )
    list(session.send(_turn()))
    session.save()
    # The next turn: its prompt carries the passage, and Maela's answer with it.
    list(session.send(_turn(text="I shoulder it again.")))
    provider = session.provider
    assert isinstance(provider, MockChatProvider)
    last = provider.requests[-1]
    joined = "\n".join(m.text for m in last.messages)
    assert "Maela reaches for her picks." in joined
    assert "intend to do" in joined


def test_a_failed_answer_never_costs_the_turn(tmp_path: Path) -> None:
    """A character's model failing is a notice, never a failed passage: the
    round is a passenger, not a pilot. Nothing is kept for a failed answer."""
    from collections.abc import Iterator

    from sealedlore.providers.base import ChatRequest, ProviderError, StreamEvent

    class SecondCallFails(MockChatProvider):
        def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
            if len(self.requests) >= 1:
                raise ProviderError("the endpoint is down")
            return super().stream(request)

    story = own_story()
    cast = own_cast()
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
    )
    session = StorySession(
        bundle,
        config,
        SecondCallFails(["The door gives."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    events = drain(session.send(_turn()))
    passage = session.path()[-1]
    assert passage.kind == "assistant"
    assert passage.meta.reactions == []
    assert any(isinstance(e, SessionNotice) and "couldn't answer" in e.text for e in events)


def _turn(text: str = "I put my shoulder to the door.") -> object:
    from sealedlore.engine.prompt import TurnRequest

    return TurnRequest(
        speaker_id="char-serrik",
        user_text=text,
        controlled_character_id="char-serrik",
    )


def test_editing_an_answer(tmp_path: Path) -> None:
    """The author can edit a character's answer (answer 4): the edited text is
    what the storyteller and every reader gets, and the passage keeps whose
    model wrote the original."""
    story = own_story()
    cast = own_cast()
    session = make_session(
        tmp_path, story, cast, ["The door gives.", "Maela reaches for her picks."]
    )
    list(session.send(_turn()))
    passage = session.path()[-1]
    session.edit_reaction(passage.id, "char-maela", "Maela steps back, hands up.")
    reaction = passage.meta.reactions[0]
    assert reaction.text == "Maela steps back, hands up."
    assert reaction.edited_at is not None
    session.save()
    # Reloaded, the edit stands.
    from sealedlore.storage.repository import load_story_bundle

    bundle = load_story_bundle(story.id, root=tmp_path)
    node = next(n for n in bundle.nodes if n.id == passage.id)
    assert node.meta.reactions[0].text == "Maela steps back, hands up."
