"""What the review of 3.1.0b15 found (Nov 21 2026), one test a fault: each
fails on da927af, but the two that hold the new matching to what it was
(plain keys as before, groups nested hundreds deep). The scripted mock
provider and made-up lorebooks: no network."""

from __future__ import annotations

import random
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest

from sealedlore.engine import tavern_firing
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.session_tavern import RETAKE
from sealedlore.engine.tavern_firing import (
    Buffer,
    FireEntry,
    TavernSettings,
    Timed,
    entry_settings,
    fire,
    fire_entry,
    set_entry_setting,
)
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.tavern_lore import lore_from_book
from tests.test_session import make_config

WORDS = lambda text: len(text.split())  # noqa: E731 - a token a word


def entry(id: str, keys=(), **fields) -> FireEntry:  # noqa: A002
    return FireEntry(
        id=id, title=id, content=fields.pop("content", f"About {id}."), keys=tuple(keys), **fields
    )


def run(entries, messages, *, settings=None, context=1000, **kw):
    return fire(
        entries,
        messages,
        settings=settings or TavernSettings(),
        context=context,
        count_tokens=WORDS,
        timed=Timed(),
        length=len(messages),
        rng=random.Random(1),
        **kw,
    )


# --- a stranger's patterns --------------------------------------------------------

SLOW = [
    r"/(\w+\s?)+$/",  # a repeat of a repeat: eight times slower a word
    r"/(a+)+$/",
    r"/(a|aa)+$/",
    r"/(a?){30}b/",
    "/" + "a?" * 40 + "a" * 40 + "/",  # forty optional letters: 2^40 ways
    r"/a.*b.*c.*d.*e/",
]


@pytest.mark.parametrize("key", SLOW)
def test_a_pattern_that_can_take_minutes_is_never_run(key: str):
    """/(\\w+\\s?)+$/ against a message ending in punctuation took 4.5 s at
    eight words and eight times that a word more, the interpreter held."""
    message = " ".join(["word"] * 40) + "! " + "a" * 60
    began = time.perf_counter()
    result = run([entry("hang", [key])], [message])
    assert time.perf_counter() - began < 1.0
    assert not result.fired
    assert "is never run" in result.not_fired["hang"]


def test_a_book_can_carry_neither_pattern_nor_text_to_hang_on():
    """The pattern and what it reads both in the book: recursion reads other
    entries' text."""
    entries = [
        entry("bait", ["start"], content="a" * 40 + "!"),
        entry("hang", [r"/(a+)+$/"]),
    ]
    began = time.perf_counter()
    result = run(entries, ["start here"])
    assert time.perf_counter() - began < 1.0
    assert result.ids == ["bait"]


@pytest.mark.parametrize(
    "key",
    [
        "/a{99999999999}/",  # OverflowError in the parser
        "/" + "(" * 2000 + "a" + ")" * 2000 + "/",  # RecursionError
        r"/\p{L}+/",  # JavaScript's, not Python's
        r"/(?<=a+)b/",
    ],
)
def test_a_pattern_python_cannot_run_costs_nothing(key: str):
    result = run([entry("odd", [key]), entry("plain", ["wolf"])], ["a wolf and aaab"])
    assert result.ids == ["plain"]
    assert "odd" in result.not_fired


def test_groups_nested_hundreds_deep_are_not_a_crash():
    run([entry("deep", ["/" + "(" * 400 + "a" + ")" * 400 + "/"])], ["a wolf"])


def test_a_number_of_four_hundred_digits_is_not_a_crash():
    """`order`, `sticky` or `probability` as an integer too large for a
    float: OverflowError out of every prompt the story assembled."""
    huge = 10**400
    raw = {"key": ["wolf"], "order": huge, "sticky": huge, "probability": huge, "delay": huge}
    read = fire_entry(
        id="e",
        title="t",
        content="c",
        keywords=["wolf"],
        always_on=False,
        enabled=True,
        raw=raw,
        numbered=True,
    )
    assert read.order == tavern_firing.DEFAULT_ORDER and read.sticky == 0
    assert read.probability == 100
    assert entry_settings(raw, True)["order"] == tavern_firing.DEFAULT_ORDER


def test_ordinary_patterns_still_fire_and_read_everything():
    text = "x" * 20_000 + " the dragons of the north breathe fire"
    for key in (r"/drag\w*/i", r"/\bdragons?\b/", r"/(?<what>dragons|wyrms) of \w+/"):
        result = run([entry("d", [key])], [text])
        assert result.ids == ["d"], key
        assert not result.not_fired


def test_a_pattern_with_an_open_repeat_reads_the_newest_and_says_so():
    """/dragon.*fire/ is quadratic on the wrong text: it reads 8,000
    characters, the newest, and the Lore page says so when that kept it
    from firing."""
    pattern = tavern_firing._pattern(r"/dragon.*fire/")
    assert pattern is not None and pattern.cap == tavern_firing.SCAN_CAPS[1]
    near = run([entry("d", [r"/dragon.*fire/"])], ["a dragon breathes fire " + "x" * 20_000])
    assert near.ids == ["d"]
    far = run([entry("d", [r"/dragon.*fire/"])], ["x" * 20_000 + " a dragon breathes fire"])
    assert not far.fired
    assert "read only the newest 8,000 characters" in far.not_fired["d"]
    # Text made to be slow, at the caps: well under a second each.
    for key, text in (
        (r"/a[\s\S]*?b/", "a" * 50_000),
        (r"/a.*b.*c/", "a" * 5_000 + "b" * 5_000),
        (r"/a.*b.*c.*d/", "a" * 500 + "b" * 500 + "c" * 500),
    ):
        began = time.perf_counter()
        run([entry("s", [key])], [text])
        assert time.perf_counter() - began < 1.5, key


def test_a_turns_patterns_stop_being_run_once_they_have_taken_too_long(monkeypatch):
    monkeypatch.setattr(tavern_firing, "PATTERN_SECONDS", 0.0)
    buffer = Buffer(["a wolf"], TavernSettings())
    first, second = entry("a", ["/wolf/"]), entry("b", ["/a wolf/"])
    text = buffer.text(first, "initial")
    buffer.spent = 0.001
    assert not buffer.matches(text, "/a wolf/", second)
    assert tavern_firing.OUT_OF_TIME in buffer.said_unread(second)


def test_a_key_is_a_pattern_only_as_the_importer_reads_one():
    """/usr/bin/ (a slash inside, unescaped) and /a/gg (a flag twice) are
    plain text to SillyTavern and to the importer; here /a/gg matched any
    text with an "a" in it."""
    assert tavern_firing._pattern("/usr/bin/") is None
    assert tavern_firing._pattern("/a/gg") is None
    assert not run([entry("e", ["/a/gg"])], ["a cat sat"]).fired


def _reference(haystack: str, key: str, case: bool, whole: bool) -> bool:
    """A plain key as it was matched before: SillyTavern's own expression."""
    if not case:
        haystack, key = haystack.lower(), key.lower()
    if not whole or len(key.split()) > 1:
        return key in haystack
    found = re.search(rf"(?:^|\W)({re.escape(key)})(?:$|\W)", haystack, re.ASCII)
    return found is not None


def test_plain_keys_match_as_they_did_whatever_the_pass():
    """Plain keys are looked for a message at a time and remembered: the
    same answers as reading the whole text with SillyTavern's expression."""
    rng = random.Random(7)
    words = ["wolf", "Wolf", "wolves", "the", "old-road", "road", "x_y", "é", "king's", "a", "!"]
    for _ in range(300):
        messages = [
            " ".join(rng.choice(words) for _ in range(rng.randint(0, 6)))
            for _ in range(rng.randint(1, 6))
        ]
        settings = TavernSettings(scan_depth=rng.randint(1, 6))
        buffer = Buffer(messages, settings)
        buffer.recurse = [
            " ".join(rng.choice(words) for _ in range(rng.randint(0, 5)))
            for _ in range(rng.randint(0, 2))
        ]
        for _ in range(12):
            key = rng.choice([*words, "old road", "the wolf", "olf", "-road", "king"])
            case, whole = rng.random() < 0.5, rng.random() < 0.5
            one = entry("e", [key], case_sensitive=case, match_whole_words=whole)
            state = rng.choice(["initial", "recursion", "min_activations"])
            buffer.skew = rng.randint(0, 3)
            text = buffer.text(one, state)
            assert buffer.matches(text, key, one) == _reference(text, key, case, whole), (
                messages,
                buffer.recurse,
                key,
                case,
                whole,
                state,
            )


def test_reaching_back_a_message_a_pass_is_quick():
    """Min activations with nothing matching: 200 messages and 100 entries
    took 38 s, the text joined and read whole for every entry every pass."""
    words = "the quick brown fox jumps over the lazy dog and runs into the dark wood " * 12
    entries = [entry(str(i), [f"zzkey{i}"], content="x " * 150) for i in range(100)]
    began = time.perf_counter()
    result = run(entries, [words] * 200, settings=TavernSettings(min_activations=1))
    assert time.perf_counter() - began < 3.0
    assert not result.fired


def test_a_cards_entry_takes_a_new_order_where_it_is_read():
    """A card's entry with `order` and no `insertion_order`: the review's
    change was written to `order`, which nothing reads for a card."""
    raw = {"keys": ["wolf"], "order": 5}
    set_entry_setting(raw, False, "order", 40)
    assert entry_settings(raw, False)["order"] == 40


# --- the session's side ----------------------------------------------------------

WORLD = {
    "entries": {
        "0": {
            "uid": 0,
            "comment": "The Valley",
            "key": [],
            "constant": True,
            "content": "The Order of Ash rules the valley from its keep.",
            "order": 10,
        },
        "1": {
            "uid": 1,
            "comment": "The Order of Ash",
            "key": ["Order of Ash"],
            "content": "The Order burns what it cannot read.",
            "order": 50,
        },
        "2": {
            "uid": 2,
            "comment": "The Grey Wolves",
            "key": ["wolf"],
            "content": "The grey wolves hunt in threes.",
            "order": 100,
            "sticky": 3,
        },
    }
}


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def tavern_session(tmp_path: Path, story, cast, world=WORLD, replies=8) -> StorySession:
    story.held_character_id = "char-serrik"
    story.defaults.lore_firing = "sillytavern"
    config = make_config()
    config.scene_reads = "manual"
    return StorySession(
        StoryBundle(story=story, cast=cast, lore=lore_from_book(world).entries),
        config,
        MockChatProvider(["A passage."] * replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_what_a_standing_entry_says_fires_the_others(tmp_path: Path, story, cast):
    """An always-on entry stands in the cached block, and was left out of
    the firing: an entry keyed on its text never fired, as it does in
    SillyTavern. Fired with the rest now, and still sent once."""
    session = tavern_session(tmp_path, story, cast)
    list(session.send(turn("I look around.")))
    report = session.last_retrieval
    assert [e.title for e in report.standing] == ["The Valley"]
    assert [i.entry.title for i in report.injected] == ["The Order of Ash"]
    request = session.provider.requests[-1]
    text = "\n".join(part.text for message in request.messages for part in message.parts)
    assert text.count("rules the valley from its keep") == 1
    # The turn's record is of what is timed: never the standing entries.
    valley = next(e for e in session.bundle.lore if e.title == "The Valley")
    assert valley.id not in session.path()[-2].meta.tavern_fired


def test_standing_entries_use_up_sillytaverns_budget(tmp_path: Path, story, cast):
    """Left out of the firing, a long always-on entry cost SillyTavern's
    budget nothing: entries fired here that SillyTavern leaves out."""
    session = tavern_session(tmp_path, story, cast)
    valley = next(e for e in session.bundle.lore if e.title == "The Valley")
    assert "The Grey Wolves" in [
        i.entry.title for i in session.retrieve(turn("A wolf watches."), []).injected
    ]
    valley.content = "The valley. " + "snow " * 4000
    valley.tavern.fields["content"] = valley.content
    valley.tavern.fields["order"] = 200  # read first, as SillyTavern orders them
    session.story.defaults.context_token_budget = 8_000  # a quarter: 2,000
    report = session.retrieve(turn("A wolf watches."), [])
    assert not report.injected
    assert dict((e.title, why) for e, why in report.not_fired)["The Grey Wolves"] == (
        "over the lore budget"
    )


def test_a_firing_that_fails_never_costs_the_turn(tmp_path: Path, story, cast, monkeypatch):
    """Nothing stood between a lorebook the firing couldn't take and the
    turn, the prompt preview at opening included."""
    from sealedlore.engine import session_tavern

    session = tavern_session(tmp_path, story, cast)

    def broken(*args, **kwargs):
        raise OverflowError("a stranger's number")

    monkeypatch.setattr(session_tavern, "fire", broken)
    list(session.send(turn("A wolf watches.")))
    report = session.last_retrieval
    assert report.selector == "sillytavern" and not report.injected
    assert "OverflowError" in report.fallback_reason
    assert [e.title for e in report.standing] == ["The Valley"]
    assert session.path()[-1].content == "A passage."


def test_an_entry_whose_settings_cannot_be_read_fires_by_its_keywords(
    tmp_path: Path, story, cast, monkeypatch
):
    from sealedlore.engine import session_tavern

    session = tavern_session(tmp_path, story, cast)
    real = session_tavern.fire_entry

    def picky(**kwargs):
        if kwargs.get("raw") is not None and kwargs["title"] == "The Grey Wolves":
            raise TypeError("an entry in no shape SillyTavern writes")
        return real(**kwargs)

    monkeypatch.setattr(session_tavern, "fire_entry", picky)
    assert session.lore_layout().mode == "select"
    report = session.retrieve(turn("A wolf watches."), [])
    assert "The Grey Wolves" in [i.entry.title for i in report.injected]


def test_a_take_that_never_came_leaves_the_turns_record(tmp_path: Path, story, cast):
    """A Regenerate stopped or failed before any text left the standing
    passage with the new firing's sticky and cooldown record."""
    session = tavern_session(tmp_path, story, cast)
    list(session.send(turn("A wolf watches from the trees.")))
    user = session.path()[-2]
    first = list(user.meta.tavern_fired)
    assert first
    retake = replace(turn("Nothing of the kind."), turn_node_id=user.id)
    session._retrieve_for_turn(retake, session.path()[:-2], trigger=RETAKE)
    assert user.meta.tavern_fired == first  # not yet: no text has come
    session.settle_tavern(False)
    assert user.meta.tavern_fired == first
    session._retrieve_for_turn(retake, session.path()[:-2], trigger=RETAKE)
    session.settle_tavern(True)
    assert user.meta.tavern_fired != first
    # A take that does come records as before.
    list(session.regenerate())
    assert session.path()[-2].meta.tavern_fired == first


# --- half a character from outside ------------------------------------------------


def test_a_lorebook_with_half_an_emoji_leaves_the_story_saveable(tmp_path: Path, story, cast):
    """ "\\ud83d" in a lorebook (a string cut mid-emoji by the program that
    wrote it) is read by Python's json and can't be written as UTF-8: added
    to a story, every later save raised and no turn played after was kept."""
    from sealedlore.storage.atomic import atomic_write_json, outside_json, read_json

    book = outside_json(
        '{"entries": {"0": {"uid": 0, "key": ["wolf\\ud83d"], "comment": "Wolves",'
        ' "content": "They hunt in threes \\ud83d", "half\\udc00": 1}}}'
    )
    session = tavern_session(tmp_path, story, cast, world=book)
    list(session.send(turn("A wolf.")))
    session.save()
    entry_ = session.bundle.lore[0]
    assert entry_.content == "They hunt in threes �"
    assert entry_.keywords == ["wolf�"]
    # A whole emoji written as a pair of escapes is a whole emoji still.
    assert outside_json('"\\ud83d\\ude00 and \\u00e9"') == "\U0001f600 and é"
    # And whatever reaches a save some other way is saved, never an error.
    path = tmp_path / "odd.json"
    atomic_write_json(path, {"text": "cut \ud83d here"})
    assert read_json(path) == {"text": "cut � here"}


def test_a_models_reply_and_a_scenario_bring_no_half_characters(tmp_path: Path):
    from sealedlore.engine.jsonreply import extract_json
    from sealedlore.models.scenario import Scenario
    from sealedlore.storage.atomic import append_jsonl, read_jsonl
    from sealedlore.storage.scenario import read_scenario, write_scenario

    assert extract_json('{"summary": "cut \\ud83d"}', dict, "a reply") == {"summary": "cut �"}
    path = tmp_path / "s.sealedlore-scenario.json"
    write_scenario(path, Scenario(title="T", world_bible="The valley."))
    path.write_text(
        path.read_text(encoding="utf-8").replace("The valley.", "The valley \\ud83d."),
        encoding="utf-8",
    )
    assert read_scenario(path).world_bible == "The valley �."
    log = tmp_path / "log.jsonl"
    append_jsonl(log, {"text": "cut \ud83d"})
    assert read_jsonl(log) == [{"text": "cut �"}]


# --- the people in a lorebook -----------------------------------------------------


def _profile(name: str, words: int) -> str:
    return f"[{name.upper()}]\n" + " ".join(f"{name} fact{i}." for i in range(words // 2))


def test_someone_left_in_the_lore_never_leaves_with_the_others():
    """An entry was taken out whole when under forty words were left, though
    those words were a person left in the lore: in no entry, and no card."""
    from sealedlore.engine.lore_people import Shown, parse_split, take_apart

    content = _profile("Alice", 400) + "\n\n" + _profile("Bob", 34)
    entry_ = LoreEntry(title="The sisters", content=content)
    shown = Shown(1, entry_.id, entry_.title, (), False, content)
    reply = (
        '{"people": [{"name": "Alice", "starts": "[ALICE]"}, {"name": "Bob", "starts": "[BOB]"}]}'
    )
    people, _warnings = parse_split(reply, shown)
    assert [p.name for p in people] == ["Alice", "Bob"]
    took = take_apart([entry_], [(people[0], "cast"), (people[1], "lore")])
    assert [c.name for c in took.cast] == ["Alice"]
    assert took.removed == 0 and took.shortened == 1
    assert "Bob fact3." in took.lore[0].content and "Alice" not in took.lore[0].content


def test_someone_with_only_a_line_stays_when_the_rest_are_taken():
    from sealedlore.engine.lore_people import Shown, parse_split, take_apart

    content = _profile("Atofe", 400) + "\n\n[BADIGADI]\nHer betrothed, a king."
    entry_ = LoreEntry(title="Demon kings", content=content)
    shown = Shown(1, entry_.id, entry_.title, (), False, content)
    reply = (
        '{"people": [{"name": "Atofe", "starts": "[ATOFE]"},'
        ' {"name": "Badigadi", "starts": "[BADIGADI]"}]}'
    )
    people, warnings = parse_split(reply, shown)
    assert [p.name for p in people] == ["Atofe"]
    assert any("stay in the lore" in warning for warning in warnings)
    assert not people[0].whole_entry  # the entry holds two people
    took = take_apart([entry_], [(people[0], "supporting")])
    assert took.removed == 0
    assert "Her betrothed, a king." in took.lore[0].content


def test_an_entry_longer_than_was_read_keeps_what_nobody_read(monkeypatch):
    """The finder is shown an entry's first 12,000 words; the last person it
    named was given everything after them, people it never saw included."""
    from sealedlore.engine import lore_people
    from sealedlore.engine.lore_people import Shown, parse_split, take_apart

    monkeypatch.setattr(lore_people, "SPLIT_MOST_WORDS", 300)
    content = "\n\n".join(_profile(name, 200) for name in ("One", "Two", "Three", "Four"))
    entry_ = LoreEntry(title="The four", content=content)
    shown = Shown(1, entry_.id, entry_.title, (), False, content)
    assert lore_people.read_to(content, 300) is not None
    assert lore_people.read_to("a few words", 300) is None
    reply = '{"people": [{"name": "One", "starts": "[ONE]"}, {"name": "Two", "starts": "[TWO]"}]}'
    people, warnings = parse_split(reply, shown)
    assert [p.name for p in people] == ["One"]
    assert any("longer than could be read" in warning for warning in warnings)
    took = take_apart([entry_], [(people[0], "cast")])
    left = took.lore[0].content
    assert "[TWO]" in left and "[FOUR]" in left and "[ONE]" not in left


def test_a_number_no_entry_has_is_not_a_crash():
    """ "n": 1e999 is infinity, and int() of it an OverflowError that ended
    the whole run, the batches already paid for with it."""
    from sealedlore.engine.lore_people import Shown, parse_find

    shown = [Shown(1, "id", "Elena", (), False, "Elena Ruiz is a doctor.")]
    reply = (
        '{"entries": [{"n": 1e999, "kind": "person", "name": "X"},'
        ' {"n": true, "kind": "person", "name": "Y"},'
        ' {"n": 1, "kind": "person", "name": "Elena Ruiz"}]}'
    )
    assert [f.name for f in parse_find(reply, shown)] == ["Elena Ruiz"]


def test_the_engine_refuses_the_people_finder_in_a_chat(tmp_path: Path, story, cast):
    from sealedlore.engine.lore_people import LorePeople

    session = tavern_session(tmp_path, story, cast)
    session.story.mode = "chat"
    with pytest.raises(ValueError, match="simple chat"):
        list(session.find_lore_people(session.bundle.lore, LorePeople()))
    assert not session.provider.requests


# --- what the story suggests -------------------------------------------------------


def _suggest_text(messages) -> str:
    return "\n".join(part.text for message in messages for part in message.parts)


def test_a_reading_is_shown_only_so_much_of_a_long_lorebook():
    """The full text of every entry the prose names: on a lorebook of long
    entries the request was many times the prose, on every archive run."""
    from sealedlore.engine.story_suggestions import (
        LORE_CHARS,
        build_suggestion_messages,
        shown_lore,
    )

    lore = [
        LoreEntry(title=f"Place {i}", keywords=[f"place{i}"], content=f"p{i} " * 2500)
        for i in range(60)
    ]
    prose = "They rode through " + ", ".join(f"place{i}" for i in range(60)) + "."
    shown = shown_lore(prose, lore)
    assert 0 < len(shown) < 60 and sum(len(e.content) for e in shown) <= LORE_CHARS
    request = _suggest_text(build_suggestion_messages(prose, lore=lore, cards=[]))
    assert len(request) < LORE_CHARS + 10_000
    assert "- Place 59" in request  # every title still
    # A lorebook of ordinary size is shown as it was: all the prose names.
    small = [LoreEntry(title="Keep", keywords=["keep"], content="An empty fortress.")]
    assert shown_lore("The keep at dusk.", small) == small


def test_a_reply_changes_only_what_the_reading_was_shown():
    """A change was kept for any entry by its title, though only titles were
    sent for those the prose doesn't name: its text rewritten unseen."""
    import json

    from sealedlore.engine.story_suggestions import parse_story_suggestions
    from sealedlore.models.character import Character

    keep = LoreEntry(title="Calder Keep", keywords=["keep"], content="An empty fortress.")
    caves = LoreEntry(title="The Hidden Caves", keywords=["caves"], content="Dry, and deep.")
    serrik = Character(name="Serrik Vaun", full_description="A veteran duellist.")
    maela = Character(name="Maela Orr", full_description="A locksmith.")
    text = json.dumps(
        {
            "lore_new": [],
            "lore_change": [
                {"title": "Calder Keep", "content": "A fortress, burned in the siege."},
                {"title": "The Hidden Caves", "content": "Flooded now, every one."},
            ],
            "card_change": [
                {"name": "Serrik Vaun", "add": "He lost two fingers at the keep."},
                {"name": "Maela Orr", "add": "She has left the city for good."},
            ],
        }
    )
    found = parse_story_suggestions(
        text,
        lore=[keep, caves],
        cards=[serrik, maela],
        prose="Serrik Vaun watched the keep burn.",
    )
    assert [(s.kind, s.title) for s in found] == [
        ("lore_change", "Calder Keep"),
        ("card_change", "Serrik Vaun"),
    ]


def test_a_change_is_never_a_guess_between_two_entries_of_one_title():
    import json

    from sealedlore.engine.story_suggestions import parse_story_suggestions

    first = LoreEntry(title="The Guild", keywords=["guild"], content="Of adventurers.")
    second = LoreEntry(title="The Guild", keywords=["guild"], content="Of thieves.")
    text = json.dumps({"lore_change": [{"title": "The Guild", "content": "Disbanded."}]})
    assert parse_story_suggestions(text, lore=[first, second], cards=[], prose="The guild.") == []


def test_a_reply_past_any_title_or_entry_is_dropped_and_a_reason_cut():
    import json

    from sealedlore.engine.story_suggestions import (
        CONTENT_CHARS,
        TITLE_CHARS,
        WHY_CHARS,
        parse_story_suggestions,
    )

    text = json.dumps(
        {
            "lore_new": [
                {"title": "T" * (TITLE_CHARS + 1), "content": "x"},
                {"title": "Huge", "content": "x" * (CONTENT_CHARS + 1)},
                {"title": "The Ford", "content": "Shallow.", "why": "w" * 5000},
            ]
        }
    )
    (kept,) = parse_story_suggestions(text, lore=[], cards=[])
    assert kept.title == "The Ford" and len(kept.why) <= WHY_CHARS


def test_no_more_are_read_for_while_forty_wait(tmp_path: Path, story, cast):
    from sealedlore.engine.session_story_suggestions import WAITING_MOST
    from sealedlore.models.story_suggestion import StorySuggestion

    session = tavern_session(tmp_path, story, cast)
    list(session.send(turn("A wolf watches.")))
    assert session.suggests_changes()
    session.story.story_suggestions.extend(
        StorySuggestion(kind="lore_new", title=f"Place {i}", content="x")
        for i in range(WAITING_MOST)
    )
    assert not session.suggests_changes()
    sent = len(session.provider.requests)
    with pytest.raises(ValueError, match="waiting already"):
        list(session.suggest_from_recent())
    assert len(session.provider.requests) == sent


def test_the_engine_reads_no_chat_for_suggestions(tmp_path: Path, story, cast):
    session = tavern_session(tmp_path, story, cast)
    list(session.send(turn("A wolf watches.")))
    session.story.mode = "chat"
    sent = len(session.provider.requests)
    with pytest.raises(ValueError, match="simple chat"):
        list(session.suggest_from_recent())
    assert len(session.provider.requests) == sent


def test_an_earlier_reviews_undo_never_takes_an_applied_suggestion(tmp_path: Path, story, cast):
    """Undo review put the lore back as it was before the review: the entry
    a suggestion had added since was gone, and the suggestion with it."""
    from sealedlore.models.story_suggestion import StorySuggestion

    session = tavern_session(tmp_path, story, cast)
    session.review_undo = object()  # a review applied earlier in the session
    suggestion = StorySuggestion(kind="lore_new", title="The Ford", content="Shallow.")
    session.story.story_suggestions.append(suggestion)
    session.apply_story_suggestion(suggestion.id)
    assert session.review_undo is None
    assert "The Ford" in [e.title for e in session.bundle.lore]


# --- smaller things in the engine ---------------------------------------------------


def test_a_reviews_infinite_number_is_one_field_refused_not_the_review():
    """{"sticky": "inf"}: int() of infinity is an OverflowError, which went
    past the handling of a change that can't apply and lost the whole review."""
    from sealedlore.engine.authoring import _entry_setting, _number

    for value in ("inf", 1e999, float("nan"), 10**400, "-inf"):
        with pytest.raises(ValueError):
            _entry_setting("sticky", value)
    with pytest.raises(ValueError):
        _number("Infinity", "the context budget")
    assert _entry_setting("sticky", "3") == 3


def test_an_entry_in_part_never_takes_more_than_its_room():
    """Room was kept for two gap marks and one written wherever text was
    left out: parts from all over a glossary came out a fifth over."""
    from sealedlore.engine.retrieval import fitted_entry

    count = lambda text: len(text) // 4  # noqa: E731
    parts = [
        (f"The wolf clan {i} hunts at night." if i % 2 else f"Dull filler number {i} goes here.")
        for i in range(400)
    ]
    glossary = LoreEntry(title="Glossary", content="\n\n".join(parts))
    for room in (150, 400, 1000):
        fitted = fitted_entry(glossary, room, "wolf clan hunts", count)
        assert fitted is not None
        assert count(f"## Glossary\n{fitted.content}") <= room
        assert "wolf clan" in fitted.content


def test_text_with_no_spaces_is_cut_not_left_out():
    from sealedlore.engine.retrieval import fitted_entry

    count = lambda text: len(text)  # noqa: E731 - a token a character
    entry_ = LoreEntry(title="竜", content="竜は北の山に住み火を吐く" * 40)
    fitted = fitted_entry(entry_, 120, "竜", count)
    assert fitted is not None and fitted.content.startswith("竜は北の山")
    assert count(f"## 竜\n{fitted.content}") <= 120


def test_a_title_of_brackets_is_cleaned_at_once():
    from sealedlore.storage.tavern_lore import clean_title

    began = time.perf_counter()
    clean_title("[" * 20_000)
    assert time.perf_counter() - began < 0.5
    assert clean_title("═══[World]═══") == "World"


# --- keys ---------------------------------------------------------------------------


def test_a_server_of_ones_own_with_no_address_gets_its_key():
    """AUTOMATIC1111 with the address left blank and user:password as the
    key, as Settings advises: the key was recorded for the chat endpoint's
    host and then withheld from the server ("saved for https://nano-gpt.com,
    and its address is now http://127.0.0.1:7860")."""
    from sealedlore.models.config import (
        Config,
        MediaEndpoint,
        ProviderConfig,
        bind_unbound_keys,
        rebind_keys,
    )

    def made(api: str, address: str = "") -> Config:
        return Config(
            providers=[
                ProviderConfig(
                    name="NanoGPT",
                    base_url="https://nano-gpt.com/api/v1",
                    api_key="k" * 20,
                    default_model="m",
                )
            ],
            active_provider_name="NanoGPT",
            image_endpoint=MediaEndpoint(api=api, base_url=address, api_key="user:password"),
        )

    config = made("a1111")
    bind_unbound_keys(config)
    assert config.image_endpoint.key_saved_for == "http://127.0.0.1:7860"
    media = config.images()
    assert media.base_url == "http://127.0.0.1:7860"
    assert media.api_key == "user:password" and not media.key_withheld
    # A key that was NanoGPT's doesn't follow the service to this computer
    # unasked: the change is a move, for Settings to ask about.
    before = made("nanogpt")
    bind_unbound_keys(before)
    after = before.model_copy(deep=True)
    after.image_endpoint.api = "comfyui"
    moves = rebind_keys(before, after, {})
    assert [move.slot for move in moves] == ["images"]
    assert after.images().key_withheld


# --- servers and files of one's own ---------------------------------------------------


def test_the_embeddings_model_is_downloaded_once_at_a_time(tmp_path: Path):
    """Settings closed and opened again mid-download began a second onto the
    same part file, which emptied what the first was writing."""
    from sealedlore.providers import local_embeddings
    from sealedlore.providers.base import ProviderError

    folder = str(local_embeddings.model_dir(tmp_path))
    local_embeddings._DOWNLOADING.add(folder)
    try:
        assert local_embeddings.downloading(tmp_path)
        with pytest.raises(ProviderError, match="downloaded already"):
            local_embeddings.download(tmp_path, client=object())
    finally:
        local_embeddings._DOWNLOADING.discard(folder)
    assert not local_embeddings.downloading(tmp_path)


def test_a_picture_given_up_on_for_time_is_stopped_on_the_server(monkeypatch):
    """ComfyUI's wait ran out and the workflow stayed queued, to be drawn
    for nobody: the server is told, as it is on Stop."""
    from sealedlore.providers.local_images import LocalDrawing, LocalSettings
    from sealedlore.providers.media_http import MediaHttp, MediaTimedOut

    http = MediaHttp("http://127.0.0.1:8188", "", timeout_seconds=1)
    drawing = LocalDrawing("comfyui", http, "http://127.0.0.1:8188", "", LocalSettings())
    stopped: list[bool] = []
    monkeypatch.setattr(drawing, "check_model", lambda model, with_picture: None)
    monkeypatch.setattr(drawing, "stop", lambda: stopped.append(True))

    def late(payload, model, references):
        raise MediaTimedOut("ComfyUI: no picture after the time allowed")

    monkeypatch.setattr(drawing, "_comfy", late)
    with pytest.raises(MediaTimedOut):
        drawing.draw({"model": "m", "prompt": "p"})
    assert stopped == [True]


def test_a_storys_copy_of_a_lorebooks_entry_shares_nothing_with_it():
    """An entry's `extensions` was the same object in the kept lorebook and
    in the story's entry: a setting changed in Lorebooks changed the open
    story's too."""
    book = {
        "entries": {
            "0": {
                "uid": 0,
                "key": ["wolf"],
                "content": "They hunt in threes.",
                "extensions": {"case_sensitive": False, "nested": {"a": [1]}},
            }
        }
    }
    (made,) = lore_from_book(book).entries
    book["entries"]["0"]["extensions"]["case_sensitive"] = True
    book["entries"]["0"]["extensions"]["nested"]["a"].append(2)
    assert made.tavern.fields["extensions"] == {"case_sensitive": False, "nested": {"a": [1]}}
