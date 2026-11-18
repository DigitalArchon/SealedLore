"""Which layout a story folder and config.json are in (storage/formats.py):
written with every save, so a version that finds a newer one's files says so
instead of dropping, the first time it saves, whatever it doesn't know of."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from sealedlore.models.aside import Aside
from sealedlore.models.authoring import ReviewRecord
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.image import GeneratedImage
from sealedlore.models.lore import LoreEntry
from sealedlore.models.memory import Recollection
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import SupportingCast
from sealedlore.models.video import GeneratedVideo
from sealedlore.storage import collection_backup
from sealedlore.storage.archive import ArchiveError, archive_data, read_archive
from sealedlore.storage.formats import (
    CONFIG_FORMAT,
    FORMAT_KEY,
    STORY_FORMAT,
    NewerStory,
    format_of,
)
from sealedlore.storage.library import story_listings
from sealedlore.storage.paths import config_file, story_dir
from sealedlore.storage.repository import (
    StoryBundle,
    config_newer,
    load_config_or_recover,
    load_story_bundle,
    save_config,
    save_over_newer,
    save_story_bundle,
)

SHAPES = Path(__file__).parent / "data" / "file_formats.json"

# What a story folder holds, one model per kind of record.
STORY_MODELS: tuple[type[BaseModel], ...] = (
    Story,
    Node,
    Character,
    SupportingCast,
    LoreEntry,
    Summary,
    EmbeddingCacheEntry,
    Aside,
    ReviewRecord,
    Recollection,
    GeneratedImage,
    GeneratedVideo,
)
# Words about a field, not its shape: changing them loses nothing.
_NOT_SHAPE = {"title", "description", "default", "examples"}


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items()) if k not in _NOT_SHAPE}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return value


def fingerprint(models: tuple[type[BaseModel], ...]) -> str:
    shapes = [_shape(model.model_json_schema()) for model in models]
    return hashlib.sha256(json.dumps(shapes, sort_keys=True).encode()).hexdigest()


def test_a_change_to_what_is_saved_raises_its_format() -> None:
    """A field added, removed or changed in what a story folder or
    config.json holds is lost by any version that doesn't know it: raise
    STORY_FORMAT or CONFIG_FORMAT (storage/formats.py), then write the
    format and fingerprint printed here into tests/data/file_formats.json."""
    recorded = json.loads(SHAPES.read_text(encoding="utf-8"))
    now = {
        "story": {"format": STORY_FORMAT, "shape": fingerprint(STORY_MODELS)},
        "config": {"format": CONFIG_FORMAT, "shape": fingerprint((Config,))},
    }
    for kind, current in now.items():
        was = recorded[kind]
        if current["shape"] != was["shape"]:
            assert current["format"] > was["format"], (
                f"What is saved in {kind} files changed: raise its format past "
                f"{was['format']} and record {json.dumps(now, indent=1)}"
            )
            pytest.fail(f"Record the new shape in {SHAPES.name}: {json.dumps(now, indent=1)}")
        assert current["format"] == was["format"], f"Record {kind}'s format in {SHAPES.name}"


def _story_json(tmp_path: Path, story_id: str) -> dict:
    return json.loads((story_dir(story_id, tmp_path) / "story.json").read_text(encoding="utf-8"))


def _saved(tmp_path: Path) -> StoryBundle:
    bundle = StoryBundle(story=Story(title="Doomsville"))
    save_story_bundle(bundle, root=tmp_path)
    return bundle


def _as_if_newer(tmp_path: Path, story_id: str, found: int) -> None:
    path = story_dir(story_id, tmp_path) / "story.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[FORMAT_KEY] = found
    data["a_newer_field"] = "kept by the newer version"
    path.write_text(json.dumps(data), encoding="utf-8")


def test_story_json_says_its_format(tmp_path: Path) -> None:
    bundle = _saved(tmp_path)
    data = _story_json(tmp_path, bundle.story.id)
    assert format_of(data) == STORY_FORMAT and next(iter(data)) == FORMAT_KEY
    # And reads back as the story it was.
    assert load_story_bundle(bundle.story.id, root=tmp_path).story.title == "Doomsville"


def test_a_story_from_before_formats_reads_as_ever(tmp_path: Path) -> None:
    bundle = _saved(tmp_path)
    path = story_dir(bundle.story.id, tmp_path) / "story.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data[FORMAT_KEY]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert format_of(data) == 0
    assert load_story_bundle(bundle.story.id, root=tmp_path).story.title == "Doomsville"


def test_a_newer_versions_story_isnt_opened_unless_asked(tmp_path: Path) -> None:
    bundle = _saved(tmp_path)
    _as_if_newer(tmp_path, bundle.story.id, STORY_FORMAT + 1)
    with pytest.raises(NewerStory) as raised:
        load_story_bundle(bundle.story.id, root=tmp_path)
    assert "newer SealedLore" in str(raised.value)
    # Untouched by the refusal.
    assert _story_json(tmp_path, bundle.story.id)["a_newer_field"]
    [listing] = story_listings(tmp_path)
    assert listing.newer and listing.title == "Doomsville" and listing.error
    # Asked, it opens, and its first save is this version's.
    opened = load_story_bundle(bundle.story.id, root=tmp_path, newer_ok=True)
    save_story_bundle(opened, root=tmp_path)
    data = _story_json(tmp_path, bundle.story.id)
    assert format_of(data) == STORY_FORMAT and "a_newer_field" not in data


def test_config_json_says_its_format(tmp_path: Path) -> None:
    save_config(Config(), root=tmp_path)
    data = json.loads(config_file(tmp_path).read_text(encoding="utf-8"))
    assert format_of(data) == CONFIG_FORMAT
    config, note = load_config_or_recover(tmp_path)
    assert note is None and config_newer(tmp_path) is None


def test_a_newer_versions_settings_are_read_and_not_saved_over(tmp_path: Path) -> None:
    save_config(Config(), root=tmp_path)
    path = config_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[FORMAT_KEY] = CONFIG_FORMAT + 1
    data["text_scale"] = 1.5
    data["a_newer_setting"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        config, note = load_config_or_recover(tmp_path)
        assert config.text_scale == 1.5  # used, as far as this version can
        assert note and "newer SealedLore" in note
        assert config_newer(tmp_path) == CONFIG_FORMAT + 1
        config.text_scale = 1.0
        save_config(config, root=tmp_path)
        assert json.loads(path.read_text(encoding="utf-8"))["a_newer_setting"] is True
        # The author's yes: saved, as this version's.
        save_over_newer(tmp_path)
        save_config(config, root=tmp_path)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert format_of(written) == CONFIG_FORMAT and "a_newer_setting" not in written
    finally:
        save_over_newer(tmp_path)


def test_a_story_backup_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    bundle = StoryBundle(story=Story(title="Doomsville"))
    data = archive_data(bundle, [])
    assert data["story_format"] == STORY_FORMAT
    path = tmp_path / "story.sealedlore-archive.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert read_archive(path)[0].story.title == "Doomsville"
    data["story_format"] = STORY_FORMAT + 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ArchiveError, match="newer SealedLore"):
        read_archive(path)


def test_a_backup_of_characters_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "characters.sealedlore-characters.zip"
    from sealedlore.storage.picture_store import MemoryPictures

    store = MemoryPictures(lambda kind, record: "")
    collection_backup.write_backup(path, [Character(name="John")], store)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(collection_backup.MANIFEST))
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    assert manifest["story_format"] == STORY_FORMAT
    manifest["story_format"] = STORY_FORMAT + 1
    members[collection_backup.MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with pytest.raises(collection_backup.BackupError, match="newer SealedLore"):
        with collection_backup.open_backup(path):
            pass


# --- the window ----------------------------------------------------------------


def test_the_window_asks_before_opening_a_newer_versions_story(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from sealedlore.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    bundle = _saved(tmp_path)
    _as_if_newer(tmp_path, bundle.story.id, STORY_FORMAT + 1)
    window = MainWindow(root=tmp_path, use_mock=True)
    asked: list[str] = []

    def answer(_parent, title, *_args, **_kwargs):
        asked.append(title)
        return answers.pop(0)

    monkeypatch.setattr(QMessageBox, "question", answer)
    answers = [QMessageBox.Cancel]
    window.open_story(bundle.story.id)
    app.processEvents()
    assert asked == ["Saved by a newer SealedLore"] and window.session is None
    assert _story_json(tmp_path, bundle.story.id)["a_newer_field"]
    answers = [QMessageBox.Open]
    window.open_story(bundle.story.id)
    app.processEvents()
    assert window.session is not None and window.session.story.id == bundle.story.id
    window.close()


def test_saving_settings_over_a_newer_versions_asks(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from sealedlore.gui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    save_config(Config(), root=tmp_path)
    path = config_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data[FORMAT_KEY] = CONFIG_FORMAT + 1
    data["a_newer_setting"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        window = MainWindow(root=tmp_path, use_mock=True)
        assert config_newer(tmp_path) == CONFIG_FORMAT + 1
        monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.Cancel)
        window._settings_saved(None, None)
        assert json.loads(path.read_text(encoding="utf-8"))["a_newer_setting"] is True
        monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.Save)
        window._settings_saved(None, None)
        assert "a_newer_setting" not in json.loads(path.read_text(encoding="utf-8"))
        window.close()
    finally:
        save_over_newer(tmp_path)
