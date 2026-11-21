"""What the review of 3.1.0b15 found (Nov 21 2026), one test a fault: each
fails on da927af. The scripted mock provider and made-up lorebooks: no
network."""

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
    session = tavern_session(tmp_path, story, cast)
    valley = next(e for e in session.bundle.lore if e.title == "The Valley")
    valley.content = "The Order of Ash rules. " + "snow " * 4000
    valley.tavern.fields["content"] = valley.content
    session.story.defaults.context_token_budget = 8_000  # a quarter: 2,000
    report = session.retrieve(turn("I look around."), [])
    # Over the budget by itself, as in SillyTavern: nothing more is read.
    assert not report.injected


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
