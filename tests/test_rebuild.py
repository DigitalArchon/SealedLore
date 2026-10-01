"""Rebuilding chapters: on the author's word only, in the background, on the right branch."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sealedlore.engine.archival import characters_in
from sealedlore.engine.prompt import SECTION_SUMMARIES, TurnRequest
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatRequest, StreamCompleted
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.tree import link_child
from tests.conftest import make_exchange
from tests.test_session import make_config


class Background:
    """Answers rebuild calls in order, numbered; optionally held at a gate."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.gate.set()
        self.lock = threading.Lock()
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> tuple[str, StreamCompleted]:
        self.gate.wait(10)
        with self.lock:
            self.requests.append(request)
            count = len(self.requests)
        return f"Rebuilt in the background {count}.", StreamCompleted(finish_reason="stop")

    def close(self) -> None:
        pass


class Storyteller(MockChatProvider):
    def __init__(self, responses, background: Background) -> None:
        super().__init__(responses)
        self.background = background

    def detached(self) -> Background:
        return self.background


def branched_nodes() -> list[Node]:
    """Six exchanges on the main line, and a branch rewritten from turn 2."""
    nodes = make_exchange(6)
    a1 = next(n for n in nodes if n.id == "a1")
    previous = a1
    for index in (2, 3):
        user = Node(
            id=f"bu{index}",
            kind="user",
            speaker_id="char-serrik",
            content=f"Branch turn {index}.",
            meta=NodeMeta(controlled_character_id="char-serrik"),
        )
        reply = Node(
            id=f"ba{index}",
            kind="assistant",
            speaker_id="__narrator__",
            content=f"The branch answers, turn {index}.",
        )
        link_child(previous, user)
        link_child(user, reply)
        nodes.extend([user, reply])
        previous = reply
    return nodes


def chapter(ids: list[str], content: str) -> Summary:
    return Summary(covered_node_ids=ids, content=content)


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    nodes = branched_nodes()
    # Made in the order they were archived: the branch's chapter 2 between
    # the main line's chapter 2 and chapter 3.
    summaries = [
        chapter(["u0", "a0", "u1", "a1"], "Main chapter one."),
        chapter(["u2", "a2", "u3", "a3"], "Main chapter two."),
        chapter(["bu2", "ba2", "bu3", "ba3"], "Branch chapter two."),
        chapter(["u4", "a4", "u5", "a5"], "Main chapter three."),
    ]
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes, summaries=summaries)
    story.active_leaf_id = "a5"
    story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        make_config(),
        Storyteller(["A passage."], Background()),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="I wait.", controlled_character_id="char-serrik"
    )


def by_content(session: StorySession, content: str) -> Summary:
    return next(s for s in session.summaries if s.content == content)


def test_a_rebuild_continues_from_its_own_branch(session: StorySession):
    """Before, it took its neighbour in summaries.json: another branch's chapter."""
    third = by_content(session, "Main chapter three.")
    assert session.previous_on_branch(third).content == "Main chapter two."
    branch = by_content(session, "Branch chapter two.")
    assert session.previous_on_branch(branch).content == "Main chapter one."
    assert session.previous_on_branch(by_content(session, "Main chapter one.")) is None

    session.provider = MockChatProvider(["Rebuilt inline."])
    session.rebuild_summary(third.id)
    sent = session.provider.last_request.messages[1].text
    assert "Main chapter two." in sent and "Branch chapter two." not in sent


def test_the_summariser_is_told_names_the_prose_really_uses():
    cast = [
        Character(id="a", name="Ann"),
        Character(id="e", name="Elena Ruiz"),
        Character(id="c", name="Captain Idris"),
    ]
    found = characters_in("The announcer called. Elena nodded to the captain.", cast)
    # "Ann" is not in "announcer", "Elena" is Elena Ruiz, a rank alone is nobody.
    assert [c.id for c in found] == ["e"]


def test_nothing_is_rebuilt_until_the_author_asks(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.edit_node("a0", "Serrik kept his sword.")
    assert first.stale
    list(session.send(turn()))
    assert first.stale and first.content == "Main chapter one."
    assert not session.provider.background.requests


def test_a_rebuild_runs_in_the_background_and_clears_the_flag(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.edit_node("a0", "Serrik kept his sword.")
    background = session.provider.background
    background.gate.clear()
    assert session.start_rebuild([first.id])
    # Nothing changes until it is in, and the old chapter is still what is sent.
    assert session.rebuilding_ids() == {first.id}
    assert not session.rebuild_ready()
    assert session.adopt_rebuild() == []
    assert first.stale
    background.gate.set()
    assert session._rebuild_job.done.wait(5)
    notices = session.adopt_rebuild()
    assert notices == ["Rebuilt Chapter 1 in the background."]
    assert first.content == "Rebuilt in the background 1." and not first.stale
    assert "Serrik kept his sword." in background.requests[0].messages[1].text
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=session.root)]
    assert kinds[-2:] == ["summary_request", "summary_response"]


def test_several_edits_then_one_rebuild_each_chapter_continuing_the_last(
    session: StorySession,
):
    first = by_content(session, "Main chapter one.")
    second = by_content(session, "Main chapter two.")
    session.edit_node("a0", "Serrik kept his sword.")
    session.edit_node("a2", "Maela opened the gate.")
    stale = [s.id for s in session.split().summaries if s.stale]
    assert stale == [first.id, second.id]
    assert session.start_rebuild(list(reversed(stale)))
    assert session._rebuild_job.done.wait(5)
    session.adopt_rebuild()
    requests = session.provider.background.requests
    # Chapter 2 continues from chapter 1's new text, not the old one.
    assert "Rebuilt in the background 1." in requests[1].messages[1].text
    assert first.content.endswith("1.") and second.content.endswith("2.")


def test_an_edit_during_the_rebuild_keeps_the_flag(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.edit_node("a0", "Serrik kept his sword.")
    background = session.provider.background
    background.gate.clear()
    session.start_rebuild([first.id])
    session.edit_node("u1", "And then he changed his mind.")
    background.gate.set()
    assert session._rebuild_job.done.wait(5)
    notices = session.adopt_rebuild()
    assert "changed while being rebuilt" in notices[0]
    assert first.stale and first.content == "Main chapter one."
    # Paid for all the same.
    assert session.unreported_usage
    log = read_api_log(session.story.id, root=session.root)
    assert log[-1]["kind"] == "summary_response" and log[-1]["text"]


def test_the_authors_own_words_written_meanwhile_win(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.edit_node("a0", "Serrik kept his sword.")
    background = session.provider.background
    background.gate.clear()
    session.start_rebuild([first.id])
    session.edit_summary(first.id, "My own account.")
    background.gate.set()
    assert session._rebuild_job.done.wait(5)
    session.adopt_rebuild()
    assert first.content == "My own account." and first.hand_edited


def test_a_turn_takes_in_a_finished_rebuild_first(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.edit_node("a0", "Serrik kept his sword.")
    session.start_rebuild([first.id])
    assert session._rebuild_job.done.wait(5)
    said = [e.text for e in session.send(turn()) if isinstance(e, SessionNotice)]
    assert "Rebuilt Chapter 1 in the background." in said
    sent = session.provider.last_request
    assert "Rebuilt in the background 1." in "\n".join(m.text for m in sent.messages)
    assert session.last_prompt.section(SECTION_SUMMARIES) is not None


def test_only_one_rebuild_at_a_time(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.provider.background.gate.clear()
    session.start_rebuild([first.id])
    with pytest.raises(ValueError):
        session.start_rebuild([first.id])
    session.provider.background.gate.set()
    assert session._rebuild_job.done.wait(5)


def test_a_rebuild_out_when_the_story_closes_is_logged_not_lost(session: StorySession):
    first = by_content(session, "Main chapter one.")
    session.start_rebuild([first.id])
    session.close()
    log = read_api_log(session.story.id, root=session.root)
    assert log[-1]["kind"] == "summary_response"
    assert log[-1]["dropped"] == "not adopted: the story was closed"
    assert first.content == "Main chapter one."


def test_rebuilding_a_chapter_drops_the_part_merged_from_it(session: StorySession):
    one = by_content(session, "Main chapter one.")
    two = by_content(session, "Main chapter two.")
    part = Summary(
        covered_node_ids=[*one.covered_node_ids, *two.covered_node_ids],
        content="A part.",
        merged_from=[one.id, two.id],
    )
    session.summaries.append(part)
    assert session.split().summaries[0] is part
    session.start_rebuild([two.id])
    assert session._rebuild_job.done.wait(5)
    session.adopt_rebuild()
    assert part not in session.summaries
    assert session.split().summaries[1].content == "Rebuilt in the background 1."


# --- the scene card's time (the author's rule, Oct 2026) ------------------


def test_the_scene_time_goes_only_into_an_unplotted_storys_opening(story, cast):
    from sealedlore.engine.prompt import SECTION_SCENE, assemble_prompt
    from sealedlore.models.plot import Plot

    def scene_text(history):
        prompt = assemble_prompt(story=story, cast=cast, history_nodes=history, turn=turn())
        return prompt.section(SECTION_SCENE).text

    assert story.scene.time_of_day == "past midnight"
    # Before the first passage the time is the author's starting time.
    assert "Time: past midnight" in scene_text([])
    # After it, the scene read kept it, and it went stale for a hundred turns.
    assert "Time:" not in scene_text(make_exchange(1))
    # A plot story's time is the plot clock's, never the card's.
    story.plot = Plot()
    assert "Time:" not in scene_text([])


def test_a_plot_storys_chapters_carry_their_days_when_asked(session: StorySession):
    from sealedlore.engine.chronicle import clock_minutes
    from sealedlore.engine.prompt import SECTION_SUMMARIES
    from sealedlore.models.plot import Chronicle, Plot

    session.story.plot = Plot(start_minutes=clock_minutes(3, 8, 0))
    a3 = next(n for n in session.nodes if n.id == "a3")
    a3.meta.chronicle = Chronicle(minutes=clock_minutes(5, 20, 0))
    a5 = next(n for n in session.nodes if n.id == "a5")
    a5.meta.chronicle = Chronicle(minutes=clock_minutes(9, 7, 0))
    assert session.chapter_days() == ()  # off unless asked
    session.config.plot_chapter_days = True
    days = dict(session.chapter_days())
    one, two, three = (by_content(session, f"Main chapter {n}.") for n in ("one", "two", "three"))
    assert days == {one.id: (3, 3), two.id: (3, 5), three.id: (5, 9)}
    text = session.assemble(turn()).section(SECTION_SUMMARIES).text
    assert "## Chapter 2 · days 3–5" in text and "## Chapter 1 · day 3" in text
    # Never without a plot.
    session.story.plot = None
    assert session.chapter_days() == ()
