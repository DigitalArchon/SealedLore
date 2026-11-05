"""A backup of all of Your characters, and putting one back
(storage/collection_backup.py): ids, versions, pictures, card files, what
was drawn for them and what it cost, back as they were, here or elsewhere."""

from __future__ import annotations

import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from sealedlore.models.collection import CollectionInfo
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.tavern import CardAsset, TavernFields
from sealedlore.storage import collection
from sealedlore.storage.collection_backup import (
    BACKUP_SUFFIX,
    BackupError,
    is_backup,
    new_log_entries,
    open_backup,
    restore,
    write_backup,
)
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import DiskPictures
from sealedlore.storage.repository import save_story_bundle
from tests.test_tavern import tiny_png


def png(red: int, green: int = 0, blue: int = 0) -> bytes:
    """A clean 1×1 PNG of one colour: a picture of its own, nothing hidden."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(bytes([0, red, green, blue]))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


GIF_WITH_COMMENT = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"\x21\xfe\x09GPS home\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)


def kept_folder(root: Path):
    """Elena (portrait, a card file) and her version, a picture drawn for her
    and its cost in the log."""
    bundle = collection.load_collection(root)
    store = DiskPictures(COLLECTION_ID, root)
    elena = collection.kept("Elena")
    elena.full_description = "{{char}} keeps the farmhouse."
    store.write("images/refs/p1.png", png(10))
    elena.reference_images = [ImageRef(id="p1", file="images/refs/p1.png", look="winter coat")]
    asset = CardAsset(type="background", name="barn", ext="png", path="assets/barn.png")
    asset.file = f"images/assets/{elena.id}/barn.png"
    store.write(asset.file, png(20))
    elena.tavern = TavernFields(assets=[asset])
    later = collection.kept("Elena")
    later.collection = CollectionInfo(family=elena.id, version="After the war")
    store.write("images/refs/p2.png", png(30))
    later.reference_images = [ImageRef(id="p2", file="images/refs/p2.png")]
    bundle.cast += [elena, later]
    save_story_bundle(bundle, root=root)
    store.write("images/drawn.png", png(40))
    store.add_images(
        [
            GeneratedImage(
                id="g1", file="images/drawn.png", prompt="Elena, older", model="m", size="1k"
            )
        ]
    )
    log = [{"id": "e1", "kind": "image_response", "usage": {"cost": 0.02}}]
    return bundle, store, log


def test_a_backup_brings_every_character_back_elsewhere_as_they_were(tmp_path: Path):
    bundle, store, log = kept_folder(tmp_path / "here")
    path = tmp_path / f"all{BACKUP_SUFFIX}"
    written = write_backup(path, bundle.cast, store, images=store.images(), log=log)
    assert written == 4 and is_backup(path)
    assert path.stat().st_mode & 0o077 == 0, "its log holds prompts: the author's alone"

    there = collection.load_collection(tmp_path / "there")
    there_store = DiskPictures(COLLECTION_ID, tmp_path / "there")
    with open_backup(path) as backup:
        done = restore(backup, there, there_store, replace=False)
        entries = new_log_entries(backup, [])
    assert done.added == ["Elena", "Elena"] and done.missing == 0
    assert [c.id for c in there.cast] == [c.id for c in bundle.cast], "ids kept: links hold"
    assert there.cast[1].collection.family == bundle.cast[0].id
    for character in bundle.cast:
        for file in collection.files_of(character):
            assert there_store.read(file) == store.read(file)
    assert [image.id for image in there_store.images()] == ["g1"]
    assert there_store.read("images/drawn.png") == store.read("images/drawn.png")
    assert entries == log
    assert new_log_entries_again(path, entries) == [], "the same log isn't added twice"


def new_log_entries_again(path: Path, log: list) -> list:
    with open_backup(path) as backup:
        return new_log_entries(backup, log)


def test_characters_here_already_are_kept_or_replaced_as_asked(tmp_path: Path):
    bundle, store, log = kept_folder(tmp_path / "here")
    path = tmp_path / f"all{BACKUP_SUFFIX}"
    write_backup(path, bundle.cast, store, images=store.images(), log=log)
    elena = bundle.cast[0]
    elena.full_description = "Changed since the backup."
    store.write("images/refs/new.png", png(50))
    elena.reference_images = [ImageRef(id="new", file="images/refs/new.png")]
    with open_backup(path) as backup:
        done = restore(backup, bundle, store, replace=False)
    assert done.kept == ["Elena", "Elena"] and done.added == []
    assert bundle.cast[0].full_description == "Changed since the backup."
    with open_backup(path) as backup:
        done = restore(backup, bundle, store, replace=True)
    assert done.replaced == ["Elena", "Elena"]
    assert bundle.cast[0].full_description == "{{char}} keeps the farmhouse."
    assert store.read("images/refs/new.png") is None, "the replaced one's own picture goes"
    assert store.read("images/refs/p1.png") == png(10)


def test_a_backups_picture_with_hidden_data_is_cleaned_or_left_out(tmp_path: Path):
    """A backup made here holds nothing hidden, but a backup may be anyone's."""
    path = tmp_path / f"theirs{BACKUP_SUFFIX}"
    elena = collection.kept("Elena")
    elena.reference_images = [
        ImageRef(id="a", file="images/refs/a.gif"),
        ImageRef(id="b", file="../../outside.png"),
    ]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 1})
        )
        archive.writestr("characters.json", json.dumps([elena.model_dump(mode="json")]))
        archive.writestr("files/images/refs/a.gif", GIF_WITH_COMMENT)
        archive.writestr("files/../../outside.png", tiny_png())
    there = collection.load_collection(tmp_path / "there")
    store = DiskPictures(COLLECTION_ID, tmp_path / "there")
    with open_backup(path) as backup:
        done = restore(backup, there, store, replace=False)
    assert done.missing == 2, "the GIF nothing cleaned, and the path out of the folder"
    assert store.read("images/refs/a.gif") is None
    assert not (tmp_path / "outside.png").exists()
    with open_backup(path) as backup:
        done = restore(backup, there, store, replace=True, clean=lambda data: tiny_png())
    assert store.read("images/refs/a.gif") == tiny_png(), "redrawn by the window's clean"


@pytest.mark.parametrize(
    ("make", "words"),
    [
        (lambda p: p.write_bytes(b"not a zip"), "isn't a backup of characters"),
        (lambda p: zipfile.ZipFile(p, "w").writestr("x.txt", "x"), "isn't a backup"),
        (
            lambda p: zipfile.ZipFile(p, "w").writestr(
                "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 99})
            ),
            "newer SealedLore",
        ),
    ],
)
def test_what_isnt_a_backup_is_refused_in_words(tmp_path: Path, make, words):
    path = tmp_path / "file.zip"
    make(path)
    assert not is_backup(path) or "newer" in words
    with pytest.raises(BackupError, match=words), open_backup(path):
        pass


def test_a_backup_with_damaged_characters_is_refused_in_words(tmp_path: Path):
    path = tmp_path / f"broken{BACKUP_SUFFIX}"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 1})
        )
        archive.writestr("characters.json", '[{"name": ')
    with pytest.raises(BackupError, match="damaged"), open_backup(path):
        pass
