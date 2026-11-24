"""The Stories list's listings (storage/library.py) are read again only
for a story whose story.json or nodes.json has changed since: the list is
rebuilt after every turn, and counting every story's messages read every
nodes.json whole, on the GUI thread."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.storage import library
from sealedlore.storage.library import story_listings
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.tree import link_child


@pytest.fixture
def reads(monkeypatch) -> list[str]:
    """Which files the listings read, by name."""
    read: list[str] = []
    real = library._read

    def counted(path: Path, default=None):
        read.append(f"{path.parent.name}/{path.name}")
        return real(path, default)

    monkeypatch.setattr(library, "_read", counted)
    return read


def _story(story_id: str, title: str, messages: int) -> StoryBundle:
    nodes: list[Node] = []
    for n in range(messages):
        node = Node(
            kind="user" if n % 2 == 0 else "assistant",
            speaker_id="mayor",
            content=f"Line {n}.",
            created_at=f"2026-11-{n + 1:02d}T10:00:00+00:00",
        )
        if nodes:
            link_child(nodes[-1], node)
        nodes.append(node)
    story = Story(id=story_id, title=title, active_leaf_id=nodes[-1].id if nodes else None)
    return StoryBundle(story=story, nodes=nodes)


def test_a_story_unchanged_is_not_read_again(tmp_path: Path, reads) -> None:
    for story_id, count in (("doomsville", 4), ("nightshift", 2)):
        save_story_bundle(_story(story_id, story_id.title(), count), root=tmp_path)
    first = {listing.id: listing.messages for listing in story_listings(tmp_path)}
    assert first == {"doomsville": 4, "nightshift": 2}
    assert sorted(reads) == sorted(
        f"{s}/{f}" for s in ("doomsville", "nightshift") for f in ("story.json", "nodes.json")
    )
    reads.clear()
    assert {listing.id: listing.messages for listing in story_listings(tmp_path)} == first
    assert reads == []


def test_a_story_played_on_shows_its_new_count_and_times(tmp_path: Path, reads) -> None:
    bundle = _story("doomsville", "Doomsville", 2)
    save_story_bundle(bundle, root=tmp_path)
    save_story_bundle(_story("nightshift", "Nightshift", 2), root=tmp_path)
    [listing] = [s for s in story_listings(tmp_path) if s.id == "doomsville"]
    assert listing.messages == 2 and listing.played == "2026-11-02T10:00:00+00:00"
    reads.clear()
    more = _story("doomsville", "Doomsville", 6)
    bundle.nodes = more.nodes
    bundle.story.active_leaf_id = more.story.active_leaf_id
    save_story_bundle(bundle, root=tmp_path)
    [listing] = [s for s in story_listings(tmp_path) if s.id == "doomsville"]
    assert listing.messages == 6 and listing.played == "2026-11-06T10:00:00+00:00"
    assert sorted(reads) == ["doomsville/nodes.json", "doomsville/story.json"]
    # Renamed alone: story.json changes, and the listing with it.
    bundle.story.title = "Doomsville at night"
    save_story_bundle(bundle, root=tmp_path)
    [listing] = [s for s in story_listings(tmp_path) if s.id == "doomsville"]
    assert listing.title == "Doomsville at night" and listing.messages == 6


def test_a_file_changed_by_hand_in_the_same_moment_is_read_again(tmp_path: Path) -> None:
    """Of the same size and with the same time as before (a coarse clock, a
    copy keeping times): it is another file, or changed since."""
    save_story_bundle(_story("doomsville", "Doomsville", 3), root=tmp_path)
    assert story_listings(tmp_path)[0].messages == 3
    path = story_dir("doomsville", tmp_path) / "nodes.json"
    before = path.stat()

    def written(target: Path, kept: int) -> None:
        nodes = json.loads(path.read_text(encoding="utf-8"))[:kept]
        target.write_text(json.dumps(nodes).ljust(before.st_size), encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    # Another file swapped in.
    replacement = path.with_name("new.json")
    written(replacement, 2)
    replacement.replace(path)
    assert path.stat().st_size == before.st_size
    assert story_listings(tmp_path)[0].messages == 2
    if sys.platform != "win32":
        # The same file, written over where it stands, its time put back:
        # told by when its inode changed, which Windows doesn't keep.
        written(path, 1)
        assert story_listings(tmp_path)[0].messages == 1


def test_a_story_gone_or_unreadable_is_listed_as_it_now_is(tmp_path: Path) -> None:
    save_story_bundle(_story("doomsville", "Doomsville", 2), root=tmp_path)
    save_story_bundle(_story("nightshift", "Nightshift", 2), root=tmp_path)
    assert len(story_listings(tmp_path)) == 2
    story_json = story_dir("nightshift", tmp_path) / "story.json"
    story_json.write_text("{ torn", encoding="utf-8")
    broken = [s for s in story_listings(tmp_path) if s.id == "nightshift"]
    assert broken[0].error
    for path in story_dir("nightshift", tmp_path).iterdir():
        path.unlink()
    story_dir("nightshift", tmp_path).rmdir()
    assert [s.id for s in story_listings(tmp_path)] == ["doomsville"]
    assert str(story_dir("nightshift", tmp_path)) not in library._LISTED
