"""A story's character and the card of the author's own they came from
(storage/collection.py). Pure: pictures are read, written and copied by the
window, given here as their marks and refs.

The author (Nov 4 2026): a character put into a story may get pictures and
details there; the story should say what has changed from the card, and let
the author save the changes to the card, make a new version of the card from
them, or keep them in the story only.

A story is given the card's fields with the names in (`given`), and keeps
what it was given (`CardLink.fields`, `.pictures`). With that, each field is
told apart three ways: changed in the story, changed on the card since, or
both. Saving one way or the other moves what was given along, so a change is
offered once. A field and its value here are in the story's terms: text
with the names in, "" for none.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sealedlore.ids import new_id
from sealedlore.models.character import Character, Competence
from sealedlore.models.collection import CardLink, CollectionInfo, PictureMark
from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import TavernFields
from sealedlore.storage.tavern_cards import example_dialogue, first_sentence, macro_filler

# What a story is given of a card, and what it can change and give back.
# Whether the author may play them is the story's own choice, never a change.
LINKED_FIELDS = (
    "name",
    "aliases",
    "summary",
    "full_description",
    "personality",
    "voice_notes",
    "competence",
    "canon",
    "sheet_looks",
    "sheet_expressions",
)
FIELD_LABELS = {
    "name": "Name",
    "aliases": "Aliases",
    "summary": "Summary",
    "full_description": "Description",
    "personality": "Personality",
    "voice_notes": "Voice",
    "competence": "Competence and skills",
    "canon": "From",
    "sheet_looks": "Looks on their sheet",
    "sheet_expressions": "Expressions on their sheet",
}
PORTRAIT = "portrait"
STORY, CARD, BOTH = "story", "card", "both"


def fields_of(character: Character) -> dict[str, Any]:
    """A character's linked fields, as plain values."""
    return {
        "name": character.name,
        "aliases": list(character.aliases),
        "summary": character.summary or "",
        "full_description": character.full_description or "",
        "personality": character.personality or "",
        "voice_notes": character.voice_notes or "",
        "competence": character.competence.model_dump(),
        "canon": character.canon or "",
        "sheet_looks": list(character.sheet_looks),
        "sheet_expressions": list(character.sheet_expressions),
    }


def set_fields(character: Character, values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        if key == "competence":
            character.competence = Competence.model_validate(value)
        elif key in ("aliases", "sheet_looks", "sheet_expressions"):
            setattr(character, key, list(value))
        elif key == "name":
            character.name = value or character.name
        elif key in LINKED_FIELDS:
            setattr(character, key, value or None)


def given(card: Character, user: str | None) -> dict[str, Any]:
    """What a story is given of a kept card: `{{char}}` and `{{user}}`
    written out (`user`: whom the card's `{{user}}` becomes), a blank
    summary the description's first sentence and a blank voice the card's
    example messages, as a card read into a story always had them."""
    fill = macro_filler(card.name, user or None)
    tavern = card.tavern or TavernFields()
    values = fields_of(card)
    description = fill(values["full_description"]).strip()
    personality = fill(values["personality"]).strip()
    competence = dict(values["competence"])
    competence["notes"] = fill(competence.get("notes") or "")
    return {
        **values,
        "aliases": [fill(alias) for alias in values["aliases"]],
        "summary": fill(values["summary"]).strip() or first_sentence(description or personality),
        "full_description": description,
        "personality": personality,
        "voice_notes": fill(values["voice_notes"]).strip()
        or example_dialogue(fill(tavern.mes_example)),
        "competence": competence,
        "canon": fill(values["canon"]).strip(),
    }


def filled_tavern(tavern: TavernFields | None, fill: Callable[[str], str]) -> TavernFields:
    """A card's own fields with the names in, as a story's copy keeps them."""
    if tavern is None:
        return TavernFields()
    copy = tavern.model_copy(deep=True)
    for key in (
        "scenario",
        "first_mes",
        "mes_example",
        "system_prompt",
        "post_history_instructions",
        "creator_notes",
    ):
        setattr(copy, key, fill(getattr(copy, key)))
    copy.alternate_greetings = [fill(text) for text in copy.alternate_greetings]
    copy.group_only_greetings = [fill(text) for text in copy.group_only_greetings]
    return copy


def story_copy(card: Character, characters: Sequence[Character], user: str | None) -> Character:
    """A kept card as a story's character: its fields as `given`, a fresh
    id, linked to the card. Its pictures are the window's to copy in
    (`picture_mark` each, into `linked.pictures`)."""
    from sealedlore.storage import collection

    values = given(card, user)
    copy = card.model_copy(deep=True)
    copy.id = new_id()
    set_fields(copy, values)
    copy.tavern = filled_tavern(card.tavern, macro_filler(card.name, user or None))
    copy.collection = None
    copy.reference_images = []
    main = collection.main_of(card, characters)
    copy.linked = CardLink(
        card_id=card.id,
        family=main.id,
        version="" if main is card else collection.info(card).version,
        user=user or "",
        fields=values,
    )
    return copy


# A story's field given from another of the card's when the card leaves it
# blank (`given`): what to look in for the card's own `{{char}}` and `{{user}}`.
DERIVED = ("summary", "voice_notes")


def _derived_from(card: Character, key: str) -> str:
    if key == "summary":
        return card.full_description or card.personality or ""
    if key == "voice_notes":
        return card.tavern.mes_example if card.tavern else ""
    return ""


def _mine(character: Character, card: Character, theirs: Mapping[str, Any]) -> dict[str, Any]:
    """The story's fields, as compared with what the card gives: a field the
    card leaves blank, and so derives, is no change where the story has
    none either (a card kept from a story with no summary)."""
    mine = fields_of(character)
    raw = fields_of(card)
    for key in DERIVED:
        if not mine[key] and not raw[key]:
            mine[key] = theirs[key]
    return mine


def picture_mark(ref: ImageRef, data: bytes) -> PictureMark:
    return PictureMark(
        sha256=hashlib.sha256(data).hexdigest(),
        look=ref.look,
        expression=ref.expression,
        caption=ref.caption,
        framing=ref.framing,
    )


def _labels(mark: PictureMark | None) -> tuple[str, str, str, str] | None:
    if mark is None:
        return None
    return (mark.look, mark.expression, mark.caption, mark.framing)


def picture_words(mark: PictureMark | None) -> str:
    if mark is None:
        return "a picture"
    words = " · ".join(part for part in (mark.look, mark.expression) if part)
    return words or mark.caption or "a picture"


def _side(story: Any, base: Any, card: Any) -> str:
    """Which side moved: "" (none, or both to the same), the story, the
    card, or both."""
    if story == card:
        return ""
    if card == base:
        return STORY
    if story == base:
        return CARD
    return BOTH


@dataclass
class Change:
    """One difference between a story's character and their card."""

    key: str  # a field's name, PORTRAIT, "picture:<sha>" or "labels:<sha>"
    label: str
    side: str  # STORY, CARD or BOTH
    story: Any  # its value in the story (a picture's mark, or None for none)
    card: Any  # its value on the card now
    sha: str = ""

    @property
    def field(self) -> bool:
        return self.key in LINKED_FIELDS


def changes(
    character: Character,
    card: Character,
    story_marks: Sequence[PictureMark],
    card_marks: Sequence[PictureMark],
    *,
    story_unknown: bool = False,
    card_unknown: bool = False,
) -> list[Change]:
    """Every difference between a linked character in a story and their card
    now, with the side it was made on. `story_marks` and `card_marks`: each
    side's pictures, in order. `story_unknown`, `card_unknown`: a side has a
    picture whose file can't be read, so a picture missing from its marks
    may be that one: never said to be taken off there."""
    link = character.linked
    if link is None:
        return []
    base = link.fields
    theirs = given(card, link.user)
    mine = _mine(character, card, theirs)
    found: list[Change] = []
    for key in LINKED_FIELDS:
        story, now = mine[key], theirs[key]
        side = _side(story, base.get(key, now), now)
        if side:
            found.append(Change(key, FIELD_LABELS[key], side, story, now))
    s_marks = {mark.sha256: mark for mark in story_marks}
    b_marks = {mark.sha256: mark for mark in link.pictures}
    c_marks = {mark.sha256: mark for mark in card_marks}
    for sha in dict.fromkeys([*s_marks, *b_marks, *c_marks]):
        story_has, card_has = sha in s_marks, sha in c_marks
        if sha in b_marks and (
            (story_unknown and not story_has) or (card_unknown and not card_has)
        ):
            continue  # perhaps the picture whose file can't be read
        side = _side(story_has, sha in b_marks, card_has)
        mark = s_marks.get(sha) or c_marks.get(sha) or b_marks.get(sha)
        if side:
            added = (side == STORY and story_has) or (side == CARD and card_has)
            what = "A new picture" if added else "A picture taken off"
            found.append(
                Change(
                    f"picture:{sha}",
                    f"{what}: {picture_words(mark)}",
                    side,
                    s_marks.get(sha),
                    c_marks.get(sha),
                    sha,
                )
            )
        elif story_has and card_has:
            story_labels, card_labels = _labels(s_marks[sha]), _labels(c_marks[sha])
            side = _side(story_labels, _labels(b_marks.get(sha)) or card_labels, card_labels)
            if side:
                found.append(
                    Change(
                        f"labels:{sha}",
                        f"A picture's labels: {picture_words(s_marks[sha])}",
                        side,
                        s_marks[sha],
                        c_marks[sha],
                        sha,
                    )
                )
    firsts = [
        marks[0].sha256 if marks else "" for marks in (story_marks, link.pictures, card_marks)
    ]
    story_first, base_first, card_first = firsts
    # Only a change of which picture comes first: one added or taken off is
    # said as that.
    if story_first in c_marks and card_first in s_marks:
        side = _side(story_first, base_first, card_first)
        if side:
            found.append(
                Change(PORTRAIT, "Which picture is the portrait", side, story_first, card_first)
            )
    return found


def fingerprint(found: Sequence[Change]) -> str:
    """The story's own changes, in a word: kept in the story when the author
    says so, and said again once they differ."""
    mine = [(change.key, _plain(change.story)) for change in found if change.side in (STORY, BOTH)]
    if not mine:
        return ""
    return hashlib.sha256(json.dumps(mine, sort_keys=True, default=str).encode()).hexdigest()


def _plain(value: Any) -> Any:
    return value.model_dump() if isinstance(value, PictureMark) else value


def in_step(
    character: Character,
    card: Character,
    story_marks: Sequence[PictureMark],
    card_marks: Sequence[PictureMark],
    *,
    story_unknown: bool = False,
    card_unknown: bool = False,
) -> None:
    """What both sides came to by themselves becomes what the story was
    given: a field changed to the same thing on each side is no change, a
    picture both took off or both have is in step, and the portrait both
    show first is. A picture only one side changed keeps its mark, so the
    change stays that side's."""
    link = character.linked
    if link is None:
        return
    theirs = given(card, link.user)
    mine = _mine(character, card, theirs)
    for key in LINKED_FIELDS:
        if mine[key] == theirs[key]:
            link.fields[key] = mine[key]
    s_marks = {mark.sha256: mark for mark in story_marks}
    c_marks = {mark.sha256: mark for mark in card_marks}
    kept = []
    for mark in link.pictures:
        both_lack = mark.sha256 not in s_marks and mark.sha256 not in c_marks
        if both_lack and not (story_unknown or card_unknown):
            continue  # taken off on both sides
        kept.append(mark)
    known = {mark.sha256 for mark in kept}
    for sha, mark in s_marks.items():
        if sha in c_marks and sha not in known:
            kept.append(mark)  # given on both sides since
    for index, mark in enumerate(kept):
        sha = mark.sha256
        if sha in s_marks and sha in c_marks and _labels(s_marks[sha]) == _labels(c_marks[sha]):
            kept[index] = s_marks[sha]
    story_first = story_marks[0].sha256 if story_marks else ""
    card_first = card_marks[0].sha256 if card_marks else ""
    if story_first and story_first == card_first:
        first = next((mark for mark in kept if mark.sha256 == story_first), None)
        if first is not None:
            kept.remove(first)
            kept.insert(0, first)
    link.pictures = kept


# --- the names, written back as the card has them ---------------------------------------


# The macros a card writes for the names, every way `macro_filler` reads them.
_CHAR_MACRO = re.compile(r"\{\{\s*char\s*\}\}|<BOT>", re.I)
_USER_MACRO = re.compile(r"\{\{\s*user\s*\}\}|<USER>", re.I)


def unfilled(value: Any, raw: Any, name: str, user: str) -> Any:
    """A story's text going back to a card that writes `{{char}}` or
    `{{user}}` (or `{{Char}}`, `<BOT>`…) in `raw`, the card's own text it
    came from: the names put back as macros where the card wrote them, the
    longer name first, so "Jane" never eats "Jane Moss", and a name the card
    doesn't write as a macro is kept as written."""
    if isinstance(value, str) and isinstance(raw, str):
        names = []
        if name:
            names.append((name, "{{char}}", bool(_CHAR_MACRO.search(raw))))
        if user and user != name:
            names.append((user, "{{user}}", bool(_USER_MACRO.search(raw))))
        held: dict[str, str] = {}
        for index, (who, macro, put_back) in enumerate(sorted(names, key=lambda n: -len(n[0]))):
            stand_in = f"\x00{index}\x00"
            value = re.sub(
                rf"(?<!\w){re.escape(who)}(?!\w)", macro if put_back else stand_in, value
            )
            if not put_back:
                held[stand_in] = who
        for stand_in, who in held.items():
            value = value.replace(stand_in, who)
        return value
    if isinstance(value, dict) and isinstance(raw, dict):
        return {key: unfilled(item, raw.get(key), name, user) for key, item in value.items()}
    if isinstance(value, list) and isinstance(raw, list):
        joined = "\n".join(str(item) for item in raw)
        return [unfilled(item, joined, name, user) for item in value]
    return value


def _with_user(value: Any, user: str) -> Any:
    if isinstance(value, str):
        return _USER_MACRO.sub(lambda _m: user, value)
    if isinstance(value, list):
        return [_with_user(item, user) for item in value]
    if isinstance(value, dict):
        return {key: _with_user(item, user) for key, item in value.items()}
    return value


def name_the_user(character: Character, user: str) -> bool:
    """A character put into a story before anyone was played: their card's
    `{{user}}` written out as whom the author plays now, in the story's fields
    and in what the story was given alike (so it is no change). Whether
    anything changed."""
    link = character.linked
    if link is None or link.user or not user or user == character.name:
        return False
    set_fields(character, _with_user(fields_of(character), user))
    link.fields = _with_user(link.fields, user)
    if character.tavern is not None:
        character.tavern = TavernFields.model_validate(
            _with_user(character.tavern.model_dump(), user)
        )
    link.user = user
    return True


def _back(card: Character, key: str, value: Any, user: str) -> None:
    """A story's value of `key` onto the card, names back as the card has
    them; a field the card left blank looks to the text it was given from."""
    raw = fields_of(card)[key] or _derived_from(card, key)
    set_fields(card, {key: unfilled(value, raw, card.name, user)})


# --- either way --------------------------------------------------------------------------


Copier = Callable[[ImageRef], "ImageRef | None"]


def _move_first(refs: list[ImageRef], ref: ImageRef | None) -> None:
    if ref is not None and ref in refs:
        refs.remove(ref)
        refs.insert(0, ref)


def _mark_of(link: CardLink, sha: str) -> PictureMark | None:
    return next((mark for mark in link.pictures if mark.sha256 == sha), None)


def _is_first(link: CardLink, sha: str) -> bool:
    return bool(link.pictures) and link.pictures[0].sha256 == sha


def _set_mark(link: CardLink, mark: PictureMark | None, sha: str, first: bool = False) -> None:
    link.pictures = [other for other in link.pictures if other.sha256 != sha]
    if mark is not None:
        link.pictures.insert(0 if first else len(link.pictures), mark)


def _relabel(ref: ImageRef, mark: PictureMark) -> None:
    ref.look, ref.expression = mark.look, mark.expression
    ref.caption, ref.framing = mark.caption, mark.framing


def save_to_card(
    character: Character,
    card: Character,
    chosen: Sequence[Change],
    *,
    story_refs: Mapping[str, ImageRef],
    card_refs: Mapping[str, ImageRef],
    copy: Copier,
) -> None:
    """The story's changes `chosen` written onto the card. `story_refs` and
    `card_refs`: each side's pictures by sha256; `copy` puts one of the
    story's pictures among the card's files and gives its new ref."""
    link = character.linked
    if link is None:
        return
    story_order = list(story_refs)
    before = given(card, link.user)
    for change in chosen:
        if change.field:
            _back(card, change.key, change.story, link.user)
            link.fields[change.key] = change.story
        elif change.key.startswith("picture:"):
            sha = change.sha
            if change.story is not None and sha not in card_refs:
                new = copy(story_refs[sha])
                if new is None:
                    continue
                first = bool(story_order) and story_order[0] == sha
                card.reference_images.insert(0 if first else len(card.reference_images), new)
                _set_mark(link, change.story, sha, first)
            elif change.story is None and sha in card_refs:
                if card_refs[sha] in card.reference_images:
                    card.reference_images.remove(card_refs[sha])
                _set_mark(link, None, sha)
        elif change.key.startswith("labels:") and change.sha in card_refs:
            _relabel(card_refs[change.sha], change.story)
            _set_mark(link, change.story, change.sha, _is_first(link, change.sha))
        elif change.key == PORTRAIT:
            _move_first(card.reference_images, card_refs.get(change.story))
            mark = _mark_of(link, change.story)
            if mark is not None:
                _set_mark(link, mark, change.story, first=True)
    _follow_derived(character, card, before)


def _follow_derived(character: Character, card: Character, before: Mapping[str, Any]) -> None:
    """A summary (or voice) the card derives from what was just saved to it,
    and the story never changed, follows in the story too: else the save
    itself read as a change on the card."""
    link = character.linked
    after = given(card, link.user)
    mine = fields_of(character)
    raw = fields_of(card)
    for key in DERIVED:
        untouched = mine[key] == link.fields.get(key, before[key]) == before[key]
        if not raw[key] and untouched and after[key] != before[key]:
            set_fields(character, {key: after[key]})
            link.fields[key] = after[key]


def take_from_card(
    character: Character,
    card: Character,
    chosen: Sequence[Change],
    *,
    story_refs: Mapping[str, ImageRef],
    card_refs: Mapping[str, ImageRef],
    copy: Copier,
) -> None:
    """The card's changes `chosen` taken into the story: as `save_to_card`,
    the other way. `copy` puts one of the card's pictures among the story's
    files."""
    link = character.linked
    if link is None:
        return
    card_order = list(card_refs)
    for change in chosen:
        if change.field:
            set_fields(character, {change.key: change.card})
            link.fields[change.key] = change.card
        elif change.key.startswith("picture:"):
            sha = change.sha
            if change.card is not None and sha not in story_refs:
                new = copy(card_refs[sha])
                if new is None:
                    continue
                first = bool(card_order) and card_order[0] == sha
                refs = character.reference_images
                refs.insert(0 if first else len(refs), new)
                _set_mark(link, change.card, sha, first)
            elif change.card is None and sha in story_refs:
                if story_refs[sha] in character.reference_images:
                    character.reference_images.remove(story_refs[sha])
                _set_mark(link, None, sha)
        elif change.key.startswith("labels:") and change.sha in story_refs:
            _relabel(story_refs[change.sha], change.card)
            _set_mark(link, change.card, change.sha, _is_first(link, change.sha))
        elif change.key == PORTRAIT:
            _move_first(character.reference_images, story_refs.get(change.card))
            mark = _mark_of(link, change.card)
            if mark is not None:
                _set_mark(link, mark, change.card, first=True)


def version_from_story(
    character: Character, card: Character, characters: Sequence[Character], *, name: str
) -> Character:
    """A new version of the card: the character as the story has them, the
    card's own fields for a chat beside, `{{char}}` and `{{user}}` put back
    where the card writes them. Its pictures are the window's to copy in."""
    from sealedlore.storage import collection

    link = character.linked
    user = link.user if link is not None else ""
    version = card.model_copy(deep=True)
    version.id = new_id()
    # The card's own text wherever the story has what the card gives it
    # (a blank voice stays blank, its examples its own); the story's,
    # names back as macros, where it changed.
    theirs = given(card, user)
    for key, value in _mine(character, card, theirs).items():
        if value != theirs[key]:
            _back(version, key, value, user)
    version.reference_images = []
    main = collection.main_of(card, characters)
    version.collection = CollectionInfo(family=main.id, version=name, came_from="a story")
    return version


def kept_from_story(character: Character, came_from: str) -> Character:
    """A story's character kept among the author's own (Save to Your
    characters): a whole copy under a fresh id. Its pictures are the
    window's to copy in."""
    kept = character.model_copy(deep=True)
    kept.id = new_id()
    kept.reference_images = []
    kept.linked = None
    kept.plot_hidden = False
    kept.origin = "author"
    kept.collection = CollectionInfo(came_from=came_from)
    if kept.tavern is None:
        kept.tavern = TavernFields()
    return kept


def relink(
    character: Character, card: Character, marks: Sequence[PictureMark], *, main_id: str
) -> None:
    """The story's character in step with `card` as it stands (a new
    version made from them, or a card kept from them)."""
    old = character.linked
    character.linked = CardLink(
        card_id=card.id,
        family=main_id,
        version=card.collection.version if card.collection else "",
        user=old.user if old is not None else "",
        fields=fields_of(character),
        pictures=list(marks),
    )


def summary(found: Sequence[Change], link: CardLink) -> str:
    """The changes in a line: the story's, then the card's."""

    def words(side_of: tuple[str, ...]) -> str:
        fields = [c.label.lower() for c in found if c.side in side_of and c.field]
        pictures = sum(1 for c in found if c.side in side_of and not c.field and c.key != PORTRAIT)
        if any(c.side in side_of and c.key == PORTRAIT for c in found):
            fields.append("the portrait")
        if pictures:
            fields.append(f"{pictures} picture{'' if pictures == 1 else 's'}")
        return ", ".join(fields)

    parts = []
    mine = words((STORY, BOTH))
    if mine:
        kept = link.kept_here and link.kept_here == fingerprint(found)
        parts.append(f"Changed in this story{' (kept here)' if kept else ''}: {mine}.")
    theirs = words((CARD, BOTH))
    if theirs:
        parts.append(f"Changed on the card since: {theirs}.")
    return " ".join(parts)
