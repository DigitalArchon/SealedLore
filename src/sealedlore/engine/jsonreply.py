"""Reading JSON out of a model's reply.

Models asked for "JSON and nothing else" still wrap it in code fences or put
a sentence in front of it. Take the outermost object or array and parse that;
anything else is a plain-words ValueError the caller can show as a notice.

Four repairs, all from live replies (GLM 5.3). Dialogue copied into a
"quote" arrives with its quotation marks unescaped; a mark inside a string
that isn't followed by what can come after a string's end is taken as part
of it. A comma before a closing brace or bracket is dropped. A bracket the
reply forgot to close is closed (`_balanced`: GLM 5.3 Flash's furnishing
of a deck ended "]]}" for "]}]}", and the deck was refused whole; the other
refusal in those 11 decks broke off inside a string, which no repair should
guess at, and is asked for again). And a reply that corrects itself
("Wait — the scene moved. Corrected:" and a second object) means its last
object.
That one is tried last: looking for whole objects in a reply that failed to
parse finds the ones nested in it, and a scene read ending
'"scene_closed": {...},\n}' was taken for its scene_closed alone, the move
and the departure lost without a word.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_CLOSER = {"{": "}", "[": "]"}
# The most closers a reply may have left off its end for `_balanced` to add
# them: more is a reply cut short, not one that slipped.
MAX_MISSING = 2
# What may follow the quotation mark that closes a JSON string.
_AFTER_STRING = frozenset(",}]:")


def extract_json(text: str, kind: type[dict] | type[list], what: str) -> Any:
    """The reply's outermost JSON object (`dict`) or array (`list`).

    `what` names the thing expected ("list of characters") for the error.
    """
    opener, closer = ("{", "}") if kind is dict else ("[", "]")
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find(opener), cleaned.rfind(closer)
    if start < 0:
        raise ValueError(f"the reply held no {what}")
    if end < start:  # its one closer forgotten
        data = _closed(cleaned[start:])
        if not isinstance(data, kind):
            raise ValueError(f"the reply held no {what}")
        return data
    span = cleaned[start : end + 1]
    try:
        data = json.loads(span)
    except json.JSONDecodeError as exc:
        data = _repaired(span)
        if data is None:
            data = _closed(cleaned[start:])
        if data is None:
            data = _last_whole(cleaned, opener, kind)
        if data is None:
            raise ValueError(f"the {what} wasn't valid JSON ({exc.msg})") from exc
    if not isinstance(data, kind):
        raise ValueError(f"the reply held no {what}")
    return data


def _repaired(span: str) -> Any:
    """The span parsed after the repairs that keep it one value, or None."""
    for fixed in (
        _without_trailing_commas(span),
        _escape_inner_quotes(span),
        _without_trailing_commas(_escape_inner_quotes(span)),
    ):
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            continue
    return None


def _closed(text: str) -> Any:
    """The value parsed once its forgotten closers are put back, with the
    other repairs if they are needed too, or None."""
    balanced = _balanced(text)
    if balanced is None:
        return None
    data = _parses(balanced)
    return data if data is not None else _repaired(balanced)


def _parses(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _balanced(text: str) -> str | None:
    """The first value in `text` with the closers it forgot put back: a
    closer that skips an open object or array closes that first, and what is
    still open at the end is closed. None when nothing was missing, when
    anything but white space follows the value (a reply that corrects itself
    is `_last_whole`'s), when a closer matches nothing open, or when the
    reply looks cut short (it stops inside a string, not on a closer, or
    with more than MAX_MISSING still open)."""
    out: list[str] = []
    stack: list[str] = []
    in_string = escaped = False
    repaired = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            out.append(char)
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSER:
            stack.append(_CLOSER[char])
        elif char in "}]":
            if char not in stack:
                return None
            while stack[-1] != char:
                out.append(stack.pop())
                repaired = True
            stack.pop()
            out.append(char)
            if not stack:
                if text[index + 1 :].strip():
                    return None
                return "".join(out) if repaired else None
            continue
        out.append(char)
    tail = text.rstrip()
    if in_string or not stack or len(stack) > MAX_MISSING or tail[-1:] not in ("}", "]"):
        return None
    return "".join(out).rstrip() + "".join(reversed(stack))


def _without_trailing_commas(text: str) -> str:
    """Commas before a closing brace or bracket dropped, outside strings."""
    out: list[str] = []
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "," and text[index + 1 :].lstrip()[:1] in ("}", "]"):
            continue
        out.append(char)
    return "".join(out)


def _last_whole(text: str, opener: str, kind: type) -> Any:
    """The last complete top-level value of `kind` in the text, if any parse."""
    decoder = json.JSONDecoder()
    found: Any = None
    position = text.find(opener)
    while position >= 0:
        try:
            value, stop = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            position = text.find(opener, position + 1)
            continue
        if isinstance(value, kind):
            found = value
        position = text.find(opener, stop)
    return found


def _escape_inner_quotes(text: str) -> str:
    out: list[str] = []
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                rest = text[index + 1 :].lstrip()
                if rest and rest[0] not in _AFTER_STRING:
                    out.append('\\"')
                    continue
                in_string = False
            elif char == "\n":
                out.append("\\n")
                continue
        elif char == '"':
            in_string = True
        out.append(char)
    return "".join(out)
