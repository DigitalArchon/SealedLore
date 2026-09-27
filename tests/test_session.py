"""End-to-end turns against the mock provider: nodes, logs, persistence."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import (
    MAX_AUTO_ARCHIVE_ROUNDS,
    SessionNotice,
    StorySession,
    TurnShape,
)
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, EmbeddingProviderConfig, ProviderConfig
from sealedlore.models.generation import GenerationParams
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node, Usage
from sealedlore.providers.base import ProviderError, TextDelta
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import (
    StoryBundle,
    load_config,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from sealedlore.tree import sibling_position
from tests.conftest import make_exchange


def make_config(model: str = "anthropic/claude-sonnet-4.5") -> Config:
    return Config(
        providers=[
            ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model=model)
        ],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        # Each turn is one call here; the scene read after it has tests of
        # its own (tests/test_scene_reads.py).
        scene_reads="manual",
        # Its own call per chapter, tested in tests/test_ledger.py; here it
        # would take the scripted replies meant for the summariser.
        story_ledger=False,
    )


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider(["The door gives with a groan."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )


def turn(text: str = "I put my shoulder to the door.") -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def test_send_appends_both_nodes_and_links_them(session: StorySession):
    list(session.send(turn()))

    assert [node.kind for node in session.nodes] == ["user", "assistant"]
    user_node, assistant_node = session.nodes
    assert assistant_node.parent_id == user_node.id
    assert user_node.children == [assistant_node.id]
    assert session.story.active_leaf_id == assistant_node.id
    assert assistant_node.content == "The door gives with a groan."


def test_assistant_node_records_usage_and_turn_metadata(session: StorySession):
    list(session.send(turn()))
    meta = session.nodes[-1].meta

    assert meta.model == "anthropic/claude-sonnet-4.5"
    assert meta.usage.prompt_tokens == 1200
    assert meta.controlled_character_id == "char-serrik"
    assert meta.agency_mode == "contested"
    assert meta.request_log_ref


def test_the_turn_is_persisted_without_an_explicit_save(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    reloaded = load_story_bundle(session.story.id, root=tmp_path)

    assert len(reloaded.nodes) == 2
    assert reloaded.story.active_leaf_id == session.story.active_leaf_id


def test_api_log_records_request_and_response(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    log = read_api_log(session.story.id, root=tmp_path)

    kinds = [entry["kind"] for entry in log]
    assert kinds == ["request", "response"]
    assert log[0]["payload"]["model"] == "anthropic/claude-sonnet-4.5"
    assert log[1]["request_log_ref"] == log[0]["id"]
    assert log[1]["text"] == "The door gives with a groan."
    assert session.nodes[-1].meta.request_log_ref == log[0]["id"]


def test_history_grows_across_turns(session: StorySession):
    list(session.send(turn("First.")))
    list(session.send(turn("Second.")))

    assert [node.kind for node in session.path()] == ["user", "assistant", "user", "assistant"]
    second_request = session.provider.requests[-1]
    whole = "\n".join(message.text for message in second_request.messages)
    assert "First." in whole
    assert "Second." in whole


def test_stopping_early_keeps_the_partial_text(session: StorySession):
    session.provider = MockChatProvider(["one two three four five six"], chunk_size=4)
    events = session.send(turn())
    first = next(events)
    assert isinstance(first, TextDelta)
    events.close()

    assert session.last_result is not None
    assert session.last_result.stopped_early is True
    assert session.last_result.text == "one "
    assert session.nodes[-1].content == "one "


def test_regenerate_creates_a_sibling_take(session: StorySession):
    session.provider = MockChatProvider(["first take", "second take"])
    list(session.send(turn()))
    first_leaf = session.story.active_leaf_id

    list(session.regenerate())
    second_leaf = session.story.active_leaf_id

    assert first_leaf != second_leaf
    assert len([node for node in session.nodes if node.kind == "user"]) == 1
    assert sibling_position(session.nodes, second_leaf) == (2, 2)
    assert session.nodes[-1].content == "second take"


def test_regenerate_resends_the_same_author_turn(session: StorySession):
    session.provider = MockChatProvider(["a", "b"])
    list(session.send(turn("I listen at the door.")))
    list(session.regenerate())

    resent = session.provider.requests[-1]
    whole = "\n".join(message.text for message in resent.messages)
    assert whole.count("I listen at the door.") == 1
    assert "first take" not in whole


def test_regenerate_needs_a_turn_to_work_from(session: StorySession):
    with pytest.raises(ValueError, match="needs a turn"):
        list(session.regenerate())


def test_a_failed_request_leaves_no_empty_assistant_node(session: StorySession):
    session.provider = MockChatProvider(error=ProviderError("upstream down"))
    with pytest.raises(ProviderError):
        list(session.send(turn()))

    assert [node.kind for node in session.nodes] == ["user"]
    assert session.last_result is not None
    assert session.last_result.node is None


def test_regenerate_retries_an_author_turn_that_never_got_a_response(session: StorySession):
    session.provider = MockChatProvider(error=ProviderError("upstream down"))
    with pytest.raises(ProviderError):
        list(session.send(turn()))

    session.provider = MockChatProvider(["a second attempt"])
    list(session.regenerate())

    assert [node.kind for node in session.nodes] == ["user", "assistant"]
    assert session.nodes[-1].content == "a second attempt"
    # A retry is the turn's first take, not a sibling of a failed one.
    assert sibling_position(session.nodes, session.nodes[-1].id) == (1, 1)


def test_a_failed_request_is_still_logged(session: StorySession, tmp_path: Path):
    session.provider = MockChatProvider(error=ProviderError("upstream down"))
    with pytest.raises(ProviderError):
        list(session.send(turn()))

    log = read_api_log(session.story.id, root=tmp_path)
    assert [entry["kind"] for entry in log] == ["request", "response"]
    assert log[1]["node_id"] is None
    assert log[1]["text"] == ""


def test_cache_control_follows_the_model_id(session: StorySession):
    assert session.uses_cache_control() is True

    session.story.defaults.main_model = "openai/gpt-5"
    assert session.uses_cache_control() is False

    session.config.cache_control_mode = "on"
    assert session.uses_cache_control() is True


def test_correction_factor_is_learned_from_reported_usage(session: StorySession, tmp_path: Path):
    session.provider = MockChatProvider(["ok"], usage=Usage(prompt_tokens=4000))
    list(session.send(turn()))

    model = "anthropic/claude-sonnet-4.5"
    learned = session.config.token_correction_factors[model]
    assert learned > 1.0
    assert load_config(root=tmp_path).token_correction_factors[model] == learned


def test_no_usage_means_no_correction(session: StorySession):
    session.provider = MockChatProvider(["ok"], usage=Usage())
    list(session.send(turn()))
    assert session.config.token_correction_factors == {}


def test_corrections_can_be_switched_off(session: StorySession, tmp_path: Path):
    """Driving a mock must not teach the estimator from invented usage."""
    session.learn_corrections = False
    session.provider = MockChatProvider(["ok"], usage=Usage(prompt_tokens=4000))
    list(session.send(turn()))

    assert session.config.token_correction_factors == {}
    assert load_config(root=tmp_path).token_correction_factors == {}


def test_a_passage_is_kept_exactly_as_written(session: StorySession):
    # No check rewrites or flags a passage: the regex validator is gone.
    text = 'Serrik said, "Then we go through it." Captain Idris ordered the riders out.'
    session.provider = MockChatProvider([text])
    list(session.send(turn()))
    assert session.nodes[-1].content == text


# --- archival (§5.2, §5.3) ------------------------------------------------


FILLER = "the lantern guttered in the draught and the water kept rising "


def grow(session: StorySession, turns: int, *, weight: int = 12) -> list[Node]:
    """A history of `turns` exchanges, padded enough to move the token budget."""
    nodes = make_exchange(turns)
    for node in nodes:
        node.content = f"{node.content} {FILLER * weight}".strip()
    session.nodes.extend(nodes)
    session.story.active_leaf_id = nodes[-1].id
    return nodes


class SummariserDown(MockChatProvider):
    """Streams turns normally but fails the one-shot summariser call."""

    def complete(self, request):
        raise ProviderError("summariser unavailable")


def test_archival_runs_when_the_prompt_reaches_the_budget(session: StorySession):
    history = grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    session.provider = MockChatProvider(["A chapter's worth of events."])

    list(session.send(turn()))

    assert session.summaries
    first = session.summaries[0]
    assert first.covered_node_ids == [node.id for node in history[:20]]
    assert first.model == "anthropic/claude-sonnet-4.5"
    assert first.hand_edited is False


def test_the_summary_stands_in_for_the_archived_prose(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    session.provider = MockChatProvider(["They crossed the river and burned the bridge."])

    list(session.send(turn()))

    sent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "THE STORY SO FAR" in sent
    assert "They crossed the river" in sent
    # The oldest exchange now reaches the model only through the summary.
    assert "Author turn 0." not in sent
    # ...while recent turns are still verbatim.
    assert "Author turn 13." in sent


def test_archival_is_bounded_to_a_couple_of_rounds_per_turn(session: StorySession):
    """Each round is a paid call, so an impossible budget must not spend freely."""
    grow(session, 30)
    session.story.defaults.context_token_budget = 4_000

    list(session.send(turn()))

    assert 0 < len(session.summaries) <= MAX_AUTO_ARCHIVE_ROUNDS


def test_archival_can_be_switched_off(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    session.config.auto_archive = False

    list(session.send(turn()))

    assert session.summaries == []


def test_a_short_story_says_so_rather_than_archiving_the_present(session: StorySession):
    """Over budget with nothing old enough to archive is a message, not a summary."""
    grow(session, 2)
    # Over budget, but with room for some of the prose: not the starved case,
    # which the next test covers. Measured rather than hard-coded, because it
    # is the fixed blocks plus one node, and editing the rules moves that.
    one_node = max(len(node.content) // 3 for node in session.nodes)
    fixed = session.assemble(turn()).budget.by_section
    session.story.defaults.context_token_budget = fixed["system"] + fixed["tail"] + one_node + 50

    notices = [event for event in session.send(turn()) if isinstance(event, SessionNotice)]

    assert session.summaries == []
    assert "aren't yet 10 older turns" in notices[0].text


def test_a_budget_too_small_for_any_prose_says_that_instead(session: StorySession):
    """Live finding: at this point the model is writing from summaries alone."""
    grow(session, 2)
    session.story.defaults.context_token_budget = 1_000

    notices = [event for event in session.send(turn()) if isinstance(event, SessionNotice)]

    assert "written from the chapter summaries alone" in notices[0].text


def test_a_partial_chunk_is_not_archived_automatically(session: StorySession):
    """Whole chunks only: a short one would rewrite the summary block next turn."""
    grow(session, 6)
    session.story.defaults.context_token_budget = 2_400

    list(session.send(turn()))

    assert session.summaries == []
    # ...but the author may still ask for one.
    assert session.next_chunk(allow_partial=True)


def test_a_summariser_failure_does_not_cost_the_author_their_turn(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    session.provider = SummariserDown(["The door gives anyway."])

    events = list(session.send(turn()))

    assert session.summaries == []
    assert [notice.text for notice in events if isinstance(notice, SessionNotice)]
    assert session.nodes[-1].content == "The door gives anyway."


def test_archival_notices_name_the_chapter(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000

    notices = [event for event in session.send(turn()) if isinstance(event, SessionNotice)]

    assert "chapter 1" in notices[0].text.lower()


def test_archiving_is_recorded_in_the_api_log(session: StorySession, tmp_path: Path):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000

    list(session.send(turn()))

    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds[:2] == ["summary_request", "summary_response"]
    assert "request" in kinds and "response" in kinds


def test_editing_a_node_marks_its_summary_stale_and_rebuilds_nothing(session: StorySession):
    history = grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))
    summary = session.summaries[0]
    original = summary.content

    went_stale = session.edit_node(history[0].id, "Author turn 0, rewritten.")

    assert went_stale == [summary]
    assert summary.stale is True
    assert summary.content == original
    assert history[0].content == "Author turn 0, rewritten."
    assert history[0].edited_at


def test_editing_a_verbatim_node_stales_nothing(session: StorySession):
    history = grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))

    assert session.edit_node(history[-1].id, "Rewritten.") == []


def test_a_hand_edited_summary_is_the_authors_words(session: StorySession, tmp_path: Path):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))
    summary = session.summaries[0]
    session.edit_node(session.nodes[0].id, "changed")
    assert summary.stale is True

    session.edit_summary(summary.id, "  What actually happened.  ")

    assert summary.content == "What actually happened."
    assert summary.hand_edited is True
    assert summary.stale is False
    assert summary.edited_at
    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert reloaded.summaries[0].hand_edited is True


def test_rebuilding_a_hand_edited_summary_needs_confirmation(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))
    summary = session.summaries[0]
    session.edit_summary(summary.id, "My own words.")

    with pytest.raises(ValueError, match="edited by hand"):
        session.rebuild_summary(summary.id)

    session.provider = MockChatProvider(["A fresh account of the same events."])
    rebuilt = session.rebuild_summary(summary.id, force=True)

    assert rebuilt.id == summary.id
    assert rebuilt.content == "A fresh account of the same events."
    assert rebuilt.hand_edited is False
    assert rebuilt.stale is False
    # The covered range is untouched, so the path chain still matches.
    assert rebuilt.covered_node_ids == summary.covered_node_ids


def test_rebuilding_clears_staleness(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))
    summary = session.summaries[0]
    session.edit_node(session.nodes[0].id, "changed")

    session.provider = MockChatProvider(["Rewritten from the edited prose."])
    session.rebuild_summary(summary.id)

    assert summary.stale is False
    assert summary.content == "Rewritten from the edited prose."


def test_the_summariser_model_can_differ_from_the_main_model(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    session.story.defaults.summarization_model = "openai/gpt-5-mini"

    list(session.send(turn()))

    assert session.summaries[0].model == "openai/gpt-5-mini"
    summariser_request = session.provider.requests[0]
    assert summariser_request.model == "openai/gpt-5-mini"
    # Reasoning and the story's prose settings have no business here.
    assert summariser_request.params == GenerationParams()


def test_summaries_survive_a_reload(session: StorySession, tmp_path: Path):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))

    reloaded = load_story_bundle(session.story.id, root=tmp_path)

    assert len(reloaded.summaries) == len(session.summaries)
    assert reloaded.summaries[0].covered_node_ids == session.summaries[0].covered_node_ids


# --- lore retrieval (§7) --------------------------------------------------


def stock_lore() -> list[LoreEntry]:
    return [
        LoreEntry(
            id="lore-accord",
            title="The Sundering Accord",
            content="A treaty signed at Calder Keep that ended the war.",
            keywords=["accord", "treaty"],
        ),
        LoreEntry(
            id="lore-locks",
            title="Calder ironwork",
            content="Four-ward locks made by the Vaun smiths, famously stubborn.",
            keywords=["lock", "ironwork"],
        ),
        LoreEntry(
            id="lore-house",
            title="House Vaun",
            content="The family that built the keep and abandoned it.",
            always_on=True,
        ),
    ]


def selecting(session: StorySession) -> None:
    """Too small to select from by default: it would be sent whole (`lore_layout`)."""
    session.bundle.lore = stock_lore()
    session.config.lore_whole_share = 0.0
    session.config.lore_selector = "similarity"


def with_embeddings(session: StorySession) -> MockEmbeddings:
    selecting(session)
    session.config.embedding_provider = EmbeddingProviderConfig(model="mock-embed")
    backend = MockEmbeddings()
    session.embeddings = backend
    return backend


def test_lore_is_retrieved_and_reaches_the_tail(session: StorySession):
    with_embeddings(session)

    list(session.send(turn("I ask Maela about the treaty they signed at Calder Keep.")))

    report = session.last_retrieval
    assert report is not None and report.used_embeddings
    sent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "# REFERENCE" in sent
    assert "The Sundering Accord" in sent


def test_the_whole_lorebook_is_not_dumped_into_every_prompt(session: StorySession):
    with_embeddings(session)
    session.config.lore_retrieval_k = 1
    session.config.lore_similarity_threshold = 0.9

    list(session.send(turn("Nothing here resembles any entry.")))

    # Nothing survives that threshold; the always-on entry stands in the system block.
    assert session.last_retrieval.entries == ()
    assert [e.title for e in session.last_retrieval.standing] == ["House Vaun"]
    assert "House Vaun" in session.last_prompt.section("system.lore").text


def test_entries_are_embedded_once_and_cached(session: StorySession, tmp_path: Path):
    backend = with_embeddings(session)

    list(session.send(turn("First.")))
    embedded_first = [call for call in backend.calls if not call[1]]
    list(session.send(turn("Second.")))
    embedded_after = [call for call in backend.calls if not call[1]]

    assert len(embedded_first) == 1
    assert embedded_after == embedded_first  # no re-embedding on the second turn
    assert len(load_story_bundle(session.story.id, root=tmp_path).embeddings) == 3


def test_editing_an_entry_re_embeds_only_that_entry(session: StorySession):
    backend = with_embeddings(session)
    list(session.send(turn("First.")))

    session.bundle.lore[0].content = "A treaty signed at Thorn Gate instead."
    list(session.send(turn("Second.")))

    documents = [call for call in backend.calls if not call[1]]
    assert len(documents) == 2
    assert len(documents[1][0]) == 1


def test_changing_the_model_re_embeds_everything_once(session: StorySession):
    backend = with_embeddings(session)
    list(session.send(turn("First.")))

    session.config.embedding_provider.model = "other-embed"
    backend.model = "other-embed"
    list(session.send(turn("Second.")))

    documents = [call for call in backend.calls if not call[1]]
    assert [len(batch) for batch, _ in documents] == [3, 3]


def test_stale_vectors_are_pruned_when_an_entry_changes(session: StorySession):
    with_embeddings(session)
    list(session.send(turn("First.")))

    session.bundle.lore[0].content = "Rewritten."
    list(session.send(turn("Second.")))

    assert len(session.bundle.embeddings) == 3


def test_the_query_is_embedded_as_a_query(session: StorySession):
    """Asymmetric models need the two sides marked differently."""
    backend = with_embeddings(session)
    list(session.send(turn("I ask about the accord.")))

    assert any(as_query for _, as_query in backend.calls)


def test_a_failing_endpoint_falls_back_to_keywords_silently(session: StorySession):
    backend = with_embeddings(session)
    backend.error = ProviderError("embeddings endpoint down")

    list(session.send(turn("The accord was signed here.")))

    report = session.last_retrieval
    assert report is not None
    assert report.used_embeddings is False
    assert "down" in (report.fallback_reason or "")
    # The keyword path still found it, and the turn went through.
    assert [e.title for e in report.entries] == ["The Sundering Accord"]
    assert session.nodes[-1].kind == "assistant"


def test_with_no_endpoint_configured_retrieval_is_keywords_only(session: StorySession):
    selecting(session)

    list(session.send(turn("Mind the lock.")))

    report = session.last_retrieval
    assert report is not None
    assert report.used_embeddings is False
    # Not a failure, so nothing to report to the user.
    assert report.fallback_reason is None
    assert [e.title for e in report.entries] == ["Calder ironwork"]


def test_assembling_for_the_inspector_never_embeds(session: StorySession):
    """The inspector runs on the GUI thread; it must not block on the network."""
    backend = with_embeddings(session)

    session.assemble(turn("The accord."))

    assert backend.calls == []
    assert session.last_prompt is not None


def test_a_dimension_change_drops_the_cache_rather_than_crashing(session: StorySession):
    backend = with_embeddings(session)
    list(session.send(turn("First.")))

    # Same model id, different width — what a `dimensions` change looks like.
    backend.dimensions = 32
    report = session.retrieve(turn("Second."), session.path())

    assert report.used_embeddings is False
    assert "dimensions" in (report.fallback_reason or "")


def test_regenerating_keeps_a_per_turn_length_override(session: StorySession):
    """Otherwise a regenerated take silently reverts to the story default."""
    session.provider = MockChatProvider(["first take", "second take"])
    list(
        session.send(
            TurnRequest(
                speaker_id="char-serrik",
                user_text="Onward.",
                controlled_character_id="char-serrik",
                response_style="literary",
            )
        )
    )
    list(session.regenerate())

    resent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "300 to 600 words" in resent
    assert session.nodes[0].meta.response_style == "literary"


def test_regenerating_keeps_per_turn_custom_wording(session: StorySession):
    session.provider = MockChatProvider(["a", "b"])
    list(
        session.send(
            TurnRequest(speaker_id="char-serrik", user_text="Onward.", length_hint="one haiku")
        )
    )
    list(session.regenerate())

    resent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "one haiku" in resent


def test_an_old_story_loads_with_the_adaptive_default(tmp_path: Path):
    """Stories saved before presets existed have no response_style on disk."""
    from sealedlore.models.story import Story

    story = Story.model_validate({"title": "Old", "style": {"length_target": None}})
    assert story.style.response_style == "adaptive"


def test_assembled_prompt_is_kept_for_the_inspector(session: StorySession):
    list(session.send(turn()))
    assert session.last_prompt is not None
    assert session.last_prompt.budget.total > 0
    assert session.last_prompt.messages[-1].role == "user"


# --- deleting messages -------------------------------------------------------


def test_deleting_takes_everything_after_the_message(session: StorySession, tmp_path: Path):
    history = grow(session, 3)
    cut = history[2]

    deletion = session.delete_from(cut.id)

    assert deletion.on_path == 4
    assert [node.id for node in session.nodes] == [node.id for node in history[:2]]
    assert session.story.active_leaf_id == history[1].id
    assert history[1].children == []
    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert [node.id for node in reloaded.nodes] == [node.id for node in history[:2]]


def test_deleting_a_reply_leaves_its_turn_ready_to_regenerate(session: StorySession):
    list(session.send(turn()))
    user_node, reply = session.nodes

    session.delete_from(reply.id)
    assert session.story.active_leaf_id == user_node.id

    list(session.regenerate())
    assert [node.kind for node in session.path()] == ["user", "assistant"]


def test_deleting_counts_other_takes_below_the_message(session: StorySession):
    list(session.send(turn()))
    list(session.regenerate())
    user_node = session.nodes[0]

    deletion = session.deletion_for(user_node.id)

    assert deletion.on_path == 2
    assert deletion.other_branches == 1


def test_deleting_the_first_message_empties_the_story(session: StorySession):
    list(session.send(turn()))

    session.delete_from(session.nodes[0].id)

    assert session.nodes == []
    assert session.story.active_leaf_id is None
    assert session.path() == []


def test_deleting_archived_prose_drops_its_summary(session: StorySession):
    history = grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))
    assert session.summaries

    deletion = session.delete_from(history[5].id)

    assert session.summaries == []
    assert len(deletion.summaries) == 1
    # What is left goes to the model verbatim again.
    assert session.split().verbatim == tuple(session.path())


def test_deleting_after_a_summary_keeps_it(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    list(session.send(turn()))
    summary = session.summaries[0]

    session.delete_from(session.path()[-1].id)

    assert session.summaries == [summary]


def test_the_api_log_survives_a_deletion(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    before = read_api_log(session.story.id, root=tmp_path)

    session.delete_from(session.nodes[0].id)

    assert read_api_log(session.story.id, root=tmp_path) == before


# --- asides ------------------------------------------------------------------


def test_asking_saves_an_aside_not_a_node(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    leaf = session.story.active_leaf_id
    session.provider = MockChatProvider(["The length rule left no room for it."])

    list(session.ask("Why did nobody react?"))

    assert len(session.nodes) == 2
    assert session.story.active_leaf_id == leaf
    (aside,) = session.asides_at(leaf)
    assert aside.question == "Why did nobody react?"
    assert aside.answer == "The length rule left no room for it."
    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert reloaded.asides == [aside]


def test_an_aside_never_reaches_the_story(session: StorySession):
    list(session.send(turn()))
    session.provider = MockChatProvider(["Because of the budget.", "The door holds."])
    list(session.ask("Why did nobody react?"))

    list(session.send(turn("I try again.")))

    sent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "Why did nobody react?" not in sent
    assert "Because of the budget." not in sent


def test_a_follow_up_sees_the_earlier_question(session: StorySession):
    list(session.send(turn()))
    session.provider = MockChatProvider(["First answer.", "Second answer."])
    list(session.ask("First question?"))

    list(session.ask("And then?"))

    sent = session.provider.requests[-1].messages[-1].text
    assert "The author asked: First question?" in sent
    assert "You answered: First answer." in sent


def test_asking_is_logged_and_marked_as_an_aside(session: StorySession, tmp_path: Path):
    list(session.ask("What is this story about?"))

    entries = read_api_log(session.story.id, root=tmp_path)
    assert [entry["kind"] for entry in entries] == ["request", "response"]
    assert all(entry["aside"] for entry in entries)
    # Asked before the story began: anchored at no node.
    assert session.asides_at(None)[0].question == "What is this story about?"


def test_a_failed_question_leaves_no_empty_aside(session: StorySession):
    session.provider = MockChatProvider(error=ProviderError("down"))

    with pytest.raises(ProviderError):
        list(session.ask("Anyone there?"))

    assert session.bundle.asides == []


def test_deleting_a_message_takes_its_asides(session: StorySession):
    list(session.send(turn()))
    list(session.ask("Why?"))

    deletion = session.delete_from(session.nodes[0].id)

    assert len(deletion.asides) == 1
    assert session.bundle.asides == []


# --- beginning a story from its setup ----------------------------------------


def test_begin_sends_the_opening_as_a_director_turn(session: StorySession):
    session.story.scene.present_character_ids = []
    session.story.setup.opening_text = "The keep's gate stands open."

    list(session.begin("char-serrik"))

    opening, reply = session.path()
    assert opening.speaker_id == "__director__"
    assert opening.content == "The keep's gate stands open."
    assert opening.meta.controlled_character_id == "char-serrik"
    assert reply.kind == "assistant"
    assert session.story.held_character_id == "char-serrik"
    assert "char-serrik" in session.story.scene.present_character_ids
    tail = session.provider.requests[-1].messages[-1].text
    assert "belongs to the author" in tail


def test_an_opening_as_written_costs_no_call(session: StorySession):
    session.story.setup.opening_text = "Rain on the gatehouse roof."
    session.story.setup.opening_mode = "as_written"

    list(session.begin("char-serrik"))

    (opening,) = session.path()
    assert opening.kind == "assistant" and opening.content == "Rain on the gatehouse roof."
    assert session.provider.requests == []

    list(session.send(turn()))
    first = session.provider.requests[-1].messages[1]
    assert first.role == "user" and first.text.startswith("[Begin the story.]")


def test_with_no_opening_the_author_writes_first(session: StorySession):
    list(session.begin("char-serrik"))

    assert session.nodes == []
    assert session.story.held_character_id == "char-serrik"


def test_a_story_can_only_begin_once(session: StorySession):
    list(session.send(turn()))

    with pytest.raises(ValueError, match="already begun"):
        list(session.begin(None))


# --- supporting characters ------------------------------------------------------

FOUND = (
    '[{"name": "Corporal Patel", "aliases": ["Patel"], "canon": null, '
    '"description": "A nervous security officer."}]'
)


def test_a_scan_keeps_new_names_as_suggestions(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    session.provider = MockChatProvider([FOUND])

    notices = list(session.scan_recent())

    (suggestion,) = session.character_suggestions
    assert suggestion.name == "Corporal Patel"
    assert "Corporal Patel" in notices[0].text
    assert session.unreported_usage
    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert reloaded.supporting.suggestions[0].name == "Corporal Patel"
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert "characters_request" in kinds and "characters_response" in kinds


def test_accepting_makes_a_supporting_card_not_a_cast_member(session: StorySession):
    list(session.send(turn()))
    session.provider = MockChatProvider([FOUND])
    list(session.scan_recent())

    card = session.accept_suggestion(session.character_suggestions[0].id)

    assert card in session.supporting and card not in session.cast
    assert card.origin == "model"
    assert session.character_suggestions == []


def test_a_supporting_card_rides_in_the_tail_only_when_mentioned(session: StorySession):
    session.supporting.append(
        Character(name="Corporal Patel", summary="A nervous security officer.", origin="model")
    )
    session.provider = MockChatProvider(["The corridor is quiet."])

    list(session.send(turn("I check the corridor.")))
    quiet = session.provider.requests[-1].messages
    list(session.send(turn("I hand Patel back his badge.")))
    named = session.provider.requests[-1].messages

    assert "# SUPPORTING CHARACTERS" not in "\n".join(m.text for m in quiet)
    assert "A nervous security officer." in named[-1].text
    # Never in the cached system block, whatever the turn.
    assert "Patel" not in named[0].text


def test_supporting_characters_are_not_on_the_roster(session: StorySession):
    session.supporting.append(Character(name="Corporal Patel", origin="model"))
    session.provider = MockChatProvider(["Patel stepped forward and saluted."])

    list(session.send(turn("I hand Patel back his badge.")))

    scene = session.last_prompt.section("tail.scene").text
    assert "Patel" not in scene
    # Not cast, so not validated as absent either.


def test_a_dismissed_name_is_not_suggested_again(session: StorySession):
    list(session.send(turn()))
    session.provider = MockChatProvider([FOUND, FOUND])
    list(session.scan_recent())
    session.dismiss_suggestion(session.character_suggestions[0].id)

    list(session.scan_recent())

    assert session.character_suggestions == []
    assert "Not to be listed" in session.provider.requests[-1].messages[-1].text


def test_promote_and_demote_move_a_card_between_tiers(session: StorySession):
    card = Character(name="Corporal Patel", origin="model")
    session.supporting.append(card)

    session.promote(card.id)
    assert card in session.cast and card not in session.supporting

    session.story.scene.present_character_ids.append(card.id)
    session.demote(card.id)
    assert card in session.supporting and card not in session.cast
    assert card.id not in session.story.scene.present_character_ids


def test_the_held_character_cannot_be_demoted(session: StorySession):
    session.story.held_character_id = "char-serrik"
    with pytest.raises(ValueError, match="holding"):
        session.demote("char-serrik")


def test_archival_looks_for_new_characters_and_never_fails_the_turn(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    # Summary, then an unparseable character reply, then the turn itself.
    session.provider = MockChatProvider(["A chapter.", "no list here", "The door holds."])

    notices = [e.text for e in session.send(turn()) if isinstance(e, SessionNotice)]

    assert any("Couldn't check the archived turns" in text for text in notices)
    assert session.nodes[-1].content == "The door holds."


def test_archival_suggestions_can_be_switched_off(session: StorySession):
    grow(session, 14)
    session.story.defaults.context_token_budget = 4_000
    session.config.suggest_characters = False
    session.provider = MockChatProvider(["A chapter.", "The door holds."])

    list(session.send(turn()))

    assert session.nodes[-1].content == "The door holds."
    assert len(session.provider.requests) == 2


# --- dice (§4.2) -------------------------------------------------------------------


def dice_turn(text: str = "I pick the lock.", **kwargs) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik",
        user_text=text,
        controlled_character_id="char-serrik",
        agency_mode="dice",
        **kwargs,
    )


def test_a_dice_turn_is_rolled_and_recorded_on_both_nodes(session: StorySession):
    session.cast[0].competence.tiers = {"lockpicking": "expert"}
    session.rng = random.Random(3)

    list(session.send(dice_turn(dice_domain="lockpicking", difficulty="hard")))

    user_node, reply = session.path()
    roll = user_node.meta.roll
    assert roll is not None and roll.tier == "expert" and roll.difficulty == "hard"
    assert reply.meta.roll == roll
    assert user_node.meta.dice_domain == "lockpicking"
    tail = session.provider.requests[-1].messages[-1].text
    assert f"Rolled {roll.value} on a d100" in tail
    assert "Serrik Vaun is expert at lockpicking" in tail


def test_regenerate_keeps_the_roll_and_reroll_replaces_it(session: StorySession):
    session.rng = random.Random(1)
    list(session.send(dice_turn()))
    first = session.path()[0].meta.roll

    list(session.regenerate())
    assert session.path()[-1].meta.roll == first
    assert f"Rolled {first.value} on a d100" in session.provider.requests[-1].messages[-1].text

    session.rng = random.Random(99)
    list(session.regenerate(reroll=True))
    second = session.path()[0].meta.roll
    assert second != first
    assert session.path()[-1].meta.roll == second


def test_direction_and_narration_are_never_rolled(session: StorySession):
    direction = TurnRequest(
        speaker_id="__director__",
        user_text="A storm rolls in.",
        controlled_character_id="char-serrik",
        agency_mode="dice",
    )
    list(session.send(direction))
    assert session.path()[0].meta.roll is None
    assert "nothing was rolled" in session.provider.requests[-1].messages[-1].text


def test_no_held_character_means_no_roll(session: StorySession):
    list(session.send(TurnRequest(speaker_id="char-serrik", user_text="x", agency_mode="dice")))
    assert session.path()[0].meta.roll is None


def test_competence_is_inferred_but_not_applied(session: StorySession):
    session.provider = MockChatProvider(['{"lockpicking": "expert", "tact": "poor"}'])

    tiers = session.infer_competence("char-serrik")

    assert tiers == {"lockpicking": "expert", "tact": "poor"}
    assert session.cast[0].competence.tiers == {}
    assert session.unreported_usage


def test_archival_keeps_going_until_the_prompt_is_well_under_the_budget(session: StorySession):
    """Human testing: stopping at the budget put the story back over it in 4 turns."""
    grow(session, 30)
    session.story.defaults.context_token_budget = 8_000
    session.config.archive_target_ratio = 0.5

    list(session.send(turn()))

    # Several chapters in one run, rather than one per turn for the next ten.
    assert len(session.summaries) > 1
    # Down to the target, or as far as whole chunks can take it: archival never
    # splits a chunk to hit the number.
    prompt = session.assemble(turn())
    assert prompt.full_total <= 4_000 or session.next_chunk() == []


def test_a_quiet_stretch_follows_an_archival_run(session: StorySession):
    """The point of the target: the turns after an archive don't archive again."""
    grow(session, 30)
    session.story.defaults.context_token_budget = 8_000

    list(session.send(turn()))
    after_first = len(session.summaries)

    for _ in range(4):
        list(session.send(turn()))

    assert len(session.summaries) == after_first


def test_archival_stops_at_the_target_rather_than_summarising_everything(
    session: StorySession,
):
    grow(session, 30)
    session.story.defaults.context_token_budget = 40_000
    session.config.archive_target_ratio = 0.5

    list(session.send(turn()))

    # Nowhere near the budget, so nothing is archived at all.
    assert session.summaries == []


def test_a_higher_target_archives_less(session: StorySession):
    grow(session, 30)
    session.story.defaults.context_token_budget = 4_000
    session.config.archive_target_ratio = 0.95

    list(session.send(turn()))

    shallow = len(session.summaries)
    assert 0 < shallow <= MAX_AUTO_ARCHIVE_ROUNDS


# --- regenerate with the author's current choices -------------------------


def test_regenerate_follows_the_shape_the_author_has_set_now(session: StorySession):
    """Human testing: asking for a longer passage and pressing Regenerate

    came back at exactly the old length, because the take was rebuilt from
    what the turn was sent with.
    """
    session.provider = MockChatProvider(["short one", "longer one"])
    list(session.send(turn()))

    list(session.regenerate(shape=TurnShape(response_style="descriptive")))

    sent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "Length, for this passage only" in sent
    user_node = next(node for node in session.nodes if node.kind == "user")
    # Written back, so the next take reproduces this one and not the first.
    assert user_node.meta.response_style == "descriptive"


def test_regenerate_without_a_shape_reproduces_the_turn(session: StorySession):
    session.provider = MockChatProvider(["a", "b"])
    list(session.send(replace(turn(), response_style="brief")))

    list(session.regenerate())

    user_node = next(node for node in session.nodes if node.kind == "user")
    assert user_node.meta.response_style == "brief"


def test_leaving_dice_mode_on_a_retake_drops_the_roll(session: StorySession):
    session.provider = MockChatProvider(["a", "b"])
    list(session.send(replace(turn(), agency_mode="dice", dice_domain=None)))
    user_node = next(node for node in session.nodes if node.kind == "user")
    assert user_node.meta.roll is not None

    list(session.regenerate(shape=TurnShape(agency_mode="contested")))

    assert user_node.meta.roll is None
    sent = "\n".join(message.text for message in session.provider.requests[-1].messages)
    assert "Resolved outcome" not in sent


def test_moving_to_dice_on_a_retake_rolls(session: StorySession):
    session.provider = MockChatProvider(["a", "b"])
    list(session.send(turn()))

    list(session.regenerate(shape=TurnShape(agency_mode="dice")))

    user_node = next(node for node in session.nodes if node.kind == "user")
    assert user_node.meta.roll is not None


# --- scene freshness ------------------------------------------------------


def test_the_scene_age_counts_passages_since_it_was_set(session: StorySession):
    grow(session, 5)
    assert session.scene_age() == 10  # never set: as old as the story

    session.set_scene(session.story.scene)
    assert session.scene_age() == 0

    grow(session, 2)
    assert session.scene_age() == 4


def test_suggest_scene_reads_the_recent_prose(session: StorySession):
    grow(session, 3)
    session.provider = MockChatProvider(
        ['{"location": "The infirmary", "present": ["Serrik Vaun"]}']
    )

    proposal = session.suggest_scene()

    assert proposal.location == "The infirmary"
    assert proposal.present_character_ids == ["char-serrik"]
    # Nothing is applied until the author accepts it.
    assert session.story.scene.location != "The infirmary"
    # The call is logged and its cost picked up by the window.
    assert session.unreported_usage


def test_suggest_scene_needs_a_story_to_read(session: StorySession):
    with pytest.raises(ValueError, match="no story yet"):
        session.suggest_scene()


def test_supporting_cards_stay_warm_for_ten_turns(session: StorySession):
    """They used to share the lore query's three turns, so a character who went

    unmentioned for a turn left the prompt and tended to stay gone.
    """
    gus = Character(id="char-gus", name="Gus", summary="A barkeep.")
    session.bundle.supporting.characters.append(gus)
    nodes = make_exchange(12)
    nodes[0].content = "Gus polished the bar and said nothing."
    session.nodes.extend(nodes)
    session.story.active_leaf_id = nodes[-1].id

    session.config.supporting_recall_turns = 3
    assert session.supporting_for("I sit down.", session.path()) == []

    session.config.supporting_recall_turns = 30
    assert session.supporting_for("I sit down.", session.path()) == [gus]


def test_a_passage_cut_off_at_the_limit_says_so():
    from sealedlore.engine.session import cut_off_notice
    from sealedlore.providers.base import StreamCompleted

    completed = StreamCompleted(
        finish_reason="length", raw_usage={"completion_tokens_details": {"reasoning_tokens": 1246}}
    )
    notice = cut_off_notice(1500, completed)
    assert "1,500" in notice and "1,246" in notice and "Max tokens" in notice
    assert "reasoning" not in cut_off_notice(1500, StreamCompleted(finish_reason="length"))


# --- the lore layout: whole while small, selected once large ----------------


def test_a_small_lorebook_is_sent_whole_in_the_system_block(session: StorySession):
    backend = with_embeddings(session)
    session.config.lore_whole_share = 0.1

    list(session.send(turn("Nothing here resembles any entry.")))

    report = session.last_retrieval
    assert report.mode == "whole" and report.entries == ()
    lore = session.last_prompt.section("system.lore").text
    assert all(entry.title in lore for entry in stock_lore())
    assert session.last_prompt.section("tail.lore") is None
    # Nothing to choose, so nothing embedded.
    assert backend.calls == []


def test_the_whole_lorebook_does_not_change_the_system_block_between_turns(
    session: StorySession,
):
    session.bundle.lore = stock_lore()
    first = session.assemble(turn("The accord."))
    second = session.assemble(turn("Mind the lock, and the ironwork."))
    assert first.messages[0] == second.messages[0]


def test_a_lorebook_past_its_share_of_the_budget_is_selected_from(session: StorySession):
    session.bundle.lore = stock_lore()
    layout = session.lore_layout()
    assert layout.mode == "whole"

    session.story.defaults.context_token_budget = int(layout.tokens / 0.1) - 100
    layout = session.lore_layout()
    assert layout.mode == "select"
    assert [e.title for e in layout.standing] == ["House Vaun"]
    assert "House Vaun" not in [e.title for e in layout.selectable]


def test_hidden_lore_never_stands_in_the_system_block(session: StorySession):
    session.bundle.lore = stock_lore()
    session.bundle.lore[0].plot_hidden = True
    session.hidden_ids = lambda *args, **kwargs: {"lore-accord"}  # type: ignore[method-assign]

    prompt = session.assemble(turn("The accord."))

    assert "Sundering Accord" not in prompt.section("system.lore").text
    assert "Calder ironwork" in prompt.section("system.lore").text
