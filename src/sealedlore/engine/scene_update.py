"""Reading the scene out of the prose.

Pure: the prompts, the parsing, and the resolution of names.
`StorySession.update_scene_after_turn` and `StorySession.suggest_scene` make
the calls.

Why the scene reads itself. Presence used to be a roster the author kept by
hand, rendered to the model as a hard constraint. Replayed over 299 logged
turns it changed three times, and every alert meant to keep it honest was
wrong: pattern matching cannot tell "Hollis is here" from "she thought of Hollis",
a labelled cutaway, or "Ruiz declared Jane fit". Under the arrival rule the
question is semantic — did this person come in, reach in from outside, or act
on what they could not know? — so a small model answers it after each passage,
and the card follows the story instead of the author having to.

Two reads:

- The per-turn read (`build_read_messages`, `parse_delta`): what the latest
  passage changed, against the card as it stood. A delta rather than a fresh
  reading, so an author-set location survives a passage that doesn't move,
  and someone silent for a turn is not dropped. When the card has never kept
  anyone but the cast (`SceneState.tracked`), it also asks who is here
  already, over a few earlier passages, to start the record.
- The full read (`build_scene_messages`, `parse_scene`): the author's
  "Update from the story", reading the scene fresh from the recent prose.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.scene_state import diff, same_person
from sealedlore.engine.validators import locate_quote, mentions, name_forms, opens_elsewhere
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.scene import (
    OffstageCharacter,
    Privacy,
    SceneShortcut,
    SceneState,
    ShortcutKind,
)
from sealedlore.models.story import Perspective

# How much earlier prose the first read of a card gets, to see who is already
# in the room. The same eight passages the full read uses.
SEED_CONTEXT_NODES = 8
# The read is a short JSON object, but a reasoning model spends tokens before
# it writes one; a tight cap would cut the reply off mid-object. Live, 1500
# left glm-5.3-flash empty 5 times in 6; 4000 answered every time.
READ_MAX_TOKENS = 4000

SHORTCUT_KINDS: tuple[ShortcutKind, ...] = (
    "already_there",
    "perceived_from_outside",
    "knew_without_being_told",
)


# --- names ------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """A written name, matched to the cast (an id) or to a card on file (its name)."""

    name: str
    character_id: str | None = None


def resolve_name(
    name: str, *, cast: Sequence[Character], supporting: Sequence[Character] = ()
) -> Resolved | None:
    """Who a name the model wrote refers to. Never guesses between two people.

    Exact names and aliases first; then a distinctive part ("Moss", "Hollis"),
    but only when exactly one person answers to it. A name nobody on file
    answers to is kept as written: the story's own people are real people in
    the scene, and dropping them is how the roster used to go blind.
    """
    cleaned = " ".join(name.split())
    if not cleaned:
        return None
    lowered = cleaned.lower()
    for character in cast:
        if lowered in {character.name.lower(), *(a.lower() for a in character.aliases)}:
            return Resolved(character.name, character.id)
    for character in supporting:
        if lowered in {character.name.lower(), *(a.lower() for a in character.aliases)}:
            return Resolved(character.name)
    cast_hits = [character for character in cast if same_person(cleaned, character)]
    if len(cast_hits) == 1:
        return Resolved(cast_hits[0].name, cast_hits[0].id)
    card_hits = [character for character in supporting if same_person(cleaned, character)]
    if not cast_hits and len(card_hits) == 1:
        return Resolved(card_hits[0].name)
    return Resolved(cleaned)


# The read is told to leave the unnamed out and still writes "Docking crew
# (unnamed)", "Young guard", "Pack mule". A proper name has a capital
# after its first word ("Rosa Delgado", "Private Kim") or is one word ("Wade");
# a description is lower-case after its first word.
_UNNAMED = re.compile(r"\bunnamed\b|^(?:an?|the|some|two|three|several)\s", re.IGNORECASE)


def looks_like_a_name(name: str) -> bool:
    """Whether a name the read gave for someone not on file is a name at all."""
    if _UNNAMED.search(name) or not name[:1].isupper():
        return False
    words = name.split()
    return len(words) == 1 or any(word[:1].isupper() for word in words[1:])


# An "already there" whose own sentence shows the arrival. Live, the read
# flagged "[the constable] appeared from the security office at a dead run"
# and "It was [the first officer] who came to find her" despite being told
# that is shown.
_SHOWN_ARRIVING = re.compile(
    r"\b(?:arrived|appeared|entered|emerged|found|followed|joined|came|walked|stepped|strode"
    r"|ran|hurried|burst|approached|reached|caught up|turned up|showed up|slipped in)\b",
    re.IGNORECASE,
)


# Every model tried put someone heard over a channel into the room, and said
# so in its own note: "+[an officer] (over security channel)", "+[another]
# (tracking on sensors)". An arrival whose "how" is a channel is not an arrival.
_REMOTE_HOW = re.compile(
    r"\b(?:comm|comms|commlink|channel|sensors?|viewscreen|screen|radio|intercom"
    r"|hologram|transmission|message)\b",
    re.IGNORECASE,
)
# Coming in person, whatever else the "how" says. Live (GLM 5.3): the commander
# "came down from [the command centre] by lift" was read as a channel, left off
# the roster,
# and flagged three passages later as already there. "Arrived" was added when
# someone who "arrived two minutes after [an officer's] comm" was dropped for
# the call that had brought them. Being somewhere is not coming in ("already
# in [their own room], having received the comm" was someone the scene had
# not reached), so only words for the coming itself belong here.
_IN_PERSON = re.compile(
    r"\b(?:came (?:down|up|across|back|over to)|arrived|walked|stepped|entered|crossed|strode"
    r"|ran|hurried|rushed|burst|lift|elevator|doors?|in person)\b",
    re.IGNORECASE,
)


# Double-quoted speech, straight or curly: what someone says about a person
# doesn't put them in the room.
_SPEECH = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")


def _sentence_of(text: str, start: int, end: int) -> str:
    """The whole sentence (or sentences) holding text[start:end]."""
    left = max(text.rfind(stop, 0, start) for stop in (". ", "! ", "? ", "\n")) + 1
    ends = [i for i in (text.find(stop, end) for stop in (".", "!", "?", "\n")) if i >= 0]
    return text[left : min(ends) + 1 if ends else len(text)]


def _remote(how: str) -> bool:
    """Whether an arrival's "how" is a channel rather than someone coming in."""
    return bool(_REMOTE_HOW.search(how)) and not _IN_PERSON.search(how)


# Following the whole story, the read still called a command centre tracking
# on sensors and security on its own channel "reaching in", on every model
# tried. A quote that is someone watching by their own means, with nothing
# furtive about it, is dropped there; staying with the character, it stands.
_BY_OWN_MEANS = re.compile(
    r"\b(?:sensors?|channel|comm|comms|monitors?|monitoring|viewscreen|screen"
    r"|readouts?|scanners?|feed|reports?|intercom|tracking|windows?"
    r"|from (?:the |her |his |their |my )?(?:kitchen|house|porch|doorway|door|yard|inside))\b",
    re.IGNORECASE,
)
# Live (Doomsville): Ruth watching her own yard "from the kitchen window" was
# called reaching in; so was Elena's "She'd checked the cot building once in
# the night" (backstory, told in past perfect) as already being there; and a
# gate guard's "some silent exchange passing between them" was pinned on
# Marcus, whom it doesn't name. A read's arrival for someone only *mentioned*
# ("Marcus decides who settles") put him in the room and then accused him.
_PAST_PERFECT = re.compile(
    r"(?:\b(?:he|she|they|i|we|you|it)'d|\bhad)\s+(?:been\s+)?"
    r"(?:\w+(?:ed|en|t|ne|wn)|come|become|run|heard|said|stood|made|held|told|led|fled|hid)\b",
    re.IGNORECASE,
)
_ONLY_MENTIONED = re.compile(
    r"\b(?:mention\w*|referred|referenced|talked about|spoken of|spoke of|named|"
    r"only in (?:speech|conversation))\b",
    re.IGNORECASE,
)
# Live (Doomsville, v2): Cal telling Jane "There was a woman there. Elena.
# She was a doctor." put Elena in the room, and a rider's "Marcus Webb sent
# me." had Marcus reaching in. A name only ever inside someone's speech is
# talked about, unless the speaker is naming themselves: "Name's Evan" from a
# rifleman who had been talking to Jane for a turn is a stranger with a name
# at last, while "This is Gus" over the comm is still Gus reaching in.
_SELF_NAMED_BEFORE = re.compile(
    r"(?:\bthis is|\bit['’]s|\bit is|\bi['’]m|\bi am|\bname['’]s|\bname is|\bcall me)"
    r"\s+(?:[\w’']+\s+)?$",
    re.IGNORECASE,
)
_SELF_NAMED_AFTER = re.compile(r"^[\w’' ]{0,30}?,?\s+here\b", re.IGNORECASE)


# A single quote that starts a word without opening speech: "seen 'em coming".
_ELISION = re.compile(r"['‘’](?:em|til|cause|bout|round|n|tis|twas)\b", re.IGNORECASE)


def _in_speech(text: str, position: int) -> bool:
    """Whether `position` is inside quoted speech in its paragraph, in double or
    single quotes. A single quote between letters is an apostrophe ("Name's"),
    and one after a letter closes ("the Calloways'"), so only one at the start
    of a word opens."""
    # From the paragraph's start, not the line's: speech that wraps over a
    # line break is still speech.
    line = text[max(text.rfind("\n\n", 0, position), 0) : position]
    if line.count('"') % 2 == 1 or line.count("“") > line.count("”"):
        return True
    depth = 0
    for index, char in enumerate(line):
        if char not in "'‘’":
            continue
        before = line[index - 1] if index else " "
        after = line[index + 1] if index + 1 < len(line) else " "
        if before.isalnum() and after.isalnum():
            continue
        if not before.isalnum() and before not in ".,!?;:" and not _ELISION.match(line, index):
            depth += 1
        else:
            depth = max(0, depth - 1)
    return depth > 0


def _named_in_speech(text: str, start: int, end: int, person: Character) -> str | None:
    """How text[start:end] names `person`, if only ever inside someone's speech.

    "self" when a speaker names themselves, "talked about" otherwise, None when
    the name appears in the narration (or not at all).
    """
    positions: list[tuple[int, int]] = []
    for name in name_forms(person):
        for match in re.finditer(rf"\b{re.escape(name)}\b", text[start:end], re.IGNORECASE):
            positions.append((start + match.start(), start + match.end()))
    if not positions or not all(_in_speech(text, left) for left, _ in positions):
        return None
    for left, right in positions:
        line_start = text.rfind("\n", 0, left) + 1
        if _SELF_NAMED_BEFORE.search(text[line_start:left]) or _SELF_NAMED_AFTER.match(
            text[right : right + 40]
        ):
            return "self"
    return "talked about"


_FURTIVE = re.compile(
    r"\b(?:overh(?:ear|eard|earing)|eavesdrop\w*|listening in|listened in|had been listening"
    r"|listening to (?:every|each|all|the whole)|heard (?:every|each|all|the whole)"
    r"|the whole time|all along|(?:half-open|open|closed|thin) (?:door|wall|window)"
    r"|outside the (?:door|room|window|tent|wall)|through the (?:door|wall|window)"
    r"|secretly|pretend\w*|unnoticed|unseen|without (?:anyone|them|him|her) knowing)\b",
    re.IGNORECASE,
)


def strict_perspective(perspective: Perspective, held_id: str | None) -> bool:
    """Whether nothing the held character can't perceive may be shown."""
    return perspective == "with_character" and held_id is not None


def _people_on_file(cast: Sequence[Character], supporting: Sequence[Character]) -> str:
    def entry(character: Character) -> str:
        aliases = [alias for alias in character.aliases if alias.strip()]
        return character.name + (f" (also: {', '.join(aliases)})" if aliases else "")

    return "; ".join(entry(character) for character in (*cast, *supporting)) or "(nobody)"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned and cleaned.lower() not in {"null", "none", "unchanged"} else None


def _privacy(value: object) -> Privacy | None:
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    return lowered if lowered in ("public", "private") else None  # type: ignore[return-value]


# --- the card ---------------------------------------------------------------


def render_card(
    scene: SceneState,
    *,
    cast: Sequence[Character],
    held_id: str | None,
) -> str:
    """The scene as the read sees it: short, and in the same terms it answers in."""
    cast_by_id = {character.id: character for character in cast}
    lines = [
        f"Location: {scene.location or '(not recorded)'}",
        f"Time: {scene.time_of_day or '(not recorded)'}",
        f"Situation: {scene.situation or '(not recorded)'}",
        f"Privacy: {scene.privacy or '(not recorded)'}",
    ]
    here: list[str] = []
    for character_id in scene.present_character_ids:
        character = cast_by_id.get(character_id)
        if character is None:
            continue
        here.append(
            f"{character.name} (the author's character)"
            if character_id == held_id
            else character.name
        )
    here.extend(scene.present_others)
    lines.append(f"In the scene: {', '.join(here) or '(nobody recorded)'}")
    notes = {entry.character_id: entry.note for entry in scene.offstage_but_nearby}
    away = [
        character.name + (f" ({notes[character.id]})" if character.id in notes else "")
        for character in cast
        if character.id not in scene.present_character_ids
    ]
    if away:
        lines.append(f"Not in the scene: {', '.join(away)}")
    return "\n".join(lines)


# --- the per-turn read ------------------------------------------------------


def build_read_messages(
    *,
    scene: SceneState,
    cast: Sequence[Character],
    supporting: Sequence[Character],
    held: Character | None,
    author_turn: str,
    passage: str,
    earlier: str = "",
    perspective: Perspective = "whole_story",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The per-turn read. `earlier` is set only for a card that isn't tracked yet.

    Staying with a character needs a character: with nobody held, the story
    follows the scene as a whole, and so does the read.
    """
    focus = (
        texts.fill("scene_read.focus_held", name=held.name)
        if held is not None
        else texts["scene_read.focus_none"]
    )
    if strict_perspective(perspective, held.id if held else None):
        assert held is not None
        cutaway = texts.fill("scene_read.cutaway_strict", name=held.name)
        channels = texts.fill("scene_read.channels_strict", name=held.name)
    else:
        cutaway, channels = texts["scene_read.cutaway_whole"], texts["scene_read.channels_whole"]
    system = texts.fill("scene_read.system", focus=focus, cutaway=cutaway, channels=channels)
    seeding = not scene.tracked
    if seeding:
        system += texts["scene_read.seed"]

    body = [
        "THE CARD, before the latest passage:\n"
        + render_card(scene, cast=cast, held_id=held.id if held else None),
        "PEOPLE ON FILE (spell them this way): " + _people_on_file(cast, supporting),
    ]
    if seeding and earlier.strip():
        body.append("EARLIER PASSAGES, for context only:\n\n" + earlier.strip())
    if author_turn.strip():
        body.append("THE AUTHOR'S TURN:\n\n" + author_turn.strip())
    body.append("THE LATEST PASSAGE:\n\n" + passage.strip())
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


@dataclass
class SceneDelta:
    """What the read found the latest passage changed, names resolved."""

    location: str | None = None
    time_of_day: str | None = None
    situation: str | None = None
    privacy: Privacy | None = None
    arrived: list[Resolved] = field(default_factory=list)
    left: list[Resolved] = field(default_factory=list)
    # Everyone in the scene, for a card being tracked for the first time.
    # None when the read wasn't asked for it.
    already_here: list[Resolved] | None = None
    closed: bool = False
    gist: str | None = None
    shortcuts: list[SceneShortcut] = field(default_factory=list)
    # How each person came or went, keyed by lower-cased resolved name.
    how: dict[str, str] = field(default_factory=dict)

    def unnamed_arrivals(
        self, texts: Sequence[str], *, people: Sequence[Character]
    ) -> list[Resolved]:
        """Arrivals no text names, removed from the delta and returned.

        The read is told to leave out the unnamed, but it matches them to a
        card instead. Live (plain Doomsville, GLM 5.3): "a girl, maybe
        sixteen" at the farmhouse window went on the roster as Elena Ruiz, and
        the next passage, reading the roster, made her Elena, "mid-twenties".
        """
        by_id = {character.id: character for character in people}
        by_name = {character.name.lower(): character for character in people}
        dropped: list[Resolved] = []
        kept: list[Resolved] = []
        for person in self.arrived:
            card = by_id.get(person.character_id or "") or by_name.get(person.name.lower())
            character = card or Character(id="", name=person.name)
            if any(mentions(text, character) for text in texts if text):
                kept.append(person)
            else:
                dropped.append(person)
        self.arrived = kept
        return dropped

    def credible_shortcuts(
        self,
        scene: SceneState,
        *,
        strict: bool = False,
        cast: Sequence[Character] = (),
        passage: str = "",
        introduced: Sequence[Character] = (),
        previous: str = "",
    ) -> list[SceneShortcut]:
        """The shortcuts worth accusing the storyteller of, against the card it read.

        A shortcut is a warning over the passage, and a wrong one is the noise
        this whole mechanism replaced. Replayed live, the read called people
        "already there" when the scene had just moved to where they were
        (a woman in the shelter the held character ran to), when their own sentence showed them
        arriving, and on a card that didn't yet know who was in the room. None
        of those is left standing, whatever the read says; nor is a shortcut by
        someone with no name, since the world's own people act freely.

        `strict` is a story that stays with the author's character. Otherwise
        watching or hearing by one's own means (sensors, one's own channel) is
        the story being told, unless the sentence says it was furtive.

        `passage` lets the quote be judged where it sits (inside speech, or in
        a cutaway paragraph); `introduced` is whoever a directed event is
        bringing into the story this passage, who is meant to turn up.

        `previous` is the passage before this one. Someone it already names is
        not "already there" by shortcut: the card missed them, not the
        storyteller. Live: the commander walked in, the read's arrival was lost, and
        three passages later he was flagged for standing where he had stood
        all along.
        """
        moved = self.closed or bool(
            self.location and scene.location and self.location.lower() != scene.location.lower()
        )
        # A card that has kept only the cast can't say who was already in the
        # room — that is why it is being read. Live, the doctor at his own
        # patient's bedside was "already there" on such a read.
        moved = moved or not scene.tracked
        cast_by_id = {character.id: character for character in cast}
        in_scene = [
            cast_by_id[character_id]
            for character_id in scene.present_character_ids
            if character_id in cast_by_id
        ]
        kept: list[SceneShortcut] = []
        for shortcut in self.shortcuts:
            person = cast_by_id.get(shortcut.character_id or "") or Character(
                id="", name=shortcut.name
            )
            if shortcut.kind == "already_there" and (
                moved
                or (previous and mentions(_SPEECH.sub(" ", previous), person))
                or _SHOWN_ARRIVING.search(shortcut.quote)
                or (_PAST_PERFECT.search(shortcut.quote) and not _FURTIVE.search(shortcut.quote))
                or any(
                    character.id == shortcut.character_id or same_person(shortcut.name, character)
                    for character in introduced
                )
            ):
                continue
            # Someone the card already has in the room can't be put there by
            # shortcut. Live: Mara in her own farmhouse doorway, Sarah setting
            # down her cup at her own table, both on the roster.
            if shortcut.kind != "knew_without_being_told" and (
                (shortcut.character_id and shortcut.character_id in scene.present_character_ids)
                or any(same_person(other, person) for other in scene.present_others)
            ):
                continue
            located = locate_quote(passage, shortcut.quote) if passage else None
            text, (start, end) = (
                (passage, located) if located else (shortcut.quote, (0, len(shortcut.quote)))
            )
            spoken = _named_in_speech(text, start, end, person)
            if spoken == "talked about" or (spoken == "self" and shortcut.kind == "already_there"):
                continue
            # Following the whole story, a cutaway is the story being told.
            # Live: "Five miles north, Marcus Webb stood at the gate…" A
            # furtive one is not: "In the corridor outside, Marcus had been
            # standing by the half-open door the whole time, listening" was
            # dropped here for every model that rightly reported it.
            if (
                not strict
                and located
                and opens_elsewhere(passage, start, in_scene)
                and not _FURTIVE.search(shortcut.quote)
            ):
                continue
            if _ONLY_MENTIONED.search(self.how.get(shortcut.name.lower(), "")):
                continue
            # A sentence that doesn't name them is the read's guess at who. The
            # quote may be a clause of it: "who sat in the corner chair … as he
            # had all along" from "She glanced at Marcus, who sat…" (GLM 5.3).
            if not mentions(_sentence_of(text, start, end), person):
                continue
            # The rule is about the named, not the world's own people.
            if shortcut.character_id is None and not looks_like_a_name(shortcut.name):
                continue
            # Nor is someone the storyteller has just named in a crowd. Live: in
            # a busy mess hall, "The nearest man … 'Reyes. I fix what you break'"
            # was flagged as already there.
            if (
                shortcut.kind == "already_there"
                and shortcut.character_id is None
                and scene.privacy == "public"
                and not (previous and mentions(previous, person))
            ):
                continue
            if (
                not strict
                and shortcut.kind == "perceived_from_outside"
                and _BY_OWN_MEANS.search(shortcut.quote)
                and not _FURTIVE.search(shortcut.quote)
            ):
                continue
            kept.append(shortcut)
        return kept

    def applied_to(
        self,
        scene: SceneState,
        *,
        cast: Sequence[Character],
        held_id: str | None,
        node_id: str,
    ) -> SceneState:
        """A copy of `scene` with this delta applied, stamped as the story's.

        The author's character is never removed: they are the scene. Everyone
        else follows the prose.
        """
        cast_by_id = {character.id: character for character in cast}
        after = scene.model_copy(deep=True)
        if self.closed:
            # A new scene: what was true of the old room isn't of this one.
            after.situation = None
            after.privacy = None
        for name in ("location", "time_of_day", "situation", "privacy"):
            value = getattr(self, name)
            if value:
                setattr(after, name, value)

        if self.already_here is not None:
            after.present_character_ids = []
            after.present_others = []
            for person in self.already_here:
                _add(after, person)
        for person in self.arrived:
            # Talked about is not here (see _ONLY_MENTIONED).
            if _ONLY_MENTIONED.search(self.how.get(person.name.lower(), "")):
                continue
            _add(after, person)
        for person in self.left:
            if person.character_id is not None and person.character_id == held_id:
                continue
            _remove(after, person)
        if held_id and held_id in cast_by_id and held_id not in after.present_character_ids:
            after.present_character_ids.insert(0, held_id)

        present = set(after.present_character_ids)
        after.offstage_but_nearby = [
            entry for entry in after.offstage_but_nearby if entry.character_id not in present
        ]
        after.tracked = True
        after.source = "story"
        after.set_at_node_id = node_id
        after.changes = diff(scene, after, cast_by_id, how=self.how)
        return after


def _add(scene: SceneState, person: Resolved) -> None:
    if person.character_id is not None:
        if person.character_id not in scene.present_character_ids:
            scene.present_character_ids.append(person.character_id)
        return
    if person.name.lower() not in {name.lower() for name in scene.present_others}:
        scene.present_others.append(person.name)


def _remove(scene: SceneState, person: Resolved) -> None:
    if person.character_id is not None:
        scene.present_character_ids = [
            i for i in scene.present_character_ids if i != person.character_id
        ]
        return
    lowered = person.name.lower()
    scene.present_others = [name for name in scene.present_others if name.lower() != lowered]


def _people(
    raw: object, *, cast: Sequence[Character], supporting: Sequence[Character]
) -> tuple[list[Resolved], dict[str, str]]:
    """A list of names or {"name", "how"} objects, resolved, with how each moved."""
    people: list[Resolved] = []
    how: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        name = _text(item.get("name")) if isinstance(item, dict) else _text(item)
        if not name:
            continue
        resolved = resolve_name(name, cast=cast, supporting=supporting)
        if resolved is None or resolved in people:
            continue
        on_file = resolved.character_id is not None or any(
            card.name == resolved.name for card in supporting
        )
        if not on_file and not looks_like_a_name(resolved.name):
            continue
        people.append(resolved)
        detail = _text(item.get("how")) if isinstance(item, dict) else None
        if detail:
            how[resolved.name.lower()] = detail
    return people, how


def parse_delta(
    text: str, *, cast: Sequence[Character], supporting: Sequence[Character] = ()
) -> SceneDelta:
    """The read's reply as a delta. Raises ValueError when there is no JSON object."""
    data = extract_json(text, dict, "scene object")
    arrived, how = _people(data.get("arrived"), cast=cast, supporting=supporting)
    arrived = [person for person in arrived if not _remote(how.get(person.name.lower(), ""))]
    left, how_left = _people(data.get("left"), cast=cast, supporting=supporting)
    how.update(how_left)
    already_here = None
    if "already_here" in data:
        already_here, _ = _people(data.get("already_here"), cast=cast, supporting=supporting)

    closed_raw = data.get("scene_closed")
    closed = bool(closed_raw) and closed_raw is not False
    gist = _text(closed_raw.get("gist")) if isinstance(closed_raw, dict) else _text(closed_raw)

    shortcuts: list[SceneShortcut] = []
    for item in data.get("shortcuts") if isinstance(data.get("shortcuts"), list) else []:
        if not isinstance(item, dict):
            continue
        name, kind, quote = (
            _text(item.get("name")),
            _text(item.get("kind")),
            _text(item.get("quote")),
        )
        if not (name and quote) or kind not in SHORTCUT_KINDS:
            continue
        resolved = resolve_name(name, cast=cast, supporting=supporting)
        assert resolved is not None
        shortcuts.append(
            SceneShortcut(
                name=resolved.name,
                character_id=resolved.character_id,
                kind=kind,  # type: ignore[arg-type]
                quote=quote,
            )
        )

    return SceneDelta(
        location=_text(data.get("location")),
        time_of_day=_text(data.get("time_of_day")),
        situation=_text(data.get("situation")),
        privacy=_privacy(data.get("privacy")),
        arrived=arrived,
        left=left,
        already_here=already_here,
        closed=closed,
        gist=gist,
        shortcuts=shortcuts,
        how=how,
    )


# --- the full read ("Update from the story") ---------------------------------


@dataclass
class SceneProposal:
    """What the full read found, resolved against the cast and the cards on file."""

    location: str | None = None
    time_of_day: str | None = None
    situation: str | None = None
    privacy: Privacy | None = None
    present_character_ids: list[str] = field(default_factory=list)
    # Everyone else present, by card name or as written.
    present_others: list[str] = field(default_factory=list)

    @property
    def found_anyone(self) -> bool:
        return bool(self.present_character_ids or self.present_others)

    def applied_to(self, scene: SceneState, *, leaf_id: str | None) -> SceneState:
        """A copy of `scene` with whatever the proposal established."""
        updated = scene.model_copy(deep=True)
        if self.location:
            updated.location = self.location
        if self.time_of_day:
            updated.time_of_day = self.time_of_day
        if self.situation:
            updated.situation = self.situation
        if self.privacy:
            updated.privacy = self.privacy
        if self.found_anyone:
            updated.present_character_ids = list(self.present_character_ids)
            updated.present_others = list(self.present_others)
            updated.tracked = True
            # A note about where someone is only applies while they are away.
            updated.offstage_but_nearby = [
                entry
                for entry in updated.offstage_but_nearby
                if entry.character_id not in self.present_character_ids
            ]
        updated.set_at_node_id = leaf_id
        return updated


def build_scene_messages(
    prose: str,
    *,
    cast: Sequence[Character],
    supporting: Sequence[Character] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    body = (
        f"The cast: {', '.join(c.name for c in cast) or '(no cast on file)'}.\n"
        f"People on file: {_people_on_file(cast, supporting)}.\n\n"
        f"The most recent passages:\n\n{prose}"
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["scene_read.full"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=body),)),
    ]


def parse_scene(
    text: str, *, cast: Sequence[Character], supporting: Sequence[Character] = ()
) -> SceneProposal:
    """The full read's reply as a proposal. Raises ValueError when there is no JSON."""
    data = extract_json(text, dict, "scene object")
    people, _ = _people(data.get("present"), cast=cast, supporting=supporting)
    ids: list[str] = []
    others: list[str] = []
    for person in people:
        if person.character_id is not None:
            if person.character_id not in ids:
                ids.append(person.character_id)
        elif person.name.lower() not in {name.lower() for name in others}:
            others.append(person.name)
    return SceneProposal(
        location=_text(data.get("location")),
        time_of_day=_text(data.get("time_of_day")),
        situation=_text(data.get("situation")),
        privacy=_privacy(data.get("privacy")),
        present_character_ids=ids,
        present_others=others,
    )


# --- moving the scene to the author's character ------------------------------


def cut_to(scene: SceneState, character: Character, *, cast: Sequence[Character]) -> SceneState:
    """The scene, moved to where `character` is: taking them up never teleports them.

    Their offstage note, if the card had one, is the best record of where that
    is. Who else is there isn't known until the next read, which is asked to
    start the record (the card is untracked).
    """
    note = next(
        (entry.note for entry in scene.offstage_but_nearby if entry.character_id == character.id),
        None,
    )
    where = f"at {_article_lowered(scene.location)}" if scene.location else "where the story was"
    left_behind = [
        OffstageCharacter(character_id=c.id, note=where)
        for c in cast
        if c.id in scene.present_character_ids and c.id != character.id
    ]
    return SceneState(
        location=note,
        situation=f"The story has moved to {character.name}.",
        present_character_ids=[character.id],
        offstage_but_nearby=left_behind,
        tracked=False,
    )


def _article_lowered(place: str) -> str:
    """ "The undercroft" reads as "at the undercroft" mid-sentence."""
    first, _, rest = place.partition(" ")
    return f"{first.lower()} {rest}" if rest and first in ("The", "A", "An") else place
