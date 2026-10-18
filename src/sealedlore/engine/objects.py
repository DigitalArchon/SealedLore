"""Objects the author makes for the map (the author, Oct 18 2026: "a create
object feature that is separate. The user just says what object or objects
they want and they are created. Then we need an interface where the user
can just place or remove any object they want", mainly to set an area up at
the start of a story). Pure: what the author typed, read as a list of
objects. Each is drawn as a thing of the tile set's (`items/<slug>`, in its
style) and placed by hand, as a correction of the map
(engine/session_corrections.py)."""

from __future__ import annotations

import re

# The most objects one request makes: each is a picture drawn.
MAX_OBJECTS = 12
# A name is a thing's name, not a paragraph.
MAX_NAME = 80

# "a table with a lamp and a vase" is one thing: " and " splits a part only
# when the part says nothing of what is with it.
_WITH = re.compile(r"\b(with|holding|full of|made of)\b", re.IGNORECASE)
_AND = re.compile(r"\s+(?:and|&)\s+", re.IGNORECASE)
_LEADING = re.compile(r"^(?:and|&|also|plus)\s+", re.IGNORECASE)
_ARTICLE = re.compile(r"^(?:a|an|the|some)\s+", re.IGNORECASE)


def parse_objects(text: str) -> list[str]:
    """The objects in what the author typed, one per comma, semicolon or
    line, a list's last " and " too ("an armchair, a side table and a rug"
    is three); without its article, as typed otherwise, each once (case
    aside), at most `MAX_OBJECTS`."""
    found: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;\n]+", text):
        part = _LEADING.sub("", " ".join(part.split())).strip(" .")
        if not part:
            continue
        pieces = [part] if _WITH.search(part) else _AND.split(part)
        for piece in pieces:
            name = _ARTICLE.sub("", piece.strip(" .")).strip()
            if not name:
                continue
            name = name[:MAX_NAME].rstrip()
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append(name)
            if len(found) >= MAX_OBJECTS:
                return found
    return found
