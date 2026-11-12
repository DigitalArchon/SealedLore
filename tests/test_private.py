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

from sealedlore.engine.picture_styles import picture_words
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
from sealedlore.models.image import GeneratedImage
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Usage
from sealedlore.providers.base import ProviderError
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import (
    load_config,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from sealedlore.storage.scenario import bundle_from_scenario

MARKER = "ZQX-PRIVATE"
PLOT = (Path(__file__).resolve().parent.parent / "SampleStories" / "Doomsville.md").read_text(
    encoding="utf-8"
)


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
        recall_in="everywhere",
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
    # A take steered by a note: the note is the scene's too.
    for _event in session.regenerate(note=f"Closer, and name {MARKER}."):
        pass
    assert session.full_path()[-1].meta.retake_note
    # And the passage written again with a change: the turn, the passage and
    # the change asked for all go to the scene's model.
    for _event in session.revise_passage(session.full_path()[-1].id, f"Add {MARKER} twice."):
        pass
    assert session.full_path()[-1].meta.revise_note
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
    # A take with a note and a passage changed by instruction, after the
    # scene: each sends the story's own turn and history again.
    for _event in session.regenerate(note="Slower."):
        pass
    for _event in session.revise_passage(session.path()[-1].id, "It is colder."):
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


def reopened(session: StorySession, tmp_path: Path) -> StorySession:
    """The story as the next launch opens it."""
    return StorySession(
        load_story_bundle(session.story.id, root=tmp_path),
        make_config(),
        MockChatProvider(["Reopened."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def ids_and_text(nodes) -> list[tuple[str, str]]:
    return [(node.id, node.content) for node in nodes]


def closed_memory_scene(tmp_path: Path):
    """A story, a scene kept in memory and closed with its summary."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session)
    play(session, turn(held, f"{MARKER} a"))
    private.responses = ["They agreed to meet at dawn."]
    for _event in session.close_private(session.summarise_private()):
        pass
    return session, main, held


def test_a_branch_from_a_memory_scenes_summary_is_saved_whole(tmp_path: Path):
    """New branch from here… on the summary puts its rewrite beside it, under
    the scene's last message, which is never saved: the story couldn't be
    opened again ("a missing parent reference")."""
    session, _main, _held = closed_memory_scene(tmp_path)
    summary = session.full_path()[-1]
    for _event in session.rewrite_from(summary.id, "They agreed to meet at dusk.", name="Dusk"):
        pass
    again = reopened(session, tmp_path)
    assert ids_and_text(again.path()) == ids_and_text(session.path())
    assert again.path()[-1].content == "They agreed to meet at dusk."
    assert not any(node.meta.private_span for node in again.nodes)


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_a_summary_rewritten_after_a_scene_s_unanswered_turn_never_sends_that_turn(
    tmp_path: Path, keep: str
):
    """A scene that ended on a turn with no reply has its summary under that
    turn, and so has a branch rewritten from the summary: read as a passage
    answering the turn, Continue sent the scene's turn to the story's model."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session, keep=keep)
    play(session, turn(held, "A first secret."))
    private.error = ProviderError("the private model is down")
    with pytest.raises(ProviderError):
        play(session, turn(held, f"{MARKER} unanswered"))
    private.error = None
    private.responses = ["They spoke."]
    for _event in session.close_private(session.summarise_private()):
        pass
    summary = session.full_path()[-1]
    for _event in session.rewrite_from(summary.id, "They spoke of other things.", name="Other"):
        pass
    rewritten = session.full_path()[-1]
    assert rewritten.meta.private_summary_of == summary.meta.private_summary_of
    assert not session.can_continue()

    # As 3.1.0b8 saved it: the rewrite marked as nothing.
    rewritten.meta.private_summary_of = None
    assert not session.can_continue()
    sent = len(main.payloads)
    with pytest.raises(ValueError, match="ended"):
        list(session.continue_passage())
    with pytest.raises(ValueError, match="ended"):
        list(session.regenerate())
    assert len(main.payloads) == sent
    assert MARKER not in json.dumps(main.payloads)


def shown_scene(session) -> list[str]:
    """The ended scenes' messages on the page: the story as the author reads it."""
    return [node.id for node in session.full_path() if node.meta.private_span]


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_a_scenes_summary_deleted_leaves_the_scene_in_the_story_as_read(tmp_path: Path, keep: str):
    """A private scene's messages are the story's to read and only kept from
    the models. Its summary deleted, the story stands on the scene's last
    message, and the next turn goes after it: attached under the last message
    a model reads, it took the scene off the page. A memory scene's messages
    are then saved around (they never reach the disk), and the story opens."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session, keep=keep)
    play(session, turn(held, f"{MARKER} a"))
    private.responses = ["They agreed to meet at dawn."]
    for _event in session.close_private(session.summarise_private()):
        pass
    scene = shown_scene(session)
    assert scene
    session.delete_from(session.full_path()[-1].id)
    assert session.story.active_leaf_id == scene[-1]

    play(session, turn(held, "Next morning."))
    assert shown_scene(session) == scene, "the scene stays on the page"
    assert session.full_path()[-3].id == scene[-1]  # then the turn and its passage
    assert not any(node.meta.private_span for node in session.path())
    assert MARKER not in json.dumps(main.payloads)

    again = reopened(session, tmp_path)
    assert ids_and_text(again.path()) == ids_and_text(session.path())
    assert shown_scene(again) == (scene if keep == "disk" else [])


@pytest.mark.parametrize("second", ["memory", "disk"])
def test_a_scene_begun_after_one_whose_summary_was_deleted_is_saved_whole(
    tmp_path: Path, second: str
):
    """The next scene begins after the ended one's last message, which stays
    on the page; one kept only in memory is never saved, and the story opened
    with an unknown message or a missing parent."""
    session, _main, held = closed_memory_scene(tmp_path)
    session.delete_from(session.full_path()[-1].id)
    scene = shown_scene(session)

    private = enter(session, keep=second)
    assert session.open_span.start_parent_id == scene[-1]
    play(session, turn(held, f"{MARKER} again"))
    assert shown_scene(session)[: len(scene)] == scene
    again = reopened(session, tmp_path)
    assert ids_and_text(again.path()) == ids_and_text(session.path())
    private.responses = ["They met again."]
    for _event in session.close_private(session.summarise_private()):
        pass
    play(session, turn(held, "Morning."))
    assert shown_scene(session)[: len(scene)] == scene
    assert ids_and_text(reopened(session, tmp_path).path()) == ids_and_text(session.path())


@pytest.mark.parametrize("second", ["memory", "disk"])
def test_a_scene_after_one_ended_with_no_summary_is_saved_whole(tmp_path: Path, second: str):
    """A scene ended without a summary leaves the story at its last message,
    still on the page; the next began under it, a message never saved."""
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    enter(session)
    play(session, turn(held, f"{MARKER} a"))
    for _event in session.close_private(""):
        pass
    scene = shown_scene(session)
    private = enter(session, keep=second)
    assert session.open_span.start_parent_id == scene[-1]
    play(session, turn(held, f"{MARKER} b"))
    assert ids_and_text(reopened(session, tmp_path).path()) == ids_and_text(session.path())
    private.responses = ["They spoke."]
    for _event in session.close_private(session.summarise_private()):
        pass
    assert shown_scene(session)[: len(scene)] == scene
    assert ids_and_text(reopened(session, tmp_path).path()) == ids_and_text(session.path())


@pytest.mark.parametrize("how", ["regenerate", "switch"])
def test_a_take_swapped_in_before_a_memory_scene_is_the_one_saved(tmp_path: Path, how: str):
    """The scene's messages move onto the new take, but the summary was
    saved under the take the scene began from: on the next launch the old
    take was back, without a word."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    first = session.path()[-1]
    private = enter(session)
    play(session, turn(held, f"{MARKER} a"))
    private.responses = ["They agreed to meet at dawn."]
    for _event in session.close_private(session.summarise_private()):
        pass
    play(session, turn(held, "Next morning."))
    main.responses = ["A different take of the first passage."]
    for _event in session.regenerate(first.id):
        pass
    if how == "switch":
        main.responses = ["A third take."]
        for _event in session.regenerate(session.path()[1].id):
            pass
        session.switch_take(session.path()[1].id, -1)
    shown = session.path()[1]
    assert shown.id != first.id

    again = reopened(session, tmp_path)
    assert ids_and_text(again.path()) == ids_and_text(session.path())
    assert again.path()[1].id == shown.id


def test_a_story_whose_current_message_is_missing_says_so(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    bundle = load_story_bundle(session.story.id, root=tmp_path)
    bundle.story.active_leaf_id = "not-a-message"
    with pytest.raises(ValueError, match="not among its messages"):
        StorySession(bundle, make_config(), MockChatProvider(["x"]), root=tmp_path)


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


@pytest.mark.parametrize("call", ["question", "picture prompt"])
def test_a_question_or_picture_prompt_in_a_scene_reads_the_scene_s_window(
    tmp_path: Path, call: str
):
    """A story that needed the handoff to fit the private model: a turn sent
    the handoff in place of the chapters, but a Question or a picture's
    prompt sent the whole chapter block to the private model instead."""
    session, main, held = build(tmp_path)
    for i in range(8):
        play(session, turn(held, f"Long ago, step {i}."))
    chapters = session.split().summaries
    assert chapters, "nothing was archived"
    private = enter(session)
    main.responses = ["The story so far, condensed."]
    session.write_handoff()
    play(session, turn(held, f"{MARKER} now"))

    if call == "question":
        private.responses = ["Because she asked."]
        list(session.ask("Why did she come?"))
    else:
        private.responses = ['{"references": [], "prompt": "A lamp by the window."}']
        session.write_image_prompt("")
    sent = json.dumps([m.text for m in private.requests[-1].messages])
    assert "The story so far, condensed." in sent
    assert not any(chapter.content in sent for chapter in chapters)


@pytest.mark.parametrize("end", ["close", "discard"])
def test_the_scene_s_last_prompt_goes_with_the_scene(tmp_path: Path, end: str):
    """Help → Model trouble offers "the story's prompt" (the last one sent) to
    other hosts, and the inspector shows it: after the scene it was the
    scene's."""
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session)
    play(session, turn(held, f"{MARKER} now"))
    assert MARKER in json.dumps([m.text for m in session.last_prompt.messages])
    if end == "close":
        private.responses = ["They spoke."]
        for _event in session.close_private(session.summarise_private()):
            pass
    else:
        session.discard_private()
    prompt = session.last_prompt
    assert prompt is None or MARKER not in json.dumps([m.text for m in prompt.messages])


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
    # After the story's style line.
    assert draft.prompt.endswith(". Two figures by a rain-streaked window.")
    assert draft.prompt.startswith(picture_words(session.story, session.texts)[0])
    assert len(main.payloads) == before
    assert MARKER in json.dumps(private.payloads[-1])
    # Memory only: the writer's call is logged without what it said.
    log = read_api_log(session.story.id, root=tmp_path)
    assert MARKER not in json.dumps(log)


def test_a_scene_s_picture_offers_no_words_of_its_prompt_once_the_scene_ends(tmp_path: Path):
    """A video's frames are the story's pictures, each told to the prompt
    writer by the start of its prompt: one made in the scene took the scene's
    words to the story model's writer afterwards."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    private = enter(session, keep="disk")
    play(session, turn(held, f"{MARKER} by the window"))
    span = session.open_span
    session.pictures.write("images/x.png", b"\x89PNG\r\n\x1a\n")
    session.pictures.add_images(
        [
            GeneratedImage(
                anchor_node_id=session.full_path()[-1].id,
                file="images/x.png",
                prompt=f"Two figures by a window, {MARKER}",
                model="hidream",
                size="1024x1024",
                private_span=span.id,
            )
        ]
    )
    frame = next(use for use in session.video_frame_choices() if use.owner_kind == "picture")
    assert MARKER in frame.caption  # to the scene's own writer, while it is open
    private.responses = ["They spoke by the window."]
    for _event in session.close_private(session.summarise_private()):
        pass

    frame = next(use for use in session.video_frame_choices() if use.owner_kind == "picture")
    assert frame.file == "images/x.png" and MARKER not in frame.caption
    main.responses = ['{"prompt": "A slow pan."}']
    sent = len(main.payloads)
    session.write_video_prompt("", start=frame)
    assert MARKER not in json.dumps(main.payloads[sent:])


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
    an aside, a regenerate (plain, and with a note), a passage changed by
    instruction and the exit summary alike."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "Before the scene."))
    before = len(read_api_log(session.story.id, root=tmp_path))
    enter(session)
    play(session, turn(held, f"In the scene {MARKER}."))
    for _ in session.ask(f"Who is here? {MARKER}"):
        pass
    for _ in session.regenerate():
        pass
    for _ in session.regenerate(note=f"Again, with {MARKER}."):
        pass
    for _ in session.revise_passage(session.full_path()[-1].id, f"Now with {MARKER}."):
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
    public_notes = {n.id: list(n.meta.notes) for n in session.nodes}
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
    # Kept as a note under the scene's own message, never a public one: a
    # memory scene's messages, notes and all, never reach the disk.
    noted = [n for n in session.nodes if n.id not in public_notes and n.meta.notes]
    assert noted and all(n.meta.private_span == session.open_span.id for n in noted)
    assert {n.id: n.meta.notes for n in session.nodes if n.id in public_notes} == public_notes
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert {n.id: n.meta.notes for n in saved.nodes} == public_notes
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


class AttestedTee:
    """A TEE/ model's TeeClient as the window leaves it once its enclave has
    attested, the network stood in for (providers/tee.py's own tests check
    the real thing): each attestation's answer comes from `answers`, an
    Attestation or an exception. While the session attests it again, what
    `_tee_guard` (where every call that reaches the model begins) says is kept."""

    def __init__(self, session, clock: list[float], answers: list) -> None:
        import httpx

        from sealedlore.providers.tee import TeeClient

        class Client(TeeClient):
            def _attest(inner):
                answer = answers.pop(0)
                if len(self.attested) > 0:  # the session's, not the window's first
                    self.blocks.append(session.tee_block)
                    try:
                        session._tee_guard()
                    except Exception as exc:  # noqa: BLE001 - what it refused with
                        self.meanwhile.append(str(exc))
                self.attested.append(answer)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            def check_reply(inner, *_args, **_kwargs):
                return "verified"

        self.attested: list = []
        self.blocks: list = []
        self.meanwhile: list[str] = []
        self.client = Client(
            "https://nano-gpt.com/api/v1",
            "k",
            "TEE/x",
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
            clock=lambda: clock[0],
        )
        self.client.attest()  # as the window does when the scene begins
        session.private_tee = self.client


def _attestation(*shortfalls: str):
    from sealedlore.models.private import Attestation

    return Attestation(
        verified=True,
        level="partial" if shortfalls else "full",
        shortfalls=list(shortfalls),
        signing_address="0xab",
    )


def test_a_tee_model_is_attested_again_once_its_attestation_is_fifteen_minutes_old(
    tmp_path: Path,
):
    """It was attested only as the scene (or chat) began, or its model changed:
    an instance Chutes started later, whose replies are signed by none, was
    never checked, nor a revocation seen. Now the request that finds the
    attestation REATTEST_SECONDS old attests it again first, as the other
    apps do, and everything else that would reach the model refuses
    meanwhile, as during the first."""
    from sealedlore.providers.tee import REATTEST_SECONDS

    session, _main, held = build(tmp_path)
    private = enter(session)
    clock = [1000.0]
    first, again = _attestation(), _attestation("no GPU evidence")
    tee = AttestedTee(session, clock, [first, again])
    session.open_span.attestation = first
    play(session, turn(held, "One."))
    clock[0] += REATTEST_SECONDS - 1
    play(session, turn(held, "Two."))
    assert len(tee.attested) == 1 and session.take_reattestation() is None
    clock[0] += 1
    play(session, turn(held, "Three."))
    assert tee.attested == [first, again] and len(private.payloads) == 3
    assert tee.blocks == ["TEE/x's enclave is still being attested"]
    assert tee.meanwhile == ["TEE/x's enclave is still being attested; nothing was sent"]
    assert session.tee_block is None and not session.tee_attesting
    assert session.open_span.attestation is again
    assert session.take_reattestation() == ("attested", again)
    assert session.take_reattestation() is None, "said once"
    play(session, turn(held, "Four."))
    assert len(tee.attested) == 2, "in force for another fifteen minutes"


def test_a_tee_model_refused_when_attested_again_is_sent_nothing_more(tmp_path: Path):
    from sealedlore.providers.tee import REATTEST_SECONDS, TeeRefused

    session, _main, held = build(tmp_path)
    private = enter(session)
    clock = [1000.0]
    revoked = TeeRefused("TEE/x's Intel TDX quote failed verification: Intel has revoked it")
    tee = AttestedTee(session, clock, [_attestation(), revoked])
    play(session, turn(held, "One."))
    clock[0] += REATTEST_SECONDS
    with pytest.raises(TeeRefused, match="refused: .*revoked it; nothing was sent"):
        play(session, turn(held, "Two, never sent."))
    assert len(private.payloads) == 1
    assert "never sent" not in json.dumps([n.content for n in session.nodes]), "not attached"
    assert session.tee_block.startswith("The attestation was refused: ")
    assert session.take_reattestation() == ("refused", str(revoked))
    with pytest.raises(TeeRefused, match="refused"):
        session.summarise_private()
    assert len(tee.attested) == 2, "not attested again on its own: the window's Try again does"


@pytest.mark.parametrize(
    "entry", ["send", "regenerate", "revise", "ask", "summary", "image prompt"]
)
def test_every_call_that_reaches_a_tee_model_attests_it_again_first(tmp_path: Path, entry):
    from sealedlore.providers.tee import REATTEST_SECONDS

    session, _main, held = build(tmp_path)
    private = enter(session)
    clock = [1000.0]
    tee = AttestedTee(session, clock, [_attestation(), _attestation()])
    play(session, turn(held, "One."))
    clock[0] += REATTEST_SECONDS
    sent = len(private.payloads)
    calls = {
        "send": lambda: play(session, turn(held, "Two.")),
        "regenerate": lambda: list(session.regenerate()),
        "revise": lambda: list(session.revise_passage(session.full_path()[-1].id, "Shorter.")),
        "ask": lambda: list(session.ask("Who is here?")),
        "summary": session.summarise_private,
        "image prompt": lambda: session.write_image_prompt(""),
    }
    private.responses = ['{"references": [], "prompt": "A lamp by the window."}']
    calls[entry]()
    assert len(tee.attested) == 2 and len(private.payloads) == sent + 1


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_a_questions_recall_carries_nothing_from_a_private_scene(tmp_path: Path, keep: str):
    """What a Question recalled rides the turns after it (Oct 2026). A
    Question asked in the scene recalls nothing and is never carried; one
    asked after it carries public chapters only, and nothing of the scene
    reaches another model or the embeddings endpoint on the way."""
    session, main, held = build(tmp_path, keep=keep)
    session.config.recall_in = "questions"  # turns get only what a Question carries
    session.config.summary_merge_ratio = 0.005
    session.config.summary_merge_keep = 1
    for i in range(12):
        play(session, turn(held, f"Early step {i}: the lamplighter's ledger."))
    session.merge_now()
    assert any(s.merged_from for s in session.split(session.path()).summaries)

    private = enter(session, keep)
    for _event in session.ask(f"Where is the lamplighter's ledger? {MARKER}"):
        pass
    in_scene = session.bundle.asides[-1]
    assert in_scene.private_span is not None and in_scene.recalled == []
    play(session, turn(held, f"{MARKER} hides the ledger."))
    private.responses = ["They talked, and parted as friends."]
    for _event in session.close_private(session.summarise_private()):
        pass

    for _event in session.ask("Where is the lamplighter's ledger?"):
        pass
    asked = session.bundle.asides[-1]
    assert asked.private_span is None and asked.recalled
    for i in range(3):
        play(session, turn(held, f"After the scene, step {i}."))
        assert session.last_recall is not None and session.last_recall.items
        assert all(item.carried for item in session.last_recall.items)
        assert all(MARKER not in item.text for item in session.last_recall.items)
    assert MARKER not in everything_sent(main, session.embeddings)


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_a_scene_teaches_its_own_model_s_count_and_never_the_story_model_s(
    tmp_path: Path, keep: str
):
    """A private turn's prompt was counted, and its reply taught, as the story
    model's: a private model counting differently moved the story's factor
    (and wrote it to config.json). A scene kept in memory teaches the config
    nothing, as a chat kept in memory doesn't, even through the window's
    estimator, whose factors are the config's own."""
    session, main, held = build(tmp_path)
    session.learn_corrections = True
    session.estimator = TokenEstimator(
        factors=session.config.token_correction_factors, counter=fallback_counter
    )
    main.usage = Usage(prompt_tokens=2_000, completion_tokens=50)
    play(session, turn(held, "Before."))
    story_model = session.model
    learned = session.estimator.factor_for(story_model)

    private = enter(session, keep=keep)
    private.usage = Usage(prompt_tokens=9_000, completion_tokens=50)
    for i in range(3):
        play(session, turn(held, f"{MARKER} {i}"))
    assert session.last_prompt.model == "local/model"
    assert session.estimator.factor_for(story_model) == learned
    assert session.estimator.factor_for("local/model") > 1.0
    written = load_config(root=tmp_path).token_correction_factors
    assert written[story_model] == learned
    if keep == "memory":
        assert "local/model" not in session.config.token_correction_factors
        assert "local/model" not in written
    else:
        assert written["local/model"] == session.estimator.factor_for("local/model")
