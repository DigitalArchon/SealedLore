"""The chronicle read: after each passage, how much time passed and what facts changed.

One call on a small model (`Config.plot_model`), made by
`StorySession.update_chronicle_after_turn` on stories with a plot. Pure here:
building the messages, parsing the reply, and holding the reply to account.

The same rule as the scene read: a read that can't point at what it claims
doesn't get to. A fact changes only when the sentence showing it can be
found in the passage; a jump of more than half a day needs a sentence saying
so, in the passage or the author's turn, or it is cut back.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sealedlore.engine.chronicle import advance, format_clock, happen, set_fact, set_place
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.validators import locate_quote
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.plot import MINUTES_PER_DAY, Chronicle, Plot

# Room for a reasoning model to think first: a cap only limits, it costs nothing
# unused. Live (GLM 5.3), 899 of 900 tokens went on reasoning and the reply was
# empty; glm-5.3-flash came back empty 5 times in 6 at 1500. Its reasoning
# can't be turned off on nano-gpt (HTTP 400).
READ_MAX_TOKENS = 4000
# The most a passage may move the clock with nothing in the text to say so.
# A night's sleep fits; "three weeks later" has to be written.
MAX_UNQUOTED_MINUTES = 12 * 60
# Minutes per unit, for the read's "amount" and "unit".
_UNITS = {
    "minute": 1,
    "hour": 60,
    "day": MINUTES_PER_DAY,
    "week": 7 * MINUTES_PER_DAY,
    "month": 30 * MINUTES_PER_DAY,
    "year": 365 * MINUTES_PER_DAY,
}
_UNIT_ALIASES = {
    "min": "minute",
    "m": "minute",
    "h": "hour",
    "hr": "hour",
    "d": "day",
    "wk": "week",
}

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINAL_ENDS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}


def _cardinal(n: int) -> str:
    """1–999 in words, hyphenated: "fifty-one", "one hundred and five"."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    rest = n % 100
    return _ONES[n // 100] + " hundred" + (" and " + _cardinal(rest) if rest else "")


def _spellings(n: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Cardinal and ordinal spellings of 1–999, hyphenated and not, longest first."""
    if not 0 < n < 1000:
        return (), ()
    words = _cardinal(n)
    head, sep, last = words.rpartition("-" if "-" in words else " ")
    if last.endswith("y"):
        ordinal_last = last[:-1] + "ieth"
    else:
        ordinal_last = _ORDINAL_ENDS.get(last, last + "th")
    ordinal = head + sep + ordinal_last

    def forms(text: str) -> tuple[str, ...]:
        return tuple(sorted({text, text.replace("-", " ")}, key=len, reverse=True))

    return forms(words), forms(ordinal)


_DAY_PARTS = "day|morning|afternoon|evening|night|dawn|dusk"
# After a number word, the word that would make it a bigger number:
# "day thirty one" is not day thirty.
_NO_UNIT_AFTER = r"(?![\w-])(?!\s+(?:" + "|".join(_ONES[1:10]) + r")\b)"


def _names_day(text: str, day: int) -> bool:
    """Whether the text names this as the story's day: "day 31", "Day thirty-one",
    "the thirty-first morning".

    A count of days is not a day number. Live, the storyteller's "fifty-one
    days into a new life" set the story to day 51, and Mistral's "day 70"
    quoting "Five weeks pass." was taken as named before that.
    """
    cardinals, ordinals = _spellings(day)
    cardinal = "|".join([str(day), *map(re.escape, cardinals)])
    ordinal = "|".join([rf"{day}(?:st|nd|rd|th)", *map(re.escape, ordinals)])
    patterns = (
        rf"\bday\s+(?:number\s+)?(?:{cardinal}){_NO_UNIT_AFTER}",
        rf"(?<![\w-])(?:{ordinal})\s+(?:{_DAY_PARTS})\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


_WORD_VALUES = {
    **{word: n for n, word in enumerate(_ONES) if n},
    **{word: n * 10 for n, word in enumerate(_TENS) if n > 1},
}
# How many a phrase means where it isn't a number.
_SPAN_QUANTITY = {
    "a couple of": 2,
    "a couple": 2,
    "a single": 1,
    "a dozen": 12,
    "a few": 3,
    "few": 3,
    "several": 4,
    "another": 1,
    "an": 1,
    "a": 1,
}


def _alternatives(words: Sequence[str]) -> str:
    return "|".join(sorted(map(re.escape, words), key=len, reverse=True))


_UNDER_HUNDRED = (
    rf"(?:(?:{_alternatives(_TENS[2:])})(?:[-\s](?:{_alternatives(_ONES[1:10])}))?"
    rf"|{_alternatives(_ONES[1:20])})"
)
_COUNT = (
    rf"(?:(?:a|{_alternatives(_ONES[1:10])})\s+hundred(?:\s+and\s+{_UNDER_HUNDRED})?"
    rf"|{_UNDER_HUNDRED})"
)
_HALF = r"(\s+and\s+a\s+half)?"
# Never inside a bigger number: "fifty-one days" is not "one day" (live, a
# Director turn's "fifty-one days" cut a thirty-day skip to one day).
_SPAN = re.compile(
    rf"(?<![\w-])(\d+|{_COUNT}|{_alternatives(_SPAN_QUANTITY)}){_HALF}"
    r"(?:\s+(?:more|further|long|full|whole|short))?"
    rf"\s+(minute|hour|day|night|week|month|year)s?{_HALF}\b",
    re.IGNORECASE,
)


def _count(phrase: str) -> int:
    """The number a matched count means: "21", "twenty-one", "a hundred", "a few"."""
    phrase = " ".join(phrase.lower().split())
    if phrase.isdigit():
        return int(phrase)
    if phrase in _SPAN_QUANTITY:
        return _SPAN_QUANTITY[phrase]
    total = 0
    for word in phrase.replace("-", " ").split():
        if word == "hundred":
            total = (total or 1) * 100
        elif word in _WORD_VALUES:
            total += _WORD_VALUES[word]
        elif word == "a":
            total += 1
    return total


def _stated_span(text: str) -> tuple[int, str] | None:
    """The longest stretch of time a Director or Narration turn states, and its sentence.

    The longest, because it is a ceiling on the read's own count: a turn that
    names a longer time it isn't skipping ("fifty-one days apart") only makes
    the ceiling generous, never wrong.
    """
    best: tuple[int, str] | None = None
    for match in _SPAN.finditer(text):
        minutes = _span_minutes(match)
        if best is None or minutes > best[0]:
            best = (minutes, _sentence_around(text, match))
    return best


def _span_minutes(match: re.Match[str]) -> int:
    unit = match.group(3).lower()
    per = _UNITS["day" if unit == "night" else unit]
    # "two and a half weeks", "a week and a half"
    half = per // 2 if match.group(2) or match.group(4) else 0
    return _count(match.group(1)) * per + half


def _sentence_around(text: str, match: re.Match[str]) -> str:
    start = max(text.rfind(end, 0, match.start()) for end in ".!?\n") + 1
    stops = [i for i in (text.find(end, match.end()) for end in ".!?\n") if i >= 0]
    return text[start : min(stops) + 1 if stops else len(text)].strip()


# A sentence that says time goes by, not one that merely counts it ("fifty-one
# days apart", "the burn takes three hours").
_PASSING = re.compile(
    r"\b(?:pass(?:es|ed|ing)?|later|go(?:es)? by|went by|gone by|blur(?:s|red)?|"
    r"slip(?:s|ped)? (?:by|past)|elapse[sd]?|forward|skip|jump|the next|the following)\b",
    re.IGNORECASE,
)
# A day said as now: "By day 81", "On the morning of day 35", "It is now day
# 65", "Day 5 dawns". Not a plan: "set for day 40", "ready by day 60".
_NAMED_DAY = re.compile(
    rf"(?:^|\b(?:by|on|of|is|now)\s+|,\s*)day\s+(?:number\s+)?(\d+|{_COUNT})(?![\w-])",
    re.IGNORECASE,
)
_FUTURE = re.compile(
    r"\b(?:will|would|must|should|shall|plans?|planned|expect(?:s|ed)?|scheduled|due|"
    r"until|before|deadline|hopes?|intends?|aims?|going to)\b",
    re.IGNORECASE,
)


def stated_skip(text: str, now: int) -> tuple[int, str] | None:
    """Where a Director or Narration turn moves the clock to, before anything
    is written: the latest day it names as now, ahead of now ("By day 81"),
    else the first stretch it says goes by ("Three weeks pass."), and the
    sentence.

    Moved before the director runs, a skip's events are known to the passage
    that tells the skip, rather than fitted in a turn later against prose
    that never mentioned them (live, five events in one Between Stars run,
    the first boarding with Myla aboard among them).
    """
    today = now // MINUTES_PER_DAY + 1
    ahead: list[tuple[int, str]] = []
    for found in re.finditer(r"[^.!?\n]+", text):
        sentence = found.group(0).strip()
        if _FUTURE.search(sentence):
            continue
        for match in _NAMED_DAY.finditer(sentence):
            if (day := _count(match.group(1))) > today:
                ahead.append((day, sentence))
    if ahead:
        # The day it names, at the hour the story was at: the read after the
        # passage says the hour, and a midnight clock carried on into
        # afternoon scenes.
        day, sentence = max(ahead, key=lambda item: item[0])
        return (day - 1) * MINUTES_PER_DAY + now % MINUTES_PER_DAY, sentence
    for match in _SPAN.finditer(text):
        sentence = _sentence_around(text, match)
        if _PASSING.search(sentence):
            return now + _span_minutes(match), sentence
    return None


def render_facts(plot: Plot, chronicle: Chronicle, *, watched_only: bool = False) -> str:
    lines: list[str] = []
    for fact in plot.facts:
        if watched_only and not fact.watched:
            continue
        current = chronicle.facts.get(fact.name, fact.initial)
        values = f" (one of: {', '.join(fact.values)})" if fact.values else ""
        meaning = f" — {fact.meaning}" if fact.meaning else ""
        lines.append(f"- {fact.name} = {current}{values}{meaning}")
    return "\n".join(lines) or "(none)"


def expected_events(plot: Plot, chronicle: Chronicle) -> str:
    """Events the storyteller was told to begin and hasn't yet been seen to."""
    lines: list[str] = []
    for event in plot.events:
        status = chronicle.events.get(event.id)
        if status is None or status.state != "directed":
            continue
        variant = next((v for v in event.variants if (v.title or None) == status.variant), None)
        what = " ".join(
            " ".join(part.split()) for part in (event.tell, variant.tell if variant else "") if part
        )
        lines.append(f"- {event.id}: {event.title}. {what}".rstrip())
    return "\n".join(lines)


def build_read_messages(
    *,
    plot: Plot,
    chronicle: Chronicle,
    author_turn: str,
    passage: str,
    places: Sequence[str] = (),
    held: str | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """Only what the read has to judge: the clock, the place among `places`
    (the plot's places the story has brought in), the watched facts and the
    events expected. Shown seven facts about people it knew nothing of, the
    read flipped them on any stranger (live, Doomsville)."""
    body = [
        *([f"THE AUTHOR'S CHARACTER: {held}"] if held else []),
        f"THE CLOCK, before the author's turn: {format_clock(chronicle.minutes)}",
        "THE PLACES: " + (", ".join(places) or "(none)") + ", or elsewhere. "
        f"Before the author's turn: {chronicle.place or 'elsewhere'}.",
    ]
    facts = render_facts(plot, chronicle, watched_only=True)
    if facts != "(none)":
        body.append("THE FACTS, before the author's turn:\n" + facts)
    expected = expected_events(plot, chronicle)
    if expected:
        body.append("EVENTS EXPECTED IN THIS PASSAGE:\n" + expected)
    if author_turn.strip():
        body.append("THE AUTHOR'S TURN:\n\n" + author_turn.strip())
    body.append("THE LATEST PASSAGE:\n\n" + passage.strip())
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["plot.chronicle_read"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


@dataclass
class ProposedFact:
    fact: str
    value: str
    quote: str


@dataclass
class ChronicleDelta:
    elapsed: int = 0
    elapsed_quote: str = ""
    # Minute of the day the passage ends at, if the read gave one.
    ends_at: int | None = None
    # A day number the text names outright, and the place it ends at
    # ("" for elsewhere, None for no answer).
    day: int | None = None
    place: str | None = None
    place_quote: str = ""
    facts: list[ProposedFact] = field(default_factory=list)
    # (event id, quote) for events the passage shows beginning.
    happened: list[tuple[str, str]] = field(default_factory=list)
    # What the read claimed and couldn't back up, for the log and the notice.
    refused: list[str] = field(default_factory=list)

    def applied_to(
        self,
        before: Chronicle,
        plot: Plot,
        *,
        passage: str,
        author_turn: str,
        node_id: str | None = None,
        places: Sequence[str] = (),
        author_states_facts: bool = False,
        scene_location: str = "",
        at_least: int | None = None,
    ) -> Chronicle:
        """The chronicle after this passage, keeping only what the text supports.

        `author_states_facts`: the author's turn is a Director or Narration
        turn, the author's own statement of what happens, so its sentences
        count as evidence for a move or a watched fact. Live: "Five weeks
        pass. Jane settles into Hellsville" left her place unrecorded (the
        passage never showed an arrival), and the fall then resolved as if
        she had never been there while she stood in the town.

        `at_least`: the start of the day the author's own skip moved the clock
        to before the passage, which the director has already acted on; the
        read may land at any hour of it or later, never before it.

        `scene_location` is the scene card's location after this passage (the
        scene read is applied first). While it still names the place they
        were at, a move to "elsewhere" is refused: live, Jane's place dropped
        to none three times in "the church in Hellsville", with a real quote
        of her stepping outside, which would have locked the town's events out.
        """
        evidence = (passage, author_turn) if author_states_facts else (passage,)

        def shown(quote: str) -> bool:
            return any(locate_quote(text, quote) is not None for text in evidence)

        after = before.model_copy(
            update={"source": "story", "elapsed": 0, "elapsed_quote": "", "changes": []},
            deep=True,
        )
        elapsed, quote = self.elapsed, self.elapsed_quote
        quoted = bool(quote) and any(
            locate_quote(text, quote) is not None for text in (passage, author_turn)
        )
        if not quoted:
            quote = ""
        # The author's own statement of a skip is the evidence, quoted or not.
        # Live: on "Five weeks pass. Jane settles into Hellsville…" four of
        # five models left the quote out some of the time, and the skip was
        # cut to 12 hours. Held to the span the turn states.
        stated = _stated_span(author_turn) if author_states_facts else None
        if stated is not None and elapsed > MAX_UNQUOTED_MINUTES:
            span, sentence = stated
            if not quoted:
                quoted, quote = True, sentence
            ceiling = max(span * 5 // 4, span + MINUTES_PER_DAY)
            if elapsed > ceiling:
                self.refused.append(
                    f"a jump of {elapsed // 60}h where the author's turn says {span // 60}h"
                )
                elapsed = span
        ends_at = self.ends_at
        if elapsed > MAX_UNQUOTED_MINUTES and not quoted:
            self.refused.append(
                f"a jump of {elapsed // 60}h with nothing in the text saying so; "
                f"counted as {MAX_UNQUOTED_MINUTES // 60}h"
            )
            elapsed = MAX_UNQUOTED_MINUTES
        minutes = advance(before.minutes, elapsed, ends_at)
        # An hour of the day that would add most of a day the read didn't
        # count is a misreading ("it's 21:50" when the clock says 22:00).
        # Over a skip of days, landing on the stated hour may take up to one
        # more: "a week passes" from an evening, then "morning".
        slack = MAX_UNQUOTED_MINUTES if elapsed < MINUTES_PER_DAY else MINUTES_PER_DAY
        if ends_at is not None and minutes - before.minutes - elapsed > slack:
            self.refused.append("a time of day that would skip most of a day")
            minutes = before.minutes + elapsed
        # A day the text names outright wins, if it is quoted and not past.
        # The author's own Director or Narration turn naming it counts too:
        # live, "By day 81" in the turn was refused because the read quoted
        # the passage's "Thirty days settled…" for the time.
        if self.day is not None:
            within = ends_at if ends_at is not None else minutes % MINUTES_PER_DAY
            named = (self.day - 1) * MINUTES_PER_DAY + within
            named_in = [quote] if quoted else []
            if author_states_facts:
                named_in.append(author_turn)
            if not any(_names_day(text, self.day) for text in named_in):
                self.refused.append(f"day {self.day} with nothing in the text naming it")
            elif named < before.minutes:
                self.refused.append(f"day {self.day} is in the past")
            else:
                minutes = named
        # The author's stated skip, which the director has already acted on.
        if at_least is not None and minutes < at_least:
            self.refused.append(
                f"a passage ending at {format_clock(minutes)} where the author's turn "
                f"moved the clock to {format_clock(at_least)} at least"
            )
            minutes = at_least
        after.minutes = minutes
        after.elapsed = minutes - before.minutes
        after.elapsed_quote = quote

        if self.place is not None:
            place = _matching_place(self.place, places) if self.place else None
            if self.place and place is None:
                self.refused.append(f"“{self.place}” isn't one of the places")
            elif place is not None and place not in places:
                self.refused.append(f"“{place}” isn't one of the places")
            elif not before.place_known:
                # Just taken up, and never held before: where they are is
                # news, not a move, so there is no sentence to ask for.
                set_place(after, place)
            elif (
                place is None
                and before.place is not None
                and re.search(rf"\b{re.escape(before.place)}\b", scene_location, re.IGNORECASE)
            ):
                self.refused.append(f"left {before.place}: the scene is still there")
            elif place != before.place:
                # A Director turn that names the place is the author moving
                # them there, quote or no quote.
                if (
                    place is not None
                    and author_states_facts
                    and re.search(
                        rf"(?<!from )(?<!away )(?<!toward )(?<!towards )(?<!leave )(?<!leaves )"
                        rf"(?<!left )(?<!near )\b{re.escape(place)}\b",
                        author_turn,
                        re.IGNORECASE,
                    )
                ):
                    set_place(after, place)
                elif not shown(self.place_quote):
                    self.refused.append(f"moved to {place or 'elsewhere'}: no sentence shows it")
                else:
                    set_place(after, place)

        # Events first: what they set is where the passage started from, and
        # a fact the passage shows moving on ("under attack", then "fallen")
        # overrides it below.
        for event_id, event_quote in self.happened:
            event = plot.event(event_id)
            status = after.events.get(event_id)
            if event is None or status is None or status.state != "directed":
                self.refused.append(f"{event_id} wasn't expected in this passage")
                continue
            if locate_quote(passage, event_quote) is None:
                self.refused.append(f"{event_id}: no sentence in the passage shows it")
                continue
            variant = next(
                (v for v in event.variants if (v.title or None) == status.variant),
                event.variants[0],
            )
            happen(after, event, variant, node_id=node_id, revealed=True, quote=event_quote)

        for proposed in self.facts:
            definition = plot.fact(proposed.fact)
            if definition is None:
                self.refused.append(f"unknown fact {proposed.fact}")
                continue
            if not definition.watched:
                # Set by events and the author only.
                self.refused.append(f"{proposed.fact} isn't the read's to change")
                continue
            if definition.values and proposed.value not in definition.values:
                self.refused.append(f"{proposed.fact} can't be “{proposed.value}”")
                continue
            if not shown(proposed.quote):
                self.refused.append(f"{proposed.fact}: no sentence in the passage shows it")
                continue
            set_fact(after, proposed.fact, proposed.value, quote=proposed.quote)
        return after


def _matching_place(name: str, places: Sequence[str]) -> str | None:
    """A place as the plot spells it, from however the read wrote it."""

    def key(text: str) -> str:
        return re.sub(r"^the\s+", "", " ".join(text.lower().split()))

    return next((place for place in places if key(place) == key(name)), None)


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _norm(value: object) -> str:
    return _clean(value).lower()


def _elapsed(data: object) -> tuple[int, str]:
    if not isinstance(data, dict):
        return 0, ""
    amount = data.get("amount")
    if isinstance(amount, str):
        try:
            amount = float(amount)
        except ValueError:
            amount = None
    if not isinstance(amount, (int, float)) or amount < 0:
        return 0, _clean(data.get("quote"))
    unit = _norm(data.get("unit")).rstrip("s") or "minute"
    per = _UNITS.get(_UNIT_ALIASES.get(unit, unit), 1)
    return round(amount * per), _clean(data.get("quote"))


def _time_of_day(value: object) -> int | None:
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", _clean(value))
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def parse_read(text: str) -> ChronicleDelta:
    """The read's reply as a delta. Raises ValueError when there is no JSON to read."""
    data = extract_json(text, dict, "record of time and facts")
    elapsed, quote = _elapsed(data.get("elapsed"))
    facts: list[ProposedFact] = []
    raw_facts = data.get("facts")
    for item in raw_facts if isinstance(raw_facts, list) else []:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"[\s-]+", "_", _norm(item.get("fact")))
        value = _norm(item.get("value"))
        if name and value:
            facts.append(ProposedFact(name, value, _clean(item.get("quote"))))
    happened: list[tuple[str, str]] = []
    raw_events = data.get("happened")
    for item in raw_events if isinstance(raw_events, list) else []:
        if isinstance(item, dict) and _clean(item.get("event")):
            happened.append((_clean(item.get("event")), _clean(item.get("quote"))))
    place: str | None = None
    place_quote = ""
    raw_place = data.get("place")
    if isinstance(raw_place, dict):
        name = _clean(raw_place.get("name"))
        place = "" if name.lower() in ("", "elsewhere", "none", "null") else name
        place_quote = _clean(raw_place.get("quote"))
    elif isinstance(raw_place, str):
        place = "" if _norm(raw_place) in ("", "elsewhere") else _clean(raw_place)
    day = data.get("day")
    if isinstance(day, str) and day.strip().isdigit():
        day = int(day)
    return ChronicleDelta(
        day=day if isinstance(day, int) and day > 0 else None,
        place=place,
        place_quote=place_quote,
        happened=happened,
        elapsed=elapsed,
        elapsed_quote=quote,
        ends_at=_time_of_day(data.get("ends_at")),
        facts=facts,
    )
