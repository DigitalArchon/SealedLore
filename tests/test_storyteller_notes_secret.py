"""The storyteller's notes reach the storyteller and nobody else.

The leak test is the one that matters: a story whose notes carry a marker,
every side call the app makes switched on and played through the mock
provider, and the marker may turn up only in requests that carry the
storyteller's own system block. The settings review reads that block too, and
its words reach the author: it is sent a line saying there are notes in their
place. So is the Prompt page, unless the author asked to see what the story
hides.

One other model does read them, because it writes them: the authoring model
asked to change a story not yet begun, which keeps the notes true to the
setup it changes (tests/test_storyteller_notes_drafting.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.followup import PLOT
from sealedlore.engine.plot_draft import PlotWishes
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.prompt import SECTION_NOTES, TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.rules import notes_kept_line
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ProviderError
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import read_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario

MARKER = "ZQX-SECRET"
NOTES = (
    f"Under the granary's false floor is the seed corn, in a crate stamped {MARKER}.\n"
    "Marcus hid it there. He wants to be gone before the thaw, and hides that he can't read."
)
PLOT_FILE = (Path(__file__).resolve().parent.parent / "SampleStories" / "Doomsville.md").read_text(
    encoding="utf-8"
)
# How the storyteller's own system block begins, and the notes' heading in it.
RULES_OPENING = DEFAULT_TEXTS["storyteller.rules"][:60]
HEADING = "# WHAT ONLY YOU KNOW"
STORY_MODEL = "anthropic/claude-sonnet-4.6"
# Long enough that a few chapters of it are worth merging.
REPLY = "The town answers in its own time, slow and dusty. " * 8


def make_config() -> Config:
    return Config(
        providers=[
            ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model=STORY_MODEL)
        ],
        active_provider_name="nano",
        private_provider=ProviderConfig(
            name="private", base_url="http://localhost:11434/v1", model="local/model"
        ),
        private_budget=6_000,
        min_cacheable_tokens=1,
        archive_chunk_turns=2,
        # Every side role on a model of its own, so a request says whose it is.
        summarization_model="side/summaries",
        authoring_model="side/authoring",
        scene_model="side/scene",
        plot_model="side/plot",
        lore_model="side/lore",
        image_prompt_model="side/pictures",
        lore_selector="picker",
        lore_whole_share=0.01,
        suggest_characters=True,
        story_ledger=True,
        summary_merge_ratio=0.05,
        summary_merge_keep=1,
        recall="exchanges",
        recall_in="everywhere",
        # The cast over its room, so its cards are condensed.
        cast_limit="tokens",
        cast_token_cap=200,
    )


def build(tmp_path: Path, plot_file: str = PLOT_FILE, notes: str | None = NOTES):
    """`notes`: given to the story by hand, or None for whatever the plot
    file itself holds."""
    bundle = bundle_from_scenario(parse_plot_markdown(plot_file).scenario)
    story = bundle.story
    story.defaults.main_model = STORY_MODEL
    story.defaults.context_token_budget = 3_000
    if notes is not None:
        story.storyteller_notes = notes
        story.storyteller_notes_by = "model"
    bundle.lore += [
        LoreEntry(title=f"Entry {i}", content=f"Old lore number {i} about the town.")
        for i in range(12)
    ]
    held = bundle.cast[0]
    # Two pictures each, so every passage has looks to pick.
    for character in bundle.cast[:2]:
        character.reference_images = [
            ImageRef(id=f"{character.id}-a", file=f"images/refs/{character.id}-a.png"),
            ImageRef(
                id=f"{character.id}-b",
                file=f"images/refs/{character.id}-b.png",
                expression="angry",
            ),
        ]
    story.held_character_id = held.id
    save_story_bundle(bundle, root=tmp_path)
    main = MockChatProvider([REPLY])
    session = StorySession(
        bundle,
        make_config(),
        main,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
        embeddings=MockEmbeddings(),
    )
    return session, main, held.id


def turn(held: str, text: str) -> TurnRequest:
    return TurnRequest(speaker_id=held, user_text=text, controlled_character_id=held)


def play(session: StorySession, request: TurnRequest) -> None:
    for _event in session.send(request):
        pass


def system_of(payload: dict) -> str:
    """A request's system message, as text."""
    for message in payload["messages"]:
        if message["role"] == "system":
            content = message["content"]
            if isinstance(content, str):
                return content
            return "\n\n".join(part.get("text", "") for part in content)
    return ""


def quietly(call) -> None:
    """The mock's reply is no plot, review or list of cards: the request went
    out, and that is what counts here."""
    try:
        result = call()
        if hasattr(result, "__next__"):
            for _event in result:
                pass
    except (ValueError, ProviderError):
        pass


def test_the_notes_reach_only_requests_that_carry_the_storytellers_system_block(tmp_path: Path):
    session, main, held = build(tmp_path)
    # Turns: the director, the plot's and the scene's reads, look picks, lore
    # picks, embeddings, archival with its ledger, merges, the character scan
    # and condensed cards go with them.
    for i in range(10):
        play(session, turn(held, f"I walk the road into town, step {i}."))
    session.start_condensing()
    for _event in session.ask("Why did Marcus look away?"):
        pass
    for _event in session.regenerate():
        pass
    quietly(session.continue_passage)
    for summary in list(session.summaries)[:2]:
        quietly(lambda summary=summary: session.rebuild_summary(summary.id, force=True))
    for call in (
        session.merge_now,
        session.scan_recent,
        session.suggest_scene,
        lambda: session.infer_competence(session.cast[0].id),
        lambda: session.condense_card(session.cast[0]),
        lambda: session.fill_lore_cast("A town of the old stories."),
        lambda: session.write_image_prompt("the granary"),
        lambda: session.write_video_prompt("the granary"),
        lambda: session.write_card_prompts(session.cast[:2]),
        lambda: session.draft_plot(PlotWishes()),
        lambda: session.follow_up_plot("More danger in the first week."),
        lambda: session.review_settings("It's slow.", replies=4),
        lambda: session.review_settings("Still slow.", replies=None, whole_prompt=True),
    ):
        quietly(call)

    # A private scene: its model is the storyteller there, and has the notes.
    private = MockChatProvider(["A quiet reply."])
    session.enter_private(keep="disk", provider=private, model="local/model")
    play(session, turn(held, "I close the granary door behind us."))
    private.responses = ["They spoke quietly and agreed to meet at dawn."]
    for _event in session.close_private(session.summarise_private()):
        pass
    play(session, turn(held, "At dawn I go to the well."))

    kinds = {entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)}
    made = {kind.removesuffix("_request") for kind in kinds if kind.endswith("_request")}
    assert made >= {
        "scene",
        "chronicle",
        "director",
        "look",
        "lore_pick",
        "summary",
        "ledger",
        "merge",
        "characters",
        "condense",
        "competence",
        "lore_cast",
        "plot_draft",
        "review",
        "image_prompt",
        "video_prompt",
        "portrait_prompt",
    }, sorted(made)

    with_marker = [p for p in main.payloads if MARKER in json.dumps(p)]
    without = [p for p in main.payloads if MARKER not in json.dumps(p)]
    assert with_marker and without
    plot_writer = DEFAULT_TEXTS["authoring.plot"][:40]
    told = DEFAULT_TEXTS["authoring.plot.notes"].split("{notes}")[0].strip()
    plots = [p for p in with_marker if system_of(p).startswith(plot_writer)]
    assert plots, "the plot's writer is told the notes, so its plot is true to them"
    for payload in plots:
        # The one other model that is: once, under its own heading, and its
        # reply is kept from the author as the notes are.
        assert json.dumps(payload).count(MARKER) == 1
        assert told in json.dumps(payload, ensure_ascii=False)
        assert payload["model"] == "side/authoring"
    with_marker = [p for p in with_marker if p not in plots]
    for payload in with_marker:
        system = system_of(payload)
        # The storyteller's own system block, the notes in their place in it,
        # and nowhere else in the request.
        assert system.startswith(RULES_OPENING), payload["model"]
        assert system.count(MARKER) == 1
        assert system.index("# SETTING") < system.index(HEADING) < system.index(MARKER)
        assert json.dumps(payload).count(MARKER) == 1
    # Every side model's requests are among those without: the reads, the
    # plot, the summaries, the lore pick, drafting and the review.
    assert {p["model"] for p in with_marker} == {STORY_MODEL}
    side = {p["model"] for p in without}
    # The picture and video prompt writers among them: they write from the
    # story's prompt, less the notes (nothing in the notes is to be seen in a
    # picture, and the prompt they write is shown to the author).
    assert {
        "side/scene",
        "side/plot",
        "side/summaries",
        "side/lore",
        "side/authoring",
        "side/pictures",
    } <= side
    pictures = [p for p in without if p["model"] == "side/pictures"]
    from_the_story = [p for p in pictures if system_of(p).startswith(RULES_OPENING)]
    assert from_the_story, "the picture and video prompts are written from the story's prompt"
    assert not any(HEADING in system_of(p) for p in pictures)
    assert MARKER not in json.dumps(session.embeddings.calls)

    # The private model wrote the scene's passages from the same block.
    scene_turns = [p for p in private.payloads if MARKER in json.dumps(p)]
    assert scene_turns and all(HEADING in system_of(p) for p in scene_turns)
    # Its summary of the scene is made from the scene alone.
    assert MARKER not in json.dumps(private.payloads[-1])

    # Nothing a side call wrote back holds it: chapters, ledgers, cards.
    assert all(
        MARKER not in s.content and MARKER not in (s.ledger or "") for s in session.summaries
    )
    assert MARKER not in json.dumps([c.model_dump() for c in session.cast])


def test_the_review_is_sent_a_line_in_the_notes_place(tmp_path: Path):
    session, main, held = build(tmp_path)
    play(session, turn(held, "I walk the road into town."))
    sent = session.assemble(turn(held, "I look under the floor."))
    assert MARKER in sent.section(SECTION_NOTES).text

    kept = notes_kept_line(NOTES)
    system = session.system_prompt_text()
    assert MARKER not in system and "Marcus hid it" not in system
    assert f"{HEADING}\n" in system and kept in system
    # All of the block but the notes is as the storyteller has it.
    real = next(m.text for m in sent.messages if m.role == "system")
    assert system == real.replace(NOTES, kept)

    for whole in (False, True):
        before = len(main.payloads)
        with pytest.raises(ValueError):
            session.review_settings("Too slow.", replies=None, whole_prompt=whole)
        request = json.dumps(main.payloads[before:], ensure_ascii=False)
        assert kept in request and HEADING in request
        assert MARKER not in request and "Marcus hid it" not in request
    # Its sizes are of what it is sent, and assembling for it leaves the
    # Prompt page's kept prompt alone.
    assert session.last_prompt is sent
    assert session.review_sizes().system_prompt > 0
    assert session.last_prompt is sent


def test_a_plot_written_again_is_told_the_notes_too(tmp_path: Path):
    """A plot written blind to the notes made a second truth of its own."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "I walk the road into town."))
    before = len(main.payloads)
    quietly(lambda: session.follow_up_plot("More danger in the first week."))
    sent = main.payloads[before:]
    assert sent and all(json.dumps(p).count(MARKER) == 1 for p in sent)
    assert session.follow_up_history(PLOT) == []


def test_the_command_lines_inspect_keeps_the_notes_unless_spoilers_are_shown(tmp_path, capsys):
    from sealedlore.cli import Repl

    session, main, held = build(tmp_path)
    repl = Repl.__new__(Repl)
    repl.session = session
    as_sent = session.assemble(turn(held, "I look under the floor."))
    built = []
    real = main.build_payload
    main.build_payload = lambda request: built.append(request) or real(request)

    repl.command_inspect()
    assert "system.storyteller_notes" in capsys.readouterr().out
    shown = "\n".join(message.text for message in built[-1].messages)
    assert MARKER not in shown and notes_kept_line(NOTES) in shown
    assert session.last_prompt is as_sent, "what is shown is never what is kept to send"

    session.config.show_plot_spoilers = True
    repl.command_inspect()
    assert MARKER in "\n".join(message.text for message in built[-1].messages)


def test_the_stories_search_never_finds_a_storys_notes(tmp_path: Path):
    """The search shows the words around what it finds: the notes are not
    among what it reads, with spoilers shown or not."""
    from sealedlore.storage.story_search import search_stories

    session, _main, _held = build(tmp_path)
    session.story.world_bible = "A town with a granary."
    session.save()
    assert [hit.story_id for hit in search_stories("granary", tmp_path)] == [session.story.id]
    for spoilers in (False, True):
        assert list(search_stories(MARKER, tmp_path, spoilers=spoilers)) == []


def test_a_plot_file_s_own_notes_come_in_unread_and_go_to_the_storyteller_alone(tmp_path: Path):
    """`# Storyteller's notes` in a plot file: whoever imports it has not
    read them, and in play they go where a story's notes go and nowhere else
    (never the director, the plot's read or the scene's)."""
    text = PLOT_FILE + f"\n# Storyteller's notes\n\n{NOTES}\n"
    session, main, held = build(tmp_path, plot_file=text, notes=None)
    story = session.story
    assert MARKER in story.storyteller_notes and story.storyteller_notes_by == "model"
    assert MARKER not in story.plot.model_dump_json(), "the plot holds none of them"
    for i in range(3):
        play(session, turn(held, f"I walk the road into town, step {i}."))
    kinds = {entry["kind"] for entry in read_api_log(story.id, root=tmp_path)}
    assert {"scene_request", "chronicle_request", "director_request"} <= kinds
    with_marker = [p for p in main.payloads if MARKER in json.dumps(p)]
    assert with_marker
    for payload in with_marker:
        system = system_of(payload)
        assert payload["model"] == STORY_MODEL and system.startswith(RULES_OPENING)
        assert system.index("# SETTING") < system.index(HEADING) < system.index(MARKER)
        assert json.dumps(payload).count(MARKER) == 1
    side = {p["model"] for p in main.payloads if MARKER not in json.dumps(p)}
    assert {"side/scene", "side/plot"} <= side
