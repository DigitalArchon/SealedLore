"""What the review of 3.5.0b1 found in lore and placeholders (Nov 22 2026),
one test a fault: each fails on 0f289da but those that hold ordinary
patterns to what they did. The scripted mock provider and made-up lorebooks:
no network."""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from sealedlore.engine import tavern_firing
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tavern_firing import (
    FireEntry,
    TavernSettings,
    Timed,
    fire,
    fire_entry,
)
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.models.plot import Direction
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.scenario import bundle_from_scenario
from sealedlore.storage.tavern_lore import lore_from_book
from tests.test_session import make_config

SERRIK, MAELA, IDRIS = "char-serrik", "char-maela", "char-idris"

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


def session_with(tmp_path: Path, story, cast, lore=(), supporting=()) -> StorySession:
    config = make_config()
    config.scene_reads = "manual"
    bundle = StoryBundle(story=story, cast=list(cast), lore=list(lore))
    bundle.supporting.characters.extend(supporting)
    return StorySession(
        bundle,
        config,
        MockChatProvider(["A passage."] * 12),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def reopened(session: StorySession, tmp_path: Path) -> StorySession:
    """The story as the window opens it again: a new session on what it holds."""
    return StorySession(
        session.bundle,
        session.config,
        MockChatProvider(["A passage."] * 12),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str, held: str | None = SERRIK) -> TurnRequest:
    return TurnRequest(speaker_id=held, user_text=text, controlled_character_id=held)


def sent(session: StorySession, index: int = -1) -> str:
    request = session.provider.requests[index]
    return "\n".join(part.text for message in request.messages for part in message.parts)


def system_of(session: StorySession, index: int = -1) -> str:
    request = session.provider.requests[index]
    return "\n".join(
        part.text
        for message in request.messages
        if message.role == "system"
        for part in message.parts
    )


def prose(chars: int, seed: int = 1) -> str:
    """Ordinary words, as a story's messages and a lorebook's entries are."""
    words = (
        "the farm was quiet when John came back from Hellsville with the cart "
        "and two sacks of grain"
    ).split()
    rng = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < chars:
        out.append(rng.choice(words))
        size += len(out[-1]) + 1
    return " ".join(out)[:chars]


# --- L1: patterns the screen let through ------------------------------------------

# Each read all it was given: 15 s on 80,000 characters for the first three
# (a look-ahead reading to the end of the text from every place in it),
# seconds for counts inside counts, and for ways-times-length 12 s on 4,900.
SLOW_ON_PROSE = [
    r"/(?![\s\S]*)/",
    r"/(?=[\s\S]*)§/",
    r"/(?=[\w\s]*)§/",
    r"/(?:(?:(?:[^§]{16}){16}){16}){16}§/",
    r"/(?:(?:(?:a{16}){16}){16}){16}b/",
    "/" + "(?:.{16}|.{15})" * 12 + ".{16}" * 150 + "§/s",
    "/" + "(?:.{16}|.{15})" * 6 + ".{16}" * 150 + "§/s",
    "/(?=" + "(?:.{16}|.{15})" * 12 + ".{16}" * 150 + "§)x/s",
]


@pytest.mark.parametrize("key", SLOW_ON_PROSE)
def test_a_pattern_that_reads_far_from_every_place_reads_only_so_much(key: str):
    for text in (prose(80_000), "a" * 80_000):
        began = time.perf_counter()
        result = run([entry("slow", [key])], [text])
        assert time.perf_counter() - began < 3.0, key
        assert not result.fired


def test_optional_parts_inside_a_look_ahead_are_counted_too():
    """Forty optional letters are 2^40 ways wherever they stand: inside a
    look-ahead they weren't counted, and forty letters hung the window."""
    key = "/(?=" + "a?" * 40 + "a" * 40 + ")x/"
    began = time.perf_counter()
    result = run([entry("hang", [key])], ["a" * 40 + " and more"])
    assert time.perf_counter() - began < 1.0
    assert "is never run" in result.not_fired["hang"]


def test_a_new_story_with_a_long_constant_entry_opens_at_once():
    """The reviewer's case: one message, a constant entry of 80,000
    characters read by recursion, and a pattern that took 16.5 s on it."""
    book = {
        "entries": {
            "0": {
                "uid": 0,
                "comment": "The valley",
                "content": prose(80_000, seed=3),
                "key": [],
                "constant": True,
                "order": 100,
            },
            "1": {
                "uid": 1,
                "comment": "The Hollow",
                "content": "A sinkhole.",
                "key": [r"/(?![\s\S]*)/"],
                "constant": False,
                "order": 90,
            },
        }
    }
    entries = [
        fire_entry(
            id=e.id,
            title=e.title,
            content=e.content,
            keywords=e.keywords,
            always_on=e.always_on,
            enabled=e.enabled,
            raw=e.tavern.fields,
            numbered=e.tavern.numbered,
            decorators=e.tavern.decorators,
        )
        for e in lore_from_book(book).entries
    ]
    began = time.perf_counter()
    result = fire(
        entries,
        ["You: I open the door."],
        settings=TavernSettings(),
        context=60000,
        count_tokens=lambda text: len(text) // 4,
        timed=Timed(),
        length=1,
        free={entries[0].id},
    )
    assert time.perf_counter() - began < 3.0
    assert "read only the newest 8,000 characters" in result.not_fired[entries[1].id]


@pytest.mark.parametrize(
    "key",
    [
        r"/wol(f|ves)/i",
        r"/\bHollow\b/",
        r"/drag\w*/i",
        r"/(?<what>wolves|wyrms) of \w+/i",
        r"/(?:king|queen|prince|princess|duke|duchess) of (?:the )?(?:north|south|east|west)/i",
        r"/\b(?:sword|blade|dagger|knife|axe|spear|bow|arrow|shield|helm|helmet)s?\b/i",
        r"/Hollow(?= road)/",
        r"/(?<!the )Hollow\b/",
        r"/\d{4}-\d{2}-\d{2}/",
    ],
)
def test_ordinary_patterns_read_all_a_story_scans(key: str):
    """Whatever they are looking for 100,000 characters back is found, and
    nothing is said of them."""
    found = "Wolves of Hollow road, 2026-11-22: the duke of the north drew a dragon dagger."
    result = run([entry("d", [key])], [prose(100_000) + " " + found])
    assert result.ids == ["d"], key
    assert not result.not_fired
    assert not run([entry("d", [key])], [prose(100_000)]).fired


@pytest.mark.parametrize(
    "key", [r"/wolves.*road/i", r"/duke [\s\S]*? dagger/", r"/Hollow(?=.* dagger)/"]
)
def test_patterns_with_a_repeat_in_the_middle_fire_as_before(key: str):
    """/dragon.*fire/ and its like: the newest 8,000 characters, as they did."""
    found = "Wolves of Hollow road: the duke of the north drew a dagger."
    pattern = tavern_firing._pattern(key)
    assert pattern is not None and pattern.cap == tavern_firing.SCAN_CAPS[1]
    assert run([entry("d", [key])], [found + " " + prose(20_000)]).ids == ["d"]
    far = run([entry("d", [key])], [prose(20_000) + " " + found])
    assert "read only the newest 8,000 characters" in far.not_fired["d"]


def test_every_pattern_reads_only_so_much():
    """No pattern reads without limit, whatever the screen makes of it: the
    simplest stop at SCAN_MOST, far past what a story scans."""
    for key in (r"/wol(f|ves)/i", r"/\bHollow\b/", r"/drag\w*/i"):
        pattern = tavern_firing._pattern(key)
        assert pattern is not None and pattern.cap == tavern_firing.SCAN_MOST
    text = "x" * tavern_firing.SCAN_MOST + " the Hollow"
    began = time.perf_counter()
    result = run([entry("far", [r"/\bHollow\b/"])], [text])
    assert time.perf_counter() - began < 1.0
    assert f"read only the newest {tavern_firing.SCAN_MOST:,} characters" in result.not_fired["far"]


# --- L2: {{user}} in a story already begun ---------------------------------------


def _debt() -> list[LoreEntry]:
    return [
        LoreEntry(
            title="The debt",
            always_on=True,
            content="{{char}} owes {{user}} forty rounds.",
            of_character=MAELA,
        )
    ]


def test_user_is_settled_when_a_story_already_begun_is_opened(tmp_path, story, cast):
    """Begun before {{user}} was settled at the start: it followed whoever
    was played ("Maela Orr owes Maela Orr"), and the cached system block
    changed with every switch."""
    story.held_character_id = SERRIK
    session = session_with(tmp_path, story, cast, _debt())
    list(session.send(turn("I open the door.")))
    before = system_of(session)
    assert "Maela Orr owes Serrik Vaun forty rounds." in before
    story.setup.placeholders.user_id = None  # as an older version left it

    session = reopened(session, tmp_path)
    assert session.story.setup.placeholders.user_id == SERRIK
    session.hold(MAELA)
    list(session.send(turn("I count the rounds.", held=MAELA)))
    assert "Maela Orr owes Serrik Vaun forty rounds." in sent(session)
    # Whom {{user}} already meant: the cached block is as it was.
    assert system_of(session) == before


def test_user_is_whom_they_played_until_the_switch_that_settles_it(tmp_path, story, cast):
    """Unsettled with someone played: the switch itself must not hand
    {{user}} to the one switched to."""
    story.held_character_id = SERRIK
    session = session_with(tmp_path, story, cast, _debt())
    list(session.send(turn("I open the door.")))
    session.story.setup.placeholders.user_id = None
    session.hold(MAELA)
    assert session.story.setup.placeholders.user_id == SERRIK


def test_user_is_settled_by_the_first_hold_after_a_start_with_nobody(tmp_path, story, cast):
    story.setup.opening_text = "Rain on the keep's one whole roof."
    story.setup.opening_mode = "as_written"
    session = session_with(tmp_path, story, cast, _debt())
    list(session.begin(None))
    assert session.nodes and session.story.setup.placeholders.user_id is None
    session.hold(SERRIK)
    assert session.story.setup.placeholders.user_id == SERRIK
    session.hold(MAELA)
    list(session.send(turn("I count the rounds.", held=MAELA)))
    assert session.story.setup.placeholders.user_id == SERRIK
    assert "Maela Orr owes Serrik Vaun forty rounds." in sent(session)


def test_user_is_settled_by_the_first_turn_of_a_story_never_begun_from_an_opening(
    tmp_path, story, cast
):
    """No opening: the author writes first, and `begin` never runs."""
    story.held_character_id = SERRIK
    session = session_with(tmp_path, story, cast, _debt())
    assert session.story.setup.placeholders.user_id is None  # not begun: still Setup's to say
    list(session.send(turn("I open the door.")))
    assert session.story.setup.placeholders.user_id == SERRIK


def test_settling_user_changes_nothing_else(tmp_path, story, cast):
    """A story with no placeholder gains the id and nothing more; one the
    author named a {{user}} for keeps theirs; a chat and a held id no card
    answers to settle nothing."""
    story.held_character_id = SERRIK
    session = session_with(tmp_path, story, cast)
    list(session.send(turn("I open the door.")))
    first = sent(session)
    story.setup.placeholders.user_id = None
    kept = story.model_dump(mode="json")
    session = reopened(session, tmp_path)
    now = session.story.model_dump(mode="json")
    assert now["setup"]["placeholders"].pop("user_id") == SERRIK
    kept["setup"]["placeholders"].pop("user_id")
    assert now == kept
    list(session.regenerate())
    assert sent(session) == first

    story.setup.placeholders.user_id = MAELA
    assert reopened(session, tmp_path).story.setup.placeholders.user_id == MAELA
    story.setup.placeholders.user_id = None
    story.held_character_id = "char-gone"
    assert reopened(session, tmp_path).story.setup.placeholders.user_id is None


# --- L3: a plot's begin direction ------------------------------------------------

DOOMSVILLE = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"


def test_a_plots_begin_direction_has_the_names_in(tmp_path):
    """The cards of whoever an event brings, and the lore it reveals, went
    to the storyteller with {{char}} and {{user}} as written: the one place
    a hidden card is first sent."""
    parsed = parse_plot_markdown(DOOMSVILLE.read_text(encoding="utf-8"))
    bundle = bundle_from_scenario(parsed.scenario)
    story = bundle.story
    event = next(e for e in story.plot.events if e.brings)
    people = {c.id: c for c in (*bundle.cast, *bundle.supporting.characters)}
    hidden = people[event.brings[0]]
    assert hidden.plot_hidden
    hidden.full_description = "{{char}} has traded with {{user}} for years and trusts nobody else."
    place = next(entry for entry in bundle.lore if entry.plot_hidden)
    place.content = "{{user}} has never been let in; {{char}} holds the gate."
    place.of_character = hidden.id
    player = next(c for c in bundle.cast if not c.plot_hidden)
    story.held_character_id = player.id
    story.setup.placeholders.user_id = player.id
    config = make_config()
    config.scene_reads = "manual"
    session = StorySession(
        bundle,
        config,
        MockChatProvider(["A passage."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    request = TurnRequest(
        speaker_id=player.id, user_text="I wait by the road.", controlled_character_id=player.id
    )
    directed = session._directed(
        request,
        [Direction(event_id=event.id, kind="begin", text="They arrive.")],
        [],
    )
    direction = directed.direction or ""
    cards = session._cards([hidden.id, place.id])
    for text in (direction, cards):
        assert f"{hidden.name} has traded with {player.name} for years" in text
        assert "{{" not in text
    assert f"{player.name} has never been let in; {hidden.name} holds the gate." in cards
    # The story's own cards keep them as written.
    assert hidden.full_description.startswith("{{char}} has traded with {{user}}")


# --- L4: a character leaves, their lorebook stays ----------------------------------


def test_lore_that_came_with_a_card_still_means_them_once_they_are_gone(tmp_path, story, cast):
    """The lore stays when its character is removed (the author's ruling),
    and its {{char}} then named the story's own character: "Serrik Vaun ran
    the clinic", of Maela's clinic."""
    story.held_character_id = SERRIK
    story.setup.placeholders.user_id = IDRIS
    story.setup.placeholders.char_id = SERRIK
    lore = [
        LoreEntry(
            title="{{char}}'s clinic",
            always_on=True,
            content="{{char}} ran the clinic before the fall, and <USER> never paid her.",
            keywords=["{{char}}'s clinic"],
            condensed="{{char}} ran the clinic.",
            of_character=MAELA,
            came_from="Maela Orr's lorebook",
        ),
        LoreEntry(title="The keep", always_on=True, content="{{char}} holds the keep."),
    ]
    session = session_with(tmp_path, story, cast, lore)
    list(session.send(turn("I open the door.")))
    assert "Maela Orr ran the clinic before the fall, and Captain Idris never paid" in sent(session)

    session.remove_character(MAELA)
    theirs, storys = session.bundle.lore
    # Their name is written in where {{char}} stood; {{user}} is as written.
    assert theirs.title == "Maela Orr's clinic"
    assert theirs.content == "Maela Orr ran the clinic before the fall, and <USER> never paid her."
    assert theirs.keywords == ["Maela Orr's clinic"]
    assert theirs.condensed == "Maela Orr ran the clinic."
    # An entry of the story's own is the story's still.
    assert storys.content == "{{char}} holds the keep."
    list(session.send(turn("I wait.")))
    text = sent(session)
    assert "Maela Orr ran the clinic before the fall, and Captain Idris never paid" in text
    assert "Serrik Vaun ran the clinic" not in text
    assert "Serrik Vaun holds the keep." in text


def test_a_condensed_version_stays_current_when_its_owner_leaves(tmp_path, story, cast):
    content = "{{char}} ran the clinic before the fall."
    entry = LoreEntry(
        title="The clinic",
        always_on=True,
        content=content,
        condensed="{{char}}: the clinic.",
        condensed_from=lore_fingerprint(content),
        of_character=MAELA,
    )
    session = session_with(tmp_path, story, cast, [entry])
    session.remove_character(MAELA)
    kept = session.bundle.lore[0]
    assert kept is entry  # the page's own object, changed where it is
    assert kept.condensed_now() == "Maela Orr: the clinic."
