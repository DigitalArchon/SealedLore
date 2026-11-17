"""The reaction round: main characters played by a model of their own.

The author's trial (Nov 2026): after the storyteller's passage, every
character whose card names a model answers it at once, blind to the others;
the storyteller gets the answers with the author's next turn and works them
in as intentions, never facts. Each character sees only what they would
know: the scenes they were in, the storyteller's passages and the author's
turns without OOC notes, their own earlier answers, never a Direction and
never another character's answer. Driven end to end against the mock.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from sealedlore.engine.archival import render_chunk
from sealedlore.engine.prompt import (
    SECTION_AUTHOR_TURN,
    SECTION_REMINDER,
    SECTION_ROUND,
    TurnRequest,
)
from sealedlore.engine.reactions import character_history, reaction_messages
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    StreamCancelled,
    StreamEvent,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)

MAELA_MODEL = "Local::maela-lora"


def a_story() -> Story:
    story = Story(
        id="story-1",
        title="The Sundering",
        world_bible="Calder Keep has stood empty for a hundred years.",
        scene=SceneState(
            location="The undercroft beneath Calder Keep",
            present_character_ids=["char-serrik", "char-maela"],
        ),
    )
    story.defaults.main_model = "story-model"
    story.held_character_id = "char-serrik"
    return story


def a_cast(*, idris_model: str | None = None) -> list[Character]:
    return [
        Character(id="char-serrik", name="Serrik Vaun", summary="A duellist in debt."),
        Character(
            id="char-maela",
            name="Maela Orr",
            summary="A locksmith who talks too much.",
            voice_notes="Quick, and full of questions.",
            own_model=MAELA_MODEL,
        ),
        Character(
            id="char-idris",
            name="Captain Idris",
            summary="Three days' ride north.",
            own_model=idris_model,
        ),
    ]


def make_session(
    tmp_path: Path,
    responses: list[str],
    *,
    story: Story | None = None,
    cast: list[Character] | None = None,
    provider: MockChatProvider | None = None,
    reads: bool = False,
) -> StorySession:
    story = story or a_story()
    bundle = StoryBundle(story=story, cast=cast if cast is not None else a_cast())
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
    )
    # Off unless a test is about the reads: then each turn's calls are the
    # passage, the scene read's JSON, then the round.
    config.scene_reads = "every_turn" if reads else "manual"
    if provider is not None:
        provider.responses = list(responses)
    return StorySession(
        bundle,
        config,
        provider or MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str = "I put my shoulder to the door.", **fields) -> TurnRequest:
    return TurnRequest(
        speaker_id=fields.pop("speaker_id", "char-serrik"),
        user_text=text,
        controlled_character_id="char-serrik",
        **fields,
    )


def play(session: StorySession, request: TurnRequest) -> list:
    return list(session.send(request))


def sent(request: ChatRequest) -> str:
    return "\n".join(part.text for message in request.messages for part in message.parts)


def round_calls(session: StorySession) -> list[ChatRequest]:
    return [r for r in session.provider.requests if r.model != "story-model"]


# --- who is asked, and on what ---------------------------------------------


def test_no_own_model_means_no_round_and_no_new_section(tmp_path: Path) -> None:
    cast = a_cast()
    cast[1].own_model = None
    session = make_session(tmp_path, ["The door gives.", "It swings wide."], cast=cast)
    play(session, turn())
    play(session, turn("I go in."))
    assert [r.model for r in session.provider.requests] == ["story-model", "story-model"]
    assert session.last_prompt.section(SECTION_ROUND) is None


def test_the_card_s_model_is_sent_whole_with_its_endpoint(tmp_path: Path) -> None:
    """ "Local::maela-lora" names the endpoint it is reached on; stripped to
    "maela-lora" it went to the default endpoint instead."""
    session = make_session(tmp_path, ["The door gives.", "I reach for my picks."])
    play(session, turn())
    assert [r.model for r in round_calls(session)] == [MAELA_MODEL]
    answer = session.path()[-1].meta.reactions[0]
    assert (answer.character_id, answer.text, answer.model) == (
        "char-maela",
        "I reach for my picks.",
        MAELA_MODEL,
    )


def test_only_those_in_the_scene_are_asked(tmp_path: Path) -> None:
    session = make_session(
        tmp_path, ["The door gives.", "I reach for my picks."], cast=a_cast(idris_model="x")
    )
    play(session, turn())
    assert [r.model for r in round_calls(session)] == [MAELA_MODEL]  # Idris is away


def test_the_character_the_author_holds_is_not_asked(tmp_path: Path) -> None:
    story = a_story()
    story.held_character_id = "char-maela"
    session = make_session(tmp_path, ["The door gives."], story=story)
    play(
        session,
        TurnRequest(speaker_id="char-maela", user_text="Hm.", controlled_character_id="char-maela"),
    )
    assert round_calls(session) == []


def test_the_round_follows_the_scene_the_read_left(tmp_path: Path) -> None:
    """After the reads: Idris arrives in this passage, so he answers it."""
    session = make_session(
        tmp_path,
        [
            "Boots on the stair: Captain Idris comes down into the undercroft.",
            json.dumps({"arrived": [{"name": "Captain Idris", "how": "came down the stair"}]}),
            "Idris: I set the lantern down.",
            "Maela: I reach for my picks.",
        ],
        cast=a_cast(idris_model="Local::idris"),
        reads=True,
    )
    play(session, turn())
    assert sorted(r.model for r in round_calls(session)) == ["Local::idris", MAELA_MODEL]


def test_a_plot_hidden_character_is_never_asked(tmp_path: Path) -> None:
    from sealedlore.models.plot import Plot

    story = a_story()
    story.plot = Plot()
    cast = a_cast()
    cast[1].plot_hidden = True
    session = make_session(tmp_path, ["The door gives."], story=story, cast=cast)
    play(session, turn())
    assert round_calls(session) == []


def test_a_passage_after_direction_or_narration_is_answered(tmp_path: Path) -> None:
    """Direction is to the storyteller; the passage it gets is fact, and
    whoever is present answers it."""
    for speaker in (DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID):
        session = make_session(tmp_path / speaker, ["Weeks pass.", "I count my coin."])
        play(session, turn("Five weeks pass.", speaker_id=speaker))
        assert [r.model for r in round_calls(session)] == [MAELA_MODEL], speaker


@pytest.mark.parametrize("mode", ["as_written", "generate"])
def test_the_opening_is_answered_however_it_is_made(tmp_path: Path, mode: str) -> None:
    """The opening is the first passage they answer. One used as written
    makes no call and went past the round."""
    story = a_story()
    story.held_character_id = None
    story.setup.opening_text = "The undercroft door stands shut."
    story.setup.opening_mode = mode
    story.setup.starting_scene = story.scene.model_copy(deep=True)
    replies = ["I try the lock."] if mode == "as_written" else ["The door is shut.", "I try it."]
    session = make_session(tmp_path, replies, story=story)
    list(session.begin("char-serrik"))
    assert [r.model for r in round_calls(session)] == [MAELA_MODEL]
    assert [a.character_id for a in session.path()[-1].meta.reactions] == ["char-maela"]


def test_no_round_in_a_private_scene(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "I reach for my picks."])
    play(session, turn())
    private = MockChatProvider(["A quiet reply."])
    session.enter_private(keep="memory", provider=private, model="local/model")
    play(session, turn("I lower my voice."))
    assert [r.model for r in private.requests] == ["local/model"]
    assert len(round_calls(session)) == 1  # only the round before the scene


# --- what each character sees ----------------------------------------------


def test_answers_are_blind_to_each_other_in_a_round_and_after(tmp_path: Path) -> None:
    story = a_story()
    story.scene.present_character_ids.append("char-idris")
    session = make_session(
        tmp_path,
        ["The door gives.", "IDRIS-ONE", "MAELA-ONE", "It swings wide.", "IDRIS-TWO", "MAELA-TWO"],
        story=story,
        cast=a_cast(idris_model="Local::idris"),
    )
    play(session, turn())
    play(session, turn("I go in."))
    idris_two, maela_two = round_calls(session)[2:]
    assert "MAELA-ONE" not in sent(idris_two)  # another's intention is theirs alone
    assert "IDRIS-ONE" in sent(idris_two)  # their own earlier answer is theirs
    assert "IDRIS-ONE" not in sent(maela_two)


def test_a_character_sees_the_authors_turn_without_its_ooc_note(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "Hm."])
    play(session, turn('"Keep watch," I say.', ooc="make Maela betray him later"))
    text = sent(round_calls(session)[0])
    assert '"Keep watch," I say.' in text
    assert "betray" not in text


def test_a_character_never_sees_a_direction_but_sees_narration(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The lamp gutters.", "Hm.", "Rain on the grate.", "Hm."])
    play(session, turn("SECRET-DIRECTION: the lamp fails.", speaker_id=DIRECTOR_SPEAKER_ID))
    play(session, turn("NARRATED-RAIN begins to fall.", speaker_id=NARRATOR_SPEAKER_ID))
    text = sent(round_calls(session)[-1])
    assert "SECRET-DIRECTION" not in text
    assert "The lamp gutters." in text  # the storyteller's passage is fact
    assert "NARRATED-RAIN" in text


def test_a_character_sees_only_scenes_they_were_in(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "Hm."])
    play(session, turn())
    history = session.path()
    away = SceneState(location="The road north", present_character_ids=["char-serrik"])
    history[-1].meta.scene = away  # the passage that took Serrik away from her
    history[-1].content = "Serrik leaves Maela and walks the north road alone."
    later = history[-1].model_copy(deep=True, update={"id": "far", "content": "ALONE-ON-ROAD"})
    later.meta.scene = away
    maela = session.cast[1]
    seen, _ = character_history(
        [*history, later], maela, session.cast, start=session.story.setup.starting_scene
    )
    joined = "\n".join(seen)
    assert "Serrik leaves Maela" in joined  # she was there when he left
    assert "ALONE-ON-ROAD" not in joined


def test_older_scenes_give_way_to_the_budget(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["OLDEST passage.", "Hm.", "NEWEST passage.", "Hm."])
    play(session, turn())
    play(session, turn("Again."))
    maela = session.cast[1]
    messages = reaction_messages(
        story=session.story,
        cast=session.cast,
        character=maela,
        history=session.path(),
        budget=20,
    )
    text = "\n".join(p.text for m in messages for p in m.parts)
    assert "NEWEST passage." in text  # the passage they answer always stays
    assert "OLDEST passage." not in text
    assert "not shown here" in text


def test_a_character_is_given_their_card_skills_and_the_setting(tmp_path: Path) -> None:
    cast = a_cast()
    cast[1].competence.tiers = {"locks": "expert"}
    session = make_session(tmp_path, ["The door gives.", "Hm."], cast=cast)
    session.story.storyteller_notes = "STORYTELLER-SECRET"
    play(session, turn())
    request = round_calls(session)[0]
    system = request.messages[0].text
    assert "Maela Orr" in system and "talks too much" in system
    assert "full of questions" in system and "locks expert" in system
    assert "Calder Keep" in system
    assert "STORYTELLER-SECRET" not in sent(request)
    assert request.messages[0].parts[-1].cache_breakpoint


# --- what the storyteller does with them -------------------------------------


def test_the_storyteller_gets_them_after_the_turn_and_nowhere_else(tmp_path: Path) -> None:
    session = make_session(
        tmp_path, ["The door gives.", "MAELA-MEANS-TO-PICK-THE-LOCK", "The lock turns."]
    )
    play(session, turn())
    play(session, turn("I hold the lamp for her."))
    prompt = session.last_prompt
    names = [section.name for section in prompt.sections]
    assert names.index(SECTION_AUTHOR_TURN) < names.index(SECTION_ROUND)
    assert names.index(SECTION_ROUND) < names.index(SECTION_REMINDER)
    block = prompt.section(SECTION_ROUND).text
    assert "[Maela Orr] MAELA-MEANS-TO-PICK-THE-LOCK" in block
    assert "means to do, not something that has happened" in block
    storyteller = session.provider.requests[2]
    assert not any("MAELA-MEANS" in m.text for m in storyteller.messages if m.role != "user")
    # No read, summary or scan sees them: they are spent once answered.
    assert "MAELA-MEANS" not in render_chunk(session.path(), session.cast)


def test_regenerate_sends_an_edited_answer(tmp_path: Path) -> None:
    session = make_session(
        tmp_path, ["The door gives.", "I pick the lock.", "The lock turns.", "Again, it turns."]
    )
    play(session, turn())
    answered = session.path()[-1]
    play(session, turn("I hold the lamp."))
    session.edit_reaction(answered.id, "char-maela", "I step back, hands up.")
    list(session.regenerate())
    assert "[Maela Orr] I step back, hands up." in session.last_prompt.section(SECTION_ROUND).text
    reloaded = load_story_bundle("story-1", root=tmp_path)
    kept = next(n for n in reloaded.nodes if n.id == answered.id).meta.reactions[0]
    assert kept.text == "I step back, hands up." and kept.edited_at


def test_a_new_take_gets_a_new_round(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "FIRST", "It holds.", "SECOND"])
    play(session, turn())
    list(session.regenerate())
    assert [r.text for r in session.path()[-1].meta.reactions] == ["SECOND"]


# --- failures, Stop and cost --------------------------------------------------


class FailsAfterPassage(MockChatProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__(["The door gives."])
        self.raise_on_round = error

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        if request.model != "story-model":
            self.requests.append(request)
            raise self.raise_on_round
        return super().stream(request)


def test_a_failed_answer_is_a_notice_and_the_turn_stands(tmp_path: Path) -> None:
    provider = FailsAfterPassage(ProviderError("the endpoint is down"))
    session = make_session(tmp_path, ["The door gives."], provider=provider)
    events = play(session, turn())
    assert session.path()[-1].content == "The door gives."
    assert session.path()[-1].meta.reactions == []
    assert any(isinstance(e, SessionNotice) and "couldn't answer" in e.text for e in events)


def test_stop_during_the_round_stops_the_turn(tmp_path: Path) -> None:
    provider = FailsAfterPassage(StreamCancelled())
    session = make_session(tmp_path, ["The door gives."], provider=provider)
    with pytest.raises(StreamCancelled):
        play(session, turn())
    assert session.path()[-1].meta.reactions == []


class DeclinesTheRound(MockChatProvider):
    """The storyteller writes; every character's model is filtered."""

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        self.finish_reason = "stop" if request.model == "story-model" else "content_filter"
        return super().stream(request)


def test_every_answer_is_logged_and_counted_even_a_declined_one(tmp_path: Path) -> None:
    provider = DeclinesTheRound()
    session = make_session(tmp_path, ["The door gives.", "I won't."], provider=provider)
    events = play(session, turn())
    assert session.path()[-1].meta.reactions == []
    assert any(isinstance(e, SessionNotice) and "content filter" in e.text for e in events)
    log = read_api_log("story-1", root=tmp_path)
    assert [e["kind"] for e in log if e["kind"].startswith("own_")] == [
        "own_request",
        "own_response",
    ]
    lines = {line.label: line for line in usage_report(log).lines}
    assert lines["Own-model characters"].calls == 1


# --- their words, as dialogue (measured: design/own-models.md) -----------------

CLAUSE = (
    "Put the words from the answer of Maela Orr into the passage as spoken dialogue, in "
    "quotation marks, never as reported speech, leaving out whatever the author's turn has "
    "already done or answered; what they only thought stays theirs."
)


def test_their_words_go_in_as_dialogue_on_the_directives_and_remember(tmp_path: Path) -> None:
    """The clause measured on 54 real turns, in both places it was measured: after
    "write everyone and everything else" in the directives and on REMEMBER."""
    session = make_session(tmp_path, ["The door gives.", "I pick the lock.", "It turns."])
    play(session, turn())
    play(session, turn("I hold the lamp."))
    prompt = session.last_prompt
    directives = prompt.section("tail.directives").text
    reminder = prompt.section(SECTION_REMINDER).text
    assert f"Write everyone and everything else. {CLAUSE}" in directives
    assert f"Write everyone and everything else. {CLAUSE}" in reminder


def test_no_answers_no_clause(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "I pick the lock.", "It turns."])
    play(session, turn())  # the first turn: the passage before it had no round
    text = "\n".join(section.text for section in session.last_prompt.sections)
    assert "into the passage as spoken dialogue" not in text


def test_two_who_answered_are_named_together() -> None:
    from sealedlore.engine.rules import round_clause

    assert round_clause(["Maela Orr", "Captain Idris"]).startswith(
        "Put the words from the answers of Maela Orr and Captain Idris into the passage"
    )
    assert round_clause([]) == ""


# --- what a character remembers (step 2c) --------------------------------------

from sealedlore.engine import reactions as reactions_module  # noqa: E402
from sealedlore.engine.reactions import (  # noqa: E402
    due_to_recollect,
    fingerprint,
    memory_chain,
    seen_entries,
)
from sealedlore.models.memory import Recollection  # noqa: E402
from sealedlore.storage.archive import archive_data, read_archive  # noqa: E402


def entries(n: int) -> list[tuple[str, str]]:
    return [(f"n{i}", f"Message {i}.") for i in range(n)]


def test_a_memory_matches_only_what_it_was_written_from() -> None:
    seen = entries(6)
    first = Recollection(
        character_id="m",
        covered_node_ids=["n0", "n1"],
        fingerprint=fingerprint(seen[:2]),
        content="A",
    )
    second = Recollection(
        character_id="m",
        covered_node_ids=["n2", "n3"],
        fingerprint=fingerprint(seen[2:4]),
        content="B",
    )
    other = Recollection(
        character_id="x",
        covered_node_ids=["n0", "n1"],
        fingerprint=fingerprint(seen[:2]),
        content="X",
    )
    chain, covered = memory_chain([second, other, first], "m", seen)
    assert [m.content for m in chain] == ["A", "B"] and covered == 4
    # An edit to a message it covers: it, and the chain after it, stop matching.
    edited = [*seen[:1], ("n1", "Message 1, changed."), *seen[2:]]
    assert memory_chain([first, second], "m", edited) == ([], 0)
    # A branch that differs at n2: the first still holds.
    branch = [*seen[:2], ("b2", "Elsewhere."), ("b3", "Elsewhere still.")]
    assert memory_chain([first, second], "m", branch)[1] == 2


def test_only_whole_stretches_are_remembered_until_the_rest_fits() -> None:
    seen = entries(50)  # each "Message N." is about 3 rough tokens
    due = due_to_recollect(seen, 0, budget=40, size=20)
    assert [len(chunk) for chunk in due] == [20, 20]
    assert due[0][0][0] == "n0" and due[1][0][0] == "n20"
    assert due_to_recollect(seen, 40, budget=40, size=20) == []  # 10 left: fits
    assert due_to_recollect(seen, 0, budget=10_000, size=20) == []  # everything fits


def test_the_round_remembers_older_scenes_and_sends_the_memory(tmp_path: Path, monkeypatch) -> None:
    """Past the recent window, the round writes what the character remembers
    first, once, and then sends it before the recent scenes."""
    monkeypatch.setattr(reactions_module, "HISTORY_TOKENS", 30)
    monkeypatch.setattr(reactions_module, "RECOLLECT_MESSAGES", 4)
    responses = []
    for i in range(4):
        responses += [f"PASSAGE-{i} " + "word " * 20, f"ANSWER-{i}"]
    responses += ["I REMEMBER THE DOOR AND THE STAIRS.", "ANSWER-4"]
    session = make_session(tmp_path, ["The door gives."] + responses)
    session.provider.responses = responses
    for i in range(4):
        play(session, turn(f"Turn {i}."))
    # Memories are written on the summarisation model (here the story's own).
    memory_calls = [r for r in session.provider.requests if "keep the memory" in r.messages[0].text]
    assert memory_calls
    calls = round_calls(session)
    written = [m for m in session.bundle.memories if m.character_id == "char-maela"]
    assert written and written[0].covered_node_ids
    last_answer = calls[-1]
    text = "\n".join(p.text for m in last_answer.messages for p in m.parts)
    assert "WHAT YOU REMEMBER" in text
    assert written[0].content in text
    # Kept with the story, and in a backup.
    reloaded = load_story_bundle("story-1", root=tmp_path)
    assert [m.id for m in reloaded.memories] == [m.id for m in session.bundle.memories]
    data = archive_data(session.bundle, [])
    assert [m["id"] for m in data["memories"]] == [m.id for m in session.bundle.memories]


def test_an_older_backup_has_no_memories(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives."])
    data = archive_data(session.bundle, [])
    del data["memories"]
    archive = tmp_path / "old.sealedlore-archive.json"
    archive.write_text(json.dumps(data))
    bundle, _log = read_archive(archive)
    assert bundle.memories == []


def test_seen_entries_name_their_messages(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "Hm."])
    play(session, turn())
    maela = session.cast[1]
    ids = [
        node_id
        for node_id, _text in seen_entries(
            session.path(), maela, session.cast, start=session.story.setup.starting_scene
        )
    ]
    assert ids == [node.id for node in session.path()]


def test_she_is_told_to_say_when_she_doesnt_remember(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "Hm."])
    play(session, turn())
    system = round_calls(session)[0].messages[0].text
    assert "you don't remember it: say so" in system
