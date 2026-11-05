"""Everything on a character's card can be changed in Your characters and goes
out with it as changed (the author, Nov 2026): the lorebook edited in place
in its own shape, files added, renamed and linked, and the card dated."""

from __future__ import annotations

import io
from pathlib import Path

from sealedlore.models.character import Character
from sealedlore.models.collection import CollectionInfo
from sealedlore.storage import collection
from sealedlore.storage import tavern_lore as lore
from sealedlore.storage.tavern_cards import card_charx, card_data, read_card, tavern_from_file
from tests.test_card_files import FARMHOUSE, kept_elena, store

SPEC_BOOK = {
    "name": "Doomsville",
    "scan_depth": 6,
    "entries": [
        {
            "id": 0,
            "keys": ["farmhouse"],
            "content": "The farmhouse has a red door.",
            "enabled": True,
            "insertion_order": 50,
            "comment": "The farmhouse",
            "name": "The farmhouse",
            "position": "after_char",
            "extensions": {"depth": 2, "probability": 70},
        }
    ],
}
WORLD_BOOK = {
    "entries": {
        "3": {"uid": 3, "key": ["creek"], "keysecondary": [], "content": "Cold.", "disable": True}
    }
}


def test_a_spec_entry_is_edited_under_its_own_names():
    book = {**SPEC_BOOK, "entries": [dict(SPEC_BOOK["entries"][0])]}
    raw = lore.book_entries(book)[0]
    lore.set_entry_title(raw, "Farm")
    lore.entry_set(raw, "keys", ["farm", "farmhouse"])
    lore.set_entry_enabled(raw, False)
    lore.entry_set(raw, "secondary_keys", ["door"])
    assert raw["comment"] == raw["name"] == "Farm"
    assert raw["keys"] == ["farm", "farmhouse"] and raw["enabled"] is False
    assert raw["secondary_keys"] == ["door"] and "keysecondary" not in raw
    # What has no field of its own is JSON, and comes back as written.
    rest = lore.entry_rest(raw)
    assert rest == {
        "id": 0,
        "position": "after_char",
        "extensions": {"depth": 2, "probability": 70},
    }
    lore.set_entry_rest(raw, {**rest, "insertion_order": 10, "priority": 3})
    assert raw["priority"] == 3
    assert raw["insertion_order"] == 50, "the order has a field of its own, not the JSON"
    assert raw["content"] == "The farmhouse has a red door.", "fields of its own untouched"


def test_sillytaverns_own_shape_stays_its_own():
    book = {"entries": {"3": dict(WORLD_BOOK["entries"]["3"])}}
    raw = lore.book_entries(book)[0]
    lore.entry_set(raw, "keys", ["creek", "water"], numbered=True)
    lore.set_entry_enabled(raw, True, numbered=True)
    assert raw["key"] == ["creek", "water"] and "keys" not in raw
    assert raw["disable"] is False and "enabled" not in raw
    added = lore.add_entry(book)
    # The first free number, as SillyTavern numbers one, and listed last.
    assert added["uid"] == 0 and sorted(book["entries"]) == ["0", "3"]
    assert lore.book_entries(book) == [raw, added]
    assert "key" in added, "a new entry in the book's own shape"
    lore.remove_entry(book, raw)
    assert list(book["entries"]) == ["0"]


def test_entries_are_added_copied_and_removed():
    book = lore.new_book("Elena")
    first = lore.add_entry(book)
    lore.set_entry_title(first, "Curfew")
    first["content"] = "Nobody goes out after dark."
    copy = lore.copy_entry(book, first)
    assert lore.entry_title(copy) == "Curfew" and copy["id"] != first["id"]
    copy["content"] = "Changed."
    assert first["content"] == "Nobody goes out after dark.", "a copy of its own"
    lore.remove_entry(book, first)
    assert [lore.entry_title(raw) for raw in lore.book_entries(book)] == ["Curfew"]
    lore.set_book_rest(book, {"scan_depth": 9, "extensions": {"x": 1}})
    assert book["scan_depth"] == 9 and "token_budget" not in book and book["name"] == "Elena"
    # A story takes the entries as they now are.
    assert [entry.content for entry in lore.lore_from_book(book).entries] == ["Changed."]


def test_files_are_added_linked_and_renamed_in_their_folder(tmp_path: Path):
    root, elena, _notes = kept_elena(tmp_path)
    pictures = store(root)
    song = collection.add_file(elena, pictures, "theme song", "MP3", b"ID3...", "x-risu-asset")
    assert song.file == f"{collection.FILES_DIR}/{elena.id}/theme song.mp3" and song.ext == "mp3"
    assert pictures.read(song.file) == b"ID3..."
    link = collection.add_link(elena, "https://example.com/art/dusk.png?size=2", "background")
    assert (link.name, link.ext, link.uri, link.held) == (
        "dusk",
        "png",
        "https://example.com/art/dusk.png?size=2",
        False,
    )
    farm = next(a for a in elena.tavern.assets if a.name == "Farmhouse")
    farm.name = "The farmhouse"
    old = collection.rename_file(elena, farm, pictures)
    assert farm.file.endswith("/The farmhouse.png") and pictures.read(farm.file) == FARMHOUSE
    assert old is not None and pictures.read(old) == FARMHOUSE, "the window removes it"
    assert collection.rename_file(elena, farm, pictures) is None, "nothing to move"


def test_an_edited_card_goes_out_as_edited(tmp_path: Path):
    root, elena, _notes = kept_elena(tmp_path)
    pictures = store(root)
    elena.tavern.character_book = {**SPEC_BOOK, "entries": [dict(SPEC_BOOK["entries"][0])]}
    lore.book_entries(elena.tavern.character_book)[0]["content"] = "Painted green now."
    elena.tavern.source = ["https://example.com/elena"]
    elena.tavern.extensions = {"talkativeness": "0.8"}
    elena.tavern.group_only_greetings = ["Everyone, inside."]
    collection.add_file(elena, pictures, "theme", "mp3", b"ID3 new", "x-risu-asset")
    files = [(a, pictures.read(a.file) if a.held else None) for a in elena.tavern.assets]
    refs = [(ref, pictures.read(ref.file)) for ref in elena.reference_images]
    _v2, v3 = card_data(elena)
    again = tavern_from_file(io.BytesIO(card_charx(v3, refs, files)), "elena.charx")
    data = again.fields
    assert data["character_book"]["entries"][0]["content"] == "Painted green now."
    assert data["character_book"]["entries"][0]["extensions"] == {"depth": 2, "probability": 70}
    assert data["source"] == ["https://example.com/elena"]
    assert data["extensions"]["talkativeness"] == "0.8"
    assert data["group_only_greetings"] == ["Everyone, inside."]
    assert any(f.name == "theme" and f.data == b"ID3 new" for f in again.files)


def test_a_card_changed_here_says_when(tmp_path: Path):
    made = Character(id="m", name="Made here")
    made.collection = CollectionInfo(
        added_at="2026-11-01T00:00:00+00:00", changed_at="2026-11-01T00:00:00+00:00"
    )
    _v2, v3 = card_data(made)
    assert v3["data"]["creation_date"] == 1793491200
    assert "modification_date" not in v3["data"], "never changed since it was made"
    made.collection.changed_at = "2026-11-04T00:00:00+00:00"
    _v2, v3 = card_data(made)
    assert v3["data"]["modification_date"] == 1793750400
    # One read in keeps its own creation date; the change is still dated.
    _root, elena, _notes = kept_elena(tmp_path)
    elena.collection.came_from = "elena.charx"
    elena.collection.added_at = "2026-11-01T00:00:00+00:00"
    elena.collection.changed_at = "2026-11-04T00:00:00+00:00"
    _v2, v3 = card_data(elena)
    assert v3["data"].get("creation_date") == elena.tavern.creation_date
    assert v3["data"]["modification_date"] == 1793750400


def test_a_card_read_back_has_its_edits(tmp_path: Path):
    """The spec's own reader, as anyone it is shared with would read it."""
    root, elena, _notes = kept_elena(tmp_path)
    elena.tavern.character_book = lore.new_book("Elena's")
    raw = lore.add_entry(elena.tavern.character_book)
    lore.set_entry_title(raw, "The creek")
    lore.entry_set(raw, "keys", ["creek"])
    raw["content"] = "Cold all year."
    _v2, v3 = card_data(elena)
    out = tmp_path / "elena.charx"
    out.write_bytes(card_charx(v3, [], []))
    book = read_card(out).book
    assert [(e.title, e.keywords, e.content) for e in lore.lore_from_book(book).entries] == [
        ("The creek", ["creek"], "Cold all year.")
    ]


# --- SillyTavern's own conversions ---------------------------------------------------------

# A card's entry as SillyTavern writes one (its convertWorldInfoToCharacterBook).
ST_CARD_BOOK = {
    "name": "Doomsville",
    "entries": [
        {
            "id": 7,
            "keys": ["Hellsville", "the town"],
            "secondary_keys": ["wall"],
            "comment": "Hellsville",
            "content": "Hellsville is walled.",
            "constant": False,
            "selective": True,
            "insertion_order": 40,
            "enabled": False,
            "position": "after_char",
            "use_regex": True,
            "extensions": {
                "position": 4,
                "depth": 2,
                "probability": 70,
                "useProbability": True,
                "group": "towns",
                "selectiveLogic": 3,
                "display_index": 5,
                "case_sensitive": None,
                "sticky": 2,
            },
        }
    ],
}


def test_a_cards_book_becomes_sillytaverns_file_as_sillytavern_makes_it():
    world = lore.world_from_book(ST_CARD_BOOK)
    entry = world["entries"]["7"]
    assert (entry["uid"], entry["key"], entry["keysecondary"], entry["comment"]) == (
        7,
        ["Hellsville", "the town"],
        ["wall"],
        "Hellsville",
    )
    assert (entry["order"], entry["disable"], entry["position"], entry["depth"]) == (
        40,
        True,
        4,
        2,
    )
    assert (entry["probability"], entry["group"], entry["selectiveLogic"]) == (70, "towns", 3)
    assert (entry["displayIndex"], entry["sticky"], entry["addMemo"]) == (5, 2, True)
    assert entry["extensions"] == ST_CARD_BOOK["entries"][0]["extensions"]
    assert entry["vectorized"] is False and entry["groupWeight"] == 100, "the rest as its template"
    assert world["originalData"] == ST_CARD_BOOK, "the card's book beside, as SillyTavern keeps it"


def test_sillytaverns_file_goes_on_a_card_as_sillytavern_puts_it_and_back():
    world = lore.world_from_book(ST_CARD_BOOK)
    book = lore.book_from_world(world, name="Doomsville")
    entry = book["entries"][0]
    assert book["name"] == "Doomsville"
    assert (entry["id"], entry["keys"], entry["insertion_order"], entry["enabled"]) == (
        7,
        ["Hellsville", "the town"],
        40,
        False,
    )
    assert entry["position"] == "after_char" and entry["use_regex"] is True
    ext = entry["extensions"]
    assert (ext["position"], ext["depth"], ext["probability"], ext["group"]) == (4, 2, 70, "towns")
    assert (ext["selectiveLogic"], ext["display_index"], ext["sticky"]) == (3, 5, 2)
    # Round and round, it settles: nothing is lost or invented a second time.
    again = lore.book_from_world(lore.world_from_book(book), name="Doomsville")
    assert again == book


def test_a_book_in_either_shape_passes_through_the_other_way_as_it_is():
    assert lore.book_from_world(ST_CARD_BOOK) == ST_CARD_BOOK
    assert lore.world_from_book(WORLD_BOOK) == WORLD_BOOK


def test_entries_numbered_by_javascripts_order():
    world = {"entries": {"10": {"uid": 10, "content": "b"}, "2": {"uid": 2, "content": "a"}}}
    assert [e["content"] for e in lore.book_from_world(world)["entries"]] == ["a", "b"]


def test_a_cards_entry_marked_as_patterns_keeps_its_plain_keywords():
    """SillyTavern marks every entry of a card it writes as patterns; read as
    no keywords, such an entry reached a story with none. It never reads the
    mark: only a key written /like this/ is a pattern, any other is matched
    as written, signs and all."""
    book = {
        "entries": [
            {"keys": ["Hellsville", "the town", "Mr. Smith"], "content": "x", "use_regex": True},
            {"keys": ["wolf(ves)?", "/howl/i", "pack"], "content": "y", "use_regex": True},
        ]
    }
    read = lore.lore_from_book(book)
    assert [entry.keywords for entry in read.entries] == [
        ["Hellsville", "the town", "Mr. Smith"],
        ["wolf(ves)?", "pack"],
    ]
    assert read.left_out["regex"] == 1, "only the entry that had patterns says so"


def test_a_copy_lands_right_after_its_original():
    book = lore.new_book()
    first, second = lore.add_entry(book), lore.add_entry(book)
    lore.set_entry_title(first, "First")
    lore.set_entry_title(second, "Second")
    lore.copy_entry(book, first)
    assert [lore.entry_title(e) for e in lore.book_entries(book)] == ["First", "First", "Second"]


def test_a_new_entry_in_sillytaverns_shape_takes_a_free_number_and_comes_last():
    """getFreeWorldEntryUid: the first number nothing is kept under. Past the
    highest uid, it took the place of entries kept with none."""
    farm, creek = {"key": ["farm"], "content": "The farm."}, {"key": ["creek"], "content": "Cold."}
    book = {"entries": {"0": farm, "1": creek}}
    added = lore.add_entry(book)
    assert (added["uid"], book["entries"]["0"], book["entries"]["1"]) == (2, farm, creek)
    assert lore.book_entries(book)[-1] is added
    # A gap is filled, and the new one is still listed after the others.
    book = {
        "entries": {
            "0": {"uid": 0, "content": "a", "displayIndex": 5},
            "2": {"uid": 2, "content": "b", "displayIndex": 6},
        }
    }
    added = lore.add_entry(book)
    assert (added["uid"], added["displayIndex"], sorted(book["entries"])) == (1, 7, ["0", "1", "2"])
    copy = lore.copy_entry(book, book["entries"]["0"])
    assert (copy["uid"], copy["displayIndex"], copy["content"]) == (3, 8, "a")
    assert lore.book_entries(book)[-2:] == [added, copy]


def test_each_field_goes_by_the_name_of_the_books_shape():
    """An entry of SillyTavern's own shape missing a field got the spec's
    name for it (`keys`, `insertion_order`, `enabled`), which SillyTavern
    never reads."""
    raw = {"uid": 3, "content": "Cold.", "disable": True}
    lore.entry_set(raw, "keys", ["creek"], numbered=True)
    lore.entry_set(raw, "secondary_keys", ["winter"], numbered=True)
    lore.entry_set(raw, "insertion_order", 40, numbered=True)
    lore.set_entry_case_sensitive(raw, True, numbered=True)
    assert {"key", "keysecondary", "order", "caseSensitive"} <= set(raw)
    spec_names = {"keys", "secondary_keys", "insertion_order", "case_sensitive", "extensions"}
    assert not spec_names & set(raw)
    unswitched = {"uid": 4, "key": ["farm"], "content": "c"}
    lore.set_entry_enabled(unswitched, False, numbered=True)
    assert unswitched["disable"] is True and "enabled" not in unswitched
    assert lore.world_from_book({"entries": {"4": unswitched}})["entries"]["4"]["disable"]
    # A card's entry under both names is edited, and read, under the spec's.
    card = {"id": 0, "keys": ["old"], "key": ["old"], "content": "c", "enabled": True}
    lore.entry_set(card, "keys", ["new"])
    assert (card["keys"], lore.entry_keys(card)) == (["new"], ["new"])
    assert lore.lore_from_book({"entries": [card]}).entries[0].keywords == ["new"]
    switched = {"keys": ["farm"], "content": "c", "disable": False}
    lore.set_entry_enabled(switched, False)
    assert switched["enabled"] is False and switched["disable"] is True


def test_the_case_tick_reaches_sillytavern_from_a_cards_book():
    """SillyTavern reads a card's entry's case from its extensions alone
    (convertCharacterBook): written only where they had it, it never got
    there."""
    book = lore.new_book("Doomsville")
    raw = lore.add_entry(book)
    raw["content"] = "Cold."
    lore.set_entry_case_sensitive(raw, True)
    assert raw["case_sensitive"] is True and raw["extensions"] == {"case_sensitive": True}
    assert lore.world_from_book(book)["entries"]["0"]["caseSensitive"] is True
    bare = {"keys": ["US"], "content": "c", "enabled": True}
    lore.set_entry_case_sensitive(bare, True)
    assert bare["extensions"] == {"case_sensitive": True}, "made for it"
    assert lore.entry_case_sensitive(bare)
    lore.set_entry_case_sensitive(bare, False)
    assert bare["extensions"]["case_sensitive"] is False and not lore.entry_case_sensitive(bare)


def test_keywords_typed_on_a_line_are_split_as_sillytavern_splits_them():
    split = lore.split_keys
    assert split(r"/\b\d{1,3} men\b/i, farm") == [r"/\b\d{1,3} men\b/i", "farm"]
    assert split("farm,/a,b/i") == ["farm", "/a,b/i"]
    assert split(r"/a\/b,c/, d") == [r"/a\/b,c/", "d"], "an escaped slash closes nothing"
    # A finished one that is no pattern after all is split at its commas.
    assert split("/a,b/x, farm") == ["/a", "b/x", "farm"]
    assert split("farm, /me, barn") == ["farm", "/me", "barn"], "the end finishes the last"
    # Only a slash where a keyword begins opens a pattern.
    assert split("AC/DC, Queen, 24/7") == ["AC/DC", "Queen", "24/7"]
    assert split(" farm , ,barn,") == ["farm", "barn"]
    for keys in (["/x/", "y"], ["/usr", "bin"], ["/a,b/i", "/c/"], ["and/or", "it"]):
        assert split(", ".join(keys)) == keys, keys
    # A card that keeps its keys as one line is read the same way.
    assert lore.entry_keys({"keys": "farm, /a,b/i"}) == ["farm", "/a,b/i"]


def test_books_go_by_javascripts_own_rules_for_keys():
    world = {
        "entries": {
            "01": {"uid": 1, "content": "c"},
            "²": {"uid": 2, "content": "d"},
            "1": {"uid": 1, "content": "b"},
            "4294967295": {"uid": 3, "content": "e"},
            "0": {"uid": 0, "content": "a"},
        }
    }
    entries = lore.book_from_world(world)["entries"]
    assert [entry["content"] for entry in entries] == ["a", "b", "c", "d", "e"]
    card = {"entries": [{"id": None, "keys": ["a"], "content": "x"}]}
    card["entries"] += [{"id": True, "content": "y"}, {"id": 3.0, "content": "z"}]
    assert list(lore.world_from_book(card)["entries"]) == ["null", "true", "3"]
