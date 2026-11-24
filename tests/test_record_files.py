"""A story's files are made again only as far as their records changed
(storage/record_files.py), and are then exactly what writing them whole
wrote before. Every save in the suite is also held to that (conftest
`_every_save_checked`); these are the cases one by one."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from sealedlore.models.character import Character
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.storage import atomic, record_files
from sealedlore.storage.paths import story_dir
from sealedlore.storage.record_files import fingerprint, write_record, write_records
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.tree import link_child


class Loose(BaseModel):
    """A record holding anything, as a card's own fields do."""

    data: dict[str, Any] = Field(default_factory=dict)


def _whole(records: list[BaseModel], *, compact: bool = False) -> bytes:
    """What `atomic_write_json` wrote for the same records."""
    how: dict[str, Any] = {"separators": (",", ":")} if compact else {"indent": 2}
    return atomic.utf8(json.dumps([r.model_dump() for r in records], ensure_ascii=False, **how))


@pytest.fixture
def swaps(monkeypatch) -> list[str]:
    """The files written to the disk (a file left alone is never swapped in)."""
    written: list[str] = []
    real = atomic.replace
    monkeypatch.setattr(
        atomic,
        "replace",
        lambda source, target: (written.append(target.name), real(source, target)),
    )
    return written


def _story() -> StoryBundle:
    held = Character(id="mayor", name="Mayor Bell")
    first = Node(kind="user", speaker_id="mayor", content="The sirens again.")
    reply = Node(
        kind="assistant",
        speaker_id="__narrator__",
        content="Doomsville's square empties.\n\nNobody runs — «they never do».",
        meta=NodeMeta(scene=SceneState(location="The square", present_character_ids=["mayor"])),
    )
    link_child(first, reply)
    story = Story(title="Doomsville", active_leaf_id=reply.id)
    return StoryBundle(story=story, cast=[held], nodes=[first, reply])


ODD = [
    Loose(),
    Loose(data={"empty": [], "nested": {"deeper": {}}, "list": [[], [{}], [1, [2, []]]]}),
    Loose(data={"text": 'line one\nline two\t"quoted" \\ back', "unicode": "café ✓   😀"}),
    Loose(data={"floats": [0.1, -0.0, 1e-07, 1e300, float("nan"), float("inf")], "big": 10**30}),
    Loose(data={"flags": [True, False, None, 0, 1]}),
]


@pytest.mark.parametrize("compact", [False, True])
def test_a_file_is_what_writing_it_whole_wrote(tmp_path: Path, compact: bool) -> None:
    path = tmp_path / "records.json"
    for records in ([], ODD[:1], ODD):
        write_records(path, records, compact=compact)
        assert path.read_bytes() == _whole(records, compact=compact)
    # Records changed, added, taken away and moved: the file still is.
    ODD[2].data["text"] = "changed"
    moved = [ODD[4], ODD[2], Loose(data={"new": 1}), ODD[0]]
    write_records(path, moved, compact=compact)
    assert path.read_bytes() == _whole(moved, compact=compact)
    write_record(tmp_path / "one.json", ODD[1].model_dump())
    assert (tmp_path / "one.json").read_bytes() == atomic.utf8(
        json.dumps(ODD[1].model_dump(), indent=2, ensure_ascii=False)
    )


def test_an_unchanged_save_leaves_every_file_and_the_backup_alone(tmp_path: Path, swaps) -> None:
    bundle = _story()
    save_story_bundle(bundle, root=tmp_path)
    bundle.nodes[0].content += " Closer."
    save_story_bundle(bundle, root=tmp_path)
    backup = story_dir(bundle.story.id, tmp_path) / "nodes.json.bak"
    kept = backup.read_bytes()
    swaps.clear()
    for _ in range(3):
        save_story_bundle(bundle, root=tmp_path)
    assert swaps == []
    assert backup.read_bytes() == kept, "the backup is the version before the last change"


def test_a_change_made_in_place_anywhere_is_written(tmp_path: Path, swaps) -> None:
    """Records are changed where they stand all over the engine: a message's
    scene, a card's list, the story's settings. Each is found by the record's
    dump, not by anything the change has to say."""
    bundle = _story()
    save_story_bundle(bundle, root=tmp_path)
    folder = story_dir(bundle.story.id, tmp_path)

    swaps.clear()
    bundle.nodes[1].meta.scene.present_character_ids.append("deputy")
    save_story_bundle(bundle, root=tmp_path)
    assert swaps == ["nodes.json.bak", "nodes.json"]
    reread = load_story_bundle(bundle.story.id, root=tmp_path)
    assert reread.nodes[1].meta.scene.present_character_ids == ["mayor", "deputy"]

    swaps.clear()
    bundle.cast[0].aliases.append("the Mayor")
    bundle.story.title = "Doomsville at night"
    save_story_bundle(bundle, root=tmp_path)
    assert sorted(swaps) == ["cast.json", "story.json"]
    reread = load_story_bundle(bundle.story.id, root=tmp_path)
    assert reread.cast[0].aliases == ["the Mayor"] and reread.story.title == "Doomsville at night"
    assert (folder / "nodes.json").read_bytes() == _whole(bundle.nodes)


@pytest.mark.parametrize(
    ("before", "after"), [(1, True), (0, False), (1, 1.0), (0.0, -0.0), ("1", 1), ([1], (1,))]
)
def test_a_value_changed_in_type_alone_is_written(tmp_path: Path, before, after) -> None:
    """Python's == takes True for 1, 1 for 1.0 and 0.0 for -0.0; JSON writes
    each differently, and the fingerprint tells them apart (a tuple is
    written as a list, so it is only made again for nothing)."""
    path = tmp_path / "records.json"
    record = Loose(data={"value": before})
    write_records(path, [record])
    record.data["value"] = after
    write_records(path, [record])
    assert path.read_bytes() == _whole([record])
    assert fingerprint({"v": before}) != fingerprint({"v": after})


def test_a_value_without_a_fingerprint_is_made_again_every_time(tmp_path: Path) -> None:
    class Text(str):
        pass

    path = tmp_path / "records.json"
    record = Loose(data={"value": Text("one")})
    assert fingerprint(record.model_dump()) is None
    write_records(path, [record])
    record.data["value"] = Text("two")
    write_records(path, [record])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"data": {"value": "two"}}]


def test_a_file_changed_or_gone_behind_the_saves_back_is_written_again(tmp_path: Path) -> None:
    bundle = _story()
    save_story_bundle(bundle, root=tmp_path)
    nodes = story_dir(bundle.story.id, tmp_path) / "nodes.json"
    nodes.write_text("[]", encoding="utf-8")  # a hand edit, another program
    save_story_bundle(bundle, root=tmp_path)
    assert nodes.read_bytes() == _whole(bundle.nodes)
    nodes.unlink()
    save_story_bundle(bundle, root=tmp_path)
    assert nodes.read_bytes() == _whole(bundle.nodes)


def _vector(n: int) -> EmbeddingCacheEntry:
    return EmbeddingCacheEntry(
        content_hash=f"h{n}", model="BAAI/bge-m3", dimensions=3, vector=[n / 10, -n, 0.5]
    )


def test_a_vector_cant_be_changed_where_it_stands() -> None:
    entry = _vector(1)
    with pytest.raises(ValidationError):
        entry.content_hash = "other"
    assert isinstance(entry.vector, tuple), "nothing in it changes in place"
    for name, field in EmbeddingCacheEntry.model_fields.items():
        assert field.annotation in (str, int, tuple[float, ...]), name


def test_vectors_are_told_apart_by_which_they_are(tmp_path: Path, swaps) -> None:
    bundle = _story()
    bundle.embeddings = [_vector(n) for n in range(3)]
    save_story_bundle(bundle, root=tmp_path)
    path = story_dir(bundle.story.id, tmp_path) / "embeddings.json"

    def saved() -> list[str]:
        swaps.clear()
        save_story_bundle(bundle, root=tmp_path)
        assert path.read_bytes() == _whole(bundle.embeddings, compact=True)
        return swaps

    # A list made again of the same entries (recall's and lore's pruning).
    bundle.embeddings = [row for row in bundle.embeddings if row.kind == "lore"]
    assert saved() == []
    bundle.embeddings.append(_vector(3))
    assert saved() == ["embeddings.json"]
    bundle.embeddings.pop(0)
    assert saved() == ["embeddings.json"]
    bundle.embeddings[0] = bundle.embeddings[0].model_copy(update={"vector": (9.0, 9.0, 9.0)})
    assert saved() == ["embeddings.json"]
    bundle.embeddings.reverse()
    assert saved() == ["embeddings.json"]
    bundle.embeddings.clear()
    assert saved() == ["embeddings.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_embeddings_are_written_compact_and_indented_ones_still_load(tmp_path: Path) -> None:
    bundle = _story()
    bundle.embeddings = [_vector(1), _vector(2)]
    save_story_bundle(bundle, root=tmp_path)
    path = story_dir(bundle.story.id, tmp_path) / "embeddings.json"
    assert b"\n" not in path.read_bytes() and b", " not in path.read_bytes()
    # As every version before wrote it.
    path.write_text(json.dumps([e.model_dump() for e in bundle.embeddings], indent=2))
    reread = load_story_bundle(bundle.story.id, root=tmp_path)
    assert reread.embeddings == bundle.embeddings
    save_story_bundle(reread, root=tmp_path)
    assert path.read_bytes() == _whole(bundle.embeddings, compact=True)


def test_what_is_kept_is_only_the_last_folders_and_no_vector(tmp_path: Path) -> None:
    names = [f"f{n}" for n in range(record_files.KEPT_FOLDERS + 1)]
    for name in names:
        write_records(tmp_path / name / "records.json", [Loose(data={"n": name})])
    kept = {Path(folder).name for folder in record_files._WRITTEN}
    assert kept == set(names[1:]), "the folder written longest ago is let go"
    entries = [_vector(1)]
    folder = tmp_path / names[-1]
    write_records(folder / "vectors.json", entries, compact=True, fixed=True)
    del entries
    gc.collect()
    [ref] = record_files._WRITTEN[str(folder)][str(folder / "vectors.json")].keys
    assert ref() is None, "a vector saved is not kept alive by having been"


def test_the_suites_check_catches_a_change_left_unwritten(tmp_path: Path, monkeypatch) -> None:
    """conftest's check is only worth having if it fails: here every
    fingerprint is the same, so a change goes unseen."""
    monkeypatch.setattr(record_files, "fingerprint", lambda _dumped: b"same")
    path = tmp_path / "records.json"
    record = Loose(data={"n": 1})
    write_records(path, [record])
    record.data["n"] = 2
    with pytest.raises(AssertionError, match="doesn't hold what was saved"):
        write_records(path, [record])
