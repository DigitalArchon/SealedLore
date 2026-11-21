"""The session's side of what the story suggests for its lore and cards
(engine/story_suggestions.py): read from prose as it is archived, beside the
character scan and on the same model (the summarisation model), or from the
recent story on demand; applied, changed or turned down by the author.

Never in a chat or a private scene; never shown a plot-hidden card or entry,
nor lore held back until mentioned. A reading that fails never costs the turn.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from sealedlore.engine.archival import render_chunk
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.story_suggestions import (
    build_suggestion_messages,
    decline_key,
    parse_story_suggestions,
    with_addition,
)
from sealedlore.ids import utc_now_iso
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.story_suggestion import StorySuggestion
from sealedlore.providers.base import ChatRequest, ProviderError

# Where a lore entry the story suggested came from, as the Lore page says.
FROM_THE_STORY = "the story (a suggestion)"
# With this many waiting, archived prose is no longer read for more: the
# author hasn't looked, and each reading is a call (and its list of what
# waits grew with every one, in story.json and in the next request).
WAITING_MOST = 40
# What was turned down is remembered so far back.
DECLINED_MOST = 500
WAITING = (
    "suggestions are waiting already (Story → Suggestions from the story…): "
    "apply them or turn them down first"
)


class StorySuggestionsRuntime:
    @property
    def story_suggestions(self) -> list[StorySuggestion]:
        return self.story.story_suggestions

    def suggests_changes(self) -> bool:
        """Read archived prose for them: on, never in a chat or a private
        scene, nor while as many wait as anyone would go through."""
        return (
            self.config.suggest_story_changes
            and not self.story.chat
            and not self.in_private
            and len(self.story_suggestions) < WAITING_MOST
        )

    def _suggestion_sources(self):  # noqa: ANN202 - (lore, cards, reserved names)
        """What the reader may see, and the names a new entry may not take."""
        lore = [e for e in self.visible_lore() if e.enabled]
        cards = [*self.visible_cast(), *self.visible_supporting()]
        shown = {id(c) for c in cards} | {id(e) for e in lore}
        reserved = [e.title for e in self.bundle.lore if id(e) not in shown]
        reserved += [c.name for c in (*self.cast, *self.supporting) if id(c) not in shown]
        return lore, cards, reserved

    def story_suggestion_request(self, prose: str) -> ChatRequest:
        lore, cards, _reserved = self._suggestion_sources()
        model = self.summarization_model
        return ChatRequest(
            model=model,
            purpose="story_suggestions_request",
            messages=build_suggestion_messages(
                prose,
                lore=lore,
                cards=cards,
                setting=self.story.world_bible or "",
                pending=self.story_suggestions,
                declined_titles=self._declined_titles(),
                texts=self.texts,
            ),
            params=self.side_params(model),
            extra_body=self.route_for("summarisation"),
        )

    def _declined_titles(self) -> list[str]:
        return [key[5:] for key in self.story.suggestions_declined if key.startswith("lore:")]

    def take_story_suggestions(self, text: str, nodes: Sequence[Node]) -> list[StorySuggestion]:
        """Keep what a reply suggests. Raises ValueError for a reply that
        can't be read."""
        lore, cards, reserved = self._suggestion_sources()
        found = parse_story_suggestions(
            text,
            lore=lore,
            cards=cards,
            pending=self.story_suggestions,
            declined=self.story.suggestions_declined,
            reserved_names=reserved,
            node_ids=[node.id for node in nodes],
            # As the request was made: a change only to what it showed.
            prose=render_chunk(nodes, self.cast, texts=self.texts),
        )
        self.story.story_suggestions.extend(found)
        if found:
            self.save()
        return found

    def suggest_from(self, nodes: Sequence[Node]) -> list[StorySuggestion]:
        """Read `nodes` for what the lore and cards should take in. Raises
        ProviderError or ValueError; `suggest_after_archive` never does."""
        self._refuse_in_private("Suggesting changes to the lore and cards")
        self._refuse_in_chat("Suggesting changes to the lore and cards")
        if len(self.story_suggestions) >= WAITING_MOST:
            raise ValueError(f"{len(self.story_suggestions)} {WAITING}")
        prose = render_chunk(nodes, self.cast, texts=self.texts)
        if not prose.strip():
            return []
        request = self.story_suggestion_request(prose)
        log_ref = self._log(
            "story_suggestions_request",
            {
                "node_ids": [node.id for node in nodes],
                "payload": self.provider.build_payload(request),
            },
        )
        text, completed = self.provider.complete(request)
        self._log(
            "story_suggestions_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, request.model))
        return self.take_story_suggestions(text, nodes)

    def suggest_after_archive(self, chunk: Sequence[Node], *, turn=None) -> Iterator:  # noqa: ANN001
        """After prose is archived inline (a provider with no background
        client): never fails the turn."""
        if self.in_private or not self.suggests_changes():
            return
        try:
            found = self.suggest_from(chunk)
        except (ProviderError, ValueError) as exc:
            yield self._failed_notice(
                f"Couldn't read the archived turns for lore and card changes ({exc}).",
                exc,
                turn=turn,
            )
            return
        if found:
            yield self._suggestions_notice(found)

    def suggest_from_recent(self, nodes: int = 30) -> Iterator:
        """On demand (Story → Suggestions from the story…): the most recent
        prose still in the window."""
        self._refuse_in_private("Suggesting changes to the lore and cards")
        self._refuse_in_chat("Suggesting changes to the lore and cards")
        recent = list(self.split().verbatim)[-nodes:]
        found = self.suggest_from(recent)
        if found:
            yield self._suggestions_notice(found)
        else:
            yield SessionNotice("The recent story suggests no changes to the lore or the cards.")

    def _suggestions_notice(self, found: Sequence[StorySuggestion]) -> SessionNotice:
        lore = sum(1 for s in found if s.kind != "card_change")
        cards = len(found) - lore
        parts = []
        if lore:
            parts.append(f"{lore} for the lore")
        if cards:
            parts.append(f"{cards} for the cards")
        return SessionNotice(
            "The story suggests " + " and ".join(parts) + ": Story → Suggestions from the story…"
        )

    # --- the author's answer ------------------------------------------------

    def _story_suggestion(self, suggestion_id: str) -> StorySuggestion:
        for suggestion in self.story_suggestions:
            if suggestion.id == suggestion_id:
                return suggestion
        raise KeyError(f"unknown suggestion id: {suggestion_id!r}")

    def apply_story_suggestion(
        self,
        suggestion_id: str,
        *,
        title: str | None = None,
        keywords: Sequence[str] | None = None,
        content: str | None = None,
    ) -> str:
        """Apply a suggestion as the author left it (edited or not). Returns
        what was done, in words. Raises KeyError for a suggestion or target
        that is gone, ValueError for an empty text."""
        self._refuse_in_private("Changing the lore or a card")
        suggestion = self._story_suggestion(suggestion_id)
        text = (content if content is not None else suggestion.content).strip()
        if not text:
            raise ValueError("there is nothing to add")
        # An earlier review's Undo would put the lore and the cards back as
        # they were before it: this change gone, and the suggestion with it.
        self.forget_review_undo()
        if suggestion.kind == "lore_new":
            name = (title if title is not None else suggestion.title).strip()
            if not name:
                raise ValueError("a lore entry needs a title")
            words = list(keywords) if keywords is not None else list(suggestion.keywords)
            self.bundle.lore.append(
                LoreEntry(
                    title=name,
                    content=text,
                    keywords=[k.strip() for k in words if k.strip()],
                    came_from=FROM_THE_STORY,
                )
            )
            done = f"Added the lore entry {name}."
        elif suggestion.kind == "lore_change":
            entry = next((e for e in self.bundle.lore if e.id == suggestion.target_id), None)
            if entry is None:
                raise KeyError("the lore entry is gone")
            entry.content = text
            done = f"Changed the lore entry {entry.title}."
        else:
            card = next(
                (c for c in (*self.cast, *self.supporting) if c.id == suggestion.target_id), None
            )
            if card is None:
                raise KeyError("the character is gone")
            card.full_description = with_addition(card.full_description or "", text)
            done = f"Added to {card.name}'s card."
        self.story.story_suggestions.remove(suggestion)
        self.story.updated_at = utc_now_iso()
        self.save()
        return done

    def decline_story_suggestion(self, suggestion_id: str) -> None:
        """Not this: gone, and never offered again."""
        suggestion = self._story_suggestion(suggestion_id)
        self.story.story_suggestions.remove(suggestion)
        key = decline_key(suggestion)
        if key not in self.story.suggestions_declined:
            self.story.suggestions_declined.append(key)
        del self.story.suggestions_declined[:-DECLINED_MOST]
        self.save()
