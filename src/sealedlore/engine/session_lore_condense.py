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
back until mentioned), never in a chat or a private scene.

The parts of an entry sent condensed that a turn is about go with the turn
from its whole text as well (engine/lore_whole_back.py, `_with_in_full`), to
every storyteller but those engine/model_tuning.py leaves them out for
(Claude: `lore_from_full_on`, asked of the storyteller's model in force):
ranked against the turn with the rest of the lore, by similarity where there
are embeddings (each part's vector made and cached as an entry's is; the
preview's from vectors already made, never embedding), by keyword scoring
alone without them and in a private scene."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace

from sealedlore.engine import lore_whole_back
from sealedlore.engine.archival import why_not_whole
from sealedlore.engine.lore_condense import (
    StandingFit,
    build_lore_condense_messages,
    fit_standing,
    least_important_first,
    parse_condensed_lore,
    reply_tokens,
    unasked_placeholders,
    worth_condensing,
)
from sealedlore.engine.model_tuning import sends_lore_from_full
from sealedlore.engine.retrieval import (
    LoreLayout,
    RetrievalReport,
    cosine_scores,
    embedding_text,
    turn_query,
    turn_ranking,
)
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.tavern_firing import budget_of
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.models.node import Node
from sealedlore.providers.base import ChatRequest, ProviderError


@dataclass(frozen=True)
class InFullRanking:
    """The turn's ranking of the lore (`in_full_ranking`): best first, the
    parts of the entries sent condensed by `lore_whole_back.piece_id`; the
    similarities and why there are none, the text ranked by, and the whole
    texts of the entries sent condensed (names filled), in the order sent."""

    ranked: list[tuple[str, float | None]]
    similarity: dict[str, float] | None
    why: str | None
    text: str
    condensed: list[LoreEntry]


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
            layout = self._layout_named(budget)
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

    # --- parts given with the turn from the whole text ----------------------

    def storyteller_model(self) -> str:
        """The model writing the story's turns now: an open private scene's,
        else the story's own (a Question goes to the same)."""
        if self.in_private:
            return self.private_model() or self.model
        return self.model

    def lore_from_full_on(self) -> bool:
        """Whether parts of the always-on entries sent condensed go with the
        turns: on (`lore_whole_back.WHOLE_BACK_TOP`), never in a chat, and
        only for a storyteller model engine/model_tuning.py sends them to."""
        return (
            lore_whole_back.WHOLE_BACK_TOP > 0
            and not self.story.chat
            and sends_lore_from_full(self.storyteller_model())
        )

    def _with_in_full(
        self,
        report: RetrievalReport,
        turn,  # noqa: ANN001 - TurnRequest
        history: Sequence[Node],
        layout: LoreLayout,
        *,
        named: LoreLayout | None = None,
        allow_network: bool,
    ) -> RetrievalReport:
        """`report` with the parts of the always-on entries sent condensed
        that the turn is about added, from their whole texts
        (engine/lore_whole_back.py). `layout` as sent, `named` before any
        entry was condensed."""
        if not layout.condensed or not self.lore_from_full_on():
            return report
        ranking = self.in_full_ranking(
            turn, history, layout, named=named, allow_network=allow_network
        )
        texts = self.texts
        sent = lore_whole_back.parts_back(
            ranking.condensed,
            ranking.ranked,
            top=lore_whole_back.WHOLE_BACK_TOP,
            room=min(self._lore_room(), lore_whole_back.ROOM_MOST),
            count_tokens=self._count_lore_tokens,
            label=lambda title: texts.fill("turn.lore.from_full", title=title),
        )
        if not sent:
            return report
        return replace(
            report,
            injected=(*report.injected, *sent),
            tokens=report.tokens + sum(injection.tokens for injection in sent),
        )

    def _condensed_whole(
        self, layout: LoreLayout, named: LoreLayout | None = None
    ) -> list[LoreEntry]:
        """The whole texts, names filled, of the entries `layout` sends
        condensed, in its order."""
        named = named or self._layout_named()
        whole = {entry.id: entry for entry in named.standing}
        return [whole[entry_id] for entry_id in layout.condensed if entry_id in whole]

    def part_items(self) -> list[LoreEntry]:
        """Each part of the entries sent condensed, as an item of the turn's
        ranking (`lore_whole_back.ranked_item`), while parts go with the turn
        (`lore_from_full_on`); none while they don't, and in a chat."""
        if not self.lore_from_full_on():
            return []
        named = self._layout_named()
        return [
            lore_whole_back.ranked_item(piece)
            for entry in self._condensed_whole(self._fit_layout(named), named)
            for piece in lore_whole_back.pieces(entry)
        ]

    def _part_texts(self) -> dict[str, str]:
        """What is embedded of each part (`part_items`), by part id, while
        parts go with the turn (`ensure_lore_vectors` makes and keeps their
        vectors beside the entries')."""
        return {item.id: embedding_text(item) for item in self.part_items()}

    def in_full_ranking(
        self,
        turn,  # noqa: ANN001 - TurnRequest
        history: Sequence[Node],
        layout: LoreLayout,
        *,
        named: LoreLayout | None = None,
        allow_network: bool,
    ) -> InFullRanking:
        """The turn's ranking of the lore for `_with_in_full`. The pool: the
        entries chosen per turn, the always-on entries sent whole, and each
        part of the whole texts of those sent condensed, names filled."""
        named = named or self._layout_named()
        condensed = self._condensed_whole(layout, named)
        sent_whole = [entry for entry in named.standing if entry.id not in layout.condensed]
        parts = [
            lore_whole_back.ranked_item(piece)
            for entry in condensed
            for piece in lore_whole_back.pieces(entry)
        ]
        pool = [*layout.selectable, *sent_whole, *parts]
        text = turn_query(turn.user_text, history)
        similarity, why = self._in_full_similarity(
            pool,
            text,
            allow_network=allow_network,
            parts={item.id: embedding_text(item) for item in parts},
        )
        ranked = turn_ranking(
            pool, text, similarity, threshold=self.config.lore_similarity_threshold
        )
        return InFullRanking(ranked, similarity, why, text, condensed)

    def _in_full_similarity(
        self,
        pool: Sequence[LoreEntry],
        text: str,
        *,
        allow_network: bool,
        parts: dict[str, str],
    ) -> tuple[dict[str, float] | None, str | None]:
        """Each of `pool`'s similarity to `text`, or None with why: the
        entries' vectors and the `parts`' (id → what is embedded). Without
        the network (the prompt's preview, on the window's own thread) only
        vectors already made, the turn's included; in a private scene none:
        nothing but the scene's own model is called, and its lore is chosen
        by words alone."""
        if self.embeddings is None:
            return None, None
        if self.in_private:
            return None, "private scene: words only"
        if not text.strip() or not pool:
            return None, None
        try:
            if allow_network:
                vectors = self.ensure_lore_vectors(parts=parts)
                target = self.query_vector(text)
            else:
                vectors = self.ensure_lore_vectors(embed=False, parts=parts)
                target = self._remembered_query_vector(text)
        except ProviderError as exc:
            return None, str(exc)
        if target is None:
            return None, "preview: not embedded" if not allow_network else "no vector"
        scored = [(entry.id, vectors[entry.id]) for entry in pool if entry.id in vectors]
        try:
            found = cosine_scores(target, [vector for _, vector in scored])
        except ValueError as exc:
            # Vectors of another width (a model changed): words alone this turn.
            return None, str(exc)
        return {entry_id: score for (entry_id, _), score in zip(scored, found, strict=True)}, None

    def _remembered_query_vector(self, text: str) -> tuple[float, ...] | None:
        """The turn's vector if a selection has already embedded it, else None."""
        settings = self.config.embeddings()
        model = settings.model if settings else ""
        return (self._query_vectors or {}).get((text, model))

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
            params=self.side_params(model, max_tokens=reply_tokens(content)),
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
