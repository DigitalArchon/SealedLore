"""Okapi BM25 over plain words, pure Python: recall's keyword ranking
(engine/recall.py) and the parts of a long lore entry a turn is about
(engine/retrieval.py `fit_lore`)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

_STOPWORDS = frozenset(
    """a an the and or but of to in on at by for with from as is was were be been
    are it its this that these those he she they them his her their him i you we us
    our your my me what who whom which when where why how did do does done had has
    have not no so if then than there here about into out up down over after before
    while during again once all any some such only own same too very can will just
    would could should said says say""".split()
)


def words(text: str) -> list[str]:
    text = text.lower().replace("\u2019", "'")
    return [w for w in re.findall(r"[a-z0-9']+", text) if len(w) > 1 and w not in _STOPWORDS]


def bm25(query: Sequence[str], units: Sequence[Sequence[str]]) -> list[float]:
    """Okapi BM25 of each unit against the query's words (k1 1.2, b 0.75):
    a word counts for more the rarer it is among the units."""
    if not units:
        return []
    average = sum(len(unit) for unit in units) / len(units) or 1.0
    frequency = Counter(word for unit in units for word in set(unit))
    total = len(units)
    out = []
    for unit in units:
        counts = Counter(unit)
        score = 0.0
        for word in query:
            seen = counts.get(word)
            if not seen:
                continue
            idf = math.log(1 + (total - frequency[word] + 0.5) / (frequency[word] + 0.5))
            score += idf * seen * 2.2 / (seen + 1.2 * (0.25 + 0.75 * len(unit) / average))
        out.append(score)
    return out
