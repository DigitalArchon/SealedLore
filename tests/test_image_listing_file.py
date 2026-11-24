"""The picture endpoint's listing is kept beside config.json, not in it
(`Config.image_models`, storage/repository.py `_keep_image_listing`): it
was most of the file (135 KB of 203) and of the time every save of the
config took, and the window saves it after nearly everything."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.models.config import Config
from sealedlore.storage import atomic
from sealedlore.storage.formats import FORMAT_KEY
from sealedlore.storage.paths import config_file, image_listing_file
from sealedlore.storage.repository import load_config, load_config_or_recover, save_config

LISTED = [{"id": "vendor/drawer", "name": "Drawer", "supported_parameters": {}}]
SOURCE = "https://pictures.example/v1"


def _fetched(config: Config, when: str = "2026-11-24T08:00:00+00:00") -> None:
    """As a fetch leaves the config (gui/image_jobs.py `_on_finished`)."""
    config.image_models = [dict(entry) for entry in LISTED]
    config.image_models_fetched_at = when
    config.image_models_source = SOURCE


def _config_json(root: Path) -> dict:
    return json.loads(config_file(root).read_text(encoding="utf-8"))


@pytest.mark.parametrize("loader", [load_config, lambda root: load_config_or_recover(root)[0]])
def test_a_listing_is_saved_beside_the_config_and_read_back(tmp_path: Path, loader) -> None:
    config = Config()
    _fetched(config)
    save_config(config, root=tmp_path)
    written = _config_json(tmp_path)
    assert not {"image_models", "image_models_fetched_at", "image_models_source"} & set(written)
    kept = json.loads(image_listing_file(tmp_path).read_text(encoding="utf-8"))
    assert kept == {
        "models": LISTED,
        "fetched_at": config.image_models_fetched_at,
        "source": SOURCE,
    }
    again = loader(tmp_path)
    assert again.image_models == LISTED and again.image_models_source == SOURCE
    assert again.image_models_fetched_at == config.image_models_fetched_at


def test_an_unchanged_listing_is_not_written_again(tmp_path: Path, monkeypatch) -> None:
    config = Config()
    _fetched(config)
    save_config(config, root=tmp_path)
    written: list[str] = []
    real = atomic.replace  # only a file that is written is swapped in
    monkeypatch.setattr(
        atomic,
        "replace",
        lambda source, target: (written.append(target.name), real(source, target)),
    )
    config.text_scale = 1.2
    save_config(config, root=tmp_path)
    assert written == ["config.json"]
    # Made stale by Settings (another endpoint, `SettingsDialog._save`): kept so.
    config.image_models_fetched_at = None
    save_config(config, root=tmp_path)
    assert written == ["config.json", "image_models.json"]
    assert load_config(root=tmp_path).image_models_fetched_at is None
    # A new fetch replaces the list whole.
    _fetched(config, "2026-11-25T08:00:00+00:00")
    save_config(config, root=tmp_path)
    assert written[-1] == "image_models.json"
    assert load_config(root=tmp_path).image_models_fetched_at == "2026-11-25T08:00:00+00:00"


def test_no_listing_makes_no_file(tmp_path: Path) -> None:
    save_config(Config(), root=tmp_path)
    assert not image_listing_file(tmp_path).exists()
    assert load_config(root=tmp_path).image_models == []


def _older_config(root: Path, when: str) -> None:
    """config.json as a version before the listing had a file saved it."""
    config_file(root).write_text(
        json.dumps(
            {
                FORMAT_KEY: 2,
                "image_models": LISTED,
                "image_models_fetched_at": when,
                "image_models_source": SOURCE,
            }
        ),
        encoding="utf-8",
    )


def test_an_older_configs_listing_is_moved_out_on_load(tmp_path: Path) -> None:
    _older_config(tmp_path, "2026-11-20T08:00:00+00:00")
    config = load_config(root=tmp_path)
    assert config.image_models == LISTED
    assert json.loads(image_listing_file(tmp_path).read_text(encoding="utf-8"))["models"] == LISTED
    save_config(config, root=tmp_path)
    assert "image_models" not in _config_json(tmp_path)
    again = load_config(root=tmp_path)
    assert again.image_models == LISTED
    assert again.image_models_fetched_at == "2026-11-20T08:00:00+00:00"


def test_the_later_of_two_listings_is_the_one_kept(tmp_path: Path) -> None:
    """Back and forth between versions: one before the file writes its
    listing into config.json, and the later fetch is the one that counts."""
    config = Config()
    _fetched(config, "2026-11-20T08:00:00+00:00")
    save_config(config, root=tmp_path)
    _older_config(tmp_path, "2026-11-22T08:00:00+00:00")
    assert load_config(root=tmp_path).image_models_fetched_at == "2026-11-22T08:00:00+00:00"
    kept = json.loads(image_listing_file(tmp_path).read_text(encoding="utf-8"))
    assert kept["fetched_at"] == "2026-11-22T08:00:00+00:00"
    # An older config.json's listing fetched before the file's is not.
    _older_config(tmp_path, "2026-11-01T08:00:00+00:00")
    assert load_config(root=tmp_path).image_models_fetched_at == "2026-11-22T08:00:00+00:00"


def test_an_unreadable_listing_file_is_fetched_again(tmp_path: Path) -> None:
    save_config(Config(), root=tmp_path)
    image_listing_file(tmp_path).write_text("{ not json", encoding="utf-8")
    config = load_config(root=tmp_path)
    assert config.image_models == [] and config.image_models_fetched_at is None
