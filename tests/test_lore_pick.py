"""Choosing lore with a model for a lorebook too big to send whole."""

from __future__ import annotations

import pytest

from sealedlore.engine.lore_pick import (
    build_pick_messages,
    choose_picked,
    jev_batches,
    parse_pick,
    pick_candidates,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import fallback_counter
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ProviderError
from sealedlore.providers.decisions import Decisions, build_decisions_payload, parse_decisions
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import read_api_log
from tests.test_session import session, stock_lore, turn  # noqa: F401 - fixture

ACCORD, LOCKS, HOUSE = "lore-accord", "lore-locks", "lore-house"


def entries() -> list[LoreEntry]:
    return [e for e in stock_lore() if not e.always_on]


# --- pure ---------------------------------------------------------------------


def test_candidates_are_the_most_similar_then_every_keyword_hit():
    found = pick_candidates(
        entries(), prose="Mind the lock.", scores={ACCORD: 0.6, LOCKS: 0.2}, top_k=1
    )
    assert [e.id for e in found] == [ACCORD, LOCKS]


def test_without_vectors_the_candidates_are_the_keyword_hits():
    assert [e.id for e in pick_candidates(entries(), prose="the treaty")] == [ACCORD]


def test_the_pick_prompt_numbers_the_candidates():
    messages = build_pick_messages("A passage.", "SCENE: the radio room; present: Rosa", entries())
    assert messages[0].text == DEFAULT_TEXTS["lore.picker"]
    assert "[1] The Sundering Accord" in messages[1].text
    assert "[2] Calder ironwork" in messages[1].text


def test_a_pick_off_the_list_or_repeated_is_ignored():
    assert parse_pick('{"pick": [2, 2, 7, "x", 1]}', entries()) == [LOCKS, ACCORD]
    with pytest.raises(ValueError):
        parse_pick("no idea", entries())


def test_picks_come_first_then_what_the_turn_names_within_the_cap():
    report = choose_picked(
        entries(), [ACCORD], turn_text="Mind the lock.", token_cap=10_000, count_tokens=len
    )
    assert [(i.entry.id, i.reason) for i in report.injected] == [
        (ACCORD, "picked"),
        (LOCKS, "keyword"),
    ]
    # The cap drops the keyword hit before anything the model chose.
    accord = len("## The Sundering Accord\n" + entries()[0].content)
    capped = choose_picked(
        entries(), [ACCORD], turn_text="Mind the lock.", token_cap=accord, count_tokens=len
    )
    assert [i.entry.id for i in capped.injected] == [ACCORD]
    assert [i.entry.id for i in capped.dropped] == [LOCKS]


def test_jev_questions_are_batched_under_the_request_limit():
    many = [LoreEntry(id=f"e{n}", title=f"Entry {n}", content="x" * 400) for n in range(300)]
    batches = jev_batches(many, fallback_counter, state_tokens=2_000)
    assert len(batches) > 1
    assert sum(len(batch) for batch in batches) == 300


def test_the_decisions_payload_asks_one_noul_per_entry():
    payload = build_decisions_payload("typesafe/jev-1.13", "state", {"a": "q1", "b": "q2"})
    assert payload["questions"] == {
        "a": {"type": "noul", "instructions": "q1"},
        "b": {"type": "noul", "instructions": "q2"},
    }


def test_an_unanswered_question_is_an_error():
    body = {"answers": {"a": {"type": "noul", "noul": 0.9}}}
    assert parse_decisions(body, expected={"a"}) == {"a": 0.9}
    with pytest.raises(ProviderError):
        parse_decisions(body, expected={"a", "b"})


# --- in the session -----------------------------------------------------------


def large(session: StorySession, selector: str) -> None:  # noqa: F811
    session.bundle.lore = stock_lore()
    session.config.lore_whole_share = 0.0
    session.config.lore_selector = selector  # type: ignore[assignment]


def test_the_picker_picks_after_the_passage_for_the_next_turn(session: StorySession):  # noqa: F811
    large(session, "picker")
    session.provider = MockChatProvider(["They talk of the treaty.", '{"pick": [1]}', "Later."])

    list(session.send(turn("I ask about the accord.")))
    passage = session.path()[-1]
    assert passage.meta.lore_picks == [ACCORD]
    assert session.provider.requests[1].model == session.config.lore_model
    # The first turn had nothing picked yet, and said so.
    assert "no picks" in (session.last_retrieval.selector_note or "")

    list(session.send(turn("Mind the lock.")))
    report = session.last_retrieval
    assert report.selector == "picker" and report.selector_note is None
    assert [(i.entry.id, i.reason) for i in report.injected] == [
        (ACCORD, "picked"),
        (LOCKS, "keyword"),
    ]
    assert [e.id for e in report.standing] == [HOUSE]
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=session.root)]
    assert "lore_pick_response" in kinds


def test_no_candidates_means_no_call_and_nothing_needed(session: StorySession):  # noqa: F811
    large(session, "picker")
    session.provider = MockChatProvider(["Nothing of note happens."])

    list(session.send(turn("I wait.")))

    assert len(session.provider.requests) == 1
    assert session.path()[-1].meta.lore_picks == []


def test_a_failed_pick_is_a_notice_and_the_next_turn_uses_similarity(
    session: StorySession,  # noqa: F811
):
    large(session, "picker")
    session.provider = MockChatProvider(["The treaty again.", "I can't decide."])

    events = list(session.send(turn("The accord.")))

    assert any(isinstance(e, SessionNotice) and "Lore pick failed" in e.text for e in events)
    assert session.path()[-1].meta.lore_picks is None


class FakeJev:
    def __init__(self, probabilities: dict[str, float] | None = None) -> None:
        self.probabilities = probabilities
        self.calls: list[dict[str, str]] = []

    def decide(self, model, state, questions):  # noqa: ANN001
        self.calls.append(dict(questions))
        if self.probabilities is None:
            raise ProviderError("https://nano-gpt.com/api/v1/decisions returned HTTP 404")
        probabilities = {key: self.probabilities.get(key, 0.1) for key in questions}
        return Decisions(probabilities, model, {"input_tokens": 900, "cost": 0.00004})


def test_jev_scores_every_entry_before_the_turn(session: StorySession):  # noqa: F811
    large(session, "jev")
    jev = FakeJev({ACCORD: 0.8, LOCKS: 0.3})
    session._decisions_client = jev  # type: ignore[assignment]

    list(session.send(turn("Mind the lock.")))

    report = session.last_retrieval
    assert set(jev.calls[0]) == {ACCORD, LOCKS}  # always-on entries stand; not asked
    assert report.selector == "jev"
    assert [(i.entry.id, i.reason) for i in report.injected] == [
        (ACCORD, "picked"),
        (LOCKS, "keyword"),
    ]
    assert report.injected[0].describe() == "picked by Jev (0.80)"
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=session.root)]
    assert "lore_decision_response" in kinds


def test_jev_failing_falls_back_to_similarity_and_says_why(session: StorySession):  # noqa: F811
    large(session, "jev")
    session._decisions_client = FakeJev(None)  # type: ignore[assignment]

    list(session.send(turn("The accord.")))

    report = session.last_retrieval
    assert report.selector == "similarity"
    assert "404" in (report.selector_note or "")
    assert [e.id for e in report.entries] == [ACCORD]
