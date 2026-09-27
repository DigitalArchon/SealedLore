"""Atomic JSON writes must never leave a torn file, and must retain exactly
one .bak when asked to."""

from __future__ import annotations

from pathlib import Path

from sealedlore.storage.atomic import append_jsonl, atomic_write_json, read_json, read_jsonl


def test_write_then_read_roundtrip(tmp_path: Path):
    target = tmp_path / "story.json"
    atomic_write_json(target, {"title": "The Sundering"})
    assert read_json(target) == {"title": "The Sundering"}


def test_no_tmp_file_left_behind(tmp_path: Path):
    target = tmp_path / "nodes.json"
    atomic_write_json(target, [{"id": "n1"}])
    assert not (tmp_path / "nodes.json.tmp").exists()
    assert target.exists()


def test_creates_parent_directories(tmp_path: Path):
    target = tmp_path / "stories" / "abc" / "story.json"
    atomic_write_json(target, {"id": "abc"})
    assert target.exists()
    assert read_json(target) == {"id": "abc"}


def test_missing_file_returns_default(tmp_path: Path):
    assert read_json(tmp_path / "missing.json", default=[]) == []
    assert read_json(tmp_path / "missing.json") is None


def test_keep_backup_retains_single_previous_version(tmp_path: Path):
    target = tmp_path / "nodes.json"
    backup = tmp_path / "nodes.json.bak"

    atomic_write_json(target, [{"turn": 1}], keep_backup=True)
    assert not backup.exists()  # nothing to back up on first write

    atomic_write_json(target, [{"turn": 2}], keep_backup=True)
    assert read_json(target) == [{"turn": 2}]
    assert read_json(backup) == [{"turn": 1}]

    atomic_write_json(target, [{"turn": 3}], keep_backup=True)
    assert read_json(target) == [{"turn": 3}]
    assert read_json(backup) == [{"turn": 2}]  # only ever one backup retained


def test_keep_backup_never_moves_the_live_file_away(tmp_path: Path, monkeypatch):
    # A crash between making the backup and swapping in the new file must
    # still leave the previous nodes.json in place, not an empty story.
    target = tmp_path / "nodes.json"
    atomic_write_json(target, [{"turn": 1}], keep_backup=True)

    real_replace = Path.replace

    def crash_on_swap(self: Path, other):
        if Path(other) == target:
            raise OSError("simulated crash")
        return real_replace(self, other)

    monkeypatch.setattr(Path, "replace", crash_on_swap)
    try:
        atomic_write_json(target, [{"turn": 2}], keep_backup=True)
    except OSError:
        pass

    assert read_json(target) == [{"turn": 1}]
    assert read_json(tmp_path / "nodes.json.bak") == [{"turn": 1}]


def test_keep_backup_false_never_creates_backup(tmp_path: Path):
    target = tmp_path / "story.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert not (tmp_path / "story.json.bak").exists()


def test_jsonl_append_only(tmp_path: Path):
    target = tmp_path / "api_log.jsonl"
    append_jsonl(target, {"request": 1})
    append_jsonl(target, {"request": 2})
    assert read_jsonl(target) == [{"request": 1}, {"request": 2}]


def test_jsonl_missing_file_returns_empty_list(tmp_path: Path):
    assert read_jsonl(tmp_path / "missing.jsonl") == []
