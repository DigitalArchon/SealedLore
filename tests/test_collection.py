"""The author's own characters, kept outside any story (storage/collection.py):
where they are kept, versions, copies, search, and a SillyTavern card read in."""

from __future__ import annotations

from pathlib import Path

from sealedlore.models.collection import CollectionInfo
from sealedlore.models.image import ImageRef
from sealedlore.models.story import Story
from sealedlore.storage import collection
from sealedlore.storage.archive import restore_archive
from sealedlore.storage.images import REFS_DIR
from sealedlore.storage.library import story_listings
from sealedlore.storage.paths import COLLECTION_ID, collection_dir, is_safe_story_id, story_dir
from sealedlore.storage.picture_store import DiskPictures
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.tavern_cards import read_card
from tests.test_tavern import ELENA, png_card, tiny_png, v2


def store(root: Path) -> DiskPictures:
    return DiskPictures(COLLECTION_ID, root)


def test_they_are_kept_beside_the_stories_never_among_them(tmp_path: Path):
    root = tmp_path / "data"
    bundle = collection.load_collection(root)
    assert bundle.story.id == COLLECTION_ID and bundle.cast == []
    assert not collection_dir(root).exists()  # nothing is written before the first

    bundle.cast.append(collection.kept("Elena"))
    save_story_bundle(bundle, root=root)
    assert (root / "characters" / "cast.json").is_file()
    assert story_dir(COLLECTION_ID, root) == root / "characters"
    save_story_bundle(StoryBundle(story=Story(title="Doomsville")), root=root)
    assert [listing.title for listing in story_listings(root)] == ["Doomsville"]

    again = collection.load_collection(root)
    assert [c.name for c in again.cast] == ["Elena"]
    assert again.cast[0].collection is not None and again.cast[0].tavern is not None


def test_an_archive_naming_their_folder_is_given_a_story_of_its_own(tmp_path: Path):
    root = tmp_path / "data"
    assert not is_safe_story_id(COLLECTION_ID)
    bundle = StoryBundle(story=Story(id=COLLECTION_ID, title="Sneaky"))
    restored = restore_archive(bundle, [], root)
    assert restored.story.id != COLLECTION_ID
    assert not collection_dir(root).exists()


def test_a_card_read_in_keeps_every_field_and_its_names_as_written(tmp_path: Path):
    root = tmp_path / "data"
    card = read_card(png_card(tmp_path, {"chara": v2()}))
    character, notes = collection.kept_from_card(card, store(root), came_from="elena.png")

    assert character.name == "Elena"
    assert character.full_description.startswith("{{char}} keeps the farmhouse")
    assert character.personality == ELENA["personality"]
    assert character.voice_notes is None  # the examples stay the card's own
    assert character.tavern.mes_example == ELENA["mes_example"]
    assert character.tavern.first_mes == ELENA["first_mes"]
    assert character.collection.came_from == "elena.png"
    portrait = character.reference_images[0]
    assert portrait.file.startswith(f"{REFS_DIR}/") and portrait.caption == "portrait"
    assert (root / "characters" / portrait.file).read_bytes() == card.picture
    assert any("description" in note for note in notes)  # the note at a depth


def test_a_copy_has_pictures_of_its_own(tmp_path: Path):
    root = tmp_path / "data"
    pictures = store(root)
    elena = collection.kept("Elena")
    pictures.write(f"{REFS_DIR}/a.png", tiny_png())
    elena.reference_images.append(ImageRef(id="a", file=f"{REFS_DIR}/a.png", look="coat"))

    version = collection.copied(elena, pictures, family=elena.id, version="After the war")
    assert version.id != elena.id
    assert version.collection.family == elena.id and version.collection.version == "After the war"
    ref = version.reference_images[0]
    assert ref.file != elena.reference_images[0].file and ref.look == "coat"
    assert pictures.read(ref.file) == tiny_png()


def test_versions_go_with_their_main_card(tmp_path: Path):
    bundle = collection.new_collection()
    elena = collection.kept("Elena")
    later = collection.kept("Elena")
    later.collection = CollectionInfo(family=elena.id, version="After the war")
    john = collection.kept("John")
    bundle.cast += [later, elena, john]

    assert collection.mains(bundle.cast) == [elena, john]
    assert collection.family_of(later, bundle.cast) == [elena, later]
    assert collection.version_name(elena, bundle.cast) == collection.MAIN_CARD
    assert collection.version_name(later, bundle.cast) == "After the war"

    collection.forget(bundle, elena)
    # Her version is the main card now.
    assert collection.mains(bundle.cast) == [later, john]
    assert later.collection.family == "" and later.collection.version == ""


def test_forgetting_one_gives_back_only_the_pictures_that_were_theirs():
    bundle = collection.new_collection()
    elena, copy = collection.kept("Elena"), collection.kept("Elena (copy)")
    elena.reference_images = [
        ImageRef(file="images/refs/a.png"),
        ImageRef(file="images/refs/b.png"),
    ]
    copy.reference_images = [ImageRef(file="images/refs/b.png")]
    bundle.cast += [elena, copy]
    assert collection.forget(bundle, elena) == ["images/refs/a.png"]


def test_search_reads_the_whole_card_and_tags_are_listed_once():
    elena = collection.kept("Elena")
    elena.full_description = "Keeps the farmhouse north of Hellsville."
    elena.tavern.tags = ["Survival", "farm"]
    john = collection.kept("John")
    john.tavern.tags = ["survival "]

    assert collection.matches(elena, "farmhouse hellsville")
    assert not collection.matches(john, "farmhouse")
    assert collection.matches(john, "SURVIVAL")
    assert collection.tags_of([elena, john]) == ["farm", "Survival"]
    assert collection.has_tag(john, "Survival")


def test_a_name_taken_is_numbered():
    taken = [collection.kept("Elena"), collection.kept("Elena (2)")]
    assert collection.unique_name("John", taken) == "John"
    assert collection.unique_name("Elena", taken) == "Elena (3)"


def test_a_character_is_found_by_their_greetings_prompts_and_lorebook():
    """The search read their name, description and tags; a line from a
    greeting or a lorebook entry found nobody."""
    from sealedlore.models.tavern import TavernFields

    elena = collection.kept("Elena")
    elena.tavern = TavernFields(
        alternate_greetings=["The barn door creaks open."],
        system_prompt="Answer as the farmer would.",
        character_book={"entries": [{"keys": ["Hellsville"], "content": "Walled and wary."}]},
    )
    for words in ("barn door", "farmer", "hellsville", "walled wary"):
        assert collection.matches(elena, words), words
    assert not collection.matches(elena, "submarine")
