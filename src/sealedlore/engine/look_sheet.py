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

Rows are the looks in the author's order (`Character.sheet_looks`, once
`sheet_ordered`), then any look a picture carries that isn't listed yet.
Columns are the author's expressions for them (`Character.sheet_expressions`,
else DEFAULT_EXPRESSIONS), then any other a picture carries. A picture with
an expression and no look is in the row "" (no look named), one with a look
and no expression in the column ""; one with neither is unlabelled, kept
apart. Labels match whatever their case and spacing, and show as first
written.

**The order stays as the author sees it** (the author, Nov 7 2026: the rows
moved about as pictures changed). Before, the rows were the pictures' looks
in card order, so a picture made the portrait, moved to another row or drawn
first brought its look to the top, and a row lost its last picture went. Now
whatever changes the sheet keeps its rows and columns on the card first
(`keep_order`), and they stay where they are until the author moves them
(`reorder`). A card never changed since reads in the old order, so nothing
moves the day this came in.

**A row or column has a heading and the words it is drawn with**
(`wording`): the heading is the label, the words what an image model is
asked for (the card's own, else the usual ones for a common expression,
else the heading).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.looks import USUAL_WORDING, look_change
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
    pictured = [r.look for r in labelled if r.look.strip()]
    looks = _ordered(
        (
            [*character.sheet_looks, *pictured]
            if character.sheet_ordered
            else [*pictured, *character.sheet_looks]
        )
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


def sheet_rows(character: Character) -> list[str]:
    """The sheet's named rows, in order."""
    return [look for look in sheet_for(character).looks if look]


def sheet_columns(character: Character) -> list[str]:
    """The sheet's named columns, in order."""
    return [expression for expression in sheet_for(character).expressions if expression]


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


def _gap_labels(gap: Gap) -> tuple[str, str]:
    """(look, expression) a gap's edit asks for: only what differs from its
    source (an unnamed look or expression is left as it is). A waiting one is
    drawn from its row's first picture, in its look already: only its
    expression."""
    source = gap.source
    if source is None:
        assert gap.after, "a gap with nothing to draw it from"
        if label_key(gap.expression) == label_key(gap.after):
            return "", ""
        return "", gap.expression
    look = gap.look if gap.look and label_key(gap.look) != label_key(source.look) else ""
    expression = (
        gap.expression
        if gap.expression and label_key(gap.expression) != label_key(source.expression)
        else ""
    )
    return look, expression


def gap_change(
    gap: Gap, texts: PromptTexts = DEFAULT_TEXTS, character: Character | None = None
) -> str:
    """What a gap's edit asks for (`_gap_labels`), in the words `character`'s
    sheet draws each with."""
    look, expression = _gap_labels(gap)
    return sheet_change(character, look=look, expression=expression, texts=texts)


def gap_words(
    gap: Gap, texts: PromptTexts = DEFAULT_TEXTS, character: Character | None = None
) -> str:
    """The words a gap is drawn with, as the sheet shows them in it: what its
    edit asks for, and no more."""
    look, expression = _gap_labels(gap)
    parts = [
        wording(character, look, look=True, texts=texts) if look else "",
        wording(character, expression, texts=texts) if expression else "",
    ]
    return "; ".join(part for part in parts if part)


# --- how a row or column is drawn ---------------------------------------------------------


def _words(character: Character, look: bool) -> dict[str, str]:
    return character.look_wording if look else character.expression_wording


def usual_wording(label: str, *, look: bool = False, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The words a label is drawn with on a card with none of its own: the
    usual ones for a common expression, else the label itself."""
    key = label_key(label)
    text_key = None if look else USUAL_WORDING.get(key)
    return _phrase(texts[text_key]) if text_key else tidy(label)


def wording(
    character: Character | None,
    label: str,
    *,
    look: bool = False,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The words a row (`look`) or a column is drawn with: the card's own,
    else the usual ones (`usual_wording`). "" for an unnamed one."""
    if not label_key(label):
        return ""
    own = _words(character, look).get(label_key(label), "") if character is not None else ""
    return _phrase(own) or usual_wording(label, look=look, texts=texts)


def own_wording(character: Character, label: str, *, look: bool = False) -> str:
    """The card's own words for a row or column, "" for none."""
    return _phrase(_words(character, look).get(label_key(label), ""))


def set_wording(character: Character, label: str, words: str, *, look: bool = False) -> bool:
    """The words a row or column is drawn with kept on the card, or taken off
    (blank); whether anything changed."""
    key = label_key(label)
    if not key:
        return False
    kept = _words(character, look)
    words = _phrase(words)
    if words:
        if kept.get(key) == words:
            return False
        kept[key] = words
        return True
    return kept.pop(key, None) is not None


def sheet_change(
    character: Character | None,
    *,
    look: str = "",
    expression: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """`look_change` in the words `character`'s sheet draws each with."""
    return look_change(
        look=wording(character, look, look=True, texts=texts) if look.strip() else "",
        expression=wording(character, expression, texts=texts) if expression.strip() else "",
        texts=texts,
    )


def _phrase(text: str) -> str:
    """Words as they fill a prompt's sentence: spacing tidied, no full stop of
    their own at the end (the sentence brings it)."""
    return tidy(text).rstrip(" .")


# --- the order of the rows and columns ----------------------------------------------------


def keep_order(character: Character) -> bool:
    """The sheet's rows and columns as they stand now, kept on the card so a
    change to the pictures never moves them; whether the card changed. Run
    before anything that changes a character's pictures or their labels."""
    looks, expressions = sheet_rows(character), sheet_columns(character)
    changed = False
    if not character.sheet_ordered or looks != list(character.sheet_looks):
        character.sheet_looks = looks
        character.sheet_ordered = True
        changed = True
    # Left blank while it is the usual few: only a picture's own column
    # needs a place kept.
    if expressions != list(character.sheet_expressions or DEFAULT_EXPRESSIONS):
        character.sheet_expressions = expressions
        changed = True
    return changed


def reorder(
    character: Character,
    *,
    looks: Sequence[str] | None = None,
    expressions: Sequence[str] | None = None,
) -> bool:
    """The rows (`looks`) or the columns put in the author's order; whether
    the order changed. A label left out keeps its place after those given;
    the unnamed row and column stay last."""
    keep_order(character)
    moved = False
    if looks is not None:
        order = _put_first(character.sheet_looks, looks)
        moved = order != character.sheet_looks
        character.sheet_looks = order
    if expressions is not None:
        listed = list(character.sheet_expressions or DEFAULT_EXPRESSIONS)
        order = _put_first(listed, expressions)
        if order != listed:
            character.sheet_expressions = order
            moved = True
    return moved


def _put_first(labels: Sequence[str], first: Sequence[str]) -> list[str]:
    by_key = {label_key(label): label for label in labels}
    order = [by_key[label_key(x)] for x in first if label_key(x) in by_key]
    return _ordered([*order, *labels])


def move(character: Character, label: str, step: int, *, look: bool = False) -> bool:
    """A row (`look`) or a column moved `step` places along; whether it moved."""
    keep_order(character)
    labels = list(
        character.sheet_looks if look else (character.sheet_expressions or DEFAULT_EXPRESSIONS)
    )
    keys = [label_key(x) for x in labels]
    if label_key(label) not in keys:
        return False
    at = keys.index(label_key(label))
    to = at + step
    if not 0 <= to < len(labels):
        return False
    labels.insert(to, labels.pop(at))
    if look:
        character.sheet_looks = labels
    else:
        character.sheet_expressions = labels
    return True


def remove_label(
    character: Character, *, look: str | None = None, expression: str | None = None
) -> None:
    """A row or a column taken off the sheet, with the words it was drawn with
    (its pictures stay where the window puts them: it is offered for an
    empty one only)."""
    keep_order(character)
    if look is not None:
        character.sheet_looks = [
            x for x in character.sheet_looks if label_key(x) != label_key(look)
        ]
        character.look_wording.pop(label_key(look), None)
    if expression is not None:
        listed = character.sheet_expressions or list(DEFAULT_EXPRESSIONS)
        character.sheet_expressions = [x for x in listed if label_key(x) != label_key(expression)]
        character.expression_wording.pop(label_key(expression), None)


def name_unnamed(character: Character, heading: str, *, look: bool) -> bool:
    """The unnamed row (`look`) or column given a heading: every picture in
    it takes the heading (onto one the sheet has, the two become one). With
    no picture in it (the one row of a sheet with nothing labelled), the
    heading becomes the sheet's first named row or column. Whether anything
    changed. Before, the unnamed row and column couldn't be named at all
    (the author, Nov 8 2026)."""
    heading = tidy(heading)
    if not heading:
        return False
    keep_order(character)
    if look:
        if relabel(character, look=("", heading)):
            return True
        return add_label(character.sheet_looks, heading)
    if relabel(character, expression=("", heading)):
        return True
    listed = list(character.sheet_expressions or DEFAULT_EXPRESSIONS)
    if not add_label(listed, heading):
        return False
    character.sheet_expressions = listed
    return True


def set_aside(
    character: Character, *, look: str | None = None, expression: str | None = None
) -> int:
    """A row (`look`) or column taken off the sheet with its pictures: each
    is set aside without labels, none deleted; how many. The unnamed row and
    column too, which never emptied of themselves (the author, Nov 8 2026)."""
    keep_order(character)
    moved = 0
    for ref in character.reference_images:
        if not (ref.look.strip() or ref.expression.strip()):
            continue
        in_row = look is not None and label_key(ref.look) == label_key(look)
        in_column = expression is not None and label_key(ref.expression) == label_key(expression)
        if in_row or in_column:
            ref.look = ref.expression = ""
            moved += 1
    remove_label(character, look=look, expression=expression)
    return moved


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
    picture of theirs and on the sheet, where it stands, with the words it is
    drawn with (a row renamed onto another keeps that one's); how many
    pictures moved."""
    keep_order(character)
    moved = 0
    if look is not None:
        _move_words(character.look_wording, look)
    if expression is not None:
        _move_words(character.expression_wording, expression)
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


def _move_words(kept: dict[str, str], rename: tuple[str, str]) -> None:
    old, new = label_key(rename[0]), label_key(rename[1])
    words = kept.pop(old, None)
    if words is not None and new and new not in kept:
        kept[new] = words
