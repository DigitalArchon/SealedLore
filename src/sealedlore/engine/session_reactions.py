"""StorySession's side of the reaction round (engine/reactions.py).

After a passage lands and the reads have kept the scene, every character in
it whose card names a model answers the passage. Every request is built
before any goes out, and they go out side by side, one client each
(`base.side_by_side`), so they answer "at once" and blind to each other.
The model is the card's reference as written ("Local::maela-lora"), which
the session's provider resolves to its endpoint like any other, so a
fine-tuned local model is reached on its own server.

The answers are kept on the passage (`NodeMeta.reactions`) and reach the
storyteller once, in the tail of the turn that answers that passage
(`rules.render_round_block`): Regenerate sends them again, edited if the
author edited one. No other model reads them.

The round never costs the turn: an answer that fails, is declined, cut off
or empty is a notice. A Stop skips the round, as it skips the reads. A
passage that was stopped or refused gets no round, and none runs in a
simple chat or a private scene.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator, Iterator
from typing import Any

from sealedlore.engine import reactions
from sealedlore.engine.reactions import (
    due_to_recollect,
    fingerprint,
    memory_chain,
    reacting_characters,
    reaction_messages,
    recollect_messages,
    seen_entries,
)
from sealedlore.engine.routing import routable, route_body
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.ids import utc_now_iso
from sealedlore.models.memory import Recollection
from sealedlore.models.node import Reaction
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    StreamCancelled,
    side_by_side,
    why_not_whole,
)
from sealedlore.threads import BackgroundThread


class ReactionRuntime:
    def own_route_body(self, character) -> dict[str, Any]:
        """The request fields for the route chosen for a character's own
        model: none in a private scene, off NanoGPT, or for a TEE or encrypted
        model (engine/routing.py `routable`), as for a role's."""
        model = (character.own_model or "").strip()
        if self.open_span is not None or not model:
            return {}
        provider = self.config.endpoint_for(model)
        if provider is None or not routable(provider.base_url, model, provider.api):
            return {}
        return route_body(character.own_route, model)

    def react_after_turn(self) -> Iterator[SessionNotice]:
        """The round after a passage: each character played by a model of
        their own answers it, blind to the others and to the author's turn.
        Not when the story has them answer after the author's turn instead
        (`react_to_turn`)."""
        if self.story.chat or self.in_private or self.story.defaults.round_order == "after":
            return
        result = self.last_result
        if result is None or result.unread or result.node is None:
            return  # nothing landed to answer: stopped, refused or empty
        passage = result.node
        if passage.kind != "assistant":
            return
        yield from self._round(self.path(), passage)

    def react_to_turn(self, turn_node, history) -> Iterator[SessionNotice]:
        """The round after the author's turn (`StoryDefaults.round_order`
        "after"): each character played by a model of their own answers it,
        having seen it, blind to the others; before the storyteller writes,
        which is given their answers with the turn (`TurnRequest.round_answers`).
        Kept on the turn, marked as answered so (`round_after`) even with none,
        so Regenerate sends the same and never the passage before's."""
        turn_node.meta.round_after = True
        turn_node.meta.reactions = []
        if self.story.chat or self.in_private:
            return
        held = self._character(turn_node.meta.controlled_character_id)
        player = held.name if held is not None else "The author"
        yield from self._round([*history, turn_node], turn_node, after_turn_of=player)

    def _round(self, history, store_on, *, after_turn_of: str | None = None):
        """One round: everyone in the scene who has a model of their own
        answers what `history` ends on; kept on `store_on`."""
        cast = self.visible_cast(history)
        characters = reacting_characters(
            cast, self.story.scene, held_character_id=self.story.held_character_id
        )
        if not characters:
            return
        # What the round answers as it now stands: a round after a Continue
        # or a change replaces the earlier one's, and a Stop (which skips the
        # round) leaves none rather than stale ones.
        store_on.meta.reactions = []
        passage = store_on

        def count(text: str) -> int:
            return self.estimator.estimate(text)

        # What each remembers of what they saw before their recent window,
        # written first where a stretch has newly fallen out of it.
        yield from self._recollect_due(characters, history, cast, count)

        # Every request before any goes out: each is given only its own
        # character's view, so no answer can reach another's prompt.
        calls: list[tuple[Any, ChatRequest, str]] = []
        for character in characters:
            model = (character.own_model or "").strip()  # "Endpoint::model", kept whole
            request = ChatRequest(
                model=model,
                messages=reaction_messages(
                    story=self.story,
                    cast=cast,
                    character=character,
                    history=history,
                    texts=self.texts,
                    count=count,
                    budget=reactions.HISTORY_TOKENS,
                    recollections=self.bundle.memories,
                    after_turn_of=after_turn_of,
                ),
                params=self.side_params(model),
                # The character's own route: a route is otherwise a role's,
                # and their model is no role's.
                extra_body=self.own_route_body(character),
                use_cache_control=self._cache_control_for(model),
                cache_ttl=self.config.cache_ttl,
            )
            log_ref = self._log(
                "own_request",
                {
                    "node_id": passage.id,
                    "character": character.name,
                    "payload": self.provider.build_payload(request),
                },
            )
            calls.append((character, request, log_ref))

        boxes = yield from self._answer_side_by_side(
            [request for _, request, _ in calls],
            "Waiting for the characters played by their own model…",
        )

        answers: list[Reaction] = []
        stopped = False
        for (character, request, log_ref), box in zip(calls, boxes, strict=True):
            error = box.get("error")
            if isinstance(error, StreamCancelled):
                stopped = True
                self._log("own_response", {"request_log_ref": log_ref, "stopped_early": True})
                continue
            if isinstance(error, (ProviderError, ValueError)):
                self._log("own_response", {"request_log_ref": log_ref, "error": str(error)})
                yield SessionNotice(f"{character.name}'s model couldn't answer: {error}")
                continue
            if error is not None:
                raise error
            text, completed = box["reply"]
            # Logged whatever came of it, so its cost counts.
            self._log(
                "own_response",
                {
                    "request_log_ref": log_ref,
                    "node_id": passage.id,
                    "character": character.name,
                    "text": text,
                    "usage": completed.raw_usage,
                    "finish_reason": completed.finish_reason,
                },
            )
            why = why_not_whole(completed) or ("it was empty" if not text.strip() else None)
            if why:
                yield SessionNotice(f"{character.name}'s model gave no answer: {why}.")
                continue
            answers.append(
                Reaction(
                    character_id=character.id,
                    name=character.name,
                    text=text.strip(),
                    model=request.model,
                    usage=self.priced(completed.usage, request.model),
                )
            )
        passage.meta.reactions = answers
        self.save()
        if stopped:
            raise StreamCancelled()
        if answers:
            names = ", ".join(answer.name for answer in answers)
            yield SessionNotice(
                f"{names} answered your turn. The storyteller works their answers in with it."
                if after_turn_of is not None
                else f"{names} answered. The storyteller gets their answers with your next "
                "turn, as what they mean to do."
            )

    def _recollect_due(self, characters, history, cast, count) -> Iterator[SessionNotice]:
        """Write what each character remembers of the stretches of the story
        they saw that are now older than their recent window, all at once, on
        the summarisation model. A failed one is said and skipped: the round
        goes on, sending as much of the story whole as fits."""
        jobs = []
        for character in characters:
            seen = seen_entries(
                history, character, cast, start=self.story.setup.starting_scene, texts=self.texts
            )
            _chain, covered = memory_chain(self.bundle.memories, character.id, seen)
            jobs += [
                (character, chunk)
                for chunk in due_to_recollect(
                    seen,
                    covered,
                    count=count,
                    budget=reactions.HISTORY_TOKENS,
                    size=reactions.RECOLLECT_MESSAGES,
                )
            ]
        if not jobs:
            return
        model = self.summarization_model
        requests = [
            ChatRequest(
                model=model,
                extra_body=self.route_for("summarisation"),
                messages=recollect_messages(character, chunk, texts=self.texts),
                params=self.side_params(model),
            )
            for character, chunk in jobs
        ]
        refs = [
            self._log(
                "recollect_request",
                {
                    "character": character.name,
                    "node_ids": [node_id for node_id, _text in chunk],
                    "payload": self.provider.build_payload(request),
                },
            )
            for (character, chunk), request in zip(jobs, requests, strict=True)
        ]
        names = ", ".join(dict.fromkeys(character.name for character, _chunk in jobs))
        boxes = yield from self._answer_side_by_side(
            requests, f"Writing what {names} remember{'s' if ',' not in names else ''}…"
        )
        stopped = False
        written = 0
        for (character, chunk), request, ref, box in zip(jobs, requests, refs, boxes, strict=True):
            error = box.get("error")
            if isinstance(error, StreamCancelled):
                stopped = True
                continue
            if isinstance(error, (ProviderError, ValueError)):
                self._log("recollect_response", {"request_log_ref": ref, "error": str(error)})
                yield SessionNotice(f"{character.name}'s memory couldn't be written: {error}")
                continue
            if error is not None:
                raise error
            text, completed = box["reply"]
            self._log(
                "recollect_response",
                {"request_log_ref": ref, "text": text, "usage": completed.raw_usage},
            )
            why = why_not_whole(completed) or ("it was empty" if not text.strip() else None)
            if why:
                yield SessionNotice(f"{character.name}'s memory wasn't written: {why}.")
                continue
            ids = [node_id for node_id, _text in chunk]
            # One a stretch: an older account of the same start (an edit, a
            # branch) gives way to this one.
            self.bundle.memories = [
                memory
                for memory in self.bundle.memories
                if memory.character_id != character.id or memory.covered_node_ids[:1] != ids[:1]
            ]
            self.bundle.memories.append(
                Recollection(
                    character_id=character.id,
                    covered_node_ids=ids,
                    fingerprint=fingerprint(chunk),
                    content=text.strip(),
                    model=request.model,
                    usage=self.priced(completed.usage, request.model),
                )
            )
            written += 1
        if written:
            self.save()
        if stopped:
            raise StreamCancelled()

    def _answer_side_by_side(
        self, requests: list[ChatRequest], waiting: str
    ) -> Generator[SessionNotice, None, list[dict[str, Any]]]:
        """Each request on a client of its own, at once, as the reads go
        (session_plot `_read_after_turn`); with fewer clients than requests,
        the last takes the rest one at a time. Returns one box per request:
        its "reply" or its "error". A Stop cancels every client, which ends
        the calls; this waits for them either way."""
        lines = side_by_side(self.provider, len(requests))
        turns = {id(line): threading.Lock() for line in lines}
        boxes: list[dict[str, Any]] = [{} for _ in requests]
        threads: list[BackgroundThread] = []
        for i, request in enumerate(requests):
            line = lines[min(i, len(lines) - 1)]

            def call(line=line, box=boxes[i], request=request) -> None:
                try:
                    with turns[id(line)]:
                        box["reply"] = line.complete(request)
                except BaseException as exc:  # noqa: BLE001 - handled by the caller
                    box["error"] = exc

            thread = BackgroundThread(target=call, name="own-model", daemon=True)
            thread.start()
            threads.append(thread)
        try:
            yield SessionNotice(waiting, waiting_since=time.monotonic())
            for thread in threads:
                thread.join()
        finally:
            for thread in threads:
                thread.join()
        return boxes

    def character_prompt(self, character_id: str) -> list:
        """What the model playing this character would be sent if their round
        ran now (the character's prompt dialog): the same request as
        `_round` builds, on the path as it stands, with the memory notes
        already written (none is written for a look)."""
        history = self.path()
        cast = self.visible_cast(history)
        character = next((c for c in cast if c.id == character_id), None)
        if character is None:
            raise ValueError("no such character in the story")
        after = None
        if self.story.defaults.round_order == "after":
            held = self._character(self.story.held_character_id)
            after = held.name if held is not None else "The author"
        return reaction_messages(
            story=self.story,
            cast=cast,
            character=character,
            history=history,
            texts=self.texts,
            count=self.estimator.estimate,
            budget=reactions.HISTORY_TOKENS,
            recollections=self.bundle.memories,
            after_turn_of=after,
        )

    def memories_of(self, character_id: str) -> list[Recollection]:
        """Their memory notes in force on this path, oldest first."""
        history = self.path()
        cast = self.visible_cast(history)
        character = next((c for c in cast if c.id == character_id), None)
        if character is None:
            return []
        seen = seen_entries(
            history, character, cast, start=self.story.setup.starting_scene, texts=self.texts
        )
        chain, _covered = memory_chain(self.bundle.memories, character_id, seen)
        return list(chain)

    def edit_memory(self, memory_id: str, text: str) -> None:
        """The author's words for one of a character's memory notes: they
        stand while the messages it covers are as they were."""
        self._refuse_in_private("Editing a character's memory")
        memory = next((m for m in self.bundle.memories if m.id == memory_id), None)
        if memory is None:
            raise ValueError("no such memory")
        if not text.strip():
            raise ValueError("a memory can't be empty")
        memory.content = text.strip()
        memory.edited_at = utc_now_iso()
        self.save()

    def set_own_notes(self, character_id: str, notes: str) -> None:
        """The author's notes for the model playing a character."""
        self._refuse_in_private("Changing a character's notes")
        character = self._character(character_id)
        if character is None:
            raise ValueError("no such character")
        character.own_notes = notes.strip() or None
        self.save()

    def edit_reaction(self, node_id: str, character_id: str, text: str) -> None:
        """The author's edit of what a character answered a passage with.
        Regenerating the passage after it sends the edited answer."""
        self._refuse_in_private("Editing a character's answer")
        node = next((n for n in self.nodes if n.id == node_id), None)
        if node is None:
            raise ValueError("no such message")
        if not text.strip():
            raise ValueError("an answer can't be empty")
        for reaction in node.meta.reactions:
            if reaction.character_id == character_id:
                reaction.text = text.strip()
                reaction.edited_at = utc_now_iso()
                break
        else:
            raise ValueError("that character didn't answer this passage")
        self.save()
