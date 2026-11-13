"""A story as a plot document, and a document's plot given back to the story
(engine/story_plot.py, StorySession.set_plot_document). The plot editor opens
a story through it and the model's plot reaches the story through it, so only
the plot may change: the story's own cards stay as the story has them."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.plot_file import render_plot_markdown
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.session import StorySession
from sealedlore.engine.story_plot import (
    PlotDocumentError,
    document_from_story,
    notes_from_plot,
    notes_mark,
    plot_changes,
    safe_prose,
    without_plot,
)
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.plot_file import (
    CharacterDoc,
    ConditionDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
)
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario
from tests.conftest import make_exchange
from tests.test_session import make_config

ROOT = Path(__file__).resolve().parents[1]
DOOMSVILLE = ROOT / "SampleStories" / "Doomsville.md"
EXTENDED = ROOT / "tests" / "data" / "doomsville_extended.md"


def _session(bundle: StoryBundle, root: Path) -> StorySession:
    save_story_bundle(bundle, root=root)
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider([]),
        root=root,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


@pytest.fixture
def plain(story: Story, cast: list[Character]) -> StoryBundle:
    """A story with no plot: two lore entries, one a place, and a supporting card."""
    story.setup.opening_text = "Torchlight in the undercroft."
    story.setup.starting_scene = story.scene.model_copy(deep=True)
    bundle = StoryBundle(
        story=story,
        cast=cast,
        lore=[
            LoreEntry(
                id="lore-keep",
                title="Calder Keep",
                content="A keep on the cliffs, empty for a hundred years.",
                keywords=["the keep"],
            ),
            LoreEntry(
                id="lore-oath",
                title="The Grey Oath",
                content="What the duellists swear.",
                enabled=False,
            ),
        ],
    )
    bundle.supporting.characters.append(
        Character(id="char-tobin", name="Old Tobin", summary="Keeps the gate.")
    )
    return bundle


def _meaning(plot, scenario) -> list[tuple]:
    """A plot by what it says, names where it holds ids, for comparing."""
    names = {c.id: c.name for c in (*scenario.cast, *scenario.supporting)}
    titles = {entry.id: entry.title for entry in scenario.lore}
    events = {event.id: event.title for event in plot.events}

    def condition(c) -> tuple:
        return (
            c.fact,
            c.op,
            c.value,
            events.get(c.event_id),
            c.happened,
            c.place,
            c.place_test,
            names.get(c.who),
        )

    def way(w) -> tuple:
        return (
            w.title,
            tuple(condition(c) for c in w.requires),
            w.trigger,
            w.tell,
            tuple((a.fact, a.value) for a in w.sets),
            w.offscreen,
            tuple(names[i] for i in w.brings),
            tuple(titles[i] for i in w.reveals),
            w.place,
        )

    return [
        tuple((f.name, f.initial, tuple(f.values), f.watched, f.meaning) for f in plot.facts),
        tuple((p.name, titles.get(p.lore_id)) for p in plot.places),
        plot.start_minutes,
        tuple(
            (
                e.title,
                e.kind,
                e.window,
                e.pacing,
                e.lead_in,
                e.aftermath,
                e.must_happen,
                tuple(condition(c) for c in e.requires),
                e.trigger,
                e.tell,
                tuple(names[i] for i in e.brings),
                tuple(titles[i] for i in e.reveals),
                e.place,
                tuple(way(v) for v in e.variants),
            )
            for e in plot.events
        ),
    ]


@pytest.mark.parametrize("path", [DOOMSVILLE, EXTENDED], ids=lambda p: p.stem)
def test_a_story_made_from_a_plot_file_reads_back_as_the_same_plot(path: Path):
    """Every part of the format, through a story and back: what the editor
    shows of a story's plot is the plot it plays."""
    parsed = parse_plot_markdown(path.read_text(encoding="utf-8"))
    bundle = bundle_from_scenario(parsed.scenario)
    document = document_from_story(bundle)
    assert document.story_id == bundle.story.id
    again = parse_plot_markdown(render_plot_markdown(document).text)
    assert again.ok, [p.describe() for p in again.problems if p.error]
    assert _meaning(again.scenario.plot, again.scenario) == _meaning(
        parsed.scenario.plot, parsed.scenario
    )
    hidden = {c.name for c in (*parsed.scenario.cast, *parsed.scenario.supporting) if c.plot_hidden}
    assert {c.name for c in document.characters if c.hidden} == hidden
    # Nothing to change: the document is the story as it is.
    changes = plot_changes(document, bundle, begun=False)
    assert not (changes.characters or changes.entries or changes.new_cast or changes.new_lore)
    assert changes.notes == []


def test_a_story_without_a_plot_is_its_own_parts_and_nothing_else(plain: StoryBundle):
    document = document_from_story(plain)
    assert [c.name for c in document.characters] == [
        "Serrik Vaun",
        "Maela Orr",
        "Captain Idris",
        "Old Tobin",
    ]
    serrik, maela, idris, tobin = document.characters
    assert serrik.present and maela.present and not idris.present
    assert tobin.role == "supporting" and serrik.role == "player"
    assert serrik.aliases == ["the Grey Wolf"] and serrik.story_id == "char-serrik"
    assert [entry.name for entry in document.lore] == ["Calder Keep", "The Grey Oath"]
    assert document.lore[0].keywords == ["the keep"], "the title is the importer's to add"
    assert document.places == [] and document.facts == [] and document.events == []
    assert document.world.startswith("Calder Keep has stood")
    assert document.opening.location == "The undercroft beneath Calder Keep"
    assert document.length is None, "a custom length is the app's, never the file's"
    # For the model, lore that is switched off is left out.
    assert [e.name for e in document_from_story(plain, for_model=True).lore] == ["Calder Keep"]
    parsed = parse_plot_markdown(render_plot_markdown(document).text)
    assert parsed.ok and parsed.scenario.plot is None
    changes = plot_changes(document, plain, begun=False)
    assert changes.plot is None and not changes.characters and not changes.new_cast


def _plot_for(document) -> None:
    """A plot added to a story's document as the editor or the model would:
    a hidden newcomer, a hidden place, one of the story's entries made a place."""
    keep = document.lore.pop(0)
    document.places.append(keep)
    document.places.append(
        EntryDoc(name="The Drowned Stair", hidden=True, description="Steps under the sea.")
    )
    document.characters.append(
        CharacterDoc(
            name="Teodor Vask",
            role="storyteller",
            hidden=True,
            description="A customs captain from the capital.",
        )
    )
    document.facts.append(FactDoc(name="alarm", initial="no", meaning="whether the keep is roused"))
    document.events.append(
        EventDoc(
            title="The captain comes",
            timeline=True,
            when=(3, 5),
            brings=["Teodor Vask"],
            reveals=["The Drowned Stair"],
            requires=[ConditionDoc(kind="at", place="Calder Keep")],
            tell="Teodor Vask rides up to the gate.",
            aftermath="A captain from the capital is at the keep.",
        )
    )
    document.events.append(
        EventDoc(
            title="Old Tobin talks",
            requires=[
                ConditionDoc(kind="happened", event="The captain comes"),
                ConditionDoc(kind="visited", place="Calder Keep", who="Maela Orr"),
            ],
            trigger="Someone asks Old Tobin about the captain.",
            tell="Old Tobin says what he saw.",
        )
    )


def test_the_document_s_plot_goes_to_the_story_keyed_to_its_own_cards(
    plain: StoryBundle, tmp_path: Path
):
    session = _session(plain, tmp_path)
    before = {c.id: c.model_dump() for c in (*session.cast, *session.supporting)}
    lore_before = {e.id: e.model_dump() for e in session.bundle.lore}
    document = document_from_story(session.bundle)
    _plot_for(document)
    assert session.set_plot_document(document) == []

    plot = session.story.plot
    vask = next(c for c in session.cast if c.name == "Teodor Vask")
    stair = next(e for e in session.bundle.lore if e.title == "The Drowned Stair")
    assert vask.plot_hidden and stair.plot_hidden
    captain, talks = plot.events
    assert captain.brings == [vask.id] and captain.reveals == [stair.id]
    assert [(p.name, p.lore_id) for p in plot.places] == [
        ("Calder Keep", "lore-keep"),
        ("The Drowned Stair", stair.id),
    ]
    assert talks.requires[1].who == "char-maela"
    assert talks.requires[0].event_id == captain.id
    # The story's own cards and entries are exactly as they were.
    for card in (*session.cast, *session.supporting):
        if card.id in before:
            assert card.model_dump() == before[card.id]
    for entry in session.bundle.lore:
        if entry.id in lore_before:
            assert entry.model_dump() == lore_before[entry.id]
    # Hidden from every model until the event brings them in.
    assert {vask.id, stair.id} <= session.hidden_ids()
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert saved.story.plot is not None and "Teodor Vask" in [c.name for c in saved.cast]

    # Opened again, the document is the story with its plot: saved as it is,
    # nothing changes and nothing is added twice.
    again = document_from_story(session.bundle)
    assert [e.title for e in again.events] == ["The captain comes", "Old Tobin talks"]
    assert [p.name for p in again.places] == ["Calder Keep", "The Drowned Stair"]
    assert session.set_plot_document(again) == []
    assert [c.name for c in session.cast].count("Teodor Vask") == 1
    assert len(session.bundle.lore) == 3


def test_only_a_hidden_card_s_words_follow_the_document(plain: StoryBundle, tmp_path: Path):
    """A card the story shows is the story's, changed on the Cast page; one
    the plot keeps hidden is the plot's, changed in the plot editor."""
    session = _session(plain, tmp_path)
    document = document_from_story(session.bundle)
    _plot_for(document)
    session.set_plot_document(document)
    document = document_from_story(session.bundle)
    serrik = next(c for c in document.characters if c.name == "Serrik Vaun")
    serrik.description = "Rewritten in the editor."
    serrik.name = "Serrik the Bold"
    vask = next(c for c in document.characters if c.name == "Teodor Vask")
    vask.description = "Grey and patient, and very hard to lie to."
    # As the editor renames: the events' references follow.
    document.rename_character("Teodor Vask", "Teodor Vasko")
    vask.role = "supporting"
    session.set_plot_document(document)
    card = next(c for c in session.cast if c.id == "char-serrik")
    assert card.name == "Serrik Vaun" and card.full_description is None
    moved = next(c for c in session.supporting if c.name == "Teodor Vasko")
    assert moved.full_description == "Grey and patient, and very hard to lie to."
    assert not moved.is_player_available
    # Still the one the event brings in, under the new name.
    assert session.story.plot.events[0].brings == [moved.id]


def test_a_story_under_way_can_t_hide_who_it_has_shown(plain: StoryBundle, tmp_path: Path):
    plain.nodes = make_exchange(1)
    plain.story.active_leaf_id = "a0"
    session = _session(plain, tmp_path)
    document = document_from_story(session.bundle)
    next(c for c in document.characters if c.name == "Captain Idris").hidden = True
    document.facts.append(FactDoc(name="alarm", initial="no"))
    notes = session.set_plot_document(document)
    assert any("Captain Idris" in note for note in notes)
    assert not next(c for c in session.cast if c.name == "Captain Idris").plot_hidden
    # Before it begins, the plot may hide anyone it brings in later.
    fresh = _session(
        StoryBundle(story=Story(title="Fresh"), cast=[Character(id="c-a", name="Ada")]),
        tmp_path,
    )
    document = document_from_story(fresh.bundle)
    document.characters[0].hidden = True
    document.events.append(EventDoc(title="Ada comes", brings=["Ada"], tell="Ada arrives."))
    assert fresh.set_plot_document(document) == []
    assert fresh.cast[0].plot_hidden and fresh.story.plot.events[0].brings == ["c-a"]


def test_a_card_s_own_headings_and_fences_stay_in_its_description(tmp_path: Path):
    """SillyTavern cards come with Markdown: "## Appearance" under a card read
    as a character of its own, and a fence left open hid every heading after it."""
    description = "Tall.\n\n## Appearance\n\nGrey eyes.\n\n# Notes\n\n```\n# not a heading"
    bundle = StoryBundle(
        story=Story(title="Cards", world_bible="# Rules\n\nNo magic.\n\n## Places\n\nOne town."),
        cast=[
            Character(id="c-a", name="Ada", full_description=description),
            Character(id="c-b", name="Bo", full_description="- voice: gruff\n\nA smith."),
        ],
    )
    document = document_from_story(bundle)
    parsed = parse_plot_markdown(render_plot_markdown(document).text)
    assert parsed.ok
    assert [c.name for c in parsed.scenario.cast] == ["Ada", "Bo"]
    ada, bo = parsed.scenario.cast
    assert "Grey eyes." in ada.full_description and "Notes" in ada.full_description
    assert bo.voice_notes is None and "voice: gruff" in bo.full_description
    assert "No magic." in parsed.scenario.world_bible
    assert "One town." in parsed.scenario.world_bible
    assert safe_prose("# A\n\ntext", under=1) == "## A\n\ntext"
    # Nothing to change: the story's text is never written back from here.
    assert plot_changes(document, bundle, begun=False).characters == []


def test_a_card_the_story_lost_is_said_and_never_brought_in(plain: StoryBundle):
    document = document_from_story(plain)
    document.events.append(EventDoc(title="Tobin comes", brings=["Old Tobin"], tell="He comes."))
    plain.supporting.characters.clear()
    changes = plot_changes(document, plain, begun=False)
    assert any("Old Tobin" in note for note in changes.notes)
    assert changes.plot.events[0].brings == []
    assert not changes.new_supporting, "a card the story lost is never made again"


def test_a_document_with_no_plot_takes_the_story_s_away(plain: StoryBundle, tmp_path: Path):
    session = _session(plain, tmp_path)
    document = document_from_story(session.bundle)
    _plot_for(document)
    session.set_plot_document(document)
    assert session.story.plot is not None
    session.set_plot_document(without_plot(document_from_story(session.bundle)))
    assert session.story.plot is None


def test_a_document_the_importer_refuses_says_why_and_changes_nothing(
    plain: StoryBundle, tmp_path: Path
):
    session = _session(plain, tmp_path)
    document = document_from_story(session.bundle)
    document.events.append(
        EventDoc(
            title="Broken",
            requires=[ConditionDoc(kind="fact", fact="nowhere", value="yes")],
            tell="Nothing.",
        )
    )
    with pytest.raises(PlotDocumentError, match="nowhere"):
        session.set_plot_document(document)
    assert session.story.plot is None
    document.story_id = "another-story"
    with pytest.raises(ValueError, match="another story"):
        plot_changes(document, session.bundle, begun=False)


def test_a_chat_and_a_private_scene_refuse_a_plot(plain: StoryBundle, tmp_path: Path):
    session = _session(plain, tmp_path)
    document = document_from_story(session.bundle)
    session.story.mode = "chat"
    with pytest.raises(ValueError, match="simple chat"):
        session.set_plot_document(document)


def test_a_plot_written_for_a_story_starts_at_the_hour_its_scene_names(
    plain: StoryBundle, tmp_path: Path
):
    """A story with no plot has no clock, and a plot given to it began at
    00:00: live, the director took a mid-morning opening for the small hours
    and began a night's event in it four turns running."""
    plain.story.setup.starting_scene.time_of_day = "Mid-morning"
    document = document_from_story(plain)
    assert (document.start_day, document.start_time) == (1, "10:00")
    document.facts.append(FactDoc(name="door", initial="closed", values=["closed", "open"]))
    document.events.append(EventDoc(title="A knock", kind="optional", trigger="Someone knocks."))
    session = _session(plain, tmp_path)
    session.set_plot_document(document)
    assert session.story.plot.start_minutes == 10 * 60

    # Words that name no hour leave the clock where it was, and a story under
    # way is not given one from where it began.
    plain.story.plot = None
    plain.story.setup.starting_scene.time_of_day = "Stardate 4512"
    assert document_from_story(plain).start_time == "00:00"
    plain.story.setup.starting_scene.time_of_day = "Dusk"
    plain.nodes.extend(make_exchange(1))
    assert document_from_story(plain).start_time == "00:00"


# --- the storyteller's notes -----------------------------------------------------------


def test_the_story_s_notes_are_in_its_document_and_stay_without_its_plot(plain: StoryBundle):
    plain.story.storyteller_notes = "The keep's well is poisoned."
    plain.story.storyteller_notes_by = "model"
    document = document_from_story(plain)
    assert document.storyteller_notes == "The keep's well is poisoned."
    _plot_for(document)
    # They are the story's, not the plot's.
    assert without_plot(document).storyteller_notes == "The keep's well is poisoned."
    assert "# Storyteller's notes\n\nThe keep's well" in render_plot_markdown(document).text
    # Unchanged in the document, they are no change to the story.
    assert plot_changes(document, plain, begun=False).storyteller_notes is None
    document.storyteller_notes = "The cistern is poisoned."
    assert plot_changes(document, plain, begun=False).storyteller_notes == (
        "The cistern is poisoned."
    )
    document.storyteller_notes = ""
    assert plot_changes(document, plain, begun=False).storyteller_notes == ""


def test_a_document_s_notes_reach_the_story_as_whoever_wrote_them(
    plain: StoryBundle, tmp_path: Path
):
    """Changed by hand they are the author's, who has read them. From the
    plot's writer they are a model's, unread in Setup, and the plot keeps
    their fingerprint (never their words) so it can tell they are its own."""
    session = _session(plain, tmp_path)
    story = session.story
    document = document_from_story(session.bundle)
    _plot_for(document)
    session.set_plot_document(document)
    assert story.storyteller_notes is None and story.plot.notes_given == ""
    assert not notes_from_plot(story)

    document = document_from_story(session.bundle)
    document.storyteller_notes = "The keep's well is poisoned."
    session.set_plot_document(document, notes_by="model")
    assert story.storyteller_notes == "The keep's well is poisoned."
    assert story.storyteller_notes_by == "model"
    assert story.plot.notes_given == notes_mark("The keep's well is poisoned.")
    assert "poisoned" not in story.plot.model_dump_json()
    assert notes_from_plot(story)
    saved = load_story_bundle(story.id, root=tmp_path).story
    assert notes_from_plot(saved)

    # The same document again changes nothing, and the mark is kept.
    session.set_plot_document(document_from_story(session.bundle), notes_by="model")
    assert notes_from_plot(story)
    # Read or changed by the author, they are the story's from then on.
    story.storyteller_notes_by = "author"
    assert not notes_from_plot(story)
    story.storyteller_notes_by = "model"
    story.storyteller_notes = "Rewritten by a follow-up on the story."
    assert not notes_from_plot(story)

    # Changed through a document by hand: the author's, and no longer the plot's.
    document = document_from_story(session.bundle)
    document.storyteller_notes = "The cistern is poisoned."
    session.set_plot_document(document)
    assert (story.storyteller_notes, story.storyteller_notes_by) == (
        "The cistern is poisoned.",
        "author",
    )
    assert story.plot.notes_given == "" and not notes_from_plot(story)
    # Taken away, they are nobody's.
    document = document_from_story(session.bundle)
    document.storyteller_notes = ""
    session.set_plot_document(document, notes_by="model")
    assert (story.storyteller_notes, story.storyteller_notes_by) == (None, "author")
