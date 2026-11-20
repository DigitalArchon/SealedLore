"""The people a lorebook describes, found by the authoring model and cut out
of it word for word (engine/lore_people.py), and a lorebook's titles read as
words (storage/tavern_lore.py `clean_title`).

The fixtures are in the shape of the lorebooks the author was given (a world
of profiles titled "[👤] Name", a crew in one always-on entry, instructions
for the storyteller among the lore), with Doomsville's people: the repo is
public."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.lore_people import (
    PART_MIN_WORDS,
    Finding,
    FoundRules,
    LorePeople,
    Shown,
    batches,
    find_messages,
    outline,
    parse_find,
    parse_split,
    render_shown,
    shown_entries,
    single,
    split_messages,
    take_apart,
)
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.tavern_lore import clean_title, heading_of, lore_from_book
from tests.test_session import make_config

JOHN = """[General Info]
Name: John Hale
Age: 41
Appearance: A broad man in a sheepskin coat, grey at the temples, a rifle always to hand.
Personality: Slow to speak and slower to trust. He kept the farm running through two winters
of the dead walking and he will not give it up for anyone, whatever Hellsville offers him."""

CREW = """[MEMBER 1: ELENA — MEDIC]
Name: Elena Ruiz
Age: 34
She was a nurse in the city before it fell. She keeps the medicine locked in a tin box and
counts every pill twice. She trusts John with her life and nobody else with the key, and she
has not slept a whole night since the barn burned.

[MEMBER 2: MARCUS — SCOUT]
Name: Marcus Webb
Age: 29
He walks the tree line at dawn and dusk and reads the snow like a newspaper. He talks too
much when he is frightened and not at all when he is angry, and the others have learned to
listen for the silence before trouble comes."""

LIST = """Hellsville's council:
- Ben Ortiz: the mayor.
- Sara Ortiz: his wife, who runs the store.
"""

RULES = """[SECTION 9: TONE RULES]
Keep it grim but never hopeless. Describe the cold. Never resolve a threat in one passage."""


def book() -> dict:
    """A lorebook in SillyTavern's own shape, its memos dressed as such books
    dress them."""
    entries = [
        ("═══════[Doomsville]═══════", "The world after the dead rose, a winter long.", True),
        ("[👤] John Hale", JOHN, False),
        ("2", CREW, True),
        ("[›] [°] Hellsville", "A walled town five miles east. " + LIST, False),
        ("[☰] Tone [☰]", RULES, True),
        ("[𖤓] The Farmhouse", "The Hale farm, its windows boarded.", False),
    ]
    return {
        "entries": {
            str(i): {
                "uid": i,
                "comment": title,
                "content": content,
                "key": [title.split()[-1]],
                "constant": constant,
            }
            for i, (title, content, constant) in enumerate(entries)
        }
    }


def test_titles_lose_their_marks_and_keep_their_words():
    assert clean_title("[👤] John Hale") == "John Hale"
    assert clean_title("═══════[World]═══════") == "World"
    assert clean_title("[›] [°] Hellsville") == "Hellsville"
    assert clean_title("⚠️⛔READ THIS⛔⚠️") == "READ THIS"
    assert clean_title("[𖤓] Sea God") == "Sea God"
    assert clean_title("[☰] World Setting [☰]") == "World Setting"
    assert clean_title("[🔰¹] Beginner") == "Beginner"
    # Words in a bracket are words; digits alone are no title.
    assert clean_title("[Draft] notes") == "[Draft] notes"
    assert clean_title("[中文] x") == "[中文] x"
    assert clean_title("The farmhouse (east)") == "The farmhouse (east)"
    assert clean_title("12") == ""
    assert heading_of("[SECTION 1: WORLD OVERVIEW]\nTHE SETTING:") == "SECTION 1: WORLD OVERVIEW"
    assert heading_of("plain words first") == ""


def test_a_book_read_as_lore_has_titles_of_words():
    titles = [entry.title for entry in lore_from_book(book()).entries]
    # "2" has no words: the entry is named by the heading it opens with.
    assert titles == [
        "Doomsville",
        "John Hale",
        "MEMBER 1: ELENA — MEDIC",
        "Hellsville",
        "Tone",
        "The Farmhouse",
    ]


def lore() -> list[LoreEntry]:
    return lore_from_book(book()).entries


def test_the_finder_is_shown_each_entry_briefly_with_the_headings_inside_it():
    shown = shown_entries(lore())
    crew = render_shown(shown[2])
    assert crew.startswith("[3] MEMBER 1: ELENA — MEDIC (always sent)")
    assert "Headings inside: [MEMBER 2: MARCUS — SCOUT]" in crew
    assert "listen for the silence" not in crew  # past the opening words
    assert outline(JOHN) == []
    messages = find_messages(shown, "Doomsville")
    assert messages[0].role == "system" and '"kind": "person"' in messages[0].text
    assert messages[1].text.startswith("The lorebook: Doomsville")
    # Small runs: a book of many entries is several calls, in order.
    many = [Shown(i, f"e{i}", f"Entry {i}", (), False, "word " * 300) for i in range(1, 101)]
    runs = batches(many, most=2_000)
    assert len(runs) >= 5 and [item.number for run in runs for item in run] == list(range(1, 101))


def test_the_reply_names_only_entries_it_was_shown_each_once():
    shown = shown_entries(lore())
    reply = json.dumps(
        {
            "entries": [
                {"n": 2, "kind": "person", "name": "John Hale", "aliases": ["John", "the farmer"]},
                {"n": 2, "kind": "instructions"},  # the first word stands
                {"n": 3, "kind": "people"},
                {"n": 5, "kind": "instructions"},
                {"n": 40, "kind": "person", "name": "Nobody"},  # not shown
                {"n": 4, "kind": "place"},  # not a kind asked for
                {"n": 6, "kind": "person"},  # no name
            ]
        }
    )
    found = parse_find(reply, shown)
    assert [(f.number, f.kind) for f in found] == [
        (2, "person"),
        (3, "people"),
        (5, "instructions"),
    ]
    # An alias that would fit others too is dropped.
    assert found[0].aliases == ("John",)
    with pytest.raises(ValueError):
        parse_find("I couldn't tell.", shown)


def test_one_persons_card_is_their_whole_entry():
    shown = shown_entries(lore())[1]
    person = single(shown, Finding(2, "person", "John Hale", ("John",), "A farmer."))
    assert person.text == JOHN and person.whole_entry


def test_several_in_one_entry_are_cut_where_each_begins():
    shown = shown_entries(lore())[2]
    assert "Elena" in split_messages(shown, "Doomsville")[1].text
    reply = json.dumps(
        {
            "people": [
                {"name": "Elena Ruiz", "summary": "The medic.", "starts": "MEMBER 1: ELENA"},
                # Retyped quotes and case are allowed for.
                {"name": "Marcus Webb", "starts": "[member 2: marcus — scout]"},
                {"name": "Ben Ortiz", "starts": "not in this entry at all, anywhere"},
            ]
        }
    )
    people, warnings = parse_split(reply, shown)
    assert [p.name for p in people] == ["Elena Ruiz", "Marcus Webb"]
    elena, marcus = people
    assert elena.text.startswith("[MEMBER 1: ELENA") and elena.text.endswith("barn burned.")
    assert marcus.text.startswith("[MEMBER 2: MARCUS") and marcus.text.endswith("comes.")
    assert elena.summary == "The medic." and not elena.whole_entry
    assert warnings == [
        "where Ben Ortiz's part of “MEMBER 1: ELENA — MEDIC” begins couldn't be found"
    ]


def test_a_line_in_a_list_is_nobodys_profile():
    shown = shown_entries(lore())[3]
    reply = json.dumps(
        {
            "people": [
                {"name": "Ben Ortiz", "starts": "- Ben Ortiz: the mayor."},
                {"name": "Sara Ortiz", "starts": "- Sara Ortiz: his wife"},
            ]
        }
    )
    people, warnings = parse_split(reply, shown)
    assert people == [] and PART_MIN_WORDS > 10
    assert "only a line" in warnings[0] and "stay in the lore" in warnings[0]


def found_in(entries: list[LoreEntry]) -> LorePeople:
    shown = shown_entries(entries)
    found = LorePeople()
    found.people.append(single(shown[1], Finding(2, "person", "John Hale")))
    reply = json.dumps(
        {
            "people": [
                {"name": "Elena Ruiz", "starts": "[MEMBER 1: ELENA — MEDIC]"},
                {"name": "Marcus Webb", "starts": "[MEMBER 2: MARCUS — SCOUT]"},
            ]
        }
    )
    found.people.extend(parse_split(reply, shown[2])[0])
    found.instructions.append(FoundRules(shown[4].entry_id, shown[4].title, 20, True))
    return found


def test_taking_them_apart_makes_cards_and_takes_their_words_out_of_the_lore():
    entries = lore()
    before = [entry.model_copy(deep=True) for entry in entries]
    found = found_in(entries)
    john, elena, marcus = found.people
    took = take_apart(
        entries,
        [(john, "cast"), (elena, "supporting"), (marcus, "supporting")],
        found.instructions,
    )
    assert entries == before  # a new list
    assert [c.name for c in took.cast] == ["John Hale"] and took.cast[0].is_player_available
    assert took.cast[0].full_description == JOHN
    assert [c.name for c in took.supporting] == ["Elena Ruiz", "Marcus Webb"]
    assert not took.supporting[0].is_player_available
    # John's entry, the crew's (all of it theirs) and the rules' are gone.
    assert [e.title for e in took.lore] == ["Doomsville", "Hellsville", "The Farmhouse"]
    assert took.removed == 3 and took.shortened == 0
    assert took.rules.startswith("Tone:\n[SECTION 9: TONE RULES]")


def test_someone_left_in_the_lore_keeps_their_words_there():
    entries = lore()
    found = found_in(entries)
    john, elena, marcus = found.people
    took = take_apart(entries, [(john, "lore"), (elena, "supporting"), (marcus, "lore")])
    crew = next(e for e in took.lore if e.title.startswith("MEMBER 1"))
    assert "Marcus Webb" in crew.content and "Elena Ruiz" not in crew.content
    assert any(e.title == "John Hale" for e in took.lore)
    assert took.shortened == 1 and crew.embedding_hash is None
    # One with a card already: their words come out, and no card is made.
    took = take_apart(entries, [(john, "drop")])
    assert not took.cast and not any(e.title == "John Hale" for e in took.lore)


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    bundle = StoryBundle(story=story, cast=cast, lore=lore())
    save_story_bundle(bundle, root=tmp_path)
    find = json.dumps(
        {
            "entries": [
                {"n": 2, "kind": "person", "name": "John Hale", "summary": "The farmer."},
                {"n": 3, "kind": "people"},
                {"n": 5, "kind": "instructions"},
            ]
        }
    )
    split = json.dumps(
        {
            "people": [
                {"name": "Elena Ruiz", "starts": "[MEMBER 1: ELENA — MEDIC]"},
                {"name": "Marcus Webb", "starts": "[MEMBER 2: MARCUS — SCOUT]"},
            ]
        }
    )
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider([find, split]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )


def test_the_session_finds_them_logs_the_calls_and_changes_nothing(
    session: StorySession, tmp_path: Path
):
    found = LorePeople()
    notices = [n.text for n in session.find_lore_people(session.bundle.lore, found, "Doomsville")]
    assert notices[0].startswith("Reading entries 1–6 of 6")
    assert notices[1].startswith("Finding everyone in “MEMBER 1")
    assert [p.name for p in found.people] == ["John Hale", "Elena Ruiz", "Marcus Webb"]
    assert [r.title for r in found.instructions] == ["Tone"]
    assert len(session.unreported_usage) == 2 and len(session.bundle.lore) == 6
    log = (tmp_path / "stories" / session.story.id / "api_log.jsonl").read_text()
    assert log.count('"lore_people_request"') == 2 and log.count('"lore_people_response"') == 2


def test_the_session_makes_their_cards_and_moves_the_rules_into_the_notes(
    session: StorySession, tmp_path: Path
):
    found = LorePeople()
    list(session.find_lore_people(session.bundle.lore, found))
    john, elena, marcus = found.people
    session.story.style.notes = "Keep it short."
    took = session.take_lore_people(
        [(john, "cast"), (elena, "supporting"), (marcus, "lore")], found.instructions
    )
    assert "John Hale" in [c.name for c in session.cast]
    assert [c.name for c in session.supporting][-1] == "Elena Ruiz"
    assert session.story.style.notes.startswith("Keep it short.\n\nTone:\n")
    assert took.removed == 2 and took.shortened == 1
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert "Tone" not in [e.title for e in saved.lore]
    assert "John Hale" in [c.name for c in saved.cast]


def test_a_hand_edited_style_takes_the_rules_in_its_own_text(session: StorySession):
    found = LorePeople()
    list(session.find_lore_people(session.bundle.lore, found))
    style = session.story.style
    style.detached, style.custom_text = True, "# STYLE\n\nMy own words."
    session.take_lore_people([], found.instructions)
    assert style.custom_text.endswith("Never resolve a threat in one passage.")
    assert not style.notes


def test_an_entry_changed_since_it_was_read_is_left_as_it_is(session: StorySession):
    found = LorePeople()
    list(session.find_lore_people(session.bundle.lore, found))
    session.bundle.lore[1].content = "John Hale, rewritten by hand."
    took = session.take_lore_people([(found.people[0], "cast")])
    assert not took.cast and session.bundle.lore[1].content == "John Hale, rewritten by hand."


def test_a_chat_has_none(session: StorySession):
    session.story.mode = "chat"
    with pytest.raises(ValueError):
        session.take_lore_people([])
