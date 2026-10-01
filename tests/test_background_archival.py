"""Archival written in the background and adopted when due: no turn waits on it."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.node import Node, NodeMeta
from sealedlore.providers.base import ChatRequest, StreamCompleted
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.tree import link_child
from tests.conftest import make_exchange
from tests.test_ledger import ledger
from tests.test_session import FILLER, make_config


class Background:
    """Answers background calls by what they ask for; thread-safe, optionally gated."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.gate.set()
        self.lock = threading.Lock()
        self.calls: list[str] = []

    def complete(self, request: ChatRequest) -> tuple[str, StreamCompleted]:
        self.gate.wait(10)
        system = request.messages[0].text
        kind = (
            "summary"
            if system == DEFAULT_TEXTS["chapters.summariser"]
            else "ledger"
            if system == DEFAULT_TEXTS["ledger.system"]
            else "scan"
        )
        with self.lock:
            self.calls.append(kind)
            count = self.calls.count(kind)
        text = {
            "summary": f"Chapter written in the background {count}.",
            "ledger": ledger(),
            "scan": "[]",
        }[kind]
        return text, StreamCompleted(finish_reason="stop")

    def close(self) -> None:
        pass


class Storyteller(MockChatProvider):
    def __init__(self, responses, background: Background) -> None:
        super().__init__(responses)
        self.background = background

    def detached(self) -> Background:
        return self.background


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    nodes = make_exchange(24)
    for node in nodes:
        node.content = f"{node.content} {FILLER * 12}".strip()
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    story.active_leaf_id = nodes[-1].id
    story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    config = make_config()
    config.story_ledger = True
    s = StorySession(
        bundle,
        config,
        Storyteller(["A passage."], Background()),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    # The 24 exchanges are near this budget, not yet over it.
    full = s.assemble(turn()).full_total
    s.story.defaults.context_token_budget = int(full / 0.95)
    return s


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="I wait.", controlled_character_id="char-serrik"
    )


def notices(events) -> list[str]:
    return [event.text for event in events if isinstance(event, SessionNotice)]


def test_chapters_are_written_after_the_passage_and_adopted_when_due(session: StorySession):
    list(session.send(turn()))
    # The turn itself made one call, the passage; the chapters went elsewhere.
    assert len(session.provider.requests) == 1
    job = session._archive_job
    assert job is not None and job.done.wait(5)
    assert not session.summaries

    # Over the budget now: adopted at once, no summariser call on the turn.
    session.story.defaults.context_token_budget = int(session.assemble(turn()).full_total * 0.9)
    said = notices(session.send(turn()))
    assert any("written in the background" in text for text in said)
    assert len(session.provider.requests) == 2
    assert session.summaries and all(s.ledger == ledger() for s in session.summaries)
    assert session.summaries[0].content == "Chapter written in the background 1."
    log = read_api_log(session.story.id, root=session.root)
    assert any(e["kind"] == "summary_request" and e.get("background") for e in log)
    assert "ledger_response" in [e["kind"] for e in log]


def test_an_edited_node_voids_the_chapter_written_from_it(session: StorySession):
    list(session.send(turn()))
    assert session._archive_job.done.wait(5)
    first = session.path()[0]
    session.edit_node(first.id, "Changed after the chapter was written.")
    session.story.defaults.context_token_budget = int(session.assemble(turn()).full_total * 0.9)
    said = notices(session.send(turn()))
    assert any("weren't used" in text for text in said)
    assert not session.summaries


def test_a_turn_never_waits_for_chapters_still_being_written(session: StorySession):
    session.provider.background.gate.clear()
    session.story.defaults.context_token_budget = int(session.assemble(turn()).full_total * 0.9)
    said = notices(session.send(turn()))
    # Over the budget with nothing ready: the passage goes out regardless.
    assert session.path()[-1].content == "A passage."
    assert any("in the background" in text for text in said)
    assert session.last_prompt.excluded_node_ids
    session.provider.background.gate.set()
    assert session._archive_job.done.wait(5)


def test_preparing_leaves_the_last_turns_prompt_for_the_inspector(session: StorySession):
    list(session.send(turn()))
    assert session._archive_job is not None
    # The prompt the turn really sent, not the measurement taken after it.
    assert "I wait." in session.last_prompt.section("tail.author_turn").text


def test_chapters_for_a_branch_the_author_left_are_kept_for_it(session: StorySession):
    """They used to be thrown away, and the branch paid for them again."""
    list(session.send(turn()))
    job = session._archive_job
    assert job is not None and job.done.wait(5)
    main_leaf = session.story.active_leaf_id
    first = job.chapters[0].node_ids
    # A branch rewritten from inside the first chapter, and the author on it.
    fork = next(n for n in session.nodes if n.id == first[3])
    rewritten = Node(
        id="rewritten",
        kind="user",
        speaker_id="char-serrik",
        content="I go the other way.",
        meta=NodeMeta(controlled_character_id="char-serrik"),
    )
    link_child(fork, rewritten)
    session.bundle.nodes.append(rewritten)
    session.story.active_leaf_id = rewritten.id

    said = notices(session.adopt_archival(session.path()))
    assert any("kept for it" in text for text in said)
    assert session._archive_job is None
    assert session.summaries[0].covered_node_ids == first
    # Nothing applies on the branch the author is on...
    assert not session.split().summaries
    # ...and on going back, the chapters are there, nothing sent again.
    session.story.active_leaf_id = main_leaf
    assert session.split().summaries[0].covered_node_ids == first


def test_a_finished_job_for_another_branch_frees_the_slot(session: StorySession):
    list(session.send(turn()))
    job = session._archive_job
    assert job is not None and job.done.wait(5)
    fork = next(n for n in session.nodes if n.id == job.chapters[0].node_ids[1])
    rewritten = Node(id="other", kind="user", speaker_id="char-serrik", content="Elsewhere.")
    link_child(fork, rewritten)
    session.bundle.nodes.append(rewritten)
    session.story.active_leaf_id = rewritten.id
    session.prepare_archival()
    assert session._archive_job is not job
    assert session.summaries
