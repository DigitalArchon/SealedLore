"""Untrusted files (backups, scenarios, cards, plot files, pictures and
videos) that could reach past what they should: each built here, small and
synthetic, against the reader that takes it in."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.story import Story
from sealedlore.models.video import GeneratedVideo
from sealedlore.storage import collection
from sealedlore.storage.archive import read_archive, restore_archive, write_archive
from sealedlore.storage.collection_backup import BACKUP_FORMAT, open_backup, restore
from sealedlore.storage.images import is_safe_relative, is_story_file
from sealedlore.storage.paths import COLLECTION_ID, collection_dir, story_dir
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from tests.test_collection_backup import png


def backup_zip(path: Path, characters: list[dict], files: dict[str, bytes]) -> Path:
    """A backup of characters as anyone could make one."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": BACKUP_FORMAT, "version": 1}))
        archive.writestr("characters.json", json.dumps(characters))
        for name, data in files.items():
            archive.writestr("files/" + name, data)
    return path


# --- a record names the store's own records or folders ---------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "images.json",
        "videos.json",
        "images/refs",
        "images/assets",
        "images/videos",
        "images/REFS",
        "images//refs",
        "images/./assets",
        "images/refs/",
    ],
)
def test_no_record_may_name_the_records_or_the_stores_folders(relative: str):
    assert not is_safe_relative(relative)


def test_a_backups_own_records_still_travel_with_it():
    assert is_story_file("images.json") and is_story_file("videos.json")
    assert is_story_file("images/refs/a.png") and not is_story_file("story.json")


def test_a_backup_naming_images_json_neither_overwrites_nor_deletes_the_records(tmp_path):
    bundle = collection.new_collection()
    bundle.cast.append(collection.kept("Jane"))
    save_story_bundle(bundle, root=tmp_path)
    store = DiskPictures(COLLECTION_ID, tmp_path)
    store.write("images/drawn.png", png(1))
    store.add_images(
        [GeneratedImage(file="images/drawn.png", prompt="Jane in winter", model="m", size="1k")]
    )
    records = collection_dir(tmp_path) / "images.json"
    before = records.read_bytes()

    mallory = Character(name="Mallory", reference_images=[ImageRef(file="images.json")])
    folder = Character(name="Folder", reference_images=[ImageRef(file="images/refs")])
    path = backup_zip(
        tmp_path / "evil.zip",
        [mallory.model_dump(mode="json"), folder.model_dump(mode="json")],
        {"images.json": b"not json at all", "images/refs": png(2)},
    )
    with open_backup(path) as backup:
        done = restore(backup, bundle, store, replace=False)
    assert done.missing == 2, "said as missing, never written"
    assert records.read_bytes() == before
    assert [image.prompt for image in store.images()] == ["Jane in winter"]
    assert not (collection_dir(tmp_path) / "images" / "refs").is_file()

    # Deleting the character hands its files to the store to remove: the
    # records stay.
    for file in collection.forget(bundle, next(c for c in bundle.cast if c.name == "Mallory")):
        store.remove(file)
    assert records.read_bytes() == before


def test_a_story_record_naming_images_json_never_removes_it(tmp_path):
    story = Story(title="Doomsville", background_image="images.json")
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    store = DiskPictures(story.id, tmp_path)
    store.write("images/real.png", png(3))
    store.add_images([GeneratedImage(file="images/real.png", prompt="p", model="m", size="1k")])
    store.put_video(
        GeneratedVideo(
            file="images.json",
            poster="videos.json",
            prompt="p",
            model="m",
            api="nanogpt",
            base_url="",
        )
    )
    records = story_dir(story.id, tmp_path) / "images.json"
    videos = story_dir(story.id, tmp_path) / "videos.json"
    store.remove_video(store.videos()[0].id)
    store.remove(story.background_image)
    assert records.is_file() and videos.is_file()
    assert [image.file for image in store.images()] == ["images/real.png"]
    with pytest.raises(ValueError):
        store.write("images.json", b"[]")


def test_a_memory_store_takes_no_record_as_a_picture():
    store = MemoryPictures(lambda kind, body: "", {"images/refs": png(4)})
    assert store.read("images/refs") is None
    with pytest.raises(ValueError):
        store.write("videos.json", b"[]")


def test_a_story_backup_still_brings_its_records_back(tmp_path):
    story = Story(title="Doomsville")
    bundle = StoryBundle(story=story)
    image = GeneratedImage(file="images/g.png", prompt="the farmhouse", model="m", size="1k")
    files = {
        "images/g.png": png(5),
        "images.json": json.dumps([image.model_dump(mode="json")]).encode(),
    }
    path = tmp_path / "backup.json"
    write_archive(path, bundle, [], files)
    read, log = read_archive(path)
    restored = restore_archive(read, log, root=tmp_path / "other")
    store = DiskPictures(restored.story.id, tmp_path / "other")
    assert [i.prompt for i in store.images()] == ["the farmhouse"]
    assert store.read("images/g.png") == png(5)
