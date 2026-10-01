"""Private scenes: nothing from the scene reaches another model, or (in memory
mode) the disk.

The leak test is the one that matters: a scene whose every message carries a
marker, then every side call the app makes, and the marker must turn up in no
payload but the private model's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.private_prompt import (
    SECTION_PRIVATE_RULES,
    build_private_condense_messages,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import StorySession, TurnRequest
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ProviderError
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import load_story_bundle, read_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario

MARKER = "ZQX-PRIVATE"
PLOT = (Path(__file__).resolve().parent.parent / "SampleStories" / "Doomsville.md").read_text()


def make_config() -> Config:
    return Config(
        providers=[
            ProviderConfig(
                name="nano",
                base_url="https://nano-gpt.com/api/v1",
                model="anthropic/claude-sonnet-4.6",
            )
        ],
        active_provider_name="nano",
        private_provider=ProviderConfig(
            name="private", base_url="http://localhost:11434/v1", model="local/model"
        ),
        private_budget=6_000,
        min_cacheable_tokens=1,
        archive_chunk_turns=2,
        lore_selector="picker",
        lore_whole_share=0.01,
        suggest_characters=True,
        # Recall at its widest, every candidate taken: the archived prose
        # itself goes to the embeddings endpoint and into the tail.
        recall="exchanges",
        recall_threshold=-1.0,
    )


def build(tmp_path: Path, *, keep: str = "memory"):
    bundle = bundle_from_scenario(parse_plot_markdown(PLOT).scenario)
    bundle.story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle.story.defaults.context_token_budget = 2_500
    bundle.lore += [
        LoreEntry(title=f"Entry {i}", content=f"Old lore number {i} about the town.")
        for i in range(12)
    ]
    held = bundle.cast[0].id
    bundle.story.held_character_id = held
    save_story_bundle(bundle, root=tmp_path)
    main = MockChatProvider(["The town answers in its own time, slow and dusty."])
    session = StorySession(
        bundle,
        make_config(),
        main,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
        embeddings=MockEmbeddings(),
    )
    return session, main, held


def turn(held: str, text: str) -> TurnRequest:
    return TurnRequest(speaker_id=held, user_text=text, controlled_character_id=held)


def play(session: StorySession, request: TurnRequest) -> None:
    for _event in session.send(request):
        pass


def enter(session: StorySession, keep: str = "memory") -> MockChatProvider:
    private = MockChatProvider([f"A quiet reply that holds {MARKER} close."])
    session.enter_private(keep=keep, provider=private, model="local/model")
    return private


def everything_sent(main: MockChatProvider, embeddings: MockEmbeddings) -> str:
    return json.dumps(main.payloads) + json.dumps(embeddings.calls)


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_nothing_from_a_private_scene_reaches_another_model(tmp_path: Path, keep: str):
    session, main, held = build(tmp_path)
    for i in range(3):
        play(session, turn(held, f"I walk down the street, step {i}."))

    private = enter(session, keep)
    play(session, turn(held, f"I whisper {MARKER} to her."))
    play(session, turn(held, f"We share {MARKER} again."))
    private.responses = ["They spoke quietly and agreed to meet at dawn by the well."]
    summary = session.summarise_private()
    assert MARKER not in summary
    for _event in session.close_private(summary):
        pass

    # Every side path afterwards: turns (director, archival, reads, lore
    # picks, embeddings, merges, scans), an aside, the manual scans, the image
    # prompt writer and a settings review.
    for i in range(6):
        play(session, turn(held, f"At dawn I go to the well, step {i}."))
    for _event in session.ask("What happened last night?"):
        pass
    for call in (
        lambda: list(session.scan_recent()),
        session.suggest_scene,
        lambda: session.write_image_prompt(""),
    ):
        try:
            call()
        except ValueError:
            pass  # the mock's reply isn't JSON; the request went out, that's what counts
    try:
        session.review_settings("It's slow.", replies=4)
    except ValueError:
        pass  # the mock's reply isn't a review; the request went out

    assert main.payloads, "the side calls were made"
    assert any(MARKER in json.dumps(p) for p in private.payloads)
    assert MARKER not in everything_sent(main, session.embeddings)
    assert "agreed to meet at dawn" in json.dumps(main.payloads)

    on_disk = "".join(
        path.read_text(errors="ignore")
        for path in (tmp_path / "stories" / session.story.id).rglob("*")
        if path.is_file()
    )
    if keep == "memory":
        assert MARKER not in on_disk
    else:
        assert MARKER in on_disk


def test_while_private_nothing_else_is_called(tmp_path: Path):
    session, main, held = build(tmp_path)
    play(session, turn(held, "I arrive."))
    before = len(main.payloads)
    private = enter(session)
    play(session, turn(held, f"{MARKER} one"))
    play(session, turn(held, f"{MARKER} two"))
    for _event in session.regenerate():
        pass
    assert len(main.payloads) == before
    assert len(private.payloads) == 3
    embeddings_before = len(session.embeddings.calls)
    play(session, turn(held, f"{MARKER} three"))
    assert len(session.embeddings.calls) == embeddings_before
    with pytest.raises(ValueError, match="private scene"):
        session.suggest_scene()


def test_the_private_prompt_uses_its_budget_rules_and_reminders(tmp_path: Path):
    session, _main, held = build(tmp_path)
    for i in range(3):
        play(session, turn(held, f"Step {i}."))
    private = enter(session)
    play(session, turn(held, "I close the door."))
    sent = private.requests[-1].messages
    system = sent[0].text
    assert DEFAULT_TEXTS["private.rules"].strip()[:60] in system
    assert "Never write" in system
    # The facts and the per-turn reminders are app-owned and always there.
    last = sent[-1].text
    assert "REMEMBER" in last
    assert session.cast[0].name in last

    # Custom rules replace only the rules.
    session.story.private_prompt.mode = "custom"
    session.story.private_prompt.custom = "Write like a noir novel."
    session.story.private_prompt.notes = "Slow and quiet."
    play(session, turn(held, "I light a cigarette."))
    sent = private.requests[-1].messages
    assert "Write like a noir novel." in sent[0].text
    assert "Slow and quiet." in sent[0].text
    assert "REMEMBER" in sent[-1].text
    names = [s.name for s in session.last_prompt.sections]
    assert SECTION_PRIVATE_RULES in names and "system.engine_rules" not in names


def test_a_private_scene_keeps_the_story_person_and_tense(tmp_path: Path):
    # Custom rules replace only the rules: the REMEMBER line still carries the
    # person, and the summary and rolling summary, which join the story as
    # passages, are told in it.
    session, _main, held = build(tmp_path)
    session.story.style.person = "second"
    session.story.style.tense = "present"
    session.story.private_prompt.mode = "custom"
    session.story.private_prompt.custom = "Write like a noir novel."
    private = enter(session)
    play(session, turn(held, "I close the door."))
    name = session.cast[0].name
    assert f'REMEMBER: Write in the second person and the present tense: {name} is "you"' in (
        private.requests[-1].messages[-1].text
    )
    private.responses = ["A calm summary."]
    session.summarise_private()
    summary_rules = private.payloads[-1]["messages"][0]["content"]
    assert f'in the present tense, in the second person ({name} is "you")' in summary_rules
    condense = build_private_condense_messages(
        None, "text", [name], "first", held=name, tense="past"
    )
    assert f'in the past tense, in the first person ({name} is "I")' in condense[0].text


def test_the_private_system_block_is_stable_across_turns(tmp_path: Path):
    session, _main, held = build(tmp_path)
    private = enter(session)
    play(session, turn(held, "One."))
    play(session, turn(held, "Two."))
    first, second = private.requests[-2].messages[0], private.requests[-1].messages[0]
    assert first.parts[0].text == second.parts[0].text


def test_the_summary_sees_only_the_scene(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "EARLIER-TEXT before the scene."))
    private = enter(session)
    play(session, turn(held, f"{MARKER} inside"))
    private.responses = ["A calm summary."]
    session.summarise_private()
    request = json.dumps(private.payloads[-1])
    assert MARKER in request
    assert "EARLIER-TEXT" not in request
    assert "keep anything intimate" in request.lower() or "safe for work" in request.lower()


def test_memory_mode_keeps_the_summary_and_drops_the_scene(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    before_leaf = session.story.active_leaf_id
    private = enter(session)
    play(session, turn(held, f"{MARKER} a"))
    # Mid-scene, the disk holds only the story up to the scene.
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert saved.story.active_leaf_id == before_leaf
    assert MARKER not in json.dumps([n.model_dump() for n in saved.nodes])

    private.responses = ["They made peace."]
    for _event in session.close_private(session.summarise_private()):
        pass
    saved = load_story_bundle(session.story.id, root=tmp_path)
    summary = next(n for n in saved.nodes if n.meta.private_summary_of)
    assert summary.parent_id == before_leaf
    assert summary.id in next(n for n in saved.nodes if n.id == before_leaf).children
    assert saved.story.active_leaf_id == summary.id

    # Reopened, the story shows the summary and none of the scene.
    again = StorySession(
        saved, make_config(), MockChatProvider(["x"]), root=tmp_path, learn_corrections=False
    )
    assert [n.id for n in again.full_path()][-1] == summary.id
    assert not any(n.meta.private_span for n in again.nodes)

    # The log kept what it cost, and nothing it said.
    log = read_api_log(session.story.id, root=tmp_path)
    assert MARKER not in json.dumps(log)
    kinds = {entry["kind"] for entry in log}
    assert {"private_request", "private_response"} <= kinds
    assert any(line.label == "Private scenes" for line in usage_report(log).lines)


def test_an_unfinished_memory_scene_is_gone_after_a_restart(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    before_leaf = session.story.active_leaf_id
    enter(session)
    play(session, turn(held, f"{MARKER} a"))
    # The app dies here: whatever was saved is all there is.
    saved = load_story_bundle(session.story.id, root=tmp_path)
    again = StorySession(
        saved, make_config(), MockChatProvider(["x"]), root=tmp_path, learn_corrections=False
    )
    assert not again.in_private
    assert again.story.private_spans[-1].status == "discarded"
    assert again.story.active_leaf_id == before_leaf
    assert again.private_notice


def test_a_disk_scene_survives_a_restart_and_stays_out_of_model_paths(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    enter(session, keep="disk")
    play(session, turn(held, f"{MARKER} a"))
    saved = load_story_bundle(session.story.id, root=tmp_path)
    again = StorySession(
        saved, make_config(), MockChatProvider(["x"]), root=tmp_path, learn_corrections=False
    )
    assert again.in_private
    assert any(MARKER in n.content for n in again.full_path())
    assert not any(MARKER in n.content for n in again.path())


def test_discarding_goes_back_to_where_the_scene_began(tmp_path: Path):
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    before_leaf = session.story.active_leaf_id
    enter(session)
    play(session, turn(held, f"{MARKER} a"))
    session.discard_private()
    assert not session.in_private
    assert session.story.active_leaf_id == before_leaf
    assert not any(n.meta.private_span for n in session.nodes)
    play(session, turn(held, "After."))
    assert MARKER not in json.dumps(main.payloads)


def test_the_handoff_reads_only_what_came_before(tmp_path: Path):
    session, main, held = build(tmp_path)
    for i in range(4):
        play(session, turn(held, f"Long ago, step {i}."))
    private = enter(session)
    main.responses = ["The story so far, condensed."]
    text = session.write_handoff()
    assert text == "The story so far, condensed."
    play(session, turn(held, f"{MARKER} now"))
    system = private.requests[-1].messages[0].text
    assert "The story so far, condensed." in system
    assert MARKER not in json.dumps(main.payloads)


def test_a_private_passage_can_only_be_retaken_inside_its_scene(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session)
    play(session, turn(held, f"{MARKER} a"))
    private_reply = session.full_path()[-1].id
    private.responses = ["Closed."]
    for _event in session.close_private(session.summarise_private()):
        pass
    with pytest.raises(ValueError, match="ended"):
        for _event in session.regenerate(private_reply):
            pass


def test_a_picture_in_a_private_scene_is_prompted_by_the_private_model(tmp_path: Path):
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session)
    play(session, turn(held, f"{MARKER} by the window"))
    before = len(main.payloads)
    private.responses = ['{"references": [], "prompt": "Two figures by a rain-streaked window."}']
    draft = session.write_image_prompt("")
    assert draft.prompt == "Two figures by a rain-streaked window."
    assert len(main.payloads) == before
    assert MARKER in json.dumps(private.payloads[-1])
    # Memory only: the writer's call is logged without what it said.
    log = read_api_log(session.story.id, root=tmp_path)
    assert MARKER not in json.dumps(log)


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_a_long_story_after_the_scene_never_brings_it_back(tmp_path: Path, keep: str):
    """Long after the scene: many turns, archival, merged parts, the ledger,
    every chapter rebuilt, scans, asides at the scene's own anchor, a review
    with the scene's passages marked as evidence. The marker never leaves."""
    session, main, held = build(tmp_path)
    session.config.story_ledger = True
    session.config.summary_merge_ratio = 0.05
    session.config.summary_merge_keep = 1
    for i in range(3):
        play(session, turn(held, f"Early step {i}."))
    start = session.story.active_leaf_id

    private = enter(session, keep)
    # An aside before the scene's first message is anchored to a public passage.
    for _event in session.ask(f"Before we start: {MARKER}?"):
        pass
    play(session, turn(held, f"{MARKER} one"))
    for _event in session.ask(f"Mid-scene {MARKER}?"):
        pass
    play(session, turn(held, f"{MARKER} two"))
    private_ids = [n.id for n in session.full_path() if n.meta.private_span]
    private.responses = ["They talked, and parted as friends."]
    for _event in session.close_private(session.summarise_private()):
        pass

    for i in range(30):
        play(session, turn(held, f"Long after, step {i}."))
    assert len(session.summaries) >= 5
    for summary in list(session.summaries):
        try:
            session.rebuild_summary(summary.id, force=True)
        except ValueError:
            pass
    for call in (
        session.merge_now,
        lambda: list(session.scan_recent()),
        session.suggest_scene,
        lambda: session.write_image_prompt("the moment"),
    ):
        try:
            call()
        except ValueError:
            pass
    for node_id in private_ids:
        session.mark_for_review(node_id)
    try:
        session.review_settings("Too slow.", replies=None, whole_prompt=True)
    except ValueError:
        pass  # the mock's reply isn't a review; the request went out
    # An aside at the passage the scene began from, as after a discard.
    session.story.active_leaf_id = start
    for _event in session.ask("What happened here?"):
        pass

    assert MARKER not in everything_sent(main, session.embeddings)
    assert all(
        MARKER not in s.content and MARKER not in (s.ledger or "") for s in session.summaries
    )
    story_dir = tmp_path / "stories" / session.story.id
    on_disk = "".join(p.read_text(errors="ignore") for p in story_dir.rglob("*") if p.is_file())
    assert (MARKER in on_disk) == (keep == "disk")


def test_a_memory_scene_writes_only_content_free_log_kinds(tmp_path: Path):
    """The constraint every later change must keep: in a memory-only scene the
    API log sees the model's name and the usage, nothing else, from the turn,
    an aside, a regenerate and the exit summary alike."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before the scene."))
    before = len(read_api_log(session.story.id, root=tmp_path))
    enter(session)
    play(session, turn(held, f"In the scene {MARKER}."))
    for _ in session.ask(f"Who is here? {MARKER}"):
        pass
    for _ in session.regenerate():
        pass
    session.summarise_private()
    entries = read_api_log(session.story.id, root=tmp_path)[before:]
    assert entries, "the scene made calls"
    assert {entry["kind"] for entry in entries} <= {"private_request", "private_response"}
    for entry in entries:
        assert MARKER not in json.dumps(entry)
        if entry["kind"] == "private_request":
            assert set(entry["payload"]) == {"model"}
        else:
            assert set(entry) <= {"id", "kind", "at", "request_log_ref", "usage"}


# --- a scene that outgrows the private budget ------------------------------------


def _long_scene(tmp_path: Path, keep: str = "memory"):
    """A tiny private budget and long turns: the scene outgrows it in a few turns."""
    session, main, held = build(tmp_path)
    session.config.private_budget = 2_600
    play(session, turn(held, "Before the scene."))
    private = enter(session, keep=keep)
    private.responses = [f"The private reply, {MARKER}, " + ("word " * 120)]
    return session, main, private, held


def test_a_private_scene_that_outgrows_the_budget_is_condensed_by_the_private_model(
    tmp_path: Path,
):
    session, main, private, held = _long_scene(tmp_path)
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    span = session.open_span
    assert span.scene_summary, "the oldest messages were condensed"
    assert span.scene_summary_through in {n.id for n in session.span_nodes(span)}
    # The condense request went to the private model and nobody else.
    condensed = [p for p in private.payloads if "THE SUMMARY SO FAR" in json.dumps(p)]
    assert condensed and not any("THE SUMMARY SO FAR" in json.dumps(p) for p in main.payloads)
    # The next prompt carries the summary, not the messages it stands in for.
    covered = set(session._condensed_ids(span, session.private_history(span)))
    assert covered
    prompt = session.private_assemble(turn(held, "Next."), session.private_history(span), span)
    sent = json.dumps([m.text for m in prompt.messages])
    assert span.scene_summary[:40] in sent
    first_covered = next(n for n in session.span_nodes(span) if n.id in covered)
    assert first_covered.content[:60] not in sent
    # In memory mode the rolling summary never reaches the disk.
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert all(s.scene_summary is None for s in saved.story.private_spans)
    assert MARKER not in json.dumps(saved.model_dump())


def test_a_disk_scene_keeps_its_rolling_summary(tmp_path: Path):
    session, _main, _private, held = _long_scene(tmp_path, keep="disk")
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert any(s.scene_summary for s in saved.story.private_spans)


def test_the_exit_summary_reads_the_condensed_scene_and_fits(tmp_path: Path):
    session, _main, private, held = _long_scene(tmp_path)
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    private.responses = ["They spoke, and agreed to meet at dawn."]
    session.summarise_private()
    request = private.requests[-1]
    text = json.dumps([m.text for m in request.messages])
    assert "WHAT CAME EARLIER IN THE SCENE" in text
    assert session.estimator.estimate(text, "local/model") <= session.config.private_budget


def test_a_retake_inside_the_scene_uses_the_condensed_window(tmp_path: Path):
    session, _main, private, held = _long_scene(tmp_path)
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    before = len(private.payloads)
    for _ in session.regenerate():
        pass
    sent = json.dumps(private.payloads[-1])
    assert session.open_span.scene_summary[:40] in sent
    assert len(private.payloads) >= before + 1


def test_a_summary_of_messages_taken_back_never_comes_back(tmp_path: Path):
    session, _main, private, held = _long_scene(tmp_path)
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    span = session.open_span
    covered = session._condensed_ids(span, session.private_history(span))
    assert covered
    span.scene_summary = "STALE-SUMMARY of messages about to be taken back."
    # Take back into the condensed part: the summary's last message is gone.
    second_turn = [n for n in session.span_nodes(span) if n.kind == "user"][1]
    assert second_turn.id in covered
    session.delete_from(second_turn.id)
    assert session._condensed_ids(span, session.private_history(span)) == []
    before = len(private.payloads)
    for i in range(4):
        play(session, turn(held, f"Again {i}: " + ("more " * 60)))
    private.responses = ["They spoke, and agreed to meet at dawn."]
    session.summarise_private()
    later = json.dumps(private.payloads[before:]) + json.dumps(
        [m.text for m in private.requests[-1].messages]
    )
    assert "STALE-SUMMARY" not in later


def test_the_condensed_part_stands_in_the_history_not_the_system_block(tmp_path: Path):
    from sealedlore.engine.session_private import CONDENSED_LEAD

    session, _main, _private, held = _long_scene(tmp_path)
    span = session.open_span
    play(session, turn(held, f"Private turn: {MARKER} " + ("more " * 60)))
    system_before = (
        session.private_assemble(turn(held, "Next."), session.private_history(span), span)
        .messages[0]
        .text
    )
    for i in range(5):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    assert span.scene_summary
    prompt = session.private_assemble(turn(held, "Next."), session.private_history(span), span)
    system, *history = [m.text for m in prompt.messages]
    assert system == system_before, "condensing never rewrites the cached system block"
    sent = "\n".join(history)
    assert CONDENSED_LEAD in sent and span.scene_summary[:40] in sent
    if "Before the scene." in sent:
        assert sent.index("Before the scene.") < sent.index(CONDENSED_LEAD)


class CutOffSummaries(MockChatProvider):
    """A private model whose summaries stop at their token limit (seen live:
    GLM 5.3 Flash spent the limit reasoning, and a summary ended mid-sentence)."""

    def stream(self, request):
        from dataclasses import replace

        from sealedlore.providers.base import StreamCompleted

        # Anything that isn't a story turn is a summary: condense or exit.
        cut = "TURN DIRECTIVES" not in json.dumps([m.text for m in request.messages])
        for event in super().stream(request):
            if cut and isinstance(event, StreamCompleted):
                event = replace(event, finish_reason="length")
            yield event


def test_a_cut_off_summary_is_never_kept_and_never_costs_the_turn(tmp_path: Path):
    from sealedlore.engine.session_plot import SessionNotice

    session, _main, held = build(tmp_path)
    session.config.private_budget = 2_600
    play(session, turn(held, "Before the scene."))
    private = CutOffSummaries([f"The private reply, {MARKER}, " + ("word " * 120)])
    session.enter_private(keep="memory", provider=private, model="local/model")
    notices = []
    for i in range(6):
        for event in session.send(turn(held, f"Private turn {i}: " + ("more " * 60))):
            if isinstance(event, SessionNotice):
                notices.append(event.text)
        assert session.full_path()[-1].kind == "assistant", "every turn still gets its passage"
    assert any("cut off" in notice for notice in notices), "a condense was tried and refused"
    assert session.open_span.scene_summary is None
    with pytest.raises(ProviderError, match="cut off"):
        session.summarise_private()


def test_a_refused_enclave_gets_nothing_from_a_private_scene(tmp_path: Path):
    """The window sets `tee_block` while a TEE/ model is attested, or when it
    was refused: neither a turn nor the summary reaches it."""
    from sealedlore.providers.tee import TeeRefused

    session, _main, held = build(tmp_path)
    private = enter(session)
    session.tee_block = "The attestation was refused: TEE/x's quote failed verification"
    with pytest.raises(TeeRefused, match="nothing was sent"):
        play(session, turn(held, f"{MARKER} whispered"))
    assert private.payloads == []
    assert not session.span_nodes(session.open_span), "the turn wasn't attached"
    session.tee_block = None
    play(session, turn(held, "Now it may go."))
    session.tee_block = "still being attested"
    with pytest.raises(TeeRefused):
        session.summarise_private()
    assert len(private.payloads) == 1
    session.discard_private()
    assert session.tee_block is None, "ending the scene lifts it"
