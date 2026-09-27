"""The story ledger: point-form facts beside the chapters, checked in code."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.ledger import (
    MAX_LINES,
    check_ledger,
    ledger_on,
    normalised,
    render_ledger_block,
)
from sealedlore.engine.prompt import SECTION_SUMMARIES, TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.summary import Summary
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from tests.conftest import make_exchange
from tests.test_merge import Sibling
from tests.test_session import make_config


def ledger(*bonds: str, knows: str = "- Rosa knows Jane was a medic") -> str:
    lines = "\n".join(bonds) or "- Serrik and Nils: wary allies"
    return (
        f"## People and bonds\n{lines}\n## What people know\n{knows}\n"
        "## Promises, plans and debts\n- Serrik owes the ferryman a sword\n"
        "## Where things stand\n- Serrik is at the keep"
    )


def test_a_whole_ledger_passes():
    assert check_ledger(ledger(), None, finish_reason="stop") is None


def test_a_cut_off_or_misshapen_reply_is_refused():
    assert check_ledger(ledger(), None, finish_reason="length") == "the reply was cut off"
    assert "missing" in check_ledger("## People and bonds\n- x", None, finish_reason="stop")
    assert check_ledger(ledger().replace("- ", "* "), None, finish_reason="stop")


def test_a_reply_that_lost_most_of_the_ledger_is_refused():
    # Live (GLM 5.3): thirty-eight lines became three.
    before = ledger(*[f"- bond {i}" for i in range(30)])
    assert "kept" in check_ledger(ledger(), before, finish_reason="stop")
    # Tidying a few lines is what an update is for.
    assert (
        check_ledger(ledger(*[f"- bond {i}" for i in range(22)]), before, finish_reason="stop")
        is None
    )


def test_a_runaway_ledger_is_refused():
    long = ledger(*[f"- bond {i}" for i in range(MAX_LINES)])
    assert "ran to" in check_ledger(long, None, finish_reason="stop")


def test_stored_ledgers_keep_only_their_sections_and_lines():
    text = "Here is the ledger:\n\n" + ledger() + "\n\nLet me know if you need more."
    assert normalised(text) == ledger()


def test_the_ledger_is_the_last_one_on_the_path():
    old = Summary(content="a", ledger=ledger("- old"))
    none = Summary(content="b")
    assert ledger_on([old, none]) == ledger("- old")
    assert "- old" in render_ledger_block([old, none])
    assert render_ledger_block([none]) == ""


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = make_config()
    config.story_ledger = True
    config.suggest_characters = False
    return StorySession(
        bundle,
        config,
        MockChatProvider(),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def archive_one(session: StorySession) -> Summary:
    """The next chunk of a 24-exchange story."""
    if not session.nodes:
        nodes = make_exchange(24)
        session.nodes.extend(nodes)
        session.story.active_leaf_id = nodes[-1].id
    return session.archive(session.next_chunk())


def test_archiving_updates_the_ledger_beside_the_summary(session: StorySession):
    session.provider = Sibling(["A chapter."], [ledger()])
    summary = archive_one(session)
    assert summary.content == "A chapter."
    assert summary.ledger == ledger()
    prompt = session.assemble(
        TurnRequest(
            speaker_id="char-serrik", user_text="I wait.", controlled_character_id="char-serrik"
        )
    )
    text = prompt.section(SECTION_SUMMARIES).text
    assert "# WHAT THE STORY HAS ESTABLISHED" in text and "Rosa knows Jane was a medic" in text
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=session.root)]
    assert "ledger_request" in kinds and "ledger_response" in kinds


def test_a_refused_update_carries_the_last_ledger_forward(session: StorySession):
    session.provider = Sibling(["Chapter one."], [ledger()])
    first = archive_one(session)
    session.provider = Sibling(["Chapter two."], ["Sorry, I can't."])
    second = archive_one(session)
    assert second.content == "Chapter two."
    assert second.ledger == first.ledger
    assert session.ledger_problem
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=session.root)]
    assert "ledger_refused" in kinds


def test_without_a_sibling_the_update_follows_the_summary(session: StorySession):
    session.provider = MockChatProvider(["A chapter.", ledger()])
    assert archive_one(session).ledger == ledger()


def test_a_first_ledger_on_a_story_with_chapters_starts_from_them(session: StorySession):
    session.config.story_ledger = False
    session.provider = MockChatProvider(["An old chapter from before the ledger."])
    archive_one(session)
    session.config.story_ledger = True
    session.provider = MockChatProvider(["Chapter two.", ledger()])
    archive_one(session)
    sent = session.provider.requests[-1].messages[1].text
    assert "An old chapter from before the ledger." in sent
    assert "THE STORY BEFORE THIS STRETCH" in sent
