"""Untrusted files (backups, scenarios, cards, plot files, pictures and
videos) that could reach past what they should: each built here, small and
synthetic, against the reader that takes it in."""

from __future__ import annotations

import io
import json
import shutil
import struct
import tracemalloc
import zipfile
from pathlib import Path

import pytest

from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.scenario import Scenario
from sealedlore.models.story import Story
from sealedlore.models.video import GeneratedVideo
from sealedlore.storage import collection, collection_backup, tavern_cards
from sealedlore.storage.archive import (
    ArchiveError,
    archive_data,
    read_archive,
    restore_archive,
    write_archive,
)
from sealedlore.storage.collection_backup import (
    BACKUP_FORMAT,
    CHARACTERS,
    BackupError,
    new_log_entries,
    open_backup,
    restore,
)
from sealedlore.storage.images import is_safe_relative, is_story_file
from sealedlore.storage.library import story_listings
from sealedlore.storage.paths import COLLECTION_ID, collection_dir, stories_dir, story_dir
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures
from sealedlore.storage.repository import (
    StoryBundle,
    append_api_log,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from sealedlore.storage.scenario import ScenarioError, read_scenario
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


# --- a zip member larger than it says -----------------------------------------------------

MIB = 1024 * 1024


def lying_zip(members: dict[str, bytes], name: str, inflated: int, claimed: int = 1000) -> bytes:
    """A zip whose member `name` is `inflated` bytes of zeros and says it is
    `claimed`, in its central directory and its own header."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, data in members.items():
            archive.writestr(member, data)
        archive.writestr(name, bytes(inflated))
    raw = bytearray(buffer.getvalue())
    central = raw.rfind(b"PK\x01\x02")
    assert raw[central + 46 : central + 46 + len(name)] == name.encode()
    struct.pack_into("<I", raw, central + 24, claimed)
    local = struct.unpack_from("<I", raw, central + 42)[0]
    struct.pack_into("<I", raw, local + 22, claimed)
    return bytes(raw)


def peak_while(read) -> int:
    """The most memory Python held at once while `read` ran, in bytes."""
    tracemalloc.start()
    try:
        read()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_charx_member_that_lies_about_its_size_is_damaged_and_never_inflated_whole(tmp_path):
    card = {
        "spec": "chara_card_v3",
        "data": {
            "name": "Jane",
            "description": "d",
            "assets": [{"type": "other", "name": "big", "uri": "embeded://big.bin", "ext": "bin"}],
        },
    }
    path = tmp_path / "jane.charx"
    path.write_bytes(lying_zip({"card.json": json.dumps(card).encode()}, "big.bin", 8 * MIB))
    found: list = []
    peak = peak_while(lambda: found.append(tavern_cards.read_card(path)))
    assert peak < 2 * MIB, "a piece at a time, never the member whole"
    assert all(file.data is None for file in found[0].files)
    with zipfile.ZipFile(path) as archive:
        with pytest.raises(tavern_cards.CardError, match="damaged"):
            tavern_cards._member(archive, "big.bin", "jane.charx")


def test_an_expression_pack_member_that_lies_is_left_out_unread(tmp_path):
    path = tmp_path / "pack.zip"
    path.write_bytes(lying_zip({"sad.png": png(6)}, "joy.png", 8 * MIB))
    result: list = []
    peak = peak_while(lambda: result.append(tavern_cards.expressions_from_zip(path)))
    assert peak < 2 * MIB
    found, notes = result[0]
    assert [name for name, _data in found] == ["sad"] and notes


def test_a_characters_backup_member_that_lies_is_missing_never_inflated(tmp_path):
    jane = Character(name="Jane", reference_images=[ImageRef(file="images/refs/a.png")])
    members = {
        "manifest.json": json.dumps({"format": BACKUP_FORMAT, "version": 1}).encode(),
        "characters.json": json.dumps([jane.model_dump(mode="json")]).encode(),
    }
    path = tmp_path / "lying.zip"
    path.write_bytes(lying_zip(members, "files/images/refs/a.png", 8 * MIB))
    read: list = []
    with open_backup(path) as backup:
        peak = peak_while(lambda: read.append(backup.read("images/refs/a.png")))
    assert read == [None] and peak < 2 * MIB

    path.write_bytes(lying_zip({"manifest.json": members["manifest.json"]}, CHARACTERS, 8 * MIB))
    with pytest.raises(BackupError, match="damaged"):
        with open_backup(path):
            pass


def test_a_backups_log_is_read_a_line_at_a_time_and_only_when_put_back(tmp_path, monkeypatch):
    lines = b"\n" * (4 * MIB) + b'{"id": "e1", "kind": "image_response"}\n'
    lines += b'{"id": "long", "text": "' + b"x" * (200 * 1024) + b'"}\n{"id": "e2"}\n'
    lines += b'{"id": "e3"}'  # a last line with no newline
    path = tmp_path / "log.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"format": BACKUP_FORMAT, "version": 1}))
        archive.writestr("characters.json", "[]")
        archive.writestr("api_log.jsonl", lines)
    del lines
    monkeypatch.setattr(collection_backup, "MAX_LOG_LINE", 64 * 1024, raising=False)

    def only_open():
        with open_backup(path) as backup:
            return backup.characters

    opened = peak_while(only_open)
    entries: list = []
    with open_backup(path) as backup:
        peak = peak_while(lambda: entries.extend(new_log_entries(backup, [{"id": "e2"}])))
    assert opened < MIB, "opening it reads no log"
    assert [entry["id"] for entry in entries] == ["e1", "e3"], "the long line passed over"
    assert peak < 2 * MIB, "four million newlines cost nothing"


# --- a story folder copied by hand ----------------------------------------------------------


def test_a_story_folder_copied_by_hand_is_a_story_of_its_own(tmp_path):
    original = StoryBundle(story=Story(title="Doomsville"))
    save_story_bundle(original, root=tmp_path)
    store = DiskPictures(original.story.id, tmp_path)
    store.write("images/refs/john.png", png(7))
    append_api_log(original.story.id, {"id": "a1", "kind": "turn_response"}, root=tmp_path)
    shutil.copytree(story_dir(original.story.id, tmp_path), stories_dir(tmp_path) / "mycopy")

    copy = load_story_bundle("mycopy", root=tmp_path)
    assert copy.story.id == "mycopy"
    assert any("copied or renamed" in notice for notice in copy.notices)
    copy.story.title = "Doomsville, played again"
    save_story_bundle(copy, root=tmp_path)
    append_api_log(copy.story.id, {"id": "a2", "kind": "turn_response"}, root=tmp_path)
    DiskPictures(copy.story.id, tmp_path).write("images/refs/jane.png", png(8))

    again = load_story_bundle(original.story.id, root=tmp_path)
    assert again.story.title == "Doomsville" and not again.notices
    assert [e["id"] for e in read_api_log(original.story.id, root=tmp_path)] == ["a1"]
    assert store.read("images/refs/jane.png") is None
    reopened = load_story_bundle("mycopy", root=tmp_path)
    assert reopened.story.title == "Doomsville, played again" and not reopened.notices
    assert [e["id"] for e in read_api_log("mycopy", root=tmp_path)] == ["a1", "a2"]
    listed = {listing.id: listing.title for listing in story_listings(tmp_path)}
    assert listed == {original.story.id: "Doomsville", "mycopy": "Doomsville, played again"}


# --- nested deeper than anything can be saved -----------------------------------------------


def nested(levels: int) -> str:
    return "[" * levels + "]" * levels


def deep_character(levels: int) -> str:
    return '{"name": "Mallory", "tavern": {"extensions": {"x": ' + nested(levels) + "}}}"


def test_a_backup_of_characters_nested_too_deep_is_refused_before_anything_is_put_back(tmp_path):
    path = tmp_path / "deep.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"format": BACKUP_FORMAT, "version": 1}))
        archive.writestr("characters.json", "[" + deep_character(1000) + "]")
    with pytest.raises(BackupError, match="nested far deeper"):
        with open_backup(path):
            pass


def test_a_story_backup_nested_too_deep_is_refused_and_writes_nothing(tmp_path):
    data = archive_data(StoryBundle(story=Story(title="Doomsville")), [], {})
    text = json.dumps(data).replace('"cast": []', '"cast": [' + deep_character(1000) + "]")
    path = tmp_path / "deep.sealedlore-archive.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ArchiveError, match="nested far deeper"):
        read_archive(path)
    assert not stories_dir(tmp_path).exists()


def test_a_scenario_nested_too_deep_is_refused(tmp_path):
    scenario = Scenario(title="Doomsville").model_dump()
    text = json.dumps(scenario).replace('"cast": []', '"cast": [' + deep_character(1000) + "]")
    path = tmp_path / "deep.sealedlore-scenario.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ScenarioError, match="nested far deeper"):
        read_scenario(path)


def test_a_card_as_deep_as_a_card_may_be_still_travels(tmp_path):
    """A card's own JSON is read to 100 levels: in a backup of characters,
    a story backup or a scenario it sits a few levels down, and still reads."""
    card = json.loads(deep_character(tavern_cards.MAX_NESTING - 3))
    character = Character.model_validate(card)
    store = DiskPictures(COLLECTION_ID, tmp_path)
    path = tmp_path / "all.zip"
    collection_backup.write_backup(path, [character], store)
    with open_backup(path) as backup:
        assert [c.name for c in backup.characters] == ["Mallory"]
    archive = tmp_path / "story.sealedlore-archive.json"
    write_archive(archive, StoryBundle(story=Story(title="Doomsville"), cast=[character]), [])
    assert read_archive(archive)[0].cast[0].name == "Mallory"
