"""A story as a plot document, and a document's plot given to the story.

The plot editor opens a story to add a plot to it, and the model writes a plot
for one (engine/plot_draft.py). Both need the story as a plot file would hold
it, so that the plot names its people and places as a file names them, and
both end by giving the story what the document's plot holds.

Only the plot is the document's to change: its facts and events, which of the
story's lore entries are places the program follows the character between,
and the people, places and lore it keeps hidden until an event brings them in.
A hidden card is the plot's, so what the document says of it is written back;
every other card is the story's, changed where cards are changed (the Cast and
Lore pages, Setup), and a document leaves it as it is. So nothing a document
can't hold (skills, pictures, looks, a card's SillyTavern fields, lore that
waits to be mentioned) is lost by passing through one.

Pure: a bundle in, a document out; a document and a bundle in, the changes
out. `StorySession.set_plot_document` applies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sealedlore.engine.picture_styles import write_style
from sealedlore.engine.plot_file import UNNAMED, render_plot_markdown
from sealedlore.engine.plot_md import _ALL_FIELDS, _key, _norm, parse_plot_markdown
from sealedlore.models.character import Character
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
    PlotDocument,
    VariantDoc,
)
from sealedlore.models.scene import SceneState
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


# --- the story as a document ---------------------------------------------------


def _role(character: Character, *, supporting: bool) -> str:
    if supporting:
        return "supporting"
    if character.author_only:
        return "author"
    return "player" if character.is_player_available else "storyteller"


def character_doc(
    character: Character,
    *,
    supporting: bool,
    present: bool = False,
    choose: bool = False,
    personality: bool = False,
) -> CharacterDoc:
    """A story's card as a plot file's character. `personality`: a card's
    own (a SillyTavern card's) after its description, as the storyteller is
    told it (`rules.full_description_text`), for a model's eyes only."""
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
        description=safe_prose(description, under=2),
        story_id=character.id,
    )


def entry_doc(entry: LoreEntry) -> EntryDoc:
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


def document_from_story(bundle: StoryBundle, *, for_model: bool = False) -> PlotDocument:
    """The story as a plot file would hold it, its plot (if it has one)
    included.

    `for_model`: for a model writing the plot: lore that is switched off is
    left out, and a card's personality joins its description.
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
        world=safe_prose(story.world_bible or "", under=1),
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
    for character in bundle.cast:
        document.characters.append(
            character_doc(
                character,
                supporting=False,
                present=character.id in present,
                choose=character.id in chosen,
                personality=for_model,
            )
        )
    for character in bundle.supporting.characters:
        document.characters.append(
            character_doc(
                character,
                supporting=True,
                present=_norm(character.name) in present_names,
                personality=for_model,
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
        document.places.append(entry_doc(entry) if entry is not None else EntryDoc(name=place.name))
    for entry in bundle.lore:
        if _norm(entry.title) in seen or (for_model and not entry.enabled):
            continue
        seen.add(_norm(entry.title))
        document.lore.append(entry_doc(entry))

    if plot is not None:
        names = {c.id: c.name for c in (*bundle.cast, *bundle.supporting.characters)}
        titles = {entry.id: entry.title for entry in bundle.lore}
        parts = plot_parts(plot, names, titles)
        document.start_day, document.start_time = parts.start_day, parts.start_time
        document.facts, document.events = parts.facts, parts.events
    return document


def without_plot(document: PlotDocument) -> PlotDocument:
    """The story's own parts alone: what a new plot is written against."""
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

    cast, supporting = iter(scenario.cast), iter(scenario.supporting)
    for doc in document.characters:
        made = next(supporting if doc.role == "supporting" else cast)
        if made.name != (doc.name.strip() or UNNAMED):
            raise ValueError(f"the plot's characters were read out of order at {doc.name}")
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

    for doc, made in zip([*document.places, *document.lore], scenario.lore, strict=True):
        if made.title != (doc.name.strip() or UNNAMED):
            raise ValueError(f"the plot's places and lore were read out of order at {doc.name}")
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

    if scenario.plot is not None:
        changes.plot = _rekeyed(scenario.plot, ids, changes.notes)
    return changes
