"""Which picture each character is shown with after a passage: one small call
after it (engine/session_looks.py), its request and its reply. Pure.

The model is given the author's turn and the passage, and, for each
character it may concern (in the scene, or named in the passage, with more
than one picture), their pictures by handle with their labels and the one
they are shown with now. It answers with a handle for each and, when none
fits well, the look and expression that would ("missing"), which the People
page offers to draw. A look that lasts (clothes, armour, an injury) holds
unless the passage changes it; the expression or pose is the moment's. Both
are said in the request and are the model's call. Nothing else of the story
is sent: the pictures are known only by their labels.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.looks import look_words
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.validators import mentions
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character


@dataclass(frozen=True)
class LookOption:
    handle: str  # "L1": the model's name for the picture, never shown elsewhere
    ref_id: str
    words: str


@dataclass(frozen=True)
class LookSubject:
    character_id: str
    name: str
    options: tuple[LookOption, ...]
    now: str  # the handle of the picture shown now


@dataclass(frozen=True)
class LookPick:
    character_id: str
    ref_id: str
    # The look and expression that would fit when none did, as "look · expression".
    missing: str = ""


def look_subjects(
    characters: Sequence[Character],
    looks: Mapping[str, str],
    present_ids: Collection[str],
    passage: str,
) -> list[LookSubject]:
    """Those a pick may concern: with more than one picture, and in the scene
    before the passage or named in it (someone coming in). Handles run on
    across them, so each names one picture."""
    subjects: list[LookSubject] = []
    number = 0
    for character in characters:
        refs = character.reference_images
        if len(refs) < 2:
            continue
        if character.id not in present_ids and not mentions(passage, character):
            continue
        options = []
        now = ""
        chosen = looks.get(character.id) or refs[0].id
        for index, ref in enumerate(refs, 1):
            number += 1
            words = look_words(ref, index)
            if index == 1 and not (ref.look or ref.expression):
                words = f"{words} (their portrait)" if words != "portrait" else "their portrait"
            options.append(LookOption(f"L{number}", ref.id, words))
            if ref.id == chosen:
                now = f"L{number}"
        subjects.append(LookSubject(character.id, character.name, tuple(options), now))
    return subjects


def render_subjects(subjects: Sequence[LookSubject]) -> str:
    blocks = []
    for subject in subjects:
        lines = [f"{subject.name} (shown now as {subject.now})"]
        lines += [f"- {option.handle}: {option.words}" for option in subject.options]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_pick_messages(
    subjects: Sequence[LookSubject],
    *,
    author_turn: str,
    passage: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The instructions are a text (`looks.pick.system`); the headings are
    the request's layout, as the scene read's are."""
    body = ["THE PEOPLE AND THEIR PICTURES:\n\n" + render_subjects(subjects)]
    if author_turn.strip():
        body.append("THE AUTHOR'S TURN:\n\n" + author_turn.strip())
    body.append("THE LATEST PASSAGE:\n\n" + passage.strip())
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["looks.pick.system"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def parse_pick(text: str, subjects: Sequence[LookSubject]) -> list[LookPick]:
    """The reply's picks, each held to its own character's pictures: a handle
    that isn't one of theirs, or a name that isn't one of them, is left out
    (they keep what they had). ValueError when the reply holds no JSON."""
    data = extract_json(text, dict, "choice of pictures")
    rows = data.get("looks")
    if not isinstance(rows, list):
        raise ValueError("the reply held no list of looks")
    by_name = {subject.name.lower(): subject for subject in subjects}
    picks: dict[str, LookPick] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        subject = by_name.get(str(row.get("name") or "").strip().lower())
        if subject is None:
            continue
        handle = str(row.get("picture") or "").strip().upper()
        option = next((o for o in subject.options if o.handle == handle), None)
        if option is None:
            continue
        picks[subject.character_id] = LookPick(
            subject.character_id, option.ref_id, _missing(row.get("missing"))
        )
    return list(picks.values())


def _missing(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    words = " ".join(value.split()).strip(" .")
    return words[:120] if words.lower() not in ("", "none", "n/a", "nothing") else ""
