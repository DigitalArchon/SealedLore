"""Drafting a whole story from a premise: the call, and turning its reply into a story.

There is no story — and so no session — until the reply is in, which is why
this isn't a `StorySession` method. The call is logged once the story exists,
into that story's own API log, so its cost counts toward the story like any
other.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path

from sealedlore.engine.authoring import (
    GENERATION_MAX_TOKENS,
    build_generation_messages,
    parse_generated,
)
from sealedlore.engine.card_link import picture_mark, story_copy
from sealedlore.engine.draft_detail import STANDARD_DETAIL, DraftDetail
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.scenario import Scenario
from sealedlore.models.story import NarrativePerson, NarrativeTense
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    StreamCompleted,
    StreamEvent,
    TextDelta,
)
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.images import REFS_DIR
from sealedlore.storage.repository import StoryBundle, append_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario
from sealedlore.storage.tavern_lore import lore_from_book, their_lorebook


class StoryDraft:
    def __init__(
        self,
        provider: ChatProvider,
        premise: str,
        model: str,
        *,
        person: NarrativePerson | None = None,
        tense: NarrativeTense | None = None,
        texts: PromptTexts = DEFAULT_TEXTS,
        route: dict | None = None,
        reasoning: ReasoningConfig,
        characters: Sequence[Character] = (),
        detail: DraftDetail = STANDARD_DETAIL,
    ) -> None:
        """`route`: the authoring role's (`routing.config_route`); `reasoning`:
        what the author's level for other calls asks of `model`
        (`reasoning.config_reasoning`). `characters`: the author's own (Your
        characters) to build the story around, their cards as they are.
        `detail`: how much to set up (engine/draft_detail.py)."""
        if not premise.strip():
            raise ValueError("write a premise first")
        self.provider = provider
        self.premise = premise
        self.model = model
        # Chosen before the draft, so its opening is written in them; they
        # stand whatever the reply's style says.
        self.person = person
        self.tense = tense
        self.characters = list(characters)
        self.detail = detail
        self.request = ChatRequest(
            model=model,
            extra_body=dict(route or {}),
            messages=build_generation_messages(
                premise,
                person=person,
                tense=tense,
                texts=texts,
                characters=self.characters,
                detail=detail,
            ),
            params=GenerationParams(max_tokens=GENERATION_MAX_TOKENS, reasoning=reasoning),
        )
        self.text = ""
        self.completed: StreamCompleted | None = None
        # Where each of the author's own characters went, once built: their
        # card, pictures and lorebook (`put_in_characters`), for the window.
        self.brought: list[str] = []
        self._requested_at = utc_now_iso()

    def stream(self) -> Iterator[StreamEvent]:
        """The call's events, passed through; the reply collects in `text`."""
        chunks: list[str] = []
        self._requested_at = utc_now_iso()
        for event in self.provider.stream(self.request):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
            elif isinstance(event, StreamCompleted):
                self.completed = event
            yield event
        self.text = "".join(chunks)

    def build(
        self,
        *,
        main_model: str | None = None,
        root: Path | None = None,
        kept: Sequence[Character] = (),
        read: Callable[[str], bytes | None] | None = None,
    ) -> tuple[StoryBundle, list[str]]:
        """Save the drafted story and log the call to it. Raises ValueError if unusable.

        `main_model` is what the story will be played with, which needn't be
        the (perhaps stronger) model that drafted it. `kept` and `read`: the
        author's own characters, and their pictures' bytes, for those the
        story is built around.
        """
        scenario, warnings = parse_generated(self.text)
        self.brought = []
        if self.characters:
            put_in_characters(
                scenario,
                extract_json(self.text, dict, "story setup"),
                self.characters,
                kept or self.characters,
                read or (lambda _file: None),
                warnings,
                self.brought,
            )
        bundle = bundle_from_scenario(scenario, main_model=main_model)
        style = bundle.story.style
        style.person = self.person or style.person
        style.tense = self.tense or style.tense
        save_story_bundle(bundle, root=root)
        request_id = new_id()
        entry = {
            "id": request_id,
            "kind": "draft_request",
            "at": self._requested_at,
            "premise": self.premise,
        }
        if self.detail.chosen():
            # What was asked for, beside the request it shaped; a standard
            # draft's entry is as it always was.
            entry["detail"] = asdict(self.detail)
        entry["payload"] = self.provider.build_payload(self.request)
        append_api_log(bundle.story.id, entry, root=root)
        append_api_log(
            bundle.story.id,
            {
                "id": new_id(),
                "kind": "draft_response",
                "at": utc_now_iso(),
                "request_log_ref": request_id,
                "text": self.text,
                "usage": self.completed.raw_usage if self.completed else {},
                "warnings": warnings,
            },
            root=root,
        )
        return bundle, warnings


def put_in_characters(
    scenario: Scenario,
    data: dict,
    characters: Sequence[Character],
    kept: Sequence[Character],
    read: Callable[[str], bytes | None],
    warnings: list[str],
    brought: list[str] | None = None,
) -> None:
    """The author's own characters in the drafted story as their cards have
    them (engine/card_link.py), in the place the draft gave them (its id, so
    the scene and whom the author plays still name them, and whether they are
    playable), with their pictures and their own lorebooks as the story's
    lore, as Add to the story brings them. What the draft says this story
    changes of one (`in_this_story`) is added to their description as the
    story's own change, which their card can take or not later. `brought`
    gets a line for each, saying where what they brought went."""
    notes: dict[str, str] = {}
    # Their lorebook's titles the draft says don't hold in this story.
    aside: dict[str, set[str]] = {}
    for key in ("cast", "supporting"):
        items = data.get(key)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name, note = item.get("name"), item.get("in_this_story")
            if isinstance(name, str) and isinstance(note, str) and note.strip():
                notes[name.strip().casefold()] = note.strip()
            titles = item.get("lorebook_set_aside")
            if isinstance(name, str) and isinstance(titles, list):
                aside[name.strip().casefold()] = {
                    title.strip().casefold() for title in titles if isinstance(title, str)
                }
    played = next((c for c in scenario.cast if c.id == scenario.suggested_character_id), None)
    if played is not None and scenario.placeholders.user_id is None:
        # Their cards' {{user}}: whom the author plays here.
        scenario.placeholders.user_id = played.id
    taken: set[str] = set()
    # Lore titles a card's lorebook brought, beside the draft's own.
    # Each title a chosen character's lorebook brought, and whose it was.
    from_cards: dict[str, str] = {}
    for card in characters:
        if card.name.casefold() in taken:
            # Two versions of one character: the draft knows one name.
            warnings.append(f"only one {card.name} can be in the story: the first chosen is")
            continue
        taken.add(card.name.casefold())
        drafted, tier = None, scenario.cast
        for tier in (scenario.cast, scenario.supporting):
            drafted = next((c for c in tier if c.name.casefold() == card.name.casefold()), None)
            if drafted is not None:
                break
        user = played.name if played is not None and played is not drafted else None
        copy = story_copy(card, kept, user)
        in_cast = drafted is None or tier is scenario.cast
        if drafted is not None:
            copy.id = drafted.id
            copy.is_player_available = drafted.is_player_available
            copy.author_only = drafted.author_only
            tier[tier.index(drafted)] = copy
        else:
            scenario.cast.append(copy)
            warnings.append(
                f"{card.name} wasn't in the draft's cast: added to it, not in the starting scene"
            )
        note = notes.get(card.name.casefold())
        if note:
            copy.full_description = "\n\n".join(
                part for part in (copy.full_description or "", f"In this story: {note}") if part
            )
        marks = []
        for ref in card.reference_images:
            data_bytes = read(ref.file)
            suffix = picture_kind(data_bytes) if data_bytes else None
            if data_bytes is None or suffix is None:
                continue
            ref_id = new_id()
            new = ref.model_copy(update={"id": ref_id, "file": f"{REFS_DIR}/{ref_id}{suffix}"})
            new.drawn_as = ""
            scenario.reference_files[new.file] = base64.b64encode(data_bytes).decode("ascii")
            copy.reference_images.append(new)
            marks.append(picture_mark(ref, data_bytes))
        copy.linked.pictures = marks
        lore, off = _put_in_lore(
            scenario,
            card,
            copy.id,
            warnings,
            from_cards,
            aside.get(card.name.casefold(), set()),
        )
        if brought is not None:
            brought.append(_brought(card.name, in_cast, len(marks), lore, off))


def _put_in_lore(
    scenario: Scenario,
    card: Character,
    owner: str,
    warnings: list[str],
    from_cards: dict[str, str],
    set_aside: set[str],
) -> tuple[int, int]:
    """A card's own lorebook into the drafted story's lore, every entry (a
    story made from a card takes all its lore; one joining a story that
    exists brings none unless asked, gui/window_card_link.py); how many
    came, and how many of them the draft set aside (`set_aside`, by title:
    they don't hold in this story, so they come switched off, kept and sent
    nowhere, for the author to switch on).
    The drafter was shown the book (authoring.render_own_characters) and told
    to write no entry by one of its titles; one it wrote anyway gives way to
    the card's, which stands as the card's other fields do. A title another
    chosen character's book brought already stays theirs: the two would say
    the same thing twice. Within one book, two entries of one title both
    come (a whole lorebook is the rule; live, a book of 44 lost 24 so).
    And one the draft copied from the book under another title (live, Sonnet
    4.6 copied whole books, once as "The Quiet Deck (lorebook entry)") gives
    way too: its text is the entry's (`_copied`)."""
    book = card.tavern.character_book if card.tavern else None
    if not book:
        return 0, 0
    # {{char}} and {{user}} as written: their {{char}} is the character
    # (`of_character`), filled as each request goes out.
    imported = lore_from_book(book, lambda text: text, their_lorebook(card.name))
    added = off = 0
    for entry in imported.entries:
        entry.of_character = owner
        title = entry.title.casefold()
        if from_cards.get(title, card.id) != card.id:
            warnings.append(
                f"{card.name}'s lore entry “{entry.title}” wasn't added: another character's "
                "lorebook has one by that name"
            )
            continue
        for own in [e for e in scenario.lore if not e.came_from]:
            if own.title.casefold() == title:
                why = "has one by that name"
            elif _copied(own.content, entry.content):
                why = f"says the same in “{entry.title}”"
            else:
                continue
            scenario.lore.remove(own)
            warnings.append(
                f"the draft's own lore entry “{own.title}” was left out: {card.name}'s "
                f"lorebook {why}"
            )
        from_cards.setdefault(title, card.id)
        if title in set_aside and entry.enabled:
            entry.enabled = False
            off += 1
        scenario.lore.append(entry)
        added += 1
    warnings += imported.said(f"{card.name}'s lorebook")
    return added, off


# How alike a drafted entry's text and a lorebook entry's must be for the
# draft's to be a copy. Over 328 entries drafted around three lorebooks
# (Nov 15 2026), the copies were 0.8 to 1.0 alike (word for word, or but for
# a name), the draft's own entries at most 0.40, those that build on one
# (a gate procedure naming the ledger it writes in) included.
COPIED_RATIO = 0.6


def _copied(drafted: str, theirs: str) -> bool:
    """Whether a drafted entry's text is a lorebook entry's, near enough."""
    a, b = (" ".join(text.casefold().split()) for text in (drafted, theirs))
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.real_quick_ratio() >= COPIED_RATIO and matcher.ratio() >= COPIED_RATIO


def _brought(name: str, in_cast: bool, pictures: int, lore: int, off: int = 0) -> str:
    """Where one of the author's own characters went in the draft, in words."""
    parts = [f"{name}: their card as it is, {'in the cast' if in_cast else 'supporting'}"]
    if pictures:
        parts[0] += f", with {pictures} picture{'s' if pictures > 1 else ''}"
    if lore:
        parts.append(
            f"{lore} {'entry' if lore == 1 else 'entries'} of their lorebook in the "
            "story's lore (the Lore page)"
        )
    if off:
        parts.append(
            f"{'all' if off == lore else off} of {'them' if lore > 1 else 'it'} disabled there, "
            "as the draft found they don't hold in this story: tick Enabled on any you want"
        )
    return "; ".join(parts) + "."
