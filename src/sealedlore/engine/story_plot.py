"""A story as a plot document, and a document given back to a story.

The plot editor opens a story, and the model writes a plot for one
(engine/plot_draft.py). Both need the story as a plot file would hold it, so
that the plot names its people and places as a file names them.

A plot the model writes is given to the story itself (`plot_changes`), and
only the plot is the document's to change there: its facts and events, which
of the story's lore entries are places the program follows the character
between, and the people, places and lore it keeps hidden until an event
brings them in. A hidden card is the plot's, so what the document says of it
is written back; every other card is left as it is.

The plot editor saves anything it holds, but never over the story it was
made from (the author, Nov 6 2026): a new story is made, the same setup
unplayed, and the document applied to it (`apply_document`). Only what the
author changed in the editor is written, so nothing a document can't hold
(skills, looks, a card's SillyTavern fields, lore that waits to be mentioned)
is lost by passing through one.

Pure: a bundle in, a document out; a document and a bundle in, the changes
out (or, for a story not yet begun, made). `StorySession.set_plot_document`
and `set_story_document` apply them to an open story.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sealedlore.engine.chronicle import time_of_day_minutes
from sealedlore.engine.picture_styles import write_style
from sealedlore.engine.plot_file import UNNAMED, render_plot_markdown
from sealedlore.engine.plot_md import _ALL_FIELDS, _key, _norm, parse_plot_markdown
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.plot import MINUTES_PER_DAY, Condition, EventDef, EventVariant, Plot
from sealedlore.models.plot_file import (
    AssignmentDoc,
    CharacterDoc,
    ConditionDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    OpeningDoc,
    PictureDoc,
    PlotDocument,
    VariantDoc,
)
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import NotesBy, Story
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.images import IMAGES_DIR, REFS_DIR
from sealedlore.storage.repository import StoryBundle

_HEADING = re.compile(r"^(#{1,3})(\s+\S.*)$")
_FENCE = re.compile(r"^\s*```")
_SETTING = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z _-]*?)\s*:")


def safe_prose(text: str, *, under: int) -> str:
    """A story's text, to stand under a heading of level `under` in a plot
    file and be read back as that text.

    A heading in it no deeper than the item's own would end the item (a
    card's "## Appearance" read as another character), a fence left open
    would hide every heading after it, and a first line shaped like one of
    the item's settings would be read as one. Each is put right here, for the
    document only: the story keeps its text as it is.
    """
    lines = text.strip("\n").split("\n") if text.strip() else []
    out: list[str] = []
    fenced = False
    for line in lines:
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (match := _HEADING.match(line)) and len(match.group(1)) <= under:
            line = "#" * (under + 1) + match.group(2)
        out.append(line)
    if fenced:
        out.append("```")
    first = next((i for i, line in enumerate(out) if line.strip()), None)
    if first is not None and (match := _SETTING.match(out[first])):
        if _key(match.group(1)) in _ALL_FIELDS:
            out[first] = "\\" + out[first].lstrip()
    return "\n".join(out).strip()


def _line(text: str | None) -> str:
    """A setting's value is one line."""
    return " ".join((text or "").split())


# --- the storyteller's notes a plot's writer gave -------------------------------


def notes_mark(notes: str | None) -> str:
    """A fingerprint of storyteller's notes, "" for none: what a plot keeps
    of the notes its writer gave the story (`Plot.notes_given`), so nothing
    holds their words twice."""
    text = (notes or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def notes_from_plot(story: Story) -> bool:
    """Whether the story's notes are its plot's own: a model wrote them, and
    they are still, word for word, the ones the plot's writer gave with the
    plot the story has (nobody has changed them since, in Setup, the editor
    or a follow-up on the story). Then a plot written again at the author's
    request may write them again (engine/plot_draft.py); any other notes are
    the story's, and always stand."""
    mark = notes_mark(story.storyteller_notes)
    return bool(
        mark
        and story.storyteller_notes_by == "model"
        and story.plot is not None
        and story.plot.notes_given == mark
    )


def carry_notes_mark(plot: Plot | None, previous: Plot | None) -> None:
    """A plot put in another's place keeps what the old one says of the
    notes: they are checked against the story's notes whenever asked, so a
    mark that no longer fits says nothing."""
    if plot is not None and previous is not None and not plot.notes_given:
        plot.notes_given = previous.notes_given


# --- the story as a document ---------------------------------------------------


def _role(character: Character, *, supporting: bool) -> str:
    if supporting:
        return "supporting"
    if character.author_only:
        return "author"
    return "player" if character.is_player_available else "storyteller"


def picture_docs(refs: list[ImageRef]) -> list[PictureDoc]:
    """A card's pictures as a document's, each by its file in the story's
    folder (the editor shows them from a copy of the folder). A caption is
    one line, and never holds a picture line's `|`."""
    return [
        PictureDoc(
            file=ref.file,
            caption=_line(ref.caption).replace("|", "/"),
            look=_line(ref.look).replace("|", "/"),
            expression=_line(ref.expression).replace("|", "/"),
        )
        for ref in refs
        if ref.file
    ]


def character_doc(
    character: Character,
    *,
    supporting: bool,
    present: bool = False,
    choose: bool = False,
    personality: bool = False,
    pictures: bool = False,
) -> CharacterDoc:
    """A story's card as a plot file's character. `personality`: a card's
    own (a SillyTavern card's) after its description, as the storyteller is
    told it (`rules.full_description_text`), for a model's eyes only.
    `pictures`: theirs, for the editor."""
    role = _role(character, supporting=supporting)
    description = character.full_description or ""
    if personality and (character.personality or "").strip():
        description = "\n\n".join(
            part for part in (description, f"Personality: {character.personality.strip()}") if part
        )
    hidden = character.plot_hidden
    return CharacterDoc(
        name=character.name,
        role=role,  # type: ignore[arg-type]
        # The importer refuses a hidden character in the opening scene or
        # chosen at the start: the story's own state stays its own.
        choose=choose and not hidden and role in ("player", "author"),
        present=present and not hidden,
        aliases=[alias for alias in character.aliases if alias.strip()],
        voice=_line(character.voice_notes),
        summary=_line(character.summary),
        canon=_line(character.canon),
        hidden=hidden,
        pictures=picture_docs(character.reference_images) if pictures else [],
        description=safe_prose(description, under=2),
        story_id=character.id,
    )


def entry_doc(entry: LoreEntry, *, pictures: bool = False) -> EntryDoc:
    """A story's lore entry as a plot file's place or lore entry. The file's
    keywords leave out the title: the importer makes it one of them."""
    return EntryDoc(
        name=entry.title,
        keywords=[
            keyword
            for keyword in entry.keywords
            if keyword.strip() and _norm(keyword) != _norm(entry.title)
        ],
        always=entry.always_on,
        hidden=entry.plot_hidden,
        pictures=picture_docs(entry.reference_images) if pictures else [],
        description=safe_prose(entry.content, under=2),
        story_id=entry.id,
    )


def _condition_doc(condition: Condition, plot: Plot, names: dict[str, str]) -> ConditionDoc:
    if condition.event_id is not None:
        event = plot.event(condition.event_id)
        return ConditionDoc(
            kind="happened",
            negated=not condition.happened,
            event=event.title if event is not None else condition.event_id,
        )
    if condition.place is not None:
        return ConditionDoc(
            kind=condition.place_test,
            negated=condition.op == "!=",
            place=condition.place,
            # Someone no longer in the story is named by their id, which the
            # importer then says it can't read: never the held character's
            # test in their place.
            who=names.get(condition.who, condition.who) if condition.who else "",
        )
    return ConditionDoc(
        kind="fact", negated=condition.op == "!=", fact=condition.fact or "", value=condition.value
    )


def _way(
    way: EventVariant | EventDef,
    plot: Plot,
    names: dict[str, str],
    titles: dict[str, str],
) -> dict:
    return {
        "requires": [_condition_doc(condition, plot, names) for condition in way.requires],
        "trigger": way.trigger,
        "tell": way.tell,
        "brings": [names.get(item_id, item_id) for item_id in way.brings],
        "reveals": [titles.get(item_id, item_id) for item_id in way.reveals],
        "at": way.place or "",
    }


def event_doc(
    event: EventDef, plot: Plot, names: dict[str, str], titles: dict[str, str]
) -> EventDoc:
    """A parsed event as the file wrote it. The importer folds an event's
    shared `sets:` into each way it goes; a single untitled way is the event
    itself, so its `sets:` and `offscreen` are the event's again."""
    doc = EventDoc(
        title=event.title,
        timeline=event.kind == "timed",
        when=event.window,
        pacing=event.pacing,
        lead_in=event.lead_in,
        aftermath=event.aftermath,
        must_happen=event.must_happen,
        **_way(event, plot, names, titles),
    )
    variants = event.variants
    if len(variants) == 1 and not variants[0].title:
        only = variants[0]
        doc.sets = [AssignmentDoc(fact=a.fact, value=a.value) for a in only.sets]
        doc.offscreen = True if only.offscreen else None
        return doc
    for variant in variants:
        way = VariantDoc(
            title=variant.title,
            sets=[AssignmentDoc(fact=a.fact, value=a.value) for a in variant.sets],
            offscreen=True if variant.offscreen else None,
            **_way(variant, plot, names, titles),
        )
        doc.variants.append(way)
    return doc


def plot_parts(plot: Plot, names: dict[str, str], titles: dict[str, str]) -> PlotDocument:
    """A parsed plot's facts and events, with names where it holds ids."""
    document = PlotDocument(
        start_day=plot.start_minutes // MINUTES_PER_DAY + 1,
        start_time=(
            f"{plot.start_minutes % MINUTES_PER_DAY // 60:02d}:{plot.start_minutes % 60:02d}"
        ),
    )
    for fact in plot.facts:
        values = list(fact.values)
        if fact.initial in ("yes", "no") and sorted(values) == ["no", "yes"]:
            values = []  # the importer's own for a yes/no fact
        document.facts.append(
            FactDoc(
                name=fact.name,
                initial=fact.initial,
                values=values,
                watched=fact.watched,
                meaning=fact.meaning,
            )
        )
    document.events = [event_doc(event, plot, names, titles) for event in plot.events]
    return document


def document_from_story(
    bundle: StoryBundle, *, for_model: bool = False, whole: bool = False
) -> PlotDocument:
    """The story as a plot file would hold it, its plot (if it has one)
    included.

    `for_model`: for a model writing the plot: lore that is switched off is
    left out, and a card's personality joins its description. `whole`: for
    the editor, the setup as a new playthrough of it has it: the cards'
    pictures and the background, and whoever a "play one of these" set aside
    back among the cast.
    """
    story = bundle.story
    setup = story.setup
    # A story whose starting scene was never set began where it is now.
    scene = setup.starting_scene if setup.starting_scene != SceneState() else story.scene
    style = story.style
    document = PlotDocument(
        story_id=story.id,
        title=story.title,
        description=_line(setup.description),
        agency=story.defaults.agency_mode,
        world_activity=story.defaults.world_activity,
        perspective=style.perspective,
        # A length of the author's own words is set in the app, never in a file.
        length=None if style.response_style == "custom" else style.response_style,
        picture_style=write_style(story.picture_style, story.image_style),
        background=(story.background_image or "") if whole else "",
        world=safe_prose(story.world_bible or "", under=1),
        storyteller_notes=safe_prose(story.storyteller_notes or "", under=1),
        opening=OpeningDoc(
            mode=setup.opening_mode,
            location=_line(scene.location),
            time=_line(scene.time_of_day),
            situation=_line(scene.situation),
            notes=safe_prose(setup.opening_text, under=1),
        ),
    )
    present = set(scene.present_character_ids)
    present_names = {_norm(name) for name in scene.present_others}
    chosen = set(setup.choose_one_of)
    for character in [*bundle.cast, *(setup.set_aside if whole else [])]:
        if not character.name.strip():
            continue  # nothing can name them, and a file can't hold them
        document.characters.append(
            character_doc(
                character,
                supporting=False,
                present=character.id in present,
                choose=character.id in chosen,
                personality=for_model,
                pictures=whole,
            )
        )
    for character in bundle.supporting.characters:
        if not character.name.strip():
            continue
        document.characters.append(
            character_doc(
                character,
                supporting=True,
                present=_norm(character.name) in present_names,
                personality=for_model,
                pictures=whole,
            )
        )

    plot = story.plot
    entries = {entry.id: entry for entry in bundle.lore}
    # Two entries of one title would be one name to the importer (it refuses
    # them): the first stands for both.
    seen: set[str] = set()
    for place in plot.places if plot is not None else []:
        entry = entries.get(place.lore_id) if place.lore_id else None
        if _norm(place.name) in seen:
            continue
        seen.add(_norm(place.name))
        # A place whose entry is gone is one the document adds back.
        document.places.append(
            entry_doc(entry, pictures=whole) if entry is not None else EntryDoc(name=place.name)
        )
    for entry in bundle.lore:
        if not entry.title.strip() or _norm(entry.title) in seen:
            continue
        if for_model and not entry.enabled:
            continue
        seen.add(_norm(entry.title))
        document.lore.append(entry_doc(entry, pictures=whole))

    if plot is None and not bundle.nodes:
        # A story with no plot yet has no clock: one written for it now (by
        # the model, or in the editor) starts at the hour its starting scene
        # names, where that reads as one, not at midnight.
        minutes = time_of_day_minutes(scene.time_of_day)
        if minutes is not None:
            document.start_time = f"{minutes // 60:02d}:{minutes % 60:02d}"
    if plot is not None:
        names = {c.id: c.name for c in (*bundle.cast, *bundle.supporting.characters)}
        titles = {entry.id: entry.title for entry in bundle.lore}
        parts = plot_parts(plot, names, titles)
        document.start_day, document.start_time = parts.start_day, parts.start_time
        document.facts, document.events = parts.facts, parts.events
    return document


def without_plot(document: PlotDocument) -> PlotDocument:
    """The story's own parts alone: what a new plot is written against. The
    storyteller's notes stay: they are the story's, not the plot's."""
    return document.model_copy(update={"facts": [], "events": []}, deep=True)


# --- the document's plot into the story ------------------------------------------


class PlotDocumentError(ValueError):
    """A document the importer refuses: its errors, as the importer says them."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "the plot can't be read")
        self.errors = errors


@dataclass
class CardChange:
    """What a document changes of one of the story's cards (a character or a
    lore entry): attributes and their new values."""

    card_id: str
    fields: dict[str, object] = field(default_factory=dict)
    # A character moving between the cast and the supporting cards: True to
    # the supporting cards, False to the cast.
    to_supporting: bool | None = None


@dataclass
class PlotChanges:
    # Keyed to the story's own cards. None: the document has no plot.
    plot: Plot | None = None
    new_cast: list[Character] = field(default_factory=list)
    new_supporting: list[Character] = field(default_factory=list)
    new_lore: list[LoreEntry] = field(default_factory=list)
    characters: list[CardChange] = field(default_factory=list)
    entries: list[CardChange] = field(default_factory=list)
    # The storyteller's notes as the document has them, in the importer's
    # words ("" for none), when it changed them; None when it left them as
    # the story has them.
    storyteller_notes: str | None = None
    # What couldn't be done, said for the author.
    notes: list[str] = field(default_factory=list)


def _hide(kept: bool, wanted: bool, name: str, begun: bool, fields: dict, notes: list[str]) -> None:
    if kept == wanted:
        return
    if wanted and begun:
        # Already told to the storyteller: hidden now, they would vanish from
        # a story that has met them.
        notes.append(f"{name} is in the story already, so the plot can't hide them now.")
        return
    fields["plot_hidden"] = wanted


def _character_change(
    card: Character,
    doc: CharacterDoc,
    before: CharacterDoc,
    made: Character,
    *,
    supporting: bool,
    begun: bool,
    notes: list[str],
) -> CardChange | None:
    change = CardChange(card.id)
    fields = change.fields
    _hide(card.plot_hidden, doc.hidden, card.name, begun, fields, notes)
    if card.plot_hidden or fields.get("plot_hidden"):
        # A hidden card is the plot's: what the document changed of it, here.
        if doc.name.strip() != before.name:
            fields["name"] = doc.name.strip()
        if doc.aliases != before.aliases:
            fields["aliases"] = list(made.aliases)
        for name, attribute in (
            ("summary", "summary"),
            ("voice", "voice_notes"),
            ("canon", "canon"),
            ("description", "full_description"),
        ):
            if getattr(doc, name).strip() != getattr(before, name):
                fields[attribute] = getattr(doc, name).strip() or None
        if doc.role != before.role:
            fields["is_player_available"] = doc.role in ("player", "author")
            fields["author_only"] = doc.role == "author"
            if (doc.role == "supporting") != supporting:
                change.to_supporting = doc.role == "supporting"
    return change if fields or change.to_supporting is not None else None


def _entry_change(
    entry: LoreEntry,
    doc: EntryDoc,
    before: EntryDoc,
    made: LoreEntry,
    *,
    begun: bool,
    notes: list[str],
) -> CardChange | None:
    change = CardChange(entry.id)
    fields = change.fields
    _hide(entry.plot_hidden, doc.hidden, entry.title, begun, fields, notes)
    if entry.plot_hidden or fields.get("plot_hidden"):
        if doc.name.strip() != before.name:
            fields["title"] = doc.name.strip()
        if doc.keywords != before.keywords or "title" in fields:
            fields["keywords"] = list(made.keywords)
        if doc.always != before.always:
            fields["always_on"] = doc.always
        if doc.description.strip() != before.description:
            fields["content"] = made.content
    return change if fields else None


def _rekeyed(plot: Plot, ids: dict[str, str | None], notes: list[str]) -> Plot:
    """The parsed plot's ids (the file's, fresh each parse) as the story's."""
    plot = plot.model_copy(deep=True)

    def kept(item_ids: list[str], where: str) -> list[str]:
        out = []
        for item_id in item_ids:
            story_id = ids.get(item_id, item_id)
            if story_id is None:
                notes.append(f"{where} can no longer bring in someone the story has lost.")
                continue
            out.append(story_id)
        return list(dict.fromkeys(out))

    for event in plot.events:
        for holder in (event, *event.variants):
            where = f"“{event.title}”"
            holder.brings = kept(holder.brings, where)
            holder.reveals = kept(holder.reveals, where)
            for condition in holder.requires:
                if condition.who is not None:
                    # Never None: that would make it a test of whoever the
                    # author holds. Someone lost keeps the id, and tests nobody.
                    condition.who = ids.get(condition.who) or condition.who
    for place in plot.places:
        if place.lore_id is not None:
            place.lore_id = ids.get(place.lore_id, place.lore_id)
    return plot


def made_of(
    document: PlotDocument, scenario: Scenario
) -> tuple[list[tuple[CharacterDoc, Character]], list[tuple[EntryDoc, LoreEntry]]]:
    """Each item of a rendered document beside what the importer made of it.

    The importer makes one card per character heading, into the cast or the
    supporting cards by role, and one entry per place or lore heading, places
    first, each in order: so order alone pairs them. A name that differs means
    they were read out of step, which is a bug, never the author's.
    """
    cast, supporting = iter(scenario.cast), iter(scenario.supporting)
    people = []
    for doc in document.characters:
        made = next(supporting if doc.role == "supporting" else cast)
        if made.name != (doc.name.strip() or UNNAMED):
            raise ValueError(f"the plot's characters were read out of order at {doc.name}")
        people.append((doc, made))
    entries = []
    for doc, made in zip([*document.places, *document.lore], scenario.lore, strict=True):
        if made.title != (doc.name.strip() or UNNAMED):
            raise ValueError(f"the plot's places and lore were read out of order at {doc.name}")
        entries.append((doc, made))
    return people, entries


def plot_changes(document: PlotDocument, bundle: StoryBundle, *, begun: bool) -> PlotChanges:
    """What giving the story this document's plot changes.

    The document is rendered and read by the importer, the one judge of a
    plot, so what a story is given is what a plot file would give it; its
    errors raise PlotDocumentError. Each item is matched to what the importer
    made of it by order (the importer makes one card or entry per heading, in
    order), and through its `story_id` to the story's own, so a name changed
    on the Cast page meanwhile still finds its card. `begun`: whether the
    story has messages (an item it has shown can't be hidden from then on).
    """
    if document.story_id and document.story_id != bundle.story.id:
        raise ValueError("this plot was made for another story")
    rendered = render_plot_markdown(document)
    parsed = parse_plot_markdown(rendered.text, fallback_title=document.title or "Untitled")
    errors = [problem.describe() for problem in parsed.problems if problem.error]
    if errors:
        raise PlotDocumentError(errors)
    scenario = parsed.scenario
    supporting_ids = {c.id for c in bundle.supporting.characters}
    cards = {c.id: c for c in (*bundle.cast, *bundle.supporting.characters)}
    entries = {entry.id: entry for entry in bundle.lore}
    original = document_from_story(bundle)
    before_cards = {doc.story_id: doc for doc in original.characters}
    before_entries = {doc.story_id: doc for doc in (*original.places, *original.lore)}
    changes = PlotChanges()
    ids: dict[str, str | None] = {}
    people, lore = made_of(document, scenario)

    for doc, made in people:
        card = cards.get(doc.story_id) if doc.story_id else None
        if doc.story_id and card is None:
            changes.notes.append(f"{doc.name} is no longer in the story.")
            ids[made.id] = None
        elif card is None:
            (changes.new_supporting if doc.role == "supporting" else changes.new_cast).append(made)
            ids[made.id] = made.id
        else:
            ids[made.id] = card.id
            change = _character_change(
                card,
                doc,
                before_cards.get(card.id, doc),
                made,
                supporting=card.id in supporting_ids,
                begun=begun,
                notes=changes.notes,
            )
            if change is not None:
                changes.characters.append(change)

    for doc, made in lore:
        entry = entries.get(doc.story_id) if doc.story_id else None
        if doc.story_id and entry is None:
            changes.notes.append(f"{doc.name} is no longer in the story's lore.")
            ids[made.id] = None
        elif entry is None:
            changes.new_lore.append(made)
            ids[made.id] = made.id
        else:
            ids[made.id] = entry.id
            change = _entry_change(
                entry,
                doc,
                before_entries.get(entry.id, doc),
                made,
                begun=begun,
                notes=changes.notes,
            )
            if change is not None:
                changes.entries.append(change)

    if document.storyteller_notes.strip() != original.storyteller_notes.strip():
        changes.storyteller_notes = scenario.storyteller_notes or ""
    if scenario.plot is not None:
        changes.plot = _rekeyed(scenario.plot, ids, changes.notes)
    return changes


def write_notes(story: Story, notes: str, by: NotesBy) -> None:
    """The storyteller's notes a document changed, on the story: `by` the
    author (whoever changed them in the plot editor has read them) or a
    model (a plot's writer: Setup keeps them unread). No notes are nobody's."""
    story.storyteller_notes = notes.strip() or None
    story.storyteller_notes_by = by if story.storyteller_notes else "author"


# --- a document made a story ---------------------------------------------------------

# The bytes of a picture a document adds, by its file (the editor's copy of
# the story's folder, gui/plot_editor.py); None when it can't be read.
PictureReader = Callable[[str], "bytes | None"]


def _index(items: list, item) -> int | None:
    """Where `item` itself is in `items`: never an equal copy of it."""
    return next((i for i, other in enumerate(items) if other is item), None)


def _pictures(
    docs: list[PictureDoc],
    old: list[PictureDoc],
    refs: list[ImageRef],
    bundle: StoryBundle,
    read: PictureReader,
    owner: str,
    notes: list[str],
) -> list[ImageRef]:
    """A card's pictures as the document lists them. One the card has keeps
    its record (how it was drawn, its framing, its style), with a caption or
    label the author changed; one the document adds is written in beside the
    story's own pictures."""
    by_file = {ref.file: ref for ref in refs}
    was = {picture.file: picture for picture in old}
    out: list[ImageRef] = []
    for picture in docs:
        ref = by_file.pop(picture.file, None)
        if ref is not None:
            before = was.get(picture.file, PictureDoc(file=picture.file))
            update = {
                name: getattr(picture, name)
                for name in ("caption", "look", "expression")
                if getattr(picture, name) != getattr(before, name)
            }
            out.append(ref.model_copy(update=update, deep=True))
            continue
        data = read(picture.file)
        suffix = picture_kind(data) if data else None
        if data is None or suffix is None:
            notes.append(f"{owner}'s picture {picture.file} couldn't be read, so it was left out.")
            continue
        ref_id = new_id()
        file = f"{REFS_DIR}/{ref_id}{suffix}"
        bundle.pending_files[file] = data
        out.append(
            ImageRef(
                id=ref_id,
                file=file,
                caption=picture.caption,
                look=picture.look,
                expression=picture.expression,
            )
        )
    return out


def plot_uses(plot: Plot | None, item_id: str) -> bool:
    """Whether a plot names a card or an entry: an event brings or reveals
    it, a condition tests where they are, or it is one of the plot's places."""
    if plot is None:
        return False
    for event in plot.events:
        for holder in (event, *event.variants):
            if item_id in holder.brings or item_id in holder.reveals:
                return True
            if any(condition.who == item_id for condition in holder.requires):
                return True
    return any(place.lore_id == item_id for place in plot.places)


def take_out_character(
    bundle: StoryBundle, card: Character, *, scenes: Iterable[SceneState] = ()
) -> None:
    """A character out of the story: `_take_out`. `scenes`: the snapshots on
    the nodes of a story under way (the scene anywhere is the nearest above,
    so the leaf's would bring them back)."""
    _take_out(bundle, card, scenes=scenes)


def _take_out(bundle: StoryBundle, card: Character, *, scenes: Iterable[SceneState] = ()) -> None:
    """A character the document no longer has, out of the story: its lists,
    whom it begins with, and either scene (an unplayed story's live scene is
    its starting scene), and any `scenes` besides."""
    setup = bundle.story.setup
    for tier in (bundle.cast, bundle.supporting.characters, setup.set_aside):
        index = _index(tier, card)
        if index is not None:
            del tier[index]
    setup.choose_one_of = [i for i in setup.choose_one_of if i != card.id]
    setup.absent_at_start = [i for i in setup.absent_at_start if i != card.id]
    if setup.suggested_character_id == card.id:
        setup.suggested_character_id = None
    if bundle.story.held_character_id == card.id:
        bundle.story.held_character_id = None
    for scene in (setup.starting_scene, bundle.story.scene, *scenes):
        scene.present_character_ids = [i for i in scene.present_character_ids if i != card.id]
        scene.offstage_but_nearby = [
            e for e in scene.offstage_but_nearby if e.character_id != card.id
        ]
        scene.present_others = [
            name for name in scene.present_others if _norm(name) != _norm(card.name)
        ]


def _apply_character(
    card: Character,
    doc: CharacterDoc,
    old: CharacterDoc,
    made: Character,
    bundle: StoryBundle,
    read: PictureReader,
    notes: list[str],
) -> None:
    """What the document changed of one of the story's own characters, in
    the words the importer read it as."""
    if doc.name != old.name and doc.name.strip():
        card.name = made.name
    if doc.aliases != old.aliases:
        card.aliases = list(made.aliases)
    for name, attribute in (
        ("summary", "summary"),
        ("voice", "voice_notes"),
        ("canon", "canon"),
        ("description", "full_description"),
    ):
        if getattr(doc, name) != getattr(old, name):
            setattr(card, attribute, getattr(made, attribute))
    if doc.hidden != old.hidden:
        card.plot_hidden = doc.hidden
    if doc.role != old.role:
        card.is_player_available = made.is_player_available
        card.author_only = made.author_only
        cast, supporting = bundle.cast, bundle.supporting.characters
        if doc.role == "supporting" and (index := _index(cast, card)) is not None:
            # As a review moves one (authoring.apply_change): supporting cards
            # have no skills and are never played.
            del cast[index]
            card.competence.tiers = {}
            supporting.append(card)
        elif doc.role != "supporting" and (index := _index(supporting, card)) is not None:
            del supporting[index]
            cast.append(card)
    if doc.pictures != old.pictures:
        card.reference_images = _pictures(
            doc.pictures, old.pictures, card.reference_images, bundle, read, card.name, notes
        )


def _apply_entry(
    entry: LoreEntry,
    doc: EntryDoc,
    old: EntryDoc,
    made: LoreEntry,
    bundle: StoryBundle,
    read: PictureReader,
    notes: list[str],
) -> None:
    if doc.name != old.name and doc.name.strip():
        entry.title = made.title
    if doc.keywords != old.keywords or doc.name != old.name:
        entry.keywords = list(made.keywords)
    if doc.always != old.always:
        entry.always_on = made.always_on
    if doc.hidden != old.hidden:
        entry.plot_hidden = made.plot_hidden
    if doc.description != old.description:
        entry.content = made.content
    if doc.pictures != old.pictures:
        entry.reference_images = _pictures(
            doc.pictures, old.pictures, entry.reference_images, bundle, read, entry.title, notes
        )


def _in_order(items: list, ordered: list) -> None:
    """`items` in place, those in `ordered` in its order, the rest (cards a
    document can't hold) after them as they were."""
    keep = [item for item in items if _index(ordered, item) is None]
    items[:] = [*ordered, *keep]


def _entry_order(document: PlotDocument) -> list[str]:
    """The story's own entries in the document's order (its places, then its
    lore): whether the author moved any."""
    return [doc.story_id for doc in (*document.places, *document.lore) if doc.story_id]


def _presence(document: PlotDocument) -> list[tuple]:
    """Whom the document begins with, and whom the author chooses between:
    what the opening's people come from."""
    return [
        (doc.story_id or doc.name, doc.name, doc.role, doc.present, doc.choose)
        for doc in document.characters
    ]


def _apply_presence(
    people: list[tuple[CharacterDoc, Character]], bundle: StoryBundle, known: set[str]
) -> None:
    """Whom the story begins with, as the document has them. `people`: each
    document character beside the story's card they are now; `known`: the
    names the cards had before (a renamed one's old name is theirs, not
    someone else's)."""
    setup = bundle.story.setup
    scene = setup.starting_scene
    cast_ids = {card.id for card in bundle.cast}
    card_names = known | {
        _norm(card.name) for card in (*bundle.cast, *bundle.supporting.characters)
    }
    choose = [card.id for doc, card in people if doc.choose and card.id in cast_ids]
    present = [
        card.id for doc, card in people if doc.present and not doc.choose and card.id in cast_ids
    ]
    setup.choose_one_of = choose if len(choose) > 1 else []
    scene.present_character_ids = present
    # A name the scene has with no card stays: the document can't say it.
    scene.present_others = [
        *(name for name in scene.present_others if _norm(name) not in card_names),
        *(card.name for doc, card in people if doc.present and doc.role == "supporting"),
    ]
    scene.offstage_but_nearby = [
        entry
        for entry in scene.offstage_but_nearby
        if entry.character_id in cast_ids and entry.character_id not in present
    ]
    setup.absent_at_start = [i for i in setup.absent_at_start if i in cast_ids and i not in present]
    playable = [card.id for card in bundle.cast if card.is_player_available]
    if choose:
        setup.suggested_character_id = choose[0]
    elif setup.suggested_character_id not in playable:
        setup.suggested_character_id = playable[0] if playable else None


def _apply_background(chosen: str, bundle: StoryBundle, read: PictureReader, notes: list[str]):
    """The picture behind the story, as the document names it: none, or a
    file of the editor's, copied in where a background always goes."""
    story = bundle.story
    if not chosen:
        story.background_image = None
        story.background_drawn_as = ""
        return
    data = read(chosen)
    suffix = picture_kind(data) if data else None
    if data is None or suffix is None:
        notes.append(f"The background {chosen} couldn't be read, so it stays as it was.")
        return
    file = f"{IMAGES_DIR}/background{suffix}"
    bundle.pending_files[file] = data
    story.background_image = file
    story.background_drawn_as = ""


def _drop_unused_files(bundle: StoryBundle) -> None:
    """Pictures waiting to be written that nothing names any more: a card
    the document took out, or a picture it took off a card."""
    story = bundle.story
    used = {
        ref.file
        for owner in (
            *bundle.cast,
            *bundle.supporting.characters,
            *story.setup.set_aside,
            *bundle.lore,
        )
        for ref in owner.reference_images
    }
    used.update(ref.file for ref in story.reference_images)
    used.update(picture.file for picture in story.setup.opening_pictures)
    used.update(file for file in (story.background_image, story.style_picture) if file)
    bundle.pending_files = {
        relative: data for relative, data in bundle.pending_files.items() if relative in used
    }


def apply_document(
    document: PlotDocument,
    bundle: StoryBundle,
    *,
    before: PlotDocument | None = None,
    read: PictureReader | None = None,
) -> list[str]:
    """Make a story that hasn't begun what the document holds, its own parts
    as well as its plot: the plot editor's Save (the author, Nov 6 2026:
    anything in the editor is editable, and it saves as a new story, never
    over the story it was made from).

    The document is rendered and read by the importer, the one judge, and
    each item paired with what it made (`made_of`) and with the story's card
    by its `story_id`. Only what the document changed since `before` (the
    document as it was made from the story, `document_from_story(whole=True)`)
    is written, in the words the importer read it as: a card's "## Appearance",
    kept a level down in the file, stays as the story had it unless the author
    changed that card's text, and a change made on the Cast page meanwhile
    stays unless the editor changed the same thing. Nothing a plot file can't
    hold (skills, looks, a card's SillyTavern fields, its link to Your
    characters, how a picture was drawn) is touched. A card or entry the
    document no longer has is taken out of the story; one it adds is the
    importer's, with its pictures, whose bytes `read` gives.

    Raises PlotDocumentError for a document the importer refuses, and
    ValueError for a story that has begun. Returns what couldn't be done,
    for the author.
    """
    if bundle.nodes:
        raise ValueError(
            f"“{bundle.story.title}” has begun, so the editor saves as a new story instead"
        )
    read = read or (lambda _file: None)
    before = before or document_from_story(bundle, whole=True)
    rendered = render_plot_markdown(document)
    parsed = parse_plot_markdown(rendered.text, fallback_title=document.title or "Untitled")
    errors = [problem.describe() for problem in parsed.problems if problem.error]
    if errors:
        raise PlotDocumentError(errors)
    scenario = parsed.scenario
    story, setup = bundle.story, bundle.story.setup
    notes: list[str] = []
    if setup.starting_scene == SceneState():
        # As the document was read: a story whose starting scene was never
        # set begins where it is.
        setup.starting_scene = story.scene.model_copy(deep=True)

    # Its title and settings, world and opening.
    if document.title != before.title and document.title.strip():
        story.title = scenario.title
    if document.description != before.description:
        setup.description = scenario.description
    if document.agency != before.agency:
        story.defaults.agency_mode = scenario.agency_mode
    if document.world_activity != before.world_activity:
        story.defaults.world_activity = scenario.world_activity
    if document.perspective != before.perspective:
        story.style.perspective = scenario.style.perspective
    if document.length != before.length:
        story.style.response_style = scenario.style.response_style
    if document.picture_style != before.picture_style:
        story.picture_style, story.image_style = scenario.picture_style, scenario.image_style
    if document.world != before.world:
        story.world_bible = scenario.world_bible
    if document.storyteller_notes != before.storyteller_notes:
        # Whoever changed them in the editor has read them. Untouched, they
        # stay as they are, and unread if a model wrote them.
        write_notes(story, scenario.storyteller_notes or "", "author")
    if document.background != before.background:
        _apply_background(document.background.strip(), bundle, read, notes)
    opening, was = document.opening, before.opening
    if opening.mode != was.mode:
        setup.opening_mode = scenario.opening_mode
    if opening.notes != was.notes:
        setup.opening_text = scenario.opening_text
    for name, attribute in (
        ("location", "location"),
        ("time", "time_of_day"),
        ("situation", "situation"),
    ):
        if getattr(opening, name) != getattr(was, name):
            setattr(setup.starting_scene, attribute, getattr(scenario.starting_scene, attribute))

    # Its characters, in the document's order.
    people_pairs, entry_pairs = made_of(document, scenario)
    cards = {c.id: c for c in (*bundle.cast, *bundle.supporting.characters, *setup.set_aside)}
    old_cards = {doc.story_id: doc for doc in before.characters if doc.story_id}
    ids: dict[str, str | None] = {}
    people: list[tuple[CharacterDoc, Character]] = []
    for doc, made in people_pairs:
        card = cards.get(doc.story_id) if doc.story_id else None
        if card is None:
            made.reference_images = _pictures(
                doc.pictures, [], [], bundle, read, made.name or UNNAMED, notes
            )
            card = made
        else:
            old = old_cards.get(card.id) or character_doc(
                card, supporting=doc.role == "supporting", pictures=True
            )
            _apply_character(card, doc, old, made, bundle, read, notes)
        ids[made.id] = card.id
        people.append((doc, card))
    for card_id in old_cards.keys() - {card.id for _, card in people}:
        if card_id in cards:
            _take_out(bundle, cards[card_id])
    # Whoever a "play one of these" set aside is back among the cast, as a
    # new playthrough has them.
    setup.set_aside = [c for c in setup.set_aside if not any(c is o for _, o in people)]
    _in_order(bundle.cast, [card for doc, card in people if doc.role != "supporting"])
    _in_order(
        bundle.supporting.characters, [card for doc, card in people if doc.role == "supporting"]
    )
    if _presence(document) != _presence(before):
        known = {_norm(name) for doc in before.characters for name in (doc.name, *doc.aliases)}
        _apply_presence(people, bundle, known)

    # Its places and lore.
    entries = {entry.id: entry for entry in bundle.lore}
    old_entries = {doc.story_id: doc for doc in (*before.places, *before.lore) if doc.story_id}
    listed: list[LoreEntry] = []
    for doc, made in entry_pairs:
        entry = entries.get(doc.story_id) if doc.story_id else None
        if entry is None:
            made.reference_images = _pictures(
                doc.pictures, [], [], bundle, read, made.title or UNNAMED, notes
            )
            entry = made
        else:
            old = old_entries.get(entry.id) or entry_doc(entry, pictures=True)
            _apply_entry(entry, doc, old, made, bundle, read, notes)
        ids[made.id] = entry.id
        listed.append(entry)
    gone = old_entries.keys() - {entry.id for entry in listed}
    bundle.lore[:] = [entry for entry in bundle.lore if entry.id not in gone]
    if _entry_order(document) != _entry_order(before):
        _in_order(bundle.lore, listed)
    else:
        bundle.lore.extend(entry for entry in listed if _index(bundle.lore, entry) is None)

    # Its plot, keyed to the story's cards; its days drawn again when it plays.
    previous = story.plot
    story.plot = _rekeyed(scenario.plot, ids, notes) if scenario.plot is not None else None
    carry_notes_mark(story.plot, previous)
    story.plot_schedule = {}
    if setup.starting_scene != SceneState():
        story.scene = setup.starting_scene.model_copy(deep=True)
    _drop_unused_files(bundle)
    return notes
