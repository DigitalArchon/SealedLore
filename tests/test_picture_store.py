"""A memory-only chat's pictures: held in memory, written nowhere, gone with
the chat (storage/picture_store.py). A story's pictures stay on disk exactly
as before (test_images.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.images import ImageRequest, run_image_request
from sealedlore.ids import new_id
from sealedlore.models.image import RefUse
from sealedlore.providers.base import ProviderError
from sealedlore.storage.images import IMAGES_FILE
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from tests.test_images import PNG, image_client, reply


def listing(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def memory_store() -> tuple[MemoryPictures, list[dict]]:
    log: list[dict] = []

    def write_log(kind: str, body: dict) -> str:
        entry_id = new_id()
        log.append({"id": entry_id, "kind": kind, **body})
        return entry_id

    return MemoryPictures(write_log), log


def request(references: tuple[RefUse, ...] = ()) -> ImageRequest:
    return ImageRequest(
        story_id="memory-chat",
        model="hidream",
        prompt="A lighthouse at dusk.",
        size="1024x1024",
        n=1,
        references=references,
        anchor_node_id=None,
    )


def test_a_picture_for_a_memory_chat_is_kept_in_memory_only(tmp_path: Path):
    before = listing(tmp_path)
    store, log = memory_store()
    store.write("images/refs/added.png", PNG)
    use = RefUse(
        owner_kind="story",
        owner_id="memory-chat",
        owner_name="Added picture",
        ref_id="added",
        file="images/refs/added.png",
    )
    sent: list = []
    records = run_image_request(
        image_client(lambda r: sent.append(r) or reply()), request((use,)), store=store
    )
    store.add_images(records)
    assert listing(tmp_path) == before, "a memory chat's picture reached the disk"
    assert sent, "the reference went with the request"
    assert [image.id for image in store.images()] == [records[0].id]
    assert store.read(records[0].file) is not None
    assert [entry["kind"] for entry in log] == ["image_request", "image_response"]


def test_a_picture_finishing_after_the_chat_closed_is_dropped():
    store, _log = memory_store()
    store.close()
    with pytest.raises(ProviderError, match="closed"):
        run_image_request(image_client(lambda r: reply()), request(), store=store)
    store.add_images([])
    assert store.images() == [] and store.all_files() == {}


def test_a_memory_store_travels_through_its_backup_and_back():
    store, _log = memory_store()
    records = run_image_request(image_client(lambda r: reply()), request(), store=store)
    store.add_images(records)
    store.write("images/background.png", PNG)
    files = store.all_files()
    assert IMAGES_FILE in files and "images/background.png" in files
    again, _ = memory_store()
    restored = MemoryPictures(again._log, files)
    assert [image.id for image in restored.images()] == [records[0].id]
    assert restored.read(records[0].file) == store.read(records[0].file)
    restored.remove_image(records[0].id)
    assert restored.images() == [] and restored.read(records[0].file) is None


def test_a_memory_store_refuses_a_path_outside_the_images():
    store, _log = memory_store()
    for bad in ("../escape.png", "/etc/passwd", "images/../x.png", IMAGES_FILE, "notes.txt"):
        with pytest.raises(ValueError):
            store.write(bad, PNG)


def test_the_disk_store_is_the_story_folder(tmp_path: Path, story):
    saved = StoryBundle(story=story)
    save_story_bundle(saved, root=tmp_path)
    store = DiskPictures(saved.story.id, tmp_path)
    store.write("images/refs/a.png", PNG)
    assert (tmp_path / "stories" / saved.story.id / "images/refs/a.png").read_bytes() == PNG
    assert store.alive()
    store.remove("images/refs/a.png")
    assert store.read("images/refs/a.png") is None


def test_a_memory_chats_backup_restored_to_disk_is_an_ordinary_chat(tmp_path: Path):
    """A chat loaded from disk and still marked "memory" could never save."""
    from sealedlore.models.story import Story
    from sealedlore.storage.archive import read_archive, restore_archive, write_archive
    from sealedlore.storage.repository import load_story_bundle

    store, log = memory_store()
    records = run_image_request(image_client(lambda r: reply()), request(), store=store)
    store.add_images(records)
    chat = StoryBundle(story=Story(title="Incognito", mode="chat", chat_keep="memory"))
    path = tmp_path / "out" / "chat.sealedlore-backup.json"
    path.parent.mkdir()
    write_archive(path, chat, log, store.all_files())
    bundle, api_log = read_archive(path)
    assert bundle.story.chat_keep == "memory", "the backup remembers where it was kept"
    restored = restore_archive(bundle, api_log, root=tmp_path / "data")
    loaded = load_story_bundle(restored.story.id, root=tmp_path / "data")
    assert loaded.story.chat_keep == "disk"
    disk = DiskPictures(restored.story.id, tmp_path / "data")
    assert [image.id for image in disk.images()] == [records[0].id]
    assert disk.read(records[0].file) == store.read(records[0].file)
