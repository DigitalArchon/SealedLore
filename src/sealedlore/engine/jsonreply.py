"""Reading JSON out of a model's reply.

Models asked for "JSON and nothing else" still wrap it in code fences or put
a sentence in front of it. Take the outermost object or array and parse that;
anything else is a plain-words ValueError the caller can show as a notice.

Two repairs, both from live replies (GLM 5.3). A reply that corrects itself
("Wait — the scene moved. Corrected:" and a second object) means its last
object. And dialogue copied into a "quote" arrives with its quotation marks
unescaped; a mark inside a string that isn't followed by what can come after
a string's end is taken as part of it.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# What may follow the quotation mark that closes a JSON string.
_AFTER_STRING = frozenset(",}]:")


def extract_json(text: str, kind: type[dict] | type[list], what: str) -> Any:
    """The reply's outermost JSON object (`dict`) or array (`list`).

    `what` names the thing expected ("list of characters") for the error.
    """
    opener, closer = ("{", "}") if kind is dict else ("[", "]")
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find(opener), cleaned.rfind(closer)
    if start < 0 or end < start:
        raise ValueError(f"the reply held no {what}")
    span = cleaned[start : end + 1]
    try:
        data = json.loads(span)
    except json.JSONDecodeError as exc:
        data = _last_whole(cleaned, opener, kind)
        if data is None:
            try:
                data = json.loads(_escape_inner_quotes(span))
            except json.JSONDecodeError:
                raise ValueError(f"the {what} wasn't valid JSON ({exc.msg})") from exc
    if not isinstance(data, kind):
        raise ValueError(f"the reply held no {what}")
    return data


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
