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

from sealedlore.engine.reactions import reacting_characters, reaction_messages
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.ids import utc_now_iso
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
    def react_after_turn(self) -> Iterator[SessionNotice]:
        """The round after a passage: each character played by a model of
        their own answers it, blind to the others and to the author's turn."""
        if self.story.chat or self.in_private:
            return
        result = self.last_result
        if result is None or result.unread or result.node is None:
            return  # nothing landed to answer: stopped, refused or empty
        passage = result.node
        if passage.kind != "assistant":
            return
        history = self.path()
        cast = self.visible_cast(history)
        characters = reacting_characters(
            cast, self.story.scene, held_character_id=self.story.held_character_id
        )
        if not characters:
            return
        # The passage as it now stands is what this round answers: a round
        # after a Continue or a change replaces the earlier one's, and a Stop
        # (which skips the round) leaves none rather than stale ones.
        passage.meta.reactions = []

        def count(text: str) -> int:
            return self.estimator.estimate(text)

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
                ),
                params=self.side_params(model),
                # No route: a route is a role's (models/route.py), and a
                # character's own model is no role's.
                extra_body={},
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

        boxes = yield from self._answer_side_by_side([request for _, request, _ in calls])

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
                f"{names} answered. The storyteller gets their answers with your next turn, "
                "as what they mean to do."
            )

    def _answer_side_by_side(
        self, requests: list[ChatRequest]
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
            yield SessionNotice(
                "Waiting for the characters played by their own model…",
                waiting_since=time.monotonic(),
            )
            for thread in threads:
                thread.join()
        finally:
            for thread in threads:
                thread.join()
        return boxes

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
