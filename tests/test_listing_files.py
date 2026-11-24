"""The endpoints' model listings are kept beside config.json, not in it
(`Config.image_models` and `Config.video_models`, storage/repository.py
`LISTINGS`): each a day's cache, the pictures' was most of the file (135 KB
of 203) and a video listing could be as much, and of the time every save of
the config took, and the window saves it after nearly everything."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.models.config import Config
from sealedlore.storage import atomic
from sealedlore.storage.formats import FORMAT_KEY
from sealedlore.storage.paths import config_file, listing_file
from sealedlore.storage.repository import (
    LISTINGS,
    _copy_of,
    load_config,
    load_config_or_recover,
    save_config,
)

LISTED = {
    "image_models": [{"id": "vendor/drawer", "name": "Drawer", "supported_parameters": {}}],
    "video_models": [{"id": "vendor/mover", "name": "Mover", "params": [], "fixed": []}],
}
SOURCE = {
    "image_models": "https://pictures.example/v1",
    "video_models": "https://videos.example/v1",
}
EVERY_LISTING = pytest.mark.parametrize("field", LISTINGS)


def test_every_listing_is_kept_beside_the_config() -> None:
    assert LISTINGS == ("image_models", "video_models")
    for field in LISTINGS:
        for name in (field, f"{field}_fetched_at", f"{field}_source"):
            assert Config.model_fields[name].exclude, f"{name} would be saved in config.json"


def _fetched(config: Config, field: str, when: str = "2026-11-24T08:00:00+00:00") -> None:
    """As a fetch leaves the config (gui/image_jobs.py, video_jobs.py `_on_finished`)."""
    setattr(config, field, [dict(entry) for entry in LISTED[field]])
    setattr(config, f"{field}_fetched_at", when)
    setattr(config, f"{field}_source", SOURCE[field])


def _config_json(root: Path) -> dict:
    return json.loads(config_file(root).read_text(encoding="utf-8"))


def _kept(root: Path, field: str) -> dict:
    return json.loads(listing_file(field, root).read_text(encoding="utf-8"))


@EVERY_LISTING
@pytest.mark.parametrize("loader", [load_config, lambda root: load_config_or_recover(root)[0]])
def test_a_listing_is_saved_beside_the_config_and_read_back(
    tmp_path: Path, loader, field: str
) -> None:
    config = Config()
    _fetched(config, field)
    save_config(config, root=tmp_path)
    written = _config_json(tmp_path)
    assert not {field, f"{field}_fetched_at", f"{field}_source"} & set(written)
    assert listing_file(field, tmp_path).name == f"{field}.json"
    assert _kept(tmp_path, field) == {
        "models": LISTED[field],
        "fetched_at": "2026-11-24T08:00:00+00:00",
        "source": SOURCE[field],
    }
    again = loader(tmp_path)
    assert getattr(again, field) == LISTED[field]
    assert getattr(again, f"{field}_source") == SOURCE[field]
    assert getattr(again, f"{field}_fetched_at") == "2026-11-24T08:00:00+00:00"


def test_both_listings_are_kept_each_in_its_own_file(tmp_path: Path) -> None:
    config = Config()
    for field in LISTINGS:
        _fetched(config, field)
    save_config(config, root=tmp_path)
    again = load_config(root=tmp_path)
    for field in LISTINGS:
        assert _kept(tmp_path, field)["models"] == LISTED[field]
        assert getattr(again, field) == LISTED[field]


@EVERY_LISTING
def test_an_unchanged_listing_is_not_written_again(tmp_path: Path, monkeypatch, field) -> None:
    config = Config()
    _fetched(config, field)
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
    setattr(config, f"{field}_fetched_at", None)
    save_config(config, root=tmp_path)
    assert written == ["config.json", f"{field}.json"]
    assert getattr(load_config(root=tmp_path), f"{field}_fetched_at") is None
    # A new fetch replaces the list whole.
    _fetched(config, field, "2026-11-25T08:00:00+00:00")
    save_config(config, root=tmp_path)
    assert written[-1] == f"{field}.json"
    assert getattr(load_config(root=tmp_path), f"{field}_fetched_at") == (
        "2026-11-25T08:00:00+00:00"
    )


def test_a_saves_copy_shares_the_listings() -> None:
    """Copying them was most of every save's time."""
    config = Config()
    for field in LISTINGS:
        _fetched(config, field)
    copied = _copy_of(config)
    for field in LISTINGS:
        assert getattr(copied, field) is getattr(config, field)
    assert copied.providers is not config.providers


def test_no_listing_makes_no_file(tmp_path: Path) -> None:
    save_config(Config(), root=tmp_path)
    for field in LISTINGS:
        assert not listing_file(field, tmp_path).exists()
        assert getattr(load_config(root=tmp_path), field) == []


def _older_config(root: Path, field: str, when: str) -> None:
    """config.json as a version before the listing had a file saved it."""
    config_file(root).write_text(
        json.dumps(
            {
                FORMAT_KEY: 2,
                field: LISTED[field],
                f"{field}_fetched_at": when,
                f"{field}_source": SOURCE[field],
            }
        ),
        encoding="utf-8",
    )


@EVERY_LISTING
def test_an_older_configs_listing_is_moved_out_on_load(tmp_path: Path, field: str) -> None:
    _older_config(tmp_path, field, "2026-11-20T08:00:00+00:00")
    config = load_config(root=tmp_path)
    assert getattr(config, field) == LISTED[field]
    assert _kept(tmp_path, field)["models"] == LISTED[field]
    save_config(config, root=tmp_path)
    assert field not in _config_json(tmp_path)
    again = load_config(root=tmp_path)
    assert getattr(again, field) == LISTED[field]
    assert getattr(again, f"{field}_fetched_at") == "2026-11-20T08:00:00+00:00"


@EVERY_LISTING
def test_the_later_of_two_listings_is_the_one_kept(tmp_path: Path, field: str) -> None:
    """Back and forth between versions: one before the file writes its
    listing into config.json, and the later fetch is the one that counts."""
    config = Config()
    _fetched(config, field, "2026-11-20T08:00:00+00:00")
    save_config(config, root=tmp_path)
    _older_config(tmp_path, field, "2026-11-22T08:00:00+00:00")
    loaded = load_config(root=tmp_path)
    assert getattr(loaded, f"{field}_fetched_at") == "2026-11-22T08:00:00+00:00"
    assert _kept(tmp_path, field)["fetched_at"] == "2026-11-22T08:00:00+00:00"
    # An older config.json's listing fetched before the file's is not.
    _older_config(tmp_path, field, "2026-11-01T08:00:00+00:00")
    loaded = load_config(root=tmp_path)
    assert getattr(loaded, f"{field}_fetched_at") == "2026-11-22T08:00:00+00:00"


@EVERY_LISTING
def test_an_unreadable_listing_file_is_fetched_again(tmp_path: Path, field: str) -> None:
    save_config(Config(), root=tmp_path)
    listing_file(field, tmp_path).write_text("{ not json", encoding="utf-8")
    config = load_config(root=tmp_path)
    assert getattr(config, field) == [] and getattr(config, f"{field}_fetched_at") is None
