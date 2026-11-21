"""The session's side of condensed always-on lore (engine/lore_condense.py):
which always-on entries are sent condensed, how they stand against the lore
budget (the Lore page says it), and writing condensed versions on the
author's word.

The lore budget the always-on entries are held to is the story's own lore
budget under the rules it fires by: SillyTavern's (its share of the context)
under SillyTavern's, the room for chosen lore (Settings → Lore) under
SealedLore's; a private scene's of the private model's context.

Condensed versions are written on the authoring model, one entry a call,
only of entries every model may see (never one the plot hides or one held
back until mentioned), never in a chat or a private scene."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace

from sealedlore.engine.archival import why_not_whole
from sealedlore.engine.lore_condense import (
    CONDENSE_MAX_TOKENS,
    StandingFit,
    build_lore_condense_messages,
    fit_standing,
    least_important_first,
    parse_condensed_lore,
    unasked_placeholders,
    worth_condensing,
)
from sealedlore.engine.retrieval import LoreLayout
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.tavern_firing import budget_of
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.providers.base import ChatRequest, ProviderError


class LoreCondenseRuntime:
    def _budget_now(self, budget: int | None = None) -> int:
        if budget is not None:
            return budget
        if self.in_private:
            return self.config.private_budget
        return self.story.defaults.context_token_budget

    def standing_room(self, budget: int | None = None) -> int:
        """The lore budget the always-on entries are held to."""
        context = self._budget_now(budget)
        if self.fires_like_tavern:
            return budget_of(self.tavern_settings(), context)
        return self.config.lore_cap(context)

    def _importance(self) -> Callable[[LoreEntry], float]:
        if self.fires_like_tavern:
            return lambda entry: self._fire_entry(entry).order
        return lambda entry: entry.priority

    def _fit_layout(self, layout: LoreLayout, budget: int | None = None) -> LoreLayout:
        if layout.mode == "whole":
            # A lorebook small enough to go whole is within its own limit.
            return layout
        fit = self.standing_fit(layout.standing, budget)
        return replace(layout, standing=fit.entries, condensed=fit.condensed)

    def standing_fit(
        self, standing: Sequence[LoreEntry] | None = None, budget: int | None = None
    ) -> StandingFit:
        """The always-on entries as sent, against the lore budget."""
        if standing is None:
            layout = self._layout_as_written(budget)
            if layout.mode == "whole":
                # Sent whole as a small lorebook is: within its own limit.
                whole = [e for e in layout.standing if e.always_on]
                tokens = sum(
                    self._count_lore_tokens(f"## {e.title}\n{e.content.strip()}") for e in whole
                )
                return StandingFit(tuple(whole), (), tokens, tokens, max(tokens, layout.limit))
            standing = layout.standing
        order = [e for e in self.bundle.lore if any(e.id == s.id for s in standing)]
        return fit_standing(
            standing,
            room=self.standing_room(budget),
            count_tokens=self._count_lore_tokens,
            least_first=least_important_first(order or list(standing), self._importance()),
            use_condensed=self.story.defaults.condense_lore,
        )

    def always_on_entries(self) -> list[LoreEntry]:
        """The always-on entries a model may see, in the lorebook's order:
        what the Always-on entries dialog lists."""
        return [entry for entry in self.visible_lore() if entry.enabled and entry.always_on]

    # --- writing them ----------------------------------------------------------

    def condense_candidates(self, entries: Sequence[LoreEntry] | None = None) -> list[LoreEntry]:
        """Of `entries` (the always-on ones), those worth a condensed version
        that have none for their text as it stands."""
        pool = self.always_on_entries() if entries is None else entries
        visible = {entry.id for entry in self.visible_lore()}
        return [
            entry
            for entry in pool
            if entry.id in visible and worth_condensing(entry.content) and not entry.condensed_now()
        ]

    def condense_lore(self, entries: Sequence[LoreEntry]) -> Iterator[SessionNotice]:
        """Write a condensed version of each of `entries`, beside it. Each
        kept as it comes, so a Stop keeps those written. Refused in a chat
        and a private scene; never an entry no model may see."""
        self._refuse_in_chat("Condensing lore")
        self._refuse_in_private("Condensing lore")
        visible = {entry.id for entry in self.visible_lore()}
        wanted = [entry for entry in entries if entry.id in visible]
        written: dict[str, str] = {}
        failed: list[str] = []
        items = [(entry.id, entry.title, entry.content) for entry in wanted]
        try:
            yield from self.condense_texts(items, written, failed)
        finally:
            by_id = {entry.id: entry for entry in wanted}
            held = next((c for c in self.cast if c.id == self.story.held_character_id), None)
            user = held.name if held is not None else "the user"
            for entry_id, text in written.items():
                entry = by_id[entry_id]
                # A story's entries have their names written in already.
                entry.condensed = unasked_placeholders(text, entry.content, user)
                entry.condensed_from = lore_fingerprint(entry.content)
            if written:
                self.forget_review_undo()
                self.save()
        if failed:
            yield SessionNotice("Couldn't condense: " + "; ".join(failed) + ".")

    def condense_texts(
        self,
        items: Sequence[tuple[str, str, str]],
        into: dict[str, str],
        failed: list[str],
    ) -> Iterator[SessionNotice]:
        """(key, title, text) each condensed into `into[key]`; what couldn't
        be into `failed`, in words. For a story's entries or a kept
        lorebook's (Lorebooks, on Your characters' session)."""
        self._refuse_in_private("Condensing lore")
        for index, (key, title, content) in enumerate(items, start=1):
            yield SessionNotice(f"Condensing “{title}” ({index} of {len(items)})…")
            try:
                text = self._lore_condense_call(title, content)
            except (ProviderError, ValueError) as exc:
                failed.append(f"“{title}” ({exc})")
                continue
            if len(text.split()) >= len(content.split()):
                failed.append(f"“{title}” (what came back was no shorter)")
                continue
            into[key] = text

    def _lore_condense_call(self, title: str, content: str) -> str:
        model = self.authoring_model
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("authoring"),
            messages=build_lore_condense_messages(title, content, texts=self.texts),
            params=self.side_params(model, max_tokens=CONDENSE_MAX_TOKENS),
            purpose="lore_condense_request",
        )
        log_ref = self._log(
            "lore_condense_request",
            {"title": title, "payload": self.provider.build_payload(request)},
        )
        text, completed = self.provider.complete(request)
        self._log(
            "lore_condense_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, model))
        unfit = why_not_whole(completed)
        if unfit:
            # Half an entry never stands in for the whole.
            raise ValueError(f"the reply wasn't whole: {unfit}")
        return parse_condensed_lore(text, title)

    def set_condensed_lore(self, entry: LoreEntry, text: str) -> None:
        """The author's own condensed version: current for the entry as it
        stands (they wrote it from that text). Blank takes it off."""
        cleaned = text.strip()
        entry.condensed = cleaned or None
        entry.condensed_from = lore_fingerprint(entry.content) if cleaned else None
