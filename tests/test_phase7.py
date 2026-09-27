"""Phase 7: branching navigation, archives, readable export, usage totals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.export import render_markdown
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.aside import Aside
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.archive import (
    ArchiveError,
    file_format,
    import_archive,
    read_archive,
    write_archive,
)
from sealedlore.storage.paths import stories_dir, story_dir
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from sealedlore.tree import latest_leaf, sibling_position


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="n", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="n",
        min_cacheable_tokens=1,
    )
    return StorySession(
        StoryBundle(story=story, cast=cast),
        config,
        MockChatProvider(["Reply."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def play(session: StorySession, *texts: str) -> None:
    for text in texts:
        session.provider = MockChatProvider([f"Reply to {text}"])
        list(session.send(turn(text)))


# --- branching ------------------------------------------------------------------


def test_regenerating_an_older_passage_swaps_it_in_place(session: StorySession):
    """The author's rule (Sept 2026): a take is another version of one passage,
    never a branch. It used to jump the story to the new take, leaving
    everything after the old one on a line of its own."""
    play(session, "one", "two", "three")
    first_reply, leaf = session.path()[1], session.path()[-1]

    session.provider = MockChatProvider(["Another take on one."])
    list(session.regenerate(first_reply.id))

    path = session.path()
    assert [n.content for n in path] == [
        "one",
        "Another take on one.",
        "two",
        "Reply to two",
        "three",
        "Reply to three",
    ]
    assert session.story.active_leaf_id == leaf.id
    assert session.take_position(path[1].id) == (2, 2)
    # Nothing was lost: the old take is kept, and is a take, not a line.
    assert len(session.nodes) == 7
    assert first_reply.children == [] and session.take_position(first_reply.id) == (1, 2)


def test_switching_takes_keeps_the_story_after_it(session: StorySession):
    play(session, "one", "two")
    first_reply = session.path()[1]
    session.provider = MockChatProvider(["Retake."])
    list(session.regenerate(first_reply.id))
    retake = session.path()[1]

    session.switch_take(retake.id, -1)

    assert [n.content for n in session.path()] == ["one", "Reply to one", "two", "Reply to two"]
    session.switch_take(first_reply.id, 1)
    assert [n.content for n in session.path()][1:] == ["Retake.", "two", "Reply to two"]


def test_a_take_swapped_under_a_chapter_makes_it_stale_and_it_still_applies(
    session: StorySession,
):
    from sealedlore.models.summary import Summary

    play(session, "one", "two", "three")
    covered = [n.id for n in session.path()[:4]]
    chapter = Summary(covered_node_ids=covered, content="One and two happened.")
    session.bundle.summaries.append(chapter)
    assert list(session.split().summaries) == [chapter]
    session.provider = MockChatProvider(["Retake."])
    list(session.regenerate(covered[1]))

    assert chapter.stale and chapter.covered_node_ids[1] == session.path()[1].id
    assert list(session.split().summaries) == [chapter], "it still matches the path"
    stale = session.switch_take(session.path()[1].id, -1)
    assert chapter.covered_node_ids == covered and stale == []  # already stale


def test_an_older_retake_runs_no_reads_and_a_stopped_one_still_swaps(session: StorySession):
    play(session, "one", "two", "three")
    first_reply = session.path()[1]
    session.provider = MockChatProvider(["Retake."])
    list(session.regenerate(first_reply.id))
    # The passage only: the reads look at where the story ends.
    assert len(session.provider.requests) == 1

    session.provider = MockChatProvider(["A stopped take that ran on"])
    events = session.regenerate(session.path()[1].id)
    next(event for event in events if type(event).__name__ == "TextDelta")
    events.close()  # Stop
    path = session.path()
    assert path[1].content.startswith("A stopped") and len(path) == 6


def test_branch_from_keeps_everything_after_it(session: StorySession):
    play(session, "one", "two", "three")
    fork_point = session.path()[1]

    session.branch_from(fork_point.id)
    play(session, "a different second turn")

    assert len(session.nodes) == 8
    assert [n.content for n in session.path()][:2] == ["one", "Reply to one"]
    assert sibling_position(session.nodes, session.path()[2].id) == (2, 2)


def test_latest_leaf_follows_the_newest_child(session: StorySession):
    play(session, "one")
    user = session.path()[0]
    session.provider = MockChatProvider(["Second take."])
    list(session.regenerate())

    assert latest_leaf(session.nodes, user.id).content == "Second take."


# --- archives -------------------------------------------------------------------


def test_an_archive_round_trips_everything(session: StorySession, tmp_path: Path):
    play(session, "one", "two")
    list(session.regenerate())
    session.bundle.asides.append(Aside(anchor_node_id=session.path()[-1].id, question="q?"))
    session.bundle.supporting.characters.append(Character(name="Patel", origin="model"))
    session.save()
    log = read_api_log(session.story.id, root=tmp_path)
    file = tmp_path / "story.sealedlore-archive.json"

    write_archive(file, session.bundle, log)
    bundle, api_log = read_archive(file)

    assert [n.id for n in bundle.nodes] == [n.id for n in session.nodes]
    assert bundle.story.active_leaf_id == session.story.active_leaf_id
    assert bundle.asides[0].question == "q?"
    assert bundle.supporting.characters[0].name == "Patel"
    assert api_log == log
    assert "embeddings" not in json.loads(file.read_text())


def test_importing_over_an_existing_story_gets_a_fresh_id(session: StorySession, tmp_path: Path):
    play(session, "one")
    file = tmp_path / "a.sealedlore-archive.json"
    write_archive(file, session.bundle, read_api_log(session.story.id, root=tmp_path))

    imported = import_archive(file, root=tmp_path)

    assert imported.story.id != session.story.id
    assert imported.story.title.endswith("(imported)")
    reloaded = load_story_bundle(imported.story.id, root=tmp_path)
    assert [n.content for n in reloaded.nodes] == [n.content for n in session.nodes]
    assert read_api_log(imported.story.id, root=tmp_path) == read_api_log(
        session.story.id, root=tmp_path
    )


def test_importing_onto_a_fresh_machine_keeps_the_id(session: StorySession, tmp_path: Path):
    play(session, "one")
    file = tmp_path / "a.sealedlore-archive.json"
    write_archive(file, session.bundle, [])

    imported = import_archive(file, root=tmp_path / "elsewhere")

    assert imported.story.id == session.story.id


@pytest.mark.parametrize("hostile_id", ["../../escaped", "/tmp/sealedlore-escaped", "a/b", ".."])
def test_an_archive_id_that_names_a_path_is_replaced(tmp_path: Path, hostile_id: str):
    # The id becomes a folder name; an archive must not choose where files go.
    root = tmp_path / "data"
    story = Story(id=hostile_id, title="Hostile")
    file = tmp_path / "a.sealedlore-archive.json"
    write_archive(file, StoryBundle(story=story), [{"kind": "request", "payload": {}}])

    imported = import_archive(file, root=root)

    assert imported.story.id != hostile_id
    assert story_dir(imported.story.id, root).parent == stories_dir(root)
    assert load_story_bundle(imported.story.id, root=root).story.title == "Hostile"
    assert not (tmp_path / "escaped").exists()
    assert not Path("/tmp/sealedlore-escaped").exists()


def test_story_dir_refuses_a_path_for_an_id(tmp_path: Path):
    with pytest.raises(ValueError):
        story_dir("../x", tmp_path)
    with pytest.raises(ValueError):
        story_dir("/abs", tmp_path)


def test_usage_report_skips_malformed_log_entries():
    # An imported archive's log is untrusted: a bad entry must not stop the story opening.
    entries = [
        {"id": ["not", "hashable"], "kind": "request", "payload": {"model": "m"}},
        {"kind": ["response"], "usage": {"prompt_tokens": 5}},
        {"kind": "response", "usage": "lots", "request_log_ref": 3},
        {"kind": "response", "usage": {"prompt_tokens": 10, "cost": 0.5}},
    ]
    report = usage_report(entries)
    assert report.total.calls == 2
    assert report.total.prompt_tokens == 10
    assert report.total.cost == 0.5


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("nope", "isn't valid JSON"),
        ('{"format": "sealedlore-scenario", "version": 1}', "isn't a SealedLore story archive"),
        ('{"format": "sealedlore-archive", "version": 7}', "version 7"),
        ('{"format": "sealedlore-archive", "version": 1}', "'story'"),
    ],
)
def test_bad_archives_are_refused_in_plain_words(tmp_path: Path, content: str, message: str):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ArchiveError, match=message):
        read_archive(path)


def test_file_format_tells_scenarios_from_archives(tmp_path: Path):
    """Either import command takes either file, so the kind must be readable."""
    cases = {
        "a.json": '{"format": "sealedlore-archive", "version": 1}',
        "s.json": '{"format": "sealedlore-scenario", "version": 1}',
        "junk.json": "nope",
        "list.json": "[1, 2]",
    }
    for name, content in cases.items():
        (tmp_path / name).write_text(content)
    assert file_format(tmp_path / "a.json") == "sealedlore-archive"
    assert file_format(tmp_path / "s.json") == "sealedlore-scenario"
    assert file_format(tmp_path / "junk.json") is None
    assert file_format(tmp_path / "list.json") is None
    assert file_format(tmp_path / "missing.json") is None


# --- readable export --------------------------------------------------------------


def test_markdown_is_prose_only(cast):
    nodes = [
        Node(kind="user", speaker_id="__director__", content="Open on the keep."),
        Node(kind="assistant", speaker_id="__narrator__", content="Rain fell on the keep."),
        Node(kind="user", speaker_id="char-serrik", content="I knock.", ooc="make it tense"),
        Node(kind="user", speaker_id="__narrator__", content="Thunder answered."),
    ]
    text = render_markdown("The Sundering", nodes, cast, subtitle="Chapter 1")

    assert text.startswith("# The Sundering\n\n*Chapter 1*")
    assert "Open on the keep." not in text
    assert "make it tense" not in text
    assert "**Serrik Vaun:** I knock." in text
    assert "Rain fell on the keep." in text and "Thunder answered." in text


# --- usage -------------------------------------------------------------------------


def test_usage_totals_every_kind_and_never_calls_a_missing_cost_free():
    entries = [
        {"kind": "request"},
        {
            "kind": "response",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "cache_read_input_tokens": 800,
                "cost": 0.01,
            },
        },
        {"kind": "response", "aside": True, "usage": {"prompt_tokens": 500, "cost": 0.002}},
        {"kind": "summary_response", "usage": {"prompt_tokens": 300, "completion_tokens": 50}},
        {"kind": "characters_response", "usage": {"prompt_tokens": 200, "cost": 0.001}},
    ]
    report = usage_report(entries)

    assert [line.label for line in report.lines] == [
        "Story turns",
        "Questions (asides)",
        "Chapter summaries",
        "Character scans",
    ]
    assert report.total.calls == 4
    assert report.total.prompt_tokens == 2000
    assert report.total.cost == pytest.approx(0.013)
    assert report.total.unpriced == 1
    assert report.lines[0].cache_hit_rate == pytest.approx(0.8)


def test_usage_report_counts_the_plots_calls():
    entries = [
        {"kind": "response", "usage": {"prompt_tokens": 10, "cost": 0.5}},
        {"kind": "chronicle_response", "usage": {"prompt_tokens": 4, "cost": 0.01}},
        {"kind": "director_response", "usage": {"prompt_tokens": 4, "cost": 0.02}},
        {"kind": "variant_response", "usage": {"prompt_tokens": 4, "cost": 0.03}},
        {"kind": "gap_response", "usage": {"prompt_tokens": 4, "cost": 0.04}},
    ]
    report = usage_report(entries)
    assert report.total.calls == 5
    assert report.total.cost == pytest.approx(0.6)
    assert {line.label for line in report.lines} >= {"Plot: director", "Plot: clock and facts"}
