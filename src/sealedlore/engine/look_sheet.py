"""A character's pictures as a sheet: their looks down the side, expressions
or poses across, in each cell the pictures labelled so (engine/looks.py),
and the gaps between, each with the picture it would be drawn from. The
cast board's model (gui/cast_board.py). Pure.

The author (Nov 3 2026): managing many pictures per person should be
seamless and engaging, a view of everyone's pictures for every emotion and
dress or pose type, with the missing ones easy to fill in and their own
types added. Other apps keep a character's pictures as one list of
expressions (SillyTavern's sprites, RisuAI's emotion images) and outfits as
separate sets swapped by hand; visual novel engines keep outfit and
expression as two attributes (Ren'Py's layered images), the shape the
labels already have. So: a sheet, its gaps in plain view, each drawn from
the nearest picture so the face and clothes match.

Rows are the looks the pictures carry, in card order (the portrait's
first), then the looks the author has planned (`Character.sheet_looks`)
that no picture has yet. Columns are the author's expressions for them
(`Character.sheet_expressions`, else DEFAULT_EXPRESSIONS), then any other a
picture carries. A picture with an expression and no look is in the row
"" (no look named), one with a look and no expression in the column "";
one with neither is unlabelled, kept apart. Labels match whatever their
case and spacing, and show as first written.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.looks import look_change
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.node import Node

# The columns a sheet has before the author chooses: the expressions most
# scenes call for (A set of expressions… begins with four of them).
DEFAULT_EXPRESSIONS = ("neutral", "happy", "sad", "angry", "afraid")
NEUTRAL = "neutral"
# What the look pick writes between the two halves of a missing look.
WANTED_JOIN = "·"


def label_key(label: str) -> str:
    """A label as it is compared: case and spacing aside."""
    return " ".join(label.split()).lower()


def tidy(label: str) -> str:
    """A label as it is kept: its spacing tidied, its case as written."""
    return " ".join(label.split())


Key = tuple[str, str]


@dataclass(frozen=True)
class Gap:
    look: str
    expression: str
    # The picture it is drawn from: one in the same look (its neutral first),
    # else, for a look with no picture yet, the one its first cell starts
    # from. None for the rest of a new look's row (its first picture is drawn
    # first, so the rest match it) or a character with no picture at all.
    source: ImageRef | None
    # How often the story asked for it (engine/look_pick.py's "missing").
    wanted: int = 0
    # The rest of a new look's row: the expression of the row's first cell,
    # whose picture this one is drawn from once it is in.
    after: str = ""


@dataclass
class Sheet:
    looks: list[str]
    expressions: list[str]
    cells: dict[Key, list[ImageRef]]
    gaps: dict[Key, Gap]
    unlabelled: list[ImageRef]
    # What the story asked for that no row and column match, with how often:
    # its own words ("winter coat · ready to fight"), to draw as they are.
    asked: list[tuple[str, int]] = field(default_factory=list)
    portrait_id: str | None = None

    def pictures(self, look: str, expression: str) -> list[ImageRef]:
        return self.cells.get((label_key(look), label_key(expression)), [])

    def gap(self, look: str, expression: str) -> Gap | None:
        return self.gaps.get((label_key(look), label_key(expression)))

    def drawable(self) -> list[Gap]:
        """The gaps that can be drawn now, row by row."""
        return [gap for gap in self.fillable() if gap.source is not None]

    def fillable(self) -> list[Gap]:
        """Every gap that can be drawn now or, the rest of a new look's row,
        once its first picture is in; row by row."""
        return [
            gap
            for look in self.looks
            for expression in self.expressions
            if (gap := self.gaps.get((label_key(look), label_key(expression)))) is not None
            and (gap.source is not None or gap.after)
        ]

    def first_of(self, gap: Gap) -> Gap | None:
        """The gap a waiting one waits for: its row's first cell."""
        return self.gap(gap.look, gap.after) if gap.after else None

    @property
    def picture_count(self) -> int:
        return sum(len(refs) for refs in self.cells.values()) + len(self.unlabelled)


def split_wanted(text: str) -> tuple[str, str]:
    """ "combat armour · angry" as (look, expression); words with no "·" are
    an expression."""
    look, sep, expression = text.partition(WANTED_JOIN)
    if not sep:
        return "", tidy(text)
    return tidy(look), tidy(expression)


def wanted_along(path: Sequence[Node]) -> dict[str, Counter[str]]:
    """Every look the story asked for along `path`, by character, with how
    often (`NodeMeta.looks_wanted`, the pick's "missing")."""
    asked: dict[str, Counter[str]] = {}
    for node in path:
        for character_id, words in node.meta.looks_wanted.items():
            if tidy(words):
                asked.setdefault(character_id, Counter())[tidy(words)] += 1
    return asked


def _ordered(labels: Iterable[str]) -> list[str]:
    """Each label once (by key), first spelling kept, in order."""
    seen: dict[str, str] = {}
    for label in labels:
        seen.setdefault(label_key(label), tidy(label))
    return list(seen.values())


def sheet_for(character: Character, wanted: Mapping[str, int] | None = None) -> Sheet:
    """The character's sheet; `wanted` is what the story asked for for them
    (`wanted_along`), each with how often."""
    refs = list(character.reference_images)
    labelled = [r for r in refs if r.look.strip() or r.expression.strip()]
    unlabelled = [r for r in refs if not (r.look.strip() or r.expression.strip())]
    looks = _ordered(
        [r.look for r in labelled if r.look.strip()]
        + list(character.sheet_looks)
        + [""] * any(not r.look.strip() for r in labelled)
    )
    # Nothing labelled yet: one row all the same, a place to drop a picture
    # on (it was a sheet of headers alone, with nowhere to put anything).
    looks = looks or [""]
    expressions = _ordered(
        [*(character.sheet_expressions or DEFAULT_EXPRESSIONS)]
        + [r.expression for r in labelled if r.expression.strip()]
        + [""] * any(not r.expression.strip() for r in labelled)
    )
    cells: dict[Key, list[ImageRef]] = {}
    for ref in labelled:
        cells.setdefault((label_key(ref.look), label_key(ref.expression)), []).append(ref)

    portrait = refs[0] if refs else None
    asked: list[tuple[str, int]] = []
    wanted_cells: Counter[Key] = Counter()
    rows = {label_key(look) for look in looks}
    columns = {label_key(expression) for expression in expressions}
    for words, count in (wanted or {}).items():
        look, expression = split_wanted(words)
        key = (label_key(look), label_key(expression))
        if key[0] in rows and key[1] in columns:
            wanted_cells[key] += count
        else:
            asked.append((words, count))

    gaps: dict[Key, Gap] = {}
    for look in looks:
        row = [r for r in labelled if label_key(r.look) == label_key(look)]
        first_cell = _first_cell(expressions)
        for expression in expressions:
            key = (label_key(look), label_key(expression))
            if key in cells:
                continue
            after = ""
            if row:
                source = _neutral_or_first(row)
            elif label_key(expression) == label_key(first_cell):
                source = _start_of_new_look(labelled, portrait, expression)
            else:
                source = None
                if _start_of_new_look(labelled, portrait, first_cell) is not None:
                    after = first_cell
            gaps[key] = Gap(look, expression, source, wanted_cells.get(key, 0), after)
    return Sheet(
        looks=looks,
        expressions=expressions,
        cells=cells,
        gaps=gaps,
        unlabelled=unlabelled,
        asked=asked,
        portrait_id=portrait.id if portrait is not None else None,
    )


def _first_cell(expressions: Sequence[str]) -> str:
    """The cell a new look is drawn in first: neutral if there is one."""
    for expression in expressions:
        if label_key(expression) == NEUTRAL:
            return expression
    return expressions[0] if expressions else ""


def _neutral_or_first(row: Sequence[ImageRef]) -> ImageRef:
    for ref in row:
        if label_key(ref.expression) == NEUTRAL:
            return ref
    return row[0]


def _start_of_new_look(
    labelled: Sequence[ImageRef], portrait: ImageRef | None, expression: str
) -> ImageRef | None:
    """A look's first picture starts from one with its expression already
    (only the clothes change), the portrait's look first, else the portrait."""
    same = [r for r in labelled if label_key(r.expression) == label_key(expression)]
    if portrait is not None:
        same.sort(key=lambda r: label_key(r.look) != label_key(portrait.look))
    return same[0] if same else portrait


def gap_change(gap: Gap, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """What a gap's edit asks for: only what differs from its source (an
    unnamed look or expression is left as it is). A waiting one is drawn
    from its row's first picture, in its look already: only its expression."""
    source = gap.source
    if source is None:
        assert gap.after, "a gap with nothing to draw it from"
        if label_key(gap.expression) == label_key(gap.after):
            return ""
        return look_change(expression=gap.expression, texts=texts)
    look = gap.look if gap.look and label_key(gap.look) != label_key(source.look) else ""
    expression = (
        gap.expression
        if gap.expression and label_key(gap.expression) != label_key(source.expression)
        else ""
    )
    return look_change(look=look, expression=expression, texts=texts)


def add_label(labels: list[str], label: str) -> bool:
    """Add a row or column the author typed, once; whether it was new."""
    if not tidy(label) or label_key(label) in {label_key(x) for x in labels}:
        return False
    labels.append(tidy(label))
    return True


def relabel(
    character: Character,
    *,
    look: tuple[str, str] | None = None,
    expression: tuple[str, str] | None = None,
) -> int:
    """Rename a row (`look`: old, new) or a column (`expression`) on every
    picture of theirs and in their planned labels; how many pictures moved."""
    moved = 0
    if look is not None:
        old, new = label_key(look[0]), tidy(look[1])
        for ref in character.reference_images:
            if (ref.look.strip() or ref.expression.strip()) and label_key(ref.look) == old:
                ref.look = new
                moved += 1
        character.sheet_looks = _ordered(
            new if label_key(x) == old else x for x in character.sheet_looks
        )
    if expression is not None:
        old, new = label_key(expression[0]), tidy(expression[1])
        for ref in character.reference_images:
            if (ref.look.strip() or ref.expression.strip()) and label_key(ref.expression) == old:
                ref.expression = new
                moved += 1
        listed = character.sheet_expressions or list(DEFAULT_EXPRESSIONS)
        character.sheet_expressions = _ordered(new if label_key(x) == old else x for x in listed)
    return moved
