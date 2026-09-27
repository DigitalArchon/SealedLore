"""Names and quotes: how the reads find people and sentences in prose.

This was the post-generation validator (§3.3, §3.4): name-and-verb patterns
for the held character and for absent ones, a warning banner, and a trim.
Measured on 100 real passages (Sept 2026) the held-character patterns flagged
2, both wrong, and missed all 3 real slips; the absent-character patterns had
been wrong every time they fired in 299 logged turns. The author found the
banner got in the way, so it is gone. What remains is what the scene read,
the plot reads and the character scan use: matching a character by name,
alias or distinctive part; finding a quoted sentence; telling a cutaway.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sealedlore.models.character import Character

# Models refer to a character called "Serrik Vaun" as "Serrik", so parts of a
# name have to match too. Particles and titles are skipped: matching "Captain"
# on its own would flag every other captain in the story.
MIN_NAME_PART = 3
_NAME_PARTICLES = frozenset("the of de van von da di du le la el al bin ibn mac mc san st".split())
_TITLES = frozenset(
    """
    captain commander lieutenant sergeant general admiral lord lady ser sir mister mr
    mrs ms miss doctor dr master mistress king queen prince princess father mother
    brother sister uncle aunt goodman goodwife chief ensign major colonel corporal
    private officer constable professor agent detective inspector senator chancellor
    knight elder
    """.split()
)
# For code elsewhere that has to recognise a rank before a name.
TITLES = _TITLES


def name_forms(character: Character) -> list[str]:
    """Every string that plausibly refers to this character, longest first.

    Longest first matters for deduplication: "Serrik Vaun said" should report
    the full name rather than the bare "Serrik" hit nested inside it.
    """
    names: set[str] = set()
    for full in (character.name, *character.aliases):
        full = full.strip()
        if not full:
            continue
        names.add(full)
        tokens = re.findall(r"[\w’']+", full)
        if len(tokens) > 1:
            for token in tokens:
                lowered = token.lower()
                if (
                    len(token) >= MIN_NAME_PART
                    and lowered not in _NAME_PARTICLES
                    and lowered not in _TITLES
                ):
                    names.add(token)
    return sorted(names, key=len, reverse=True)


def _name_pattern(name: str, *, allow_possessive: bool = False) -> str:
    # For violations, possessives are description rather than action: "Serrik's
    # shadow fell" is the world reacting, which the model may write. When
    # checking the author's own turn we do want them — "Nils's eye level"
    # still means they are addressing Nils.
    suffix = "" if allow_possessive else r"(?!['’]s)"
    return rf"\b{re.escape(name)}\b{suffix}"


_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The paragraph containing text[start:end]."""
    left = 0
    for match in _PARAGRAPH_BREAK.finditer(text, 0, start):
        left = match.end()
    following = _PARAGRAPH_BREAK.search(text, end)
    return left, following.start() if following else len(text)


def mentions(text: str, character: Character) -> bool:
    """Whether `text` names this character by name, alias or distinctive part."""
    return any(
        re.search(_name_pattern(name, allow_possessive=True), text, flags=re.IGNORECASE)
        for name in name_forms(character)
        if name.strip()
    )


# How the model actually opens a cutaway, from live whole-story runs: a scene
# break ("---") before the paragraph, or a place or time phrase and a comma at
# its start ("In the temple's eastern meditation hall, [a character]…", "In
# the command centre, the voice…", "Meanwhile, …").
_SCENE_BREAK = re.compile(r"(?:^|\n)\s*(?:-{3,}|\*\s*\*\s*\*|#{1,3})\s*$")
# "Cutaway: In the training station, [a character]…" is a live one: the model
# labelled its own cutaway, and the label hid the place phrase behind it.
# MULTILINE because it is matched at a paragraph's offset, where a bare "^"
# never matches: without it only a cutaway in the opening paragraph was seen.
# "Five miles north," "Two decks down," "A day's ride away,".
_DISTANCE = (
    r"(?:an?|one|two|three|four|five|six|seven|eight|nine|ten|twenty|a few|several|\d+)"
    r"\s+(?:miles?|kilometres?|kilometers?|klicks?|leagues?|blocks?|streets?|decks?"
    r"|hours?|days?)(?:['’]s? (?:ride|walk|drive|march))?"
    r"\s+(?:away|off|out|north|south|east|west|behind|ahead|up|down|below|above)\w*"
)
_SCENE_SHIFT = re.compile(
    r"^\s*(?:cutaway|meanwhile|elsewhere|far away|far from|back (?:in|at|on|aboard)|across"
    r"|in|at|on|aboard|inside|outside|beyond|light-years away|later|hours later|that night"
    r"|the next (?:morning|day)|by (?:morning|nightfall)|" + _DISTANCE + r")\b[^.!?\n]{0,90}[,:]",
    re.IGNORECASE | re.MULTILINE,
)
# The openers that can only mean somewhere else. "In the doorway, Mara…" opens
# with a place phrase and is often an arrival; "Five miles north, Marcus Webb
# stood at the gate" (live, Doomsville) is never one.
_FAR_SHIFT = re.compile(
    r"^\s*(?:cutaway|meanwhile|elsewhere|far away|far from|back (?:in|at|on|aboard)|across"
    r"|light-years away|" + _DISTANCE + r")\b[^.!?\n]{0,90}[,:]",
    re.IGNORECASE | re.MULTILINE,
)


def opens_elsewhere(text: str, start: int, in_scene: Sequence[Character]) -> bool:
    """Whether the paragraph holding text[start] is a cutaway, for the scene read.

    Stricter than `_is_cutaway`, because a shortcut's quote comes from a model
    rather than a pattern: a paragraph that opens with a mere place phrase
    ("In the doorway, Mara stood…") counts only if it names nobody in the
    scene, while a scene break or an opener that can only mean somewhere else
    counts whoever it names.
    """
    left, right = _paragraph_bounds(text, start, start)
    before = text[:left].rstrip()
    if _SCENE_BREAK.search(before[-12:] if before else "") or _FAR_SHIFT.match(text, left):
        return True
    if not _SCENE_SHIFT.match(text, left):
        return False
    paragraph = text[left:right]
    return not any(mentions(paragraph, character) for character in in_scene)


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Where a quoted sentence sits in `text`, allowing for the model's retyping.

    Models copying a sentence change its quotation marks, its spacing, and now
    and then a word's case. Match on the words in order, with anything between
    them; failing that, on the first eight.
    """
    if not quote.strip():
        return None
    exact = text.find(quote.strip())
    if exact >= 0:
        return exact, exact + len(quote.strip())
    words = re.findall(r"[\w’']+", quote)
    for run in (words, words[:8]):
        if len(run) < 3:
            continue
        pattern = r"\W+".join(re.escape(word) for word in run)
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.start(), match.end()
    return None
