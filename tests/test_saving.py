"""A story's save survives being cut short: what each file holds when the
next one was never written, and what loading makes of it."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.storage import atomic
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.tree import link_child, standing_leaf


def chain(count: int) -> list[Node]:
    """A line of `count` messages, author and storyteller in turn."""
    nodes: list[Node] = []
    for number in range(count):
        kind = "user" if number % 2 == 0 else "assistant"
        node = Node(kind=kind, speaker_id="char-1", content=f"message {number}")
        node.created_at = f"2026-11-12T10:{number:02d}:00Z"
        if nodes:
            link_child(nodes[-1], node)
        nodes.append(node)
    return nodes


def bundle_of(nodes: list[Node]) -> StoryBundle:
    story = Story(title="Doomsville")
    story.active_leaf_id = nodes[-1].id if nodes else None
    return StoryBundle(story=story, nodes=nodes)


def write_raw(directory: Path, name: str, data) -> None:
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


# --- a leaf that wasn't saved -------------------------------------------------


def test_a_leaf_nodes_json_never_got_opens_at_the_newest_message_it_has(tmp_path):
    # Saved after four messages; then a turn added two more and the save
    # stopped after story.json, before nodes.json.
    nodes = chain(4)
    bundle = bundle_of(nodes)
    save_story_bundle(bundle, root=tmp_path)
    directory = story_dir(bundle.story.id, tmp_path)
    turn = chain(6)[4:]
    link_child(nodes[-1], turn[0])
    story = bundle.story.model_dump()
    story["active_leaf_id"] = turn[-1].id
    write_raw(directory, "story.json", story)

    loaded = load_story_bundle(bundle.story.id, root=tmp_path)

    assert loaded.story.active_leaf_id == nodes[-1].id
    assert any("wasn't saved" in notice for notice in loaded.notices)


def test_a_leaf_a_deletion_took_away_opens_where_its_line_left_the_story(tmp_path):
    # Six messages, then the last two deleted: nodes.json was written
    # without them, story.json never got the leaf moved back.
    nodes = chain(6)
    bundle = bundle_of(nodes)
    save_story_bundle(bundle, root=tmp_path)
    directory = story_dir(bundle.story.id, tmp_path)
    kept = [node.model_copy(deep=True) for node in nodes[:4]]
    kept[-1].children = []
    # A newer message on another line, so "the newest" would be wrong here.
    other = Node(kind="user", speaker_id="char-1", content="a branch")
    other.created_at = "2026-11-12T11:00:00Z"
    link_child(kept[1], other)
    (directory / "nodes.json.bak").write_text(
        json.dumps([node.model_dump() for node in nodes]), encoding="utf-8"
    )
    write_raw(directory, "nodes.json", [node.model_dump() for node in [*kept, other]])

    loaded = load_story_bundle(bundle.story.id, root=tmp_path)

    assert loaded.story.active_leaf_id == kept[-1].id
    assert loaded.notices


def test_nodes_json_unreadable_and_its_backup_a_save_behind_still_opens(tmp_path):
    # A simple chat's every save adds the leaf: the backup never has it.
    nodes = chain(4)
    bundle = bundle_of(nodes)
    save_story_bundle(bundle, root=tmp_path)
    link_child(nodes[-1], extra := chain(5)[4])
    bundle.nodes.append(extra)
    bundle.story.active_leaf_id = extra.id
    save_story_bundle(bundle, root=tmp_path)
    directory = story_dir(bundle.story.id, tmp_path)
    (directory / "nodes.json").write_text("", encoding="utf-8")  # a power loss's empty file

    loaded = load_story_bundle(bundle.story.id, root=tmp_path)

    assert loaded.story.active_leaf_id == nodes[-1].id
    assert len(loaded.notices) == 2  # the backup, and the leaf


def test_a_whole_story_opens_as_it_was_and_says_nothing(tmp_path):
    bundle = bundle_of(chain(3))
    save_story_bundle(bundle, root=tmp_path)
    loaded = load_story_bundle(bundle.story.id, root=tmp_path)
    assert loaded.story.active_leaf_id == bundle.story.active_leaf_id
    assert loaded.notices == []


def test_the_nearest_message_needs_a_whole_line_back_to_the_start():
    nodes = chain(4)
    orphan = Node(kind="user", speaker_id="char-1", content="parent gone", parent_id="gone")
    orphan.created_at = "2026-11-12T12:00:00Z"  # newest, but cut off
    assert standing_leaf([*nodes, orphan], "missing") == nodes[-1].id
    assert standing_leaf([*nodes, orphan], orphan.id) == nodes[-1].id
    assert standing_leaf([orphan], "missing") is None
    assert standing_leaf(nodes, None) is None


# --- each file reaches the disk before it is swapped in -----------------------


def watch_the_disk(monkeypatch) -> list[tuple[str, str]]:
    """Every flush (of a file or a folder) and every swap, in order."""
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, atomic.replace

    def fsync(fd: int) -> None:
        kind = "folder" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(("flush", kind))
        real_fsync(fd)

    def replace(source: Path, target: Path) -> None:
        events.append(("swap", Path(target).name))
        real_replace(source, target)

    monkeypatch.setattr(atomic, "DURABLE", True, raising=False)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(atomic, "replace", replace)
    return events


def test_a_file_is_on_the_disk_before_it_replaces_the_last_and_the_folder_after(
    tmp_path, monkeypatch
):
    events = watch_the_disk(monkeypatch)
    atomic.atomic_write_json(tmp_path / "story.json", {"title": "Doomsville"})
    assert events == [("flush", "file"), ("swap", "story.json"), ("flush", "folder")]


def test_the_backup_is_the_last_files_own_bytes_and_needs_no_flush_of_its_own(
    tmp_path, monkeypatch
):
    target = tmp_path / "nodes.json"
    atomic.atomic_write_json(target, [{"turn": 1}], keep_backup=True)
    events = watch_the_disk(monkeypatch)
    atomic.atomic_write_json(target, [{"turn": 2}], keep_backup=True)
    assert events == [
        ("flush", "file"),
        ("swap", "nodes.json.bak"),
        ("swap", "nodes.json"),
        ("flush", "folder"),
    ]
    assert atomic.read_json(tmp_path / "nodes.json.bak") == [{"turn": 1}]
    assert not list(tmp_path.glob("*.tmp"))


def test_where_links_cant_be_made_the_backup_is_a_copy_flushed_too(tmp_path, monkeypatch):
    target = tmp_path / "nodes.json"
    atomic.atomic_write_json(target, [{"turn": 1}], keep_backup=True)
    events = watch_the_disk(monkeypatch)

    def no_links(source, link) -> None:
        raise OSError("this file system has no links")

    monkeypatch.setattr(os, "link", no_links)
    atomic.atomic_write_json(target, [{"turn": 2}], keep_backup=True)
    assert events[:3] == [("flush", "file"), ("flush", "file"), ("swap", "nodes.json.bak")]
    assert atomic.read_json(tmp_path / "nodes.json.bak") == [{"turn": 1}]


def test_on_windows_no_folder_is_opened_to_flush(tmp_path, monkeypatch):
    events = watch_the_disk(monkeypatch)
    opened: list[str] = []
    real_open = os.open

    def watched_open(path, flags, *args):
        opened.append(str(path))
        return real_open(path, flags, *args)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "open", watched_open)
    atomic.sync_folder(tmp_path)
    assert opened == [] and events == []


# --- whichever file a save stops at, story.json names a message on disk -------


class Stopped(Exception):
    pass


def stop_at_swap(monkeypatch, number: int) -> None:
    """The process killed as it is about to make its `number`th swap."""
    real_replace = atomic.replace
    made = [0]

    def replace(source: Path, target: Path) -> None:
        if made[0] == number:
            raise Stopped(Path(target).name)
        made[0] += 1
        real_replace(source, target)

    monkeypatch.setattr(atomic, "replace", replace)


def leaf_on_disk(directory: Path) -> bool:
    story = json.loads((directory / "story.json").read_text(encoding="utf-8"))
    nodes = json.loads((directory / "nodes.json").read_text(encoding="utf-8"))
    return story["active_leaf_id"] in {node["id"] for node in nodes}


def test_a_turn_cut_short_anywhere_leaves_the_leaf_on_disk(tmp_path, monkeypatch):
    for number in range(6):
        root = tmp_path / str(number)
        nodes = chain(4)
        bundle = bundle_of(nodes)
        bundle.cast = []
        save_story_bundle(bundle, root=root)
        link_child(nodes[-1], extra := chain(5)[4])
        bundle.nodes.append(extra)
        bundle.story.active_leaf_id = extra.id
        bundle.story.title = "Doomsville, day two"  # story.json changes too
        with monkeypatch.context() as patch:
            stop_at_swap(patch, number)
            try:
                save_story_bundle(bundle, root=root)
            except Stopped:
                pass
        directory = story_dir(bundle.story.id, root)
        assert leaf_on_disk(directory), f"stopped at swap {number}"
        assert load_story_bundle(bundle.story.id, root=root).notices == []


def test_a_first_save_cut_short_still_leaves_a_story(tmp_path, monkeypatch):
    bundle = bundle_of(chain(2))
    stop_at_swap(monkeypatch, 1)
    try:
        save_story_bundle(bundle, root=tmp_path)
    except Stopped:
        pass
    monkeypatch.undo()
    loaded = load_story_bundle(bundle.story.id, root=tmp_path)  # a story, if an empty one
    assert loaded.story.title == "Doomsville"


# --- only what changed is written ---------------------------------------------


def watch_swaps(monkeypatch) -> list[str]:
    swapped: list[str] = []
    real_replace = atomic.replace

    def replace(source: Path, target: Path) -> None:
        swapped.append(Path(target).name)
        real_replace(source, target)

    monkeypatch.setattr(atomic, "replace", replace)
    return swapped


def test_a_turns_save_writes_only_the_files_it_changed(tmp_path, monkeypatch):
    nodes = chain(4)
    bundle = bundle_of(nodes)
    save_story_bundle(bundle, root=tmp_path)
    swapped = watch_swaps(monkeypatch)
    link_child(nodes[-1], extra := chain(5)[4])
    bundle.nodes.append(extra)
    bundle.story.active_leaf_id = extra.id

    save_story_bundle(bundle, root=tmp_path)

    assert sorted(swapped) == ["nodes.json", "nodes.json.bak", "story.json"]


def test_saving_what_is_on_disk_writes_nothing_and_the_backup_stays_a_version_behind(
    tmp_path, monkeypatch
):
    nodes = chain(4)
    bundle = bundle_of(nodes)
    save_story_bundle(bundle, root=tmp_path)
    link_child(nodes[-1], extra := chain(5)[4])
    bundle.nodes.append(extra)
    save_story_bundle(bundle, root=tmp_path)
    swapped = watch_swaps(monkeypatch)

    save_story_bundle(bundle, root=tmp_path)
    save_story_bundle(load_story_bundle(bundle.story.id, root=tmp_path), root=tmp_path)

    assert swapped == []
    directory = story_dir(bundle.story.id, tmp_path)
    assert len(atomic.read_json(directory / "nodes.json.bak")) == 4
    assert len(atomic.read_json(directory / "nodes.json")) == 5


def test_a_file_changed_since_it_was_written_is_written_again(tmp_path, monkeypatch):
    target = tmp_path / "story.json"
    atomic.atomic_write_json(target, {"title": "Doomsville"})
    swapped = watch_swaps(monkeypatch)

    target.write_text('{"title": "Hellsville, by hand"}', encoding="utf-8")
    atomic.atomic_write_json(target, {"title": "Doomsville"})
    other = tmp_path / "other.json"
    other.write_text('{\n  "title": "Doomsvilla"\n}', encoding="utf-8")  # the same size
    os.replace(other, target)
    atomic.atomic_write_json(target, {"title": "Doomsville"})
    target.unlink()
    atomic.atomic_write_json(target, {"title": "Doomsville"})

    assert swapped == ["story.json"] * 3
    assert atomic.read_json(target) == {"title": "Doomsville"}


def test_a_config_saved_again_unchanged_is_left_alone_and_keeps_its_mode(tmp_path, monkeypatch):
    from sealedlore.models.config import Config
    from sealedlore.storage.repository import CONFIG_FILE_MODE, config_file, save_config

    config = Config()
    save_config(config, root=tmp_path)
    swapped = watch_swaps(monkeypatch)
    save_config(config, root=tmp_path)
    assert swapped == []
    if sys.platform != "win32":
        os.chmod(config_file(tmp_path), 0o644)  # its mode is part of the save
        save_config(config, root=tmp_path)
        assert swapped == ["config.json"]
        assert stat.S_IMODE(os.stat(config_file(tmp_path)).st_mode) == CONFIG_FILE_MODE
