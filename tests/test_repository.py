"""Full story bundle and config save/load round-trip against a temp data dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import (
    StoryBundle,
    append_api_log,
    copy_story,
    delete_story,
    load_config,
    load_story_bundle,
    read_api_log,
    save_config,
    save_story_bundle,
)
from tests.conftest import make_exchange


def make_bundle() -> StoryBundle:
    story = Story(title="The Sundering")
    root = Node(kind="user", speaker_id="char-1", content="I open the door.")
    reply = Node(
        parent_id=root.id, kind="assistant", speaker_id="__narrator__", content="It creaks open."
    )
    root.children.append(reply.id)
    story.active_leaf_id = reply.id

    return StoryBundle(
        story=story,
        cast=[Character(name="Serrik Vaun")],
        lore=[LoreEntry(title="The Accord", content="...")],
        nodes=[root, reply],
        summaries=[Summary(covered_node_ids=[root.id], content="Serrik opened the door.")],
    )


def test_story_bundle_roundtrip(tmp_path: Path):
    bundle = make_bundle()
    save_story_bundle(bundle, root=tmp_path)
    loaded = load_story_bundle(bundle.story.id, root=tmp_path)
    assert loaded == bundle


def test_story_bundle_files_exist_on_disk(tmp_path: Path):
    bundle = make_bundle()
    save_story_bundle(bundle, root=tmp_path)
    directory = story_dir(bundle.story.id, root=tmp_path)
    for filename in (
        "story.json",
        "cast.json",
        "lore.json",
        "nodes.json",
        "summaries.json",
        "embeddings.json",
    ):
        assert (directory / filename).exists(), filename


def test_load_missing_story_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_story_bundle("does-not-exist", root=tmp_path)


def test_nodes_json_keeps_backup_across_saves(tmp_path: Path):
    bundle = make_bundle()
    save_story_bundle(bundle, root=tmp_path)

    bundle.nodes.append(
        Node(parent_id=bundle.nodes[-1].id, kind="user", speaker_id="char-1", content="I step in.")
    )
    save_story_bundle(bundle, root=tmp_path)

    directory = story_dir(bundle.story.id, root=tmp_path)
    assert (directory / "nodes.json.bak").exists()


def test_config_roundtrip(tmp_path: Path):
    config = Config(
        providers=[
            ProviderConfig(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key="sk-test",
                model="anthropic/claude-3.5-sonnet",
            )
        ],
        active_provider_name="openrouter",
    )
    save_config(config, root=tmp_path)
    assert load_config(root=tmp_path) == config


def test_config_load_without_file_returns_defaults(tmp_path: Path):
    assert load_config(root=tmp_path) == Config()


def test_api_log_append_and_read(tmp_path: Path):
    bundle = make_bundle()
    save_story_bundle(bundle, root=tmp_path)

    append_api_log(bundle.story.id, {"request": {"model": "x"}}, root=tmp_path)
    append_api_log(bundle.story.id, {"request": {"model": "y"}}, root=tmp_path)

    log = read_api_log(bundle.story.id, root=tmp_path)
    assert len(log) == 2
    assert log[0]["request"]["model"] == "x"


def test_copy_story_duplicates_everything_under_a_fresh_id(tmp_path, story, cast):
    nodes = make_exchange(2)
    bundle = StoryBundle(
        story=story,
        cast=cast,
        nodes=nodes,
        summaries=[Summary(covered_node_ids=["u0", "a0"], content="Chapter one.")],
        embeddings=[
            EmbeddingCacheEntry(content_hash="h", model="m", dimensions=2, vector=[0.1, 0.2])
        ],
    )
    save_story_bundle(bundle, root=tmp_path)
    append_api_log(story.id, {"kind": "request", "payload": {}}, root=tmp_path)
    append_api_log(story.id, {"kind": "response", "usage": {"cost": 0.1}}, root=tmp_path)

    copy = copy_story(story.id, title="The Sundering (copy)", root=tmp_path)

    assert copy.story.id != story.id and copy.story.title == "The Sundering (copy)"
    loaded = load_story_bundle(copy.story.id, root=tmp_path)
    assert [n.id for n in loaded.nodes] == [n.id for n in nodes]
    assert loaded.summaries[0].content == "Chapter one."
    assert len(loaded.embeddings) == 1 and len(loaded.cast) == len(cast)
    assert read_api_log(copy.story.id, root=tmp_path) == read_api_log(story.id, root=tmp_path)
    # The original is untouched.
    assert load_story_bundle(story.id, root=tmp_path).story.title == "The Sundering"


def test_delete_story_removes_its_folder_and_refuses_paths(tmp_path, story):
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    delete_story(story.id, root=tmp_path)
    assert not story_dir(story.id, tmp_path).exists()
    with pytest.raises(ValueError):
        delete_story("../escape", root=tmp_path)
