"""Lorebooks kept for any story (storage/lorebooks.py): kept as they came,
read back, named, exported as SillyTavern reads them, and made into a new
story's lore."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.models.lorebook import StoredLorebook
from sealedlore.storage import lorebooks
from sealedlore.storage.formats import FORMAT_KEY, LOREBOOK_FORMAT
from sealedlore.storage.tavern_lore import lore_from_book, open_lorebook
from tests.test_lore_people import book


def kept(tmp_path: Path) -> StoredLorebook:
    path = tmp_path / "doomsville.json"
    path.write_text(json.dumps(book()))
    stored = lorebooks.kept_from(open_lorebook(path), "doomsville", path.name)
    lorebooks.save_lorebook(stored, tmp_path)
    return stored


def test_a_kept_lorebook_is_its_own_file_and_reads_back_whole(tmp_path: Path):
    stored = kept(tmp_path)
    path = lorebooks.lorebook_path(stored.id, tmp_path)
    data = json.loads(path.read_text())
    assert data["format"] == "sealedlore-lorebook" and data[FORMAT_KEY] == LOREBOOK_FORMAT
    assert path.stat().st_mode & 0o777 == 0o600
    (again,) = lorebooks.load_lorebooks(tmp_path).books
    assert again == stored and again.book == book()
    assert again.name == "doomsville" and again.came_from == "doomsville.json"


def test_a_copy_kept_is_its_own(tmp_path: Path):
    source = book()
    from sealedlore.storage.tavern_lore import ReadLorebook

    stored = lorebooks.kept_from(ReadLorebook(source), "x", "x.json")
    stored.book["entries"]["0"]["content"] = "changed"
    assert source["entries"]["0"]["content"] != "changed"
    copy = lorebooks.copied(stored, "x (copy)")
    assert copy.id != stored.id and copy.book == stored.book and copy.name == "x (copy)"
    copy.book["entries"]["0"]["content"] = "again"
    assert stored.book["entries"]["0"]["content"] == "changed"


def test_what_cant_be_read_is_listed_and_left(tmp_path: Path):
    stored = kept(tmp_path)
    folder = lorebooks.lorebooks_dir(tmp_path)
    (folder / "broken.json").write_text("{not json")
    (folder / "other.json").write_text(json.dumps({"entries": {}}))
    newer = json.loads(lorebooks.lorebook_path(stored.id, tmp_path).read_text())
    newer[FORMAT_KEY] = LOREBOOK_FORMAT + 1
    (folder / "newer.json").write_text(json.dumps(newer))
    shelf = lorebooks.load_lorebooks(tmp_path)
    assert [b.id for b in shelf.books] == [stored.id]
    whys = {path.name: why for path, why in shelf.unreadable}
    assert set(whys) == {"broken.json", "other.json", "newer.json"}
    assert "newer SealedLore" in whys["newer.json"]
    assert (folder / "broken.json").read_text() == "{not json"


def test_a_file_copied_by_hand_is_known_by_its_name(tmp_path: Path):
    stored = kept(tmp_path)
    path = lorebooks.lorebook_path(stored.id, tmp_path)
    (path.parent / "copied.json").write_text(path.read_text())
    ids = sorted(b.id for b in lorebooks.load_lorebooks(tmp_path).books)
    assert ids == sorted([stored.id, "copied"])


def test_a_name_from_the_book_else_the_file_and_never_twice():
    assert lorebooks.name_of({"name": "The Reach"}, "file") == "The Reach"
    assert lorebooks.name_of({"originalData": {"name": "Kept beside"}}, "file") == "Kept beside"
    assert lorebooks.name_of({"entries": {}}, "file") == "file"
    taken = [StoredLorebook(name="Doomsville"), StoredLorebook(name="Doomsville (2)")]
    assert lorebooks.unique_name("Doomsville", taken) == "Doomsville (3)"
    assert lorebooks.unique_name("Hellsville", taken) == "Hellsville"


def test_its_size_in_words(tmp_path: Path):
    found = lorebooks.stats(book())
    assert (found.entries, found.always) == (6, 3)
    assert lorebooks.stats_words(book()).startswith("6 entries · ")


def test_exported_as_sillytavern_reads_it_with_every_setting(tmp_path: Path):
    stored = kept(tmp_path)
    stored.book["entries"]["1"]["probability"] = 40
    out = tmp_path / "out.json"
    lorebooks.export(stored, out)
    again = json.loads(out.read_text())
    assert again == stored.book
    # A card's book goes out in SillyTavern's own shape.
    from sealedlore.storage.tavern_lore import book_from_lore

    card_book = StoredLorebook(name="c", book=book_from_lore(lore_from_book(book()).entries))
    lorebooks.export(card_book, out)
    written = json.loads(out.read_text())
    assert isinstance(written["entries"], dict)
    titles = [e.title for e in lore_from_book(written).entries]
    assert titles == [e.title for e in lore_from_book(book()).entries]


def test_a_story_from_it_takes_a_copy_of_its_lore_and_whom_the_author_plays(tmp_path: Path):
    stored = kept(tmp_path)
    stored.book["entries"]["3"]["content"] = "{{user}} rides into Hellsville."
    stored.book["description"] = "After the dead rose."
    stored.converted = ["NovelAI's Always On (1 entry)"]
    scenario, notes = lorebooks.scenario_from(
        stored, title="  ", player="Jane Hale", about_player="John's sister."
    )
    assert scenario.title == "doomsville" and scenario.description == "After the dead rose."
    assert [e.title for e in scenario.lore][:2] == ["Doomsville", "John Hale"]
    assert scenario.lore[3].content == "Jane Hale rides into Hellsville."
    assert all(e.came_from == "the lorebook doomsville" for e in scenario.lore)
    (you,) = scenario.cast
    assert you.name == "Jane Hale" and you.summary == "John's sister."
    assert scenario.starting_scene.present_character_ids == [you.id]
    assert scenario.suggested_character_id == you.id
    assert "Converting it: left out NovelAI's Always On (1 entry)." in notes
    # Nobody named: {{user}} stays to be written out at Start.
    scenario, _notes = lorebooks.scenario_from(stored, title="Dust")
    assert not scenario.cast and "{{user}}" in scenario.lore[3].content


def test_searched_by_its_name_and_its_entries_titles(tmp_path: Path):
    stored = kept(tmp_path)
    assert "hellsville" in lorebooks.searchable(stored)
    assert "rifle" not in lorebooks.searchable(stored)  # not the entries' text
