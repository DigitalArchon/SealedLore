"""The cast's cards on the session's side: how they fit their room, and
condensing the ones that don't. A mixin for `StorySession`; the pure parts
are engine/card_condense.py and engine/cast_fit.py.

Condensed cards are written in the background, as chapters are: the requests
are built by the session's one writer (the worker after a passage, or the
window when nothing runs), the calls go out on a thread with a `detached()`
client, and the results are adopted by the writer at the start of the next
turn or by the window's poll. Meanwhile a card to be condensed is sent whole
(`CastFit.waiting`). A condensed text written from a card that has changed
since is not used. A provider without `detached` (the scripted mock)
condenses inline.

A supporting card is condensed, to about 150 words, when its description
is too long to ride the turns that name it whole (engine/characters.py).

Never in a chat (no cast) or a private scene (nothing else is called while
one is open). Cards are condensed for a private scene's room too, when a
private model is set up, so a scene finds them written.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.card_condense import (
    CONDENSE_LEAST_WORDS,
    CONDENSE_MAX_TOKENS,
    build_condense_messages,
    fingerprint,
    parse_condensed,
)
from sealedlore.engine.cast_fit import CastFit, fit_cast
from sealedlore.engine.characters import needs_condensing
from sealedlore.engine.session_merge import _close
from sealedlore.models.character import Character
from sealedlore.providers.base import ChatRequest
from sealedlore.threads import BackgroundThread


@dataclass
class _Condense:
    character_id: str
    name: str
    # The card as it was asked from: a card changed meanwhile isn't given it.
    fingerprint: str
    request: ChatRequest
    reply: tuple[str, Any] | None = None
    error: str | None = None


@dataclass
class _CondenseJob:
    items: list[_Condense]
    done: threading.Event = field(default_factory=threading.Event)


class CastRuntime:
    _condense_job: _CondenseJob | None = None
    # A card whose condensing failed, by id: (its fingerprint, why). Not asked
    # for again on its own until the card changes or the author asks.
    _condense_failed: dict[str, tuple[str, str]]

    def _count(self, text: str) -> int:
        return self.estimator.estimate(text, self.model)

    def cast_fit(self) -> CastFit:
        """How the cast's cards go on the next turn. No network."""
        options = self.assembly_options()
        return fit_cast(self.visible_cast(), options.cast_token_cap, self._count, texts=self.texts)

    def private_cast_fit(self) -> CastFit | None:
        """How they go in a private scene, when a private model is set up."""
        if self.config.private() is None or self.story.chat:
            return None
        return fit_cast(
            self.visible_cast(),
            self.config.cast_cap(self.config.private_budget),
            self._count,
            waiting_form="short",
            texts=self.texts,
        )

    def condense_failures(self) -> dict[str, str]:
        """Why a card's condensed text couldn't be written, by character id,
        while the card is as it was."""
        failed = getattr(self, "_condense_failed", None) or {}
        by_id = {c.id: c for c in (*self.cast, *self.supporting)}
        return {
            cid: why
            for cid, (sha, why) in failed.items()
            if cid in by_id and fingerprint(by_id[cid]) == sha
        }

    def cards_to_condense(self) -> list[Character]:
        """The cards waiting for a condensed text, for the next turn's room
        or a private scene's, less those being written or that failed as
        they stand."""
        if self.story.chat:
            return []
        waiting: dict[str, None] = dict.fromkeys(self.cast_fit().waiting)
        private = self.private_cast_fit()
        if private is not None:
            waiting.update(dict.fromkeys(private.waiting))
        waiting.update(
            dict.fromkeys(c.id for c in self.visible_supporting() if needs_condensing(c))
        )
        busy = self.condensing_ids()
        failed = self.condense_failures()
        by_id = {c.id: c for c in (*self.cast, *self.supporting)}
        return [by_id[cid] for cid in waiting if cid not in busy and cid not in failed]

    def condensing_ids(self) -> set[str]:
        job = self._condense_job
        return {item.character_id for item in job.items} if job is not None else set()

    def condensing_ready(self) -> bool:
        job = self._condense_job
        return job is not None and job.done.is_set()

    def author_name_for(self, character: Character) -> str | None:
        """Whom the author plays beside `character`: the one held, else the
        one suggested, never the character itself."""
        for wanted in (self.story.held_character_id, self.story.setup.suggested_character_id):
            other = next((c for c in self.cast if c.id == wanted), None)
            if other is not None and other.id != character.id and other.name.strip():
                return other.name
        return None

    def _condense_request(self, character: Character, words: int | None = None) -> ChatRequest:
        model = self.authoring_model
        return ChatRequest(
            model=model,
            extra_body=self.route_for("authoring"),
            messages=build_condense_messages(
                character, player=self.author_name_for(character), words=words, texts=self.texts
            ),
            params=self.side_params(model, max_tokens=CONDENSE_MAX_TOKENS),
        )

    def start_condensing(self, characters: Sequence[Character] | None = None) -> bool:
        """Have the waiting cards (or `characters`, when the author asks for
        them) condensed; never blocks with a provider that has `detached`.
        False when there was nothing to do or a job is still out."""
        if self.story.chat or self.in_private or self._condense_job is not None:
            return False
        wanted = list(characters) if characters is not None else self.cards_to_condense()
        if not wanted:
            return False
        failed = getattr(self, "_condense_failed", None)
        if failed is None:
            failed = self._condense_failed = {}
        for character in wanted:
            failed.pop(character.id, None)
        supporting = {c.id for c in self.supporting}
        items = [
            _Condense(
                c.id,
                c.name,
                fingerprint(c),
                self._condense_request(c, CONDENSE_LEAST_WORDS if c.id in supporting else None),
            )
            for c in wanted
        ]
        job = _CondenseJob(items=items)
        detached = getattr(self.provider, "detached", None)
        client = detached() if callable(detached) else None

        def run(provider: Any) -> None:
            try:
                for item in items:
                    try:
                        item.reply = provider.complete(item.request)
                    except Exception as exc:  # noqa: BLE001 - reported at adoption
                        item.error = str(exc) or type(exc).__name__
            finally:
                job.done.set()

        self._condense_job = job
        if client is None:
            run(self.provider)
            return True

        def background() -> None:
            try:
                run(client)
            finally:
                _close(client)

        BackgroundThread(target=background, name="card-condense", daemon=True).start()
        return True

    def adopt_condensed(self) -> list[str]:
        """Put finished condensed cards on their characters; the notices to
        show. The worker calls it at the start of a turn, the window when
        nothing runs: never both at once. Waits while a private scene is
        open (its cast is frozen)."""
        job = self._condense_job
        if job is None or not job.done.is_set() or self.in_private:
            return []
        self._condense_job = None
        failed = getattr(self, "_condense_failed", None)
        if failed is None:
            failed = self._condense_failed = {}
        by_id = {c.id: c for c in (*self.cast, *self.supporting)}
        written: list[str] = []
        problems: list[str] = []
        for item in job.items:
            log_ref = self._log(
                "condense_request",
                {
                    "character_id": item.character_id,
                    "payload": self.provider.build_payload(item.request),
                    "background": True,
                },
            )
            if item.reply is None:
                self._log("condense_response", {"request_log_ref": log_ref, "error": item.error})
                failed[item.character_id] = (item.fingerprint, item.error or "it failed")
                problems.append(item.name)
                continue
            text, completed = item.reply
            character = by_id.get(item.character_id)
            dropped = None
            if character is None:
                dropped = "not adopted: the character is gone"
            elif fingerprint(character) != item.fingerprint:
                dropped = "not adopted: the card changed while it was written"
            elif completed.finish_reason == "length":
                dropped = "not adopted: cut off"
            self._log(
                "condense_response",
                {
                    "request_log_ref": log_ref,
                    "text": text,
                    "usage": completed.raw_usage,
                    **({"dropped": dropped} if dropped else {}),
                },
            )
            self.unreported_usage.append(self.priced(completed.usage, item.request.model))
            if dropped == "not adopted: cut off":
                failed[item.character_id] = (
                    item.fingerprint,
                    "the model ran out of room before it finished the card",
                )
                problems.append(item.name)
                continue
            if character is None or dropped:
                continue
            try:
                condensed = parse_condensed(text, character)
            except ValueError as exc:
                failed[item.character_id] = (item.fingerprint, str(exc))
                problems.append(item.name)
                continue
            character.condensed = condensed
            character.condensed_from = item.fingerprint
            written.append(character.name)
        if written:
            self.save()
        notices = []
        if written:
            fit = self.cast_fit()
            state = (
                "the cast still doesn't fit its room: see the cast board"
                if fit.still_over
                else "the cast fits its room"
            )
            notices.append(f"Condensed the cards of {_names(written)}: {state}.")
        if problems:
            notices.append(
                f"Couldn't condense the cards of {_names(problems)}: they go whole "
                "meanwhile. Their card on the cast board has Try again."
            )
        return notices

    def condense_card(self, character: Character, *, words: int | None = None) -> str:
        """Write `character`'s condensed card now, on the authoring model, and
        keep it on them. Network: off the GUI thread. Raises ProviderError, or
        ValueError for a reply cut off or empty (half a card never stands in
        for the card)."""
        self._refuse_in_chat("Condensing a card")
        self._refuse_in_private("Condensing a card")
        request = self._condense_request(character, words)
        log_ref = self._log(
            "condense_request",
            {"character_id": character.id, "payload": self.provider.build_payload(request)},
        )
        text, completed = self.provider.complete(request)
        self._log(
            "condense_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, request.model))
        if completed.finish_reason == "length":
            raise ValueError("the model ran out of room before it finished the card")
        condensed = parse_condensed(text, character)
        character.condensed = condensed
        character.condensed_from = fingerprint(character)
        return condensed

    def set_condensed(self, character: Character, text: str) -> None:
        """The author's own condensed text: kept as written, and current for
        the card as it now stands (they wrote it from that card). Blank takes
        it off."""
        cleaned = text.strip()
        character.condensed = cleaned or None
        character.condensed_from = fingerprint(character) if cleaned else None
        failed = getattr(self, "_condense_failed", None)
        if failed:
            failed.pop(character.id, None)

    def _drop_condensing(self, reason: str) -> None:
        """The story is being left: what the job has is logged, not adopted."""
        job = self._condense_job
        if job is None:
            return
        self._condense_job = None
        for item in job.items:
            if item.reply is None and item.error is None:
                continue
            log_ref = self._log(
                "condense_request",
                {
                    "character_id": item.character_id,
                    "payload": self.provider.build_payload(item.request),
                    "background": True,
                },
            )
            if item.reply is None:
                self._log("condense_response", {"request_log_ref": log_ref, "error": item.error})
                continue
            text, completed = item.reply
            self._log(
                "condense_response",
                {
                    "request_log_ref": log_ref,
                    "text": text,
                    "usage": completed.raw_usage,
                    "dropped": reason,
                },
            )


def _names(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]
