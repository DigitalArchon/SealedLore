"""Old chapters merged into a part, in the background, adopted at the next archival."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.archival import chapter_number, plan_merge
from sealedlore.engine.prompt import SECTION_SUMMARIES, TurnRequest
from sealedlore.engine.rules import render_summaries_block
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.summary import Summary
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from tests.conftest import make_exchange
from tests.test_session import make_config

CHAPTER = "Serrik crossed the river, bargained with the ferryman and lost his sword. " * 6


def chapters(nodes, count: int) -> list[Summary]:
    """`count` chapters of two exchanges each over `nodes`, oldest first."""
    return [
        Summary(covered_node_ids=[n.id for n in nodes[4 * i : 4 * i + 4]], content=f"{CHAPTER}{i}")
        for i in range(count)
    ]


class Sibling(MockChatProvider):
    """A mock that can make a call alongside its own, as the real client can."""

    def sibling(self) -> MockChatProvider:
        return self.side

    def detached(self) -> MockChatProvider:
        return self.side

    def __init__(self, responses, side_responses) -> None:
        super().__init__(responses)
        self.side = MockChatProvider(side_responses)


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    nodes = make_exchange(16)
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes, summaries=chapters(nodes, 6))
    story.active_leaf_id = nodes[-1].id
    save_story_bundle(bundle, root=tmp_path)
    s = StorySession(
        bundle,
        make_config(),
        MockChatProvider(["Merged."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    # Six chapters of ~80 words are well over a fifth of this budget.
    s.story.defaults.context_token_budget = 2_000
    return s


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="I wait.", controlled_character_id="char-serrik"
    )


def test_nothing_is_merged_under_the_threshold_or_short_of_two():
    run = chapters(make_exchange(16), 6)
    assert plan_merge(run, over_threshold=False, keep=3) == []
    assert plan_merge(run[:4], over_threshold=True, keep=3) == []
    assert plan_merge(run, over_threshold=True, keep=3) == run[:3]


def test_a_stale_or_hand_edited_chapter_ends_the_run():
    run = chapters(make_exchange(16), 6)
    run[2].hand_edited = True
    assert plan_merge(run, over_threshold=True, keep=1) == run[:2]
    run[1].stale = True
    assert plan_merge(run, over_threshold=True, keep=1) == []


def test_a_part_is_numbered_by_the_chapters_it_stands_for():
    run = chapters(make_exchange(16), 4)
    part = Summary(covered_node_ids=[], content="The part.", merged_from=[s.id for s in run[:3]])
    text = render_summaries_block([part, run[3]])
    assert "## Chapters 1–3\n\nThe part." in text
    assert "## Chapter 4\n\n" in text
    # The chapters stay in the list; the part doesn't shift their numbers.
    assert chapter_number([*run, part], run[3]) == 4
    assert chapter_number([*run, part], part) == 1


def test_merging_replaces_the_oldest_chapters_on_the_path(session: StorySession):
    run = list(session.split().summaries)
    part = session.merge_now()
    assert part is not None and part.merged_from == [s.id for s in run[:3]]
    assert part.covered_node_ids == [i for s in run[:3] for i in s.covered_node_ids]
    split = session.split()
    assert split.summaries[0] is part and list(split.summaries[1:]) == run[3:]
    prompt = session.assemble(turn())
    text = prompt.section(SECTION_SUMMARIES).text
    assert "## Chapters 1–3\n\nMerged." in text and "## Chapter 4" in text
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=session.root)]
    assert kinds[-2:] == ["merge_request", "merge_response"]


def test_a_part_is_merged_again_with_what_follows_it(session: StorySession):
    first = session.merge_now()
    session.config.summary_merge_keep = 1
    # Past the threshold again, as a longer story would be.
    session.story.defaults.context_token_budget = 1_000
    second = session.merge_now()
    assert second is not None
    assert second.merged_from == [s.id for s in session.summaries if not s.merged_from][:5]
    # The first part is superseded; the chapters themselves stay on file.
    assert first not in session.summaries
    assert len([s for s in session.summaries if not s.merged_from]) == 6


def test_the_merge_runs_off_the_turn_and_goes_in_at_the_next_archival(
    session: StorySession,
):
    session.provider = Sibling(["A passage."], ["Merged in the background."])
    list(session.send(turn()))
    job = session._merge_job
    assert job is not None
    assert job.done.wait(5)
    # Ready, but not in: the summary block changes only when archival does.
    assert not any(s.merged_from for s in session.summaries)
    assert len(session.provider.requests) == 1  # the passage only

    # The next turn over budget adopts it before archiving.
    session.provider.responses = ["Another chapter.", "Next passage."]
    session.story.defaults.context_token_budget = 900
    notices = [e.text for e in session.send(turn()) if hasattr(e, "text") and "Merged" in e.text]
    assert notices
    assert any(s.merged_from for s in session.split().summaries)


def test_a_reply_about_chapters_that_changed_meanwhile_is_dropped(session: StorySession):
    session.provider = Sibling(["A passage."], ["Merged."])
    list(session.send(turn()))
    assert session._merge_job is not None and session._merge_job.done.wait(5)
    session.edit_summary(session.summaries[0].id, "The author's own words.")
    assert session.adopt_merge() is None
    assert not any(s.merged_from for s in session.summaries)


def test_editing_under_a_part_drops_it_and_its_chapters_stand_in(session: StorySession):
    part = session.merge_now()
    assert part is not None
    first = session.summaries[0]
    stale = session.edit_node(first.covered_node_ids[0], "Changed.")
    assert part not in session.summaries
    assert stale == [first]
    assert session.split().summaries[0] is first
