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
from pathlib import Path

from sealedlore.engine.authoring import (
    GENERATION_MAX_TOKENS,
    build_generation_messages,
    parse_generated,
)
from sealedlore.engine.card_link import fields_of, picture_mark, story_copy
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
from sealedlore.storage.tavern_cards import macro_filler
from sealedlore.storage.tavern_lore import lore_from_book


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
        if self.characters:
            put_in_characters(
                scenario,
                extract_json(self.text, dict, "story setup"),
                self.characters,
                kept or self.characters,
                read or (lambda _file: None),
                warnings,
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
) -> None:
    """The author's own characters in the drafted story as their cards have
    them (engine/card_link.py), in the place the draft gave them (its id, so
    the scene and whom the author plays still name them, and whether they are
    playable), with their pictures and their own lorebooks as the story's
    lore, as Add to the story brings them. What the draft says this story
    changes of one (`in_this_story`) is added to their description as the
    story's own change, which their card can take or not later."""
    notes: dict[str, str] = {}
    for key in ("cast", "supporting"):
        items = data.get(key)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name, note = item.get("name"), item.get("in_this_story")
            if isinstance(name, str) and isinstance(note, str) and note.strip():
                notes[name.strip().casefold()] = note.strip()
    played = next((c for c in scenario.cast if c.id == scenario.suggested_character_id), None)
    taken: set[str] = set()
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
        if user is None and "{{user}}" in str(fields_of(copy)):
            warnings.append(
                f"{card.name}'s card names {{{{user}}}}, the one they talk to: write that "
                "person's name in on the cast board"
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
        _put_in_lore(scenario, card, user, warnings)


def _put_in_lore(
    scenario: Scenario, card: Character, user: str | None, warnings: list[str]
) -> None:
    """A card's own lorebook into the drafted story's lore (it was left out,
    where Add to the story brings it). An entry the draft already has by its
    title is the draft's: the two would say the same thing twice."""
    book = card.tavern.character_book if card.tavern else None
    if not book:
        return
    imported = lore_from_book(book, macro_filler(card.name, user))
    titles = {entry.title.casefold() for entry in scenario.lore}
    for entry in imported.entries:
        if entry.title.casefold() in titles:
            warnings.append(
                f"{card.name}'s lore entry “{entry.title}” wasn't added: the draft has one by "
                "that name"
            )
            continue
        titles.add(entry.title.casefold())
        scenario.lore.append(entry)
    warnings += [f"{card.name}'s lorebook: left out {note}" for note in imported.notes()]
