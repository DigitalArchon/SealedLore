"""Habits of phrasing, counted in code: no model, no network.

A storyteller copies its own recent passages, so a turn of phrase it used
twice becomes the way it writes ("the stillness of someone who…" in nine
passages of ten, by the middle of a long story). `feedback` names the habits
the recent passages lean on, for the turn's REMEMBER line; a story that has
none gets nothing.

The patterns read English prose. Another language's passages match none of
them, and its repeated phrases are still found.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts

# How many of the latest passages are looked at, and how few are too few to
# call anything a habit.
RECENT_PASSAGES = 8
MIN_PASSAGES = 4
# A habit is named when at least this share of them show it (and never fewer
# than three), a phrase when it stands in at least three.
HABIT_SHARE = 0.5
PHRASE_PASSAGES = 3
# The most habits and phrases named at once: a longer list reads as a ban on
# writing at all.
MOST_HABITS = 4
MOST_PHRASES = 3

_KINDS_OF_PEOPLE = (
    "man|woman|person|officer|soldier|doctor|captain|girl|boy|sailor|commander|surgeon|pilot|"
    "engineer|child|people|creature|thing"
)
# Each family's key names its prompt text, `habits.<key>`.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "who": re.compile(
        rf"\b(?:of|with|like|in the way of) (?:a|an|the) (?:{_KINDS_OF_PEOPLE})s? who\b"
        r"|\b(?:of|like|as) (?:someone|somebody|people|those|anyone|one) who\b",
        re.IGNORECASE,
    ),
    "particular": re.compile(r"\b(?:the|a|that) particular\b", re.IGNORECASE),
    "something": re.compile(
        r"\bsomething (?:in (?:his|her|their|its) \w+(?: \w+)? )?(?:moved|shifted|flickered|"
        r"crossed|settled|changed|eased|tightened|went|passed|underneath|"
        r"that (?:was|might|could|wasn't|had))\b"
        r"|\b(?:with|of|in) something (?:like|close to|that)\b",
        re.IGNORECASE,
    ),
    "notbut": re.compile(
        r"\bnot (?:quite |exactly )?[^.;:!?\n]{2,40}?(?:,| —|—| -) (?:but|just|exactly)\b"
        r"|\b(?:wasn't|was not|isn't|is not) [^.!?\n]{2,50}[.;—] ?(?:It|That|This) (?:was|is)\b"
        r"|\bnot quite [^.!?\n]{2,40} (?:and|but) not quite\b"
        r"|\b(?:It|That) (?:wasn't|was not|isn't)\.",
        re.IGNORECASE,
    ),
    "beat": re.compile(
        r"\b(?:a beat|a pause|a long moment|for a (?:long )?moment|let the silence"
        r"|the silence (?:stretch|sat|held|settled))",
        re.IGNORECASE,
    ),
}
_OPENER = re.compile(
    r"^[^.!?\n]{0,60}?\b(?:was (?:quiet|silent|still)|said nothing|"
    r"looked at (?:him|her|them|\w+) for|held (?:his|her|their) (?:gaze|look)|"
    r"let the (?:silence|quiet)|absorbed|considered (?:this|that|him|her|it)|"
    r"studied (?:him|her|them)|for a (?:long )?moment|a beat|watched (?:him|her|them)|"
    r"didn't answer|did not answer)",
    re.IGNORECASE,
)
# In the order they are named when equally common.
FAMILIES = (*_PATTERNS, "opener", "echo", "button")

_STOP = frozenset(
    "the a an and or but of to in on at for with is was are were be been it its i you he she "
    "they his her their my your me him them that this as by from not no so if then than there "
    "here have has had do does did will would can could just into out up down over what which "
    "who when where said".split()
)


def _quotes(text: str) -> list[str]:
    return re.findall(r"[\"“]([^\"”]{3,160})[\"”]", text)


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z']+", text.lower().replace("’", "'")))


def echoes(passage: str, author_turn: str) -> bool:
    """Whether someone in the passage hands the author's own words back as a
    short line of their own ("Boring," he repeated)."""
    said = _plain(" ".join(_quotes(author_turn)))
    if not said:
        return False
    for quote in _quotes(passage)[:3]:
        words = _plain(quote).split()
        if not 1 <= len(words) <= 6 or f" {' '.join(words)} " not in f" {said} ":
            continue
        if len(words) >= 2 or len(words[0]) >= 6:
            return True
    return False


def found(passage: str, author_turn: str = "") -> set[str]:
    """The habit families a passage shows, by key."""
    names = {key for key, pattern in _PATTERNS.items() if pattern.search(passage)}
    paragraphs = [p.strip() for p in passage.strip().split("\n") if p.strip()]
    if paragraphs and _OPENER.search(paragraphs[0][:200]):
        names.add("opener")
    if author_turn and echoes(passage, author_turn):
        names.add("echo")
    if (
        len(paragraphs) > 1
        and len(paragraphs[-1].split()) <= 12
        and not re.search(r"[\"“”]", paragraphs[-1])
    ):
        names.add("button")
    return names


def _matter(texts: Sequence[str]) -> set[str]:
    """The words of the author's own turns that carry something."""
    words = re.findall(r"[^\W\d_]+", " ".join(texts).lower())
    return {word for word in words if len(word) > 3 and word not in _STOP}


def repeated_phrases(
    passages: Sequence[str],
    at_least: int = PHRASE_PASSAGES,
    most: int = MOST_PHRASES,
    authors: Sequence[str] = (),
) -> list[str]:
    """Four-word phrases standing in `at_least` of the passages. Left out: a
    phrase holding a name, one that is mostly small words, and one using a
    word of the author's own turns (`authors`): what the scene is about is
    bound to come back ("at the false floor"), and is not a habit."""
    matter = _matter(authors)
    counts: Counter[str] = Counter()
    for passage in passages:
        tokens = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", passage.replace("’", "'"))
        seen = set()
        for start in range(len(tokens) - 3):
            gram = tokens[start : start + 4]
            if any(word[0].isupper() for word in gram):
                continue
            low = [word.lower() for word in gram]
            if sum(word in _STOP for word in low) <= 2 and not matter.intersection(low):
                seen.add(" ".join(low))
        counts.update(seen)
    chosen: list[str] = []
    for phrase, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if count < at_least or len(chosen) == most:
            break
        # "the particular quality of" and "particular quality of a" are one habit.
        if not any(len(set(phrase.split()) & set(other.split())) >= 3 for other in chosen):
            chosen.append(phrase)
    return chosen


def habits_of(recent: Sequence[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(the habit families, the phrases) the recent passages lean on.
    `recent`: (the author's turn, the passage that answered it), oldest first."""
    recent = list(recent)[-RECENT_PASSAGES:]
    if len(recent) < MIN_PASSAGES:
        return [], []
    counts: Counter[str] = Counter()
    for author_turn, passage in recent:
        counts.update(found(passage, author_turn))
    need = max(3, round(len(recent) * HABIT_SHARE))
    ordered = sorted(
        (key for key in FAMILIES if counts[key] >= need),
        key=lambda key: (-counts[key], FAMILIES.index(key)),
    )
    phrases = repeated_phrases(
        [passage for _, passage in recent], authors=[turn for turn, _ in recent]
    )
    return ordered[:MOST_HABITS], phrases


def feedback(recent: Sequence[tuple[str, str]], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The clause for the REMEMBER line naming the habits of the recent
    passages, or "" when they have none."""
    families, phrases = habits_of(recent)
    named = [texts[f"habits.{key}"] for key in families]
    if phrases:
        named.append(
            texts.fill("habits.phrases", phrases=", ".join(f"“{phrase}”" for phrase in phrases))
        )
    if not named:
        return ""
    return texts.fill("turn.remember.habits", habits="; ".join(named))
