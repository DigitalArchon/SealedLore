"""A story's save survives being cut short: what each file holds when the
next one was never written, and what loading makes of it."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.models.node import Node
from sealedlore.models.story import Story
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
