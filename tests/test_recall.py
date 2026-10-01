"""Recall: the detail a merge condensed away, brought back into the tail when it matters."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.archival import split_path
from sealedlore.engine.prompt import SECTION_RECALL, TurnRequest
from sealedlore.engine.recall import RecallItem, recall_items, select_recall
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.summary import Summary
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from tests.conftest import make_exchange
from tests.test_session import make_config

CHAPTERS = [
    "Serrik bargained with the ferryman and lost his sword in the river.",
    "Maela picked the lock of the old chapel and found a map of the tunnels.",
    "Serrik promised the ferryman's daughter he would bring back her brother.",
    "Captain Idris sent word from the north that the bridge was down.",
]


def part_and_chapters(nodes) -> tuple[Summary, list[Summary]]:
    chapters = [
        Summary(covered_node_ids=[n.id for n in nodes[4 * i : 4 * i + 4]], content=text)
        for i, text in enumerate(CHAPTERS)
    ]
    part = Summary(
        covered_node_ids=[i for c in chapters[:3] for i in c.covered_node_ids],
        content="A short account of the river, the chapel and a promise.",
        merged_from=[c.id for c in chapters[:3]],
    )
    return part, chapters


def test_the_chapters_a_part_stands_for_are_the_candidates():
    nodes = make_exchange(10)
    part, chapters = part_and_chapters(nodes)
    split = split_path([*chapters, part], nodes)
    assert split.summaries[0] is part
    items = recall_items(split, [*chapters, part], [], mode="chapters")
    # Chapter 4 is already in the prompt; the part's three are not.
    assert [i.id for i in items] == [c.id for c in chapters[:3]]
    assert items[0].label == "Chapter 1, as first written"
    assert recall_items(split, [*chapters, part], [], mode="off") == []


def test_exchanges_are_candidates_too_labelled_by_their_chapter():
    nodes = make_exchange(10)
    part, chapters = part_and_chapters(nodes)
    split = split_path([*chapters, part], nodes)
    items = recall_items(split, [*chapters, part], [], mode="exchanges")
    exchanges = [i for i in items if i.kind == "exchange"]
    assert len(exchanges) == 8  # four chapters of two exchanges
    assert exchanges[0].label == "From chapter 1, as written"
    assert exchanges[-1].label == "From chapter 4, as written"


def test_a_private_scenes_messages_are_never_candidates():
    nodes = make_exchange(10)
    nodes[1].content = "SECRET-MARKER said in private."
    nodes[1].meta.private_span = "span-1"
    part, chapters = part_and_chapters(nodes)
    split = split_path([*chapters, part], nodes)
    items = recall_items(split, [*chapters, part], [], mode="exchanges")
    assert not any("SECRET-MARKER" in item.text for item in items)


def item(id_: str, position: int, text: str = "x", label: str | None = None) -> RecallItem:
    return RecallItem(
        id=id_, kind="chapter", label=label or f"Chapter {position}", text=text, position=position
    )


def test_choosing_takes_the_best_in_story_order_and_reports_near_misses():
    items = [item("a", 1), item("b", 2), item("c", 3), item("d", 4)]
    report = select_recall(
        items,
        {"a": 0.7, "b": 0.4, "c": 0.9, "d": 0.6},
        k=2,
        threshold=0.5,
        token_cap=100,
        count_tokens=len,
    )
    assert [i.id for i in report.items] == ["a", "c"]
    assert [i.id for i in report.near_misses] == ["b"]


def test_an_exchange_from_a_chapter_already_chosen_is_passed_over():
    items = [
        item("ch", 1, label="Chapter 3, as first written"),
        RecallItem(
            id="x:1", kind="exchange", label="From chapter 3, as written", text="x", position=9
        ),
        RecallItem(
            id="x:2", kind="exchange", label="From chapter 5, as written", text="x", position=10
        ),
    ]
    report = select_recall(
        items,
        {"ch": 0.9, "x:1": 0.8, "x:2": 0.7},
        k=3,
        threshold=0.5,
        token_cap=100,
        count_tokens=len,
    )
    assert [i.id for i in report.items] == ["ch", "x:2"]


def test_the_token_cap_holds():
    items = [item("a", 1, "x" * 60), item("b", 2, "y" * 60)]
    report = select_recall(
        items, {"a": 0.9, "b": 0.8}, k=2, threshold=0.5, token_cap=100, count_tokens=len
    )
    assert [i.id for i in report.items] == ["a"]


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    nodes = make_exchange(10)
    part, chapters = part_and_chapters(nodes)
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes, summaries=[*chapters, part])
    story.active_leaf_id = nodes[-1].id
    story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    config = make_config()
    config.recall = "chapters"
    config.recall_threshold = 0.1
    return StorySession(
        bundle,
        config,
        MockChatProvider(["A passage."] * 3),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
        embeddings=MockEmbeddings(),
    )


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def test_a_turn_recalls_the_merged_chapter_it_is_about(session: StorySession):
    list(session.send(turn("I ask the ferryman's daughter about her brother and my promise.")))
    section = session.last_prompt.section(SECTION_RECALL)
    assert section is not None
    assert "EARLIER IN THE STORY" in section.text
    assert "promised the ferryman's daughter" in section.text
    assert session.last_recall.items


def test_recall_vectors_survive_lores_pruning_and_go_when_stale(session: StorySession):
    list(session.send(turn("The ferryman's promise.")))
    kinds = {row.kind for row in session.bundle.embeddings}
    assert "recall" in kinds
    session._prune_embeddings(set())
    assert any(row.kind == "recall" for row in session.bundle.embeddings)
    # A chapter rewritten: its old vector is dropped when the new one is made.
    old = {row.content_hash for row in session.bundle.embeddings if row.kind == "recall"}
    session.summaries[0].content = "Serrik never lost his sword after all."
    list(session.send(turn("The sword in the river.")))
    new = {row.content_hash for row in session.bundle.embeddings if row.kind == "recall"}
    assert len(new) == len(old) and new != old


def test_nothing_is_recalled_without_an_embeddings_endpoint(session: StorySession):
    session.embeddings = None
    list(session.send(turn("The ferryman's promise.")))
    assert session.last_prompt.section(SECTION_RECALL) is None
    assert session.last_recall.reason == "no embeddings endpoint"


def test_off_by_default_and_never_in_a_chat(session: StorySession):
    session.config.recall = "off"
    list(session.send(turn("The ferryman's promise.")))
    assert session.last_prompt.section(SECTION_RECALL) is None


def test_a_question_recalls_for_the_question(session: StorySession):
    list(session.ask("What did Maela find in the chapel?"))
    section = session.last_prompt.section(SECTION_RECALL)
    assert section is not None and "map of the tunnels" in section.text
