"""A document made a story (engine/story_plot.py `apply_document`): the plot
editor's Save. The author (Nov 6 2026): in the editor anything is editable,
but it saves as a new story, never over the story it was made from. So the
new story is a fresh playthrough of the old one's setup with the document
applied, and only what the author changed in the editor is written: what a
plot file can't hold (skills, a card's SillyTavern fields, how a picture was
drawn, lore that waits to be mentioned) comes along untouched."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.session import StorySession
from sealedlore.engine.story_plot import PlotDocumentError, apply_document, document_from_story
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.plot_file import CharacterDoc, EntryDoc, PictureDoc
from sealedlore.models.story import Story
from sealedlore.models.tavern import TavernFields
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario, fresh_playthrough
from tests.conftest import make_exchange
from tests.test_session import make_config
from tests.test_story_plot import _meaning, _plot_for

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [
    ROOT / "SampleStories" / "Doomsville.md",
    ROOT / "tests" / "data" / "doomsville_extended.md",
]
PNG = b"\x89PNG\r\n\x1a\n" + b"picture" * 4


@pytest.fixture
def source(story: Story, cast: list[Character]) -> StoryBundle:
    """A story with what no plot file holds: skills, a card's SillyTavern
    fields, a drawn picture's record, lore that waits to be mentioned."""
    story.setup.opening_text = "Torchlight in the undercroft."
    story.setup.starting_scene = story.scene.model_copy(deep=True)
    serrik, maela, _idris = cast
    serrik.competence.tiers = {"swordplay": "legendary"}
    serrik.tavern = TavernFields(creator="someone", tags=["duellist"])
    serrik.full_description = "A duellist.\n\n## Appearance\n\nGrey at the temples."
    serrik.reference_images = [
        ImageRef(id="ref-1", file="images/refs/ref-1.png", caption="front", prompt="a duellist"),
        ImageRef(id="ref-2", file="images/refs/ref-2.png", caption="side", model="klein"),
    ]
    maela.competence.tiers = {"locks": "expert"}
    bundle = StoryBundle(
        story=story,
        cast=cast,
        lore=[
            LoreEntry(
                id="lore-keep",
                title="Calder Keep",
                content="A keep on the cliffs, empty for a hundred years.",
                keywords=["the keep"],
                until_mentioned=True,
            ),
            LoreEntry(id="lore-oath", title="The Grey Oath", content="What the duellists swear."),
        ],
    )
    bundle.supporting.characters.append(
        Character(id="char-tobin", name="Old Tobin", summary="Keeps the gate.")
    )
    return bundle


def _files(bundle: StoryBundle) -> dict[str, bytes]:
    return {ref.file: PNG for c in bundle.cast for ref in c.reference_images}


def _copy(bundle: StoryBundle, title: str = "The Sundering (copy)") -> StoryBundle:
    return fresh_playthrough(bundle, title, _files(bundle))


def _doc(document, name: str):
    return next(c for c in document.characters if c.name == name)


def test_the_new_story_has_the_editor_s_changes_and_the_old_one_is_untouched(
    source: StoryBundle, tmp_path: Path
):
    save_story_bundle(source, root=tmp_path)
    on_disk = load_story_bundle(source.story.id, root=tmp_path).model_dump()
    kept = source.model_dump()
    before = document_from_story(source, whole=True)
    document = before.model_copy(deep=True)
    document.world = "The keep fell in the night."
    _doc(document, "Serrik Vaun").summary = "A duellist with nothing left to lose."
    _doc(document, "Maela Orr").role = "supporting"
    document.characters.remove(_doc(document, "Captain Idris"))
    _plot_for(document)
    _doc(document, "Teodor Vask").pictures = [PictureDoc(file="pictures/vask.png", caption="him")]
    document.lore[0].description = "What the duellists swear, and what it costs."
    document.lore.append(EntryDoc(name="The Salt Road", description="The way to the coast."))

    new = _copy(source)
    notes = apply_document(
        document, new, before=before, read=lambda f: PNG if f == "pictures/vask.png" else None
    )

    assert notes == []
    assert source.model_dump() == kept, "the story it was made from never changes"
    assert load_story_bundle(source.story.id, root=tmp_path).model_dump() == on_disk
    assert new.story.id != source.story.id and new.story.title == "The Sundering (copy)"
    assert new.story.world_bible == "The keep fell in the night."
    serrik = next(c for c in new.cast if c.id == "char-serrik")
    assert serrik.summary == "A duellist with nothing left to lose."
    assert serrik.competence.tiers == {"swordplay": "legendary"}
    assert serrik.tavern is not None and serrik.tavern.tags == ["duellist"]
    assert [(r.file, r.prompt, r.model) for r in serrik.reference_images] == [
        ("images/refs/ref-1.png", "a duellist", ""),
        ("images/refs/ref-2.png", "", "klein"),
    ]
    assert "## Appearance" in serrik.full_description, "unchanged text as the story had it"
    assert [c.name for c in new.cast] == ["Serrik Vaun", "Teodor Vask"]
    maela = next(c for c in new.supporting.characters if c.name == "Maela Orr")
    assert maela.id == "char-maela" and maela.competence.tiers == {}
    assert not maela.is_player_available
    assert "char-idris" not in {c.id for c in (*new.cast, *new.supporting.characters)}
    assert "char-idris" not in new.story.setup.starting_scene.present_character_ids
    vask = next(c for c in new.cast if c.name == "Teodor Vask")
    assert vask.plot_hidden and [r.caption for r in vask.reference_images] == ["him"]
    assert new.pending_files[vask.reference_images[0].file] == PNG
    keep = next(e for e in new.lore if e.id == "lore-keep")
    assert keep.until_mentioned, "what no file holds stays"
    oath = next(e for e in new.lore if e.id == "lore-oath")
    assert oath.content == "What the duellists swear, and what it costs."
    assert [e.title for e in new.lore][-2:] == ["The Drowned Stair", "The Salt Road"]
    plot = new.story.plot
    assert plot is not None and plot.events[0].brings == [vask.id]
    assert [(p.name, p.lore_id) for p in plot.places][0] == ("Calder Keep", "lore-keep")
    assert new.story.scene == new.story.setup.starting_scene
    assert new.nodes == []


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_an_unchanged_document_leaves_the_new_story_as_the_copy_made_it(path: Path):
    bundle = bundle_from_scenario(parse_plot_markdown(path.read_text()).scenario)
    before = document_from_story(bundle, whole=True)
    copy = _copy(bundle, bundle.story.title)
    expected = copy.model_dump()
    plot = copy.story.plot
    assert apply_document(before.model_copy(deep=True), copy, before=before) == []
    after = copy.model_dump()
    for part in ("cast", "lore", "supporting"):
        assert after[part] == expected[part], part
    story, was = after["story"], expected["story"]
    story.pop("plot"), was.pop("plot")
    assert story == was
    cards = SimpleNamespace(cast=copy.cast, supporting=copy.supporting.characters, lore=copy.lore)
    assert _meaning(copy.story.plot, cards) == _meaning(plot, cards)


def test_a_change_made_on_the_story_meanwhile_stays_unless_the_editor_made_one(
    source: StoryBundle,
):
    before = document_from_story(source, whole=True)
    document = before.model_copy(deep=True)
    _doc(document, "Maela Orr").description = "She talks less now."
    # Meanwhile, on the Cast page: Maela's summary and Serrik's voice.
    source.cast[1].summary = "A locksmith with a secret."
    source.cast[0].voice_notes = "Clipped."
    new = _copy(source)
    apply_document(document, new, before=before)
    maela = next(c for c in new.cast if c.id == "char-maela")
    assert maela.full_description == "She talks less now."
    assert maela.summary == "A locksmith with a secret."
    assert new.cast[0].voice_notes == "Clipped."


def test_whom_the_story_begins_with_follows_the_document(source: StoryBundle):
    before = document_from_story(source, whole=True)
    assert [c.name for c in before.characters if c.present] == ["Serrik Vaun", "Maela Orr"]
    document = before.model_copy(deep=True)
    _doc(document, "Maela Orr").present = False
    _doc(document, "Captain Idris").present = True
    _doc(document, "Old Tobin").present = True
    new = _copy(source)
    apply_document(document, new, before=before)
    start = new.story.setup.starting_scene
    assert start.present_character_ids == ["char-serrik", "char-idris"]
    assert start.present_others == ["Old Tobin"]
    assert new.story.scene.present_character_ids == ["char-serrik", "char-idris"]


def test_pictures_follow_the_document_and_keep_their_records(source: StoryBundle):
    before = document_from_story(source, whole=True)
    assert [p.file for p in _doc(before, "Serrik Vaun").pictures] == [
        "images/refs/ref-1.png",
        "images/refs/ref-2.png",
    ]
    document = before.model_copy(deep=True)
    serrik = _doc(document, "Serrik Vaun")
    serrik.pictures = [
        PictureDoc(file="pictures/new.png", caption="in armour", look="armour"),
        serrik.pictures[0].model_copy(update={"expression": "angry"}),
    ]
    new = _copy(source)
    assert "images/refs/ref-2.png" in new.pending_files
    apply_document(document, new, before=before, read=lambda _f: PNG)
    refs = next(c for c in new.cast if c.id == "char-serrik").reference_images
    assert [(r.caption, r.look, r.expression) for r in refs] == [
        ("in armour", "armour", ""),
        ("front", "", "angry"),
    ]
    assert refs[1].id == "ref-1" and refs[1].prompt == "a duellist", "its record kept"
    assert refs[0].file.startswith("images/refs/") and new.pending_files[refs[0].file] == PNG
    assert "images/refs/ref-2.png" not in new.pending_files, "a picture taken off isn't written"


def test_a_picture_that_can_t_be_read_is_left_out_and_said(source: StoryBundle):
    before = document_from_story(source, whole=True)
    document = before.model_copy(deep=True)
    document.characters.append(
        CharacterDoc(name="Teodor Vask", pictures=[PictureDoc(file="pictures/gone.png")])
    )
    new = _copy(source)
    notes = apply_document(document, new, before=before)
    assert notes == [
        "Teodor Vask's picture pictures/gone.png couldn't be read, so it was left out."
    ]
    assert next(c for c in new.cast if c.name == "Teodor Vask").reference_images == []


def test_whoever_was_set_aside_is_back_for_the_new_playthrough(source: StoryBundle, tmp_path):
    source.story.setup.choose_one_of = ["char-serrik", "char-maela"]
    session = StorySession(
        source,
        make_config(),
        MockChatProvider([]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    session.set_aside_unchosen("char-serrik")
    assert [c.name for c in source.cast] == ["Serrik Vaun", "Captain Idris"]
    before = document_from_story(source, whole=True)
    assert [(c.name, c.choose) for c in before.characters if c.role != "supporting"] == [
        ("Serrik Vaun", True),
        ("Captain Idris", False),
        ("Maela Orr", True),
    ]
    assert len(document_from_story(source).characters) == 3, "the story under way's alone"
    new = _copy(source)
    apply_document(before.model_copy(deep=True), new, before=before)
    assert sorted(c.name for c in new.cast) == ["Captain Idris", "Maela Orr", "Serrik Vaun"]
    assert new.story.setup.set_aside == []
    assert new.story.setup.choose_one_of == ["char-serrik", "char-maela"]


def test_a_story_that_has_begun_is_never_written_over(source: StoryBundle):
    source.nodes = make_exchange(1)
    before = document_from_story(source, whole=True)
    with pytest.raises(ValueError, match="has begun"):
        apply_document(before, source, before=before)


def test_a_document_the_importer_refuses_changes_nothing(source: StoryBundle):
    before = document_from_story(source, whole=True)
    document = before.model_copy(deep=True)
    _doc(document, "Serrik Vaun").hidden = True  # in the opening scene
    new = _copy(source)
    kept = new.model_dump()
    with pytest.raises(PlotDocumentError):
        apply_document(document, new, before=before)
    assert new.model_dump() == kept


def test_the_session_saves_a_story_it_hasn_t_begun_and_refuses_one_under_way(
    source: StoryBundle, tmp_path: Path
):
    save_story_bundle(source, root=tmp_path)
    session = StorySession(
        source,
        make_config(),
        MockChatProvider([]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    before = document_from_story(source, whole=True)
    document = before.model_copy(deep=True)
    document.opening.location = "The gatehouse"
    assert session.set_story_document(document, before=before) == []
    assert session.story.scene.location == "The gatehouse"
    reread = load_story_bundle(source.story.id, root=tmp_path)
    assert reread.story.setup.starting_scene.location == "The gatehouse"
    session.bundle.nodes = make_exchange(1)
    with pytest.raises(ValueError, match="has begun"):
        session.set_story_document(document, before=before)


def test_a_background_chosen_in_the_editor_or_taken_away(source: StoryBundle):
    source.story.background_image = "images/background.jpg"
    files = {**_files(source), "images/background.jpg": b"\xff\xd8\xff" + b"old" * 4}
    before = document_from_story(source, whole=True)
    assert before.background == "images/background.jpg"
    assert document_from_story(source).background == "", "only the editor's document"

    document = before.model_copy(update={"background": "pictures/sky.png"}, deep=True)
    new = fresh_playthrough(source, "Sky", files)
    apply_document(
        document, new, before=before, read=lambda f: PNG if f == "pictures/sky.png" else None
    )
    assert new.story.background_image == "images/background.png"
    assert new.pending_files["images/background.png"] == PNG
    assert "images/background.jpg" not in new.pending_files

    document = before.model_copy(update={"background": ""}, deep=True)
    new = fresh_playthrough(source, "None", files)
    apply_document(document, new, before=before)
    assert new.story.background_image is None and "images/background.jpg" not in new.pending_files

    unchanged = fresh_playthrough(source, "Same", files)
    apply_document(before.model_copy(deep=True), unchanged, before=before)
    assert unchanged.story.background_image == "images/background.jpg"
    assert "images/background.jpg" in unchanged.pending_files
