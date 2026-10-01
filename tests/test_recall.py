"""Recall: the detail a merge condensed away, brought back into the tail when it matters."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.archival import split_path
from sealedlore.engine.prompt import SECTION_RECALL, TurnRequest
from sealedlore.engine.recall import (
    RecallItem,
    fuse,
    keyword_scores,
    rank_recall,
    recall_items,
    select_recall,
)
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


def test_choosing_takes_the_best_in_story_order_and_reports_the_next():
    items = [item("a", 1), item("b", 2), item("c", 3), item("d", 4)]
    report = select_recall(
        items,
        {"a": 0.7, "b": 0.4, "c": 0.9, "d": 0.6},
        k=2,
        token_cap=100,
        count_tokens=len,
    )
    assert [i.id for i in report.items] == ["a", "c"]
    assert [i.id for i in report.near_misses] == ["d", "b"]


def test_a_chapter_is_found_by_the_words_of_its_own_exchanges():
    """Measured: the right chapter came first 20/79 by similarity alone, 33/79
    with keywords over the chapter and its archived exchanges fused in."""
    nodes = make_exchange(10)
    nodes[5].content = "Under the altar, Maela finds a brass astrolabe wrapped in oilcloth."
    part, chapters = part_and_chapters(nodes)
    split = split_path([*chapters, part], nodes)
    items = recall_items(split, [*chapters, part], [], mode="chapters")
    assert any("astrolabe" in text for text in items[1].detail)
    assert "astrolabe" not in items[1].text
    own, detail = keyword_scores("Where did the astrolabe come from?", items)
    assert own == {} and list(detail) == [chapters[1].id]
    # Fused with a similarity that prefers another chapter, the keywords win
    # the tie-break of two rankings to one.
    fused = rank_recall(
        "Where did the astrolabe come from?", items, {chapters[0].id: 0.6, chapters[1].id: 0.5}
    )
    assert max(fused, key=fused.__getitem__) == chapters[1].id


def test_rare_words_count_for_more_than_common_ones():
    items = [
        item("a", 1, "Serrik and Maela walked to the river."),
        item("b", 2, "Serrik and Maela found the drowned bell."),
        item("c", 3, "Serrik and Maela slept."),
    ]
    own, _ = keyword_scores("Serrik Maela bell", items)
    assert max(own, key=own.__getitem__) == "b"


def test_fusion_adds_reciprocal_ranks():
    fused = fuse([{"a": 0.9, "b": 0.1}, {"b": 5.0}])
    assert fused["b"] > fused["a"]


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
        token_cap=100,
        count_tokens=len,
    )
    assert [i.id for i in report.items] == ["ch", "x:2"]


def test_the_token_cap_holds():
    items = [item("a", 1, "x" * 60), item("b", 2, "y" * 60)]
    report = select_recall(items, {"a": 0.9, "b": 0.8}, k=2, token_cap=100, count_tokens=len)
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
    config.recall_in = "everywhere"
    return StorySession(
        bundle,
        config,
        MockChatProvider(["A passage."] * 20),
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


def test_without_an_embeddings_endpoint_the_keywords_recall_alone(session: StorySession):
    session.embeddings = None
    list(session.ask("What did Maela find in the chapel?"))
    section = session.last_prompt.section(SECTION_RECALL)
    assert section is not None and "map of the tunnels" in section.text
    assert session.last_recall.reason == "keywords only: no embeddings endpoint"


def test_off_by_default_and_never_in_a_chat(session: StorySession):
    session.config.recall = "off"
    list(session.send(turn("The ferryman's promise.")))
    assert session.last_prompt.section(SECTION_RECALL) is None


def test_a_question_recalls_for_the_question(session: StorySession):
    list(session.ask("What did Maela find in the chapel?"))
    section = session.last_prompt.section(SECTION_RECALL)
    assert section is not None and "map of the tunnels" in section.text


def test_by_default_only_a_question_recalls(session: StorySession):
    """Measured: Questions about merged chapters were answered far better with
    recall; in story turns it brought old states back as if current."""
    session.config.recall_in = "questions"
    list(session.send(turn("I ask the ferryman's daughter about her brother and my promise.")))
    assert session.last_prompt.section(SECTION_RECALL) is None
    assert session.last_recall is None
    list(session.ask("What did Serrik promise the ferryman's daughter?"))
    assert session.last_prompt.section(SECTION_RECALL) is not None
    assert session.last_recall.items


def test_a_turn_recalls_for_its_own_text_not_the_scene(session: StorySession):
    """Measured on 67 turns referring back: the last few messages and the
    turn found the right chapter first 7 times, the turn alone 16."""
    request = turn("The ferryman's daughter and my promise.")
    assert session._recall_query(request, session.path()) == request.user_text


def carrying(session: StorySession, turns: int = 2) -> StorySession:
    session.config.recall_in = "questions"
    session.config.recall_carry_turns = turns
    list(session.ask("What did Serrik promise the ferryman's daughter?"))
    return session


def test_a_questions_recall_rides_the_turns_after_it(session: StorySession):
    """The author: a Question answered from recalled detail mustn't be
    contradicted by the story a moment later, for want of that detail."""
    carrying(session, turns=2)
    aside = session.bundle.asides[-1]
    assert aside.recalled
    for _ in range(2):
        list(session.send(turn("I walk on.")))
        section = session.last_prompt.section(SECTION_RECALL)
        assert section is not None
        assert "promised the ferryman's daughter" in section.text
        assert "asked about some of this out of character" in section.text
        assert session.last_recall.items and all(i.carried for i in session.last_recall.items)
        assert {i.id for i in session.last_recall.items} == set(aside.recalled)
    list(session.send(turn("I walk on again.")))
    assert session.last_prompt.section(SECTION_RECALL) is None
    assert session.last_recall is None


def test_a_retake_carries_what_its_turn_carried(session: StorySession):
    carrying(session, turns=1)
    list(session.send(turn("I walk on.")))
    first = session.last_prompt.section(SECTION_RECALL).text
    list(session.regenerate())
    assert session.last_prompt.section(SECTION_RECALL).text == first


def test_a_question_is_carried_on_its_own_branch_only(session: StorySession):
    carrying(session)
    asked_at = session.story.active_leaf_id
    # Back to the passage before the Question: another line of the story.
    session.story.active_leaf_id = session.path()[-3].id
    assert asked_at not in {n.id for n in session.path()}
    list(session.send(turn("I take the other road.")))
    assert session.last_prompt.section(SECTION_RECALL) is None


def test_carrying_can_be_switched_off(session: StorySession):
    carrying(session, turns=0)
    list(session.send(turn("I walk on.")))
    assert session.last_prompt.section(SECTION_RECALL) is None


def test_a_private_scenes_question_is_never_carried(session: StorySession):
    carrying(session)
    session.bundle.asides[-1].private_span = "span-1"
    list(session.send(turn("I walk on.")))
    assert session.last_prompt.section(SECTION_RECALL) is None


def test_a_chapter_back_in_the_prompt_is_not_carried(session: StorySession):
    carrying(session)
    part = next(s for s in session.bundle.summaries if s.merged_from)
    session.bundle.summaries.remove(part)  # taken apart: its chapters are sent as they are
    list(session.send(turn("I walk on.")))
    assert session.last_prompt.section(SECTION_RECALL) is None


def test_in_turns_too_carried_and_found_are_sent_once_each(session: StorySession):
    carrying(session)
    session.config.recall_in = "everywhere"
    list(session.send(turn("The ferryman's daughter and my promise.")))
    ids = [i.id for i in session.last_recall.items]
    assert len(ids) == len(set(ids))
    assert any(i.carried for i in session.last_recall.items)
