"""Whose move it is: a turn that leaves the next one to the storyteller.

An author who writes "I nod", "I wait and see" or "Three days pass" has
handed the story over, and a storyteller told only to show how people react
to the author's turn reacts to nothing: the scene idles, and a time skip is
summarised and stops. `move_left` reads the author's turn for that, in code,
so the turn's REMEMBER line can ask for something to happen instead.

It reads English turns. A turn it doesn't recognise is an ordinary turn,
which is what every turn was before.
"""

from __future__ import annotations

import re
from typing import Literal

Move = Literal["", "idle", "skip"]

# An out-of-character turn longer than this is setting something up, not
# just letting time go by.
SKIP_MOST_WORDS = 25
# A turn in character this short says nothing whatever its words; up to the
# longer limit it must also read as waiting.
IDLE_ALWAYS_WORDS = 5
IDLE_MOST_WORDS = 14

_TIME_PASSES = re.compile(
    r"\b(?:pass(?:es|ed)?|goes by|go by|went by|later|"
    r"the next (?:day|morning|evening|night|week|month)|the rest of the)\b",
    re.IGNORECASE,
)
_WAITS = re.compile(
    r"\b(?:wait|watch|listen|nod|shrug|stay|rest|sat\b|sit|stood|stand|"
    r"carr(?:y|ies|ied) on|ke(?:pt|ep|eps) to|sa(?:id|y|ys) nothing|w?ent about|"
    r"go(?:es)? about|look(?:s|ed)? on)\w*",
    re.IGNORECASE,
)
# Speech or a question is a turn with something in it, however short.
_SAYS_SOMETHING = re.compile(r"[\"“”?]")


def move_left(text: str, *, in_character: bool) -> Move:
    """ "skip": an out-of-character turn that lets time pass. "idle": a turn in
    character that waits, watches or says next to nothing. "": anything else.

    `in_character`: the turn is a character's, not Direction or Narration.
    """
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text)
    if not words:
        return ""
    if not in_character:
        return "skip" if len(words) <= SKIP_MOST_WORDS and _TIME_PASSES.search(text) else ""
    if _SAYS_SOMETHING.search(text):
        return ""
    if len(words) <= IDLE_ALWAYS_WORDS:
        return "idle"
    return "idle" if len(words) <= IDLE_MOST_WORDS and _WAITS.search(text) else ""
