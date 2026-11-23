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
author edited one. No other model reads them. One answer can be asked for
again (`regenerate_reaction`), with the request the round built for that
character there and nobody else asked.

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
from sealedlore.engine.model_tuning import tuned_texts
from sealedlore.engine.reactions import (
    due_to_recollect,
    fingerprint,
    memories_then,
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

# Said when a Stop ends the wait for their answers to the author's turn.
ROUND_SKIPPED = (
    "Stopped waiting for the characters played by their own model: the storyteller writes "
    "without their answers. Regenerate asks them again."
)
# Said with whatever stops an answer asked for again: the one there stands.
ANSWER_KEPT = "Their answer stays as it was."


def _not_asked(character, exc: Exception) -> str:
    """Why a character's model couldn't be asked: their request couldn't
    even be built (their model names an endpoint that isn't set up)."""
    model = (character.own_model or "").strip()
    return f"{character.name}'s model ({model or 'none named'}) couldn't be asked: {exc}"


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

        When the story has them answer after the author's turn instead
        (`react_to_turn`), only the record and the passage views run here,
        while the author writes: run before the answers, they were part of
        the wait before the storyteller began (Nov 22 2026, round_order kit:
        the first word 9.8-14.6s after sending, the answer alone 4-8s)."""
        if self.story.chat or self.in_private:
            return
        result = self.last_result
        if result is None or result.unread or result.node is None:
            return  # nothing landed to answer: stopped, refused or empty
        passage = result.node
        if passage.kind != "assistant":
            return
        history = self.path()
        yield from self.keep_public_record(history)
        yield from self.mark_views(history)
        if self.story.defaults.round_order == "after":
            return
        yield from self._round(history, passage)

    def react_to_turn(self, turn_node, history) -> Iterator[SessionNotice]:
        """The round after the author's turn (`StoryDefaults.round_order`
        "after"): each character played by a model of their own answers it,
        having seen it, blind to the others; before the storyteller writes,
        which is given their answers with the turn (`TurnRequest.round_answers`).
        Kept on the turn, marked as answered so (`round_after`) even with none,
        so Regenerate sends the same and never the passage before's.

        A Direction, a Narration and the opening get no round
        (`reactions.answers_turn`): the turn is marked the same way, with
        nobody's answer, and nothing is asked, waited for or said. So no
        other take of its passage asks either, and the storyteller is never
        told they answered.

        A Stop while their answers are waited for skips the wait, as the
        wait's words say and as it does after a passage: the job goes on, the
        storyteller writing without them (`ROUND_SKIPPED`). It used to end
        the job, which took the author's turn back out of the story, so a
        model that hung could not be got past. A round that didn't run to
        its end (stopped, or the job failed in it) leaves the turn unmarked:
        the next take of its passage asks them (`StorySession._round_owed`)."""
        turn_node.meta.round_after = True
        turn_node.meta.reactions = []
        if self.story.chat or self.in_private or not reactions.answers_turn(turn_node):
            return
        turn_node.meta.round_after = False  # until the round has run to its end
        yield from self.keep_public_record(history)
        yield from self.mark_views(history)
        try:
            yield from self._round(
                [*history, turn_node], turn_node, after_turn_of=self._turn_taker(turn_node)
            )
        except StreamCancelled:
            # None of them is used: an answer that had come would stand as
            # the whole round, and the others would never be asked.
            turn_node.meta.reactions = []
            self.save()
            # The cancel stays set until a request starts: the passage's would
            # be stopped by it at once.
            self.provider.reset_cancel()
            yield SessionNotice(ROUND_SKIPPED, stop_taken=True)
            return
        turn_node.meta.round_after = True

    def _turn_taker(self, turn_node) -> str:
        """Who took the turn a round after it answers, for their ask: the
        character the author played, else the author."""
        held = self._character(turn_node.meta.controlled_character_id)
        return held.name if held is not None else "The author"

    def _round(self, history, store_on, *, after_turn_of: str | None = None):
        """One round: everyone in the scene who has a model of their own
        answers what `history` ends on; kept on `store_on`."""
        cast = self.visible_cast(history)
        characters = reacting_characters(
            cast, self.story.scene, held_character_id=self.story.held_character_id
        )
        # What the round answers as it now stands: a round after a Continue
        # or a change replaces the earlier one's. Before anything else: with
        # nobody left to ask (the one who answered left as the passage went
        # on), their answer to the shorter passage stayed, and went to the
        # storyteller with the next turn as what they mean to do.
        if store_on.meta.reactions:
            store_on.meta.reactions = []
            self.save()
        if not characters:
            return
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
            call = yield from self._own_call(
                character, cast, history, passage, count, after_turn_of
            )
            if call is not None:
                calls.append(call)
        if not calls:
            return

        boxes = yield from self._answer_side_by_side(
            [request for _, request, _ in calls],
            "Waiting for the characters played by their own model…",
        )
        yield from self._keep_answers(calls, boxes, passage, after_turn_of)

    def _own_call(self, character, cast, history, passage, count, after_turn_of):
        """One character's request, built and logged; None, with a notice,
        when it can't be (their model names an endpoint that isn't set up: a
        story from another computer, an endpoint renamed since). The round
        never costs the turn: built outside any guard, that failed the whole
        job, and with the round after the author's turn no passage could be
        written until the card was changed."""
        try:
            request, payload = self._own_request(character, cast, history, count, after_turn_of)
        except (ProviderError, ValueError) as exc:
            yield SessionNotice(_not_asked(character, exc))
            return None
        return self._own_logged(character, request, payload, passage)

    def _own_request(
        self, character, cast, history, count, after_turn_of, *, recollections=None
    ) -> tuple[ChatRequest, dict[str, Any]]:
        """One character's request and its payload, built as the round sends
        it: the round's and asking one of them again (`regenerate_reaction`)
        are built here alike, so an answer asked for again is asked for as
        the round asked. `recollections`: what they remember, every memory
        the round would match by default. Raises ProviderError or ValueError
        when it can't be built."""
        model = (character.own_model or "").strip()  # "Endpoint::model", kept whole
        names = self.placeholder_names()
        request = ChatRequest(
            model=model,
            messages=reaction_messages(
                # The names in, as the storyteller reads them.
                story=names.story(self.story),
                cast=names.characters(cast),
                character=names.character(character),
                history=history,
                # Their model's own wording, where SealedLore adjusts for it.
                texts=tuned_texts(self.texts, model, "character"),
                count=count,
                budget=reactions.HISTORY_TOKENS,
                recollections=self.bundle.memories if recollections is None else recollections,
                after_turn_of=after_turn_of,
                public_record=self.public_record(history),
            ),
            params=self.side_params(model),
            # The character's own route: a route is otherwise a role's,
            # and their model is no role's.
            extra_body=self.own_route_body(character),
            use_cache_control=self._cache_control_for(model),
            cache_ttl=self.config.cache_ttl,
        )
        return request, self.provider.build_payload(request)

    def _own_logged(self, character, request, payload, passage) -> tuple[Any, ChatRequest, str]:
        """(character, request, log ref): the request logged before it goes."""
        log_ref = self._log(
            "own_request",
            {"node_id": passage.id, "character": character.name, "payload": payload},
        )
        return character, request, log_ref

    def _keep_answers(self, calls, boxes, passage, after_turn_of) -> Iterator[SessionNotice]:
        """Each answer logged, priced and kept on the passage."""
        answers: list[Reaction] = []
        # Said once everything is logged and kept, and not at all after a
        # Stop: whoever runs the job ends a stopped one at the next thing
        # said, which left the answers after it unlogged and none kept, and
        # would end a round after the author's turn before it could go on.
        said: list[SessionNotice] = []
        stopped = False
        for (character, request, log_ref), box in zip(calls, boxes, strict=True):
            answer, notice, was_stopped = self._taken(character, request, log_ref, box, passage)
            stopped = stopped or was_stopped
            if notice is not None:
                said.append(notice)
            if answer is not None:
                answers.append(answer)
        passage.meta.reactions = answers
        if after_turn_of is not None and not stopped:
            # The round has run to its end, and is marked so before anything
            # is said of it: a Stop landing on one of the notices below ends
            # the job there, and a turn that stays in the story (a phone's)
            # was left with its answers and no mark, so the next take asked,
            # and paid, everyone again.
            passage.meta.round_after = True
        self.save()
        if stopped:
            raise StreamCancelled()
        yield from said
        if answers:
            names = ", ".join(answer.name for answer in answers)
            yield SessionNotice(
                f"{names} answered your turn. The storyteller works their answers in with it."
                if after_turn_of is not None
                else f"{names} answered. The storyteller gets their answers with your next "
                "turn, as what they mean to do."
            )

    def _taken(
        self, character, request: ChatRequest, log_ref: str, box: dict[str, Any], passage
    ) -> tuple[Reaction | None, SessionNotice | None, bool]:
        """One character's reply, logged and priced: (their answer, what is
        said in its place, whether the call was stopped). The round's and
        asking one of them again (`regenerate_reaction`) go through here, so
        both are logged and counted alike."""
        error = box.get("error")
        if isinstance(error, StreamCancelled):
            self._log("own_response", {"request_log_ref": log_ref, "stopped_early": True})
            return None, None, True
        if isinstance(error, (ProviderError, ValueError)):
            self._log("own_response", {"request_log_ref": log_ref, "error": str(error)})
            return None, SessionNotice(f"{character.name}'s model couldn't answer: {error}"), False
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
        # In the session's spend too, whatever came of it (the status
        # bar's readout left every character's answers out).
        usage = self.priced(completed.usage, request.model)
        self.unreported_usage.append(usage)
        why = why_not_whole(completed) or ("it was empty" if not text.strip() else None)
        if why:
            return None, SessionNotice(f"{character.name}'s model gave no answer: {why}."), False
        answer = Reaction(
            character_id=character.id,
            name=character.name,
            text=text.strip(),
            model=request.model,
            usage=usage,
        )
        return answer, None, False

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
                messages=recollect_messages(
                    self.placeholder_names().character(character), chunk, texts=self.texts
                ),
                params=self.side_params(model),
            )
            for character, chunk in jobs
        ]
        try:
            payloads = [self.provider.build_payload(request) for request in requests]
        except (ProviderError, ValueError) as exc:
            # The round goes on, sending as much of the story whole as fits.
            yield SessionNotice(f"What the characters remember couldn't be written: {exc}")
            return
        refs = [
            self._log(
                "recollect_request",
                {
                    "character": character.name,
                    "node_ids": [node_id for node_id, _text in chunk],
                    "payload": payload,
                },
            )
            for (character, chunk), payload in zip(jobs, payloads, strict=True)
        ]
        names = ", ".join(dict.fromkeys(character.name for character, _chunk in jobs))
        boxes = yield from self._answer_side_by_side(
            requests, f"Writing what {names} remember{'s' if ',' not in names else ''}…"
        )
        stopped = False
        written = 0
        said: list[SessionNotice] = []  # after the save and the stop, as `_keep_answers`
        for (character, chunk), request, ref, box in zip(jobs, requests, refs, boxes, strict=True):
            error = box.get("error")
            if isinstance(error, StreamCancelled):
                stopped = True
                continue
            if isinstance(error, (ProviderError, ValueError)):
                self._log("recollect_response", {"request_log_ref": ref, "error": str(error)})
                said.append(
                    SessionNotice(f"{character.name}'s memory couldn't be written: {error}")
                )
                continue
            if error is not None:
                raise error
            text, completed = box["reply"]
            self._log(
                "recollect_response",
                {"request_log_ref": ref, "text": text, "usage": completed.raw_usage},
            )
            usage = self.priced(completed.usage, request.model)
            self.unreported_usage.append(usage)
            why = why_not_whole(completed) or ("it was empty" if not text.strip() else None)
            if why:
                said.append(SessionNotice(f"{character.name}'s memory wasn't written: {why}."))
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
                    usage=usage,
                )
            )
            written += 1
        if written:
            self.save()
        if stopped:
            raise StreamCancelled()
        yield from said

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
        names = self.placeholder_names()
        return reaction_messages(
            story=names.story(self.story),
            cast=names.characters(cast),
            character=names.character(character),
            history=history,
            texts=tuned_texts(self.texts, character.own_model, "character"),
            count=self.estimator.estimate,
            budget=reactions.HISTORY_TOKENS,
            recollections=self.bundle.memories,
            after_turn_of=after,
            public_record=self.public_record(history),
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

    def reaction_refusal(self, node_id: str, character_id: str) -> str | None:
        """Why one character's answer to a message can't be asked for again,
        in words for the author, or None when it can (`regenerate_reaction`).
        Never in a chat or a private scene, where no round runs, nor for a
        Direction, a Narration or the opening, which nobody answers, nor for
        the character the author plays now."""
        if self.story.chat:
            return "A chat has no characters played by their own model."
        try:
            self._refuse_in_private("Asking a character's model again")
        except ValueError as exc:
            return str(exc)
        node = next((n for n in self.nodes if n.id == node_id), None)
        if node is None:
            return "That message is no longer in the story."
        if node.meta.private_span is not None:
            return "Nobody answers a private scene's messages."
        if node.kind == "user" and not reactions.answers_turn(node):
            return "Nobody answers a Direction, a Narration or the opening."
        answer = next((a for a in node.meta.reactions if a.character_id == character_id), None)
        if answer is None:
            return "That character didn't answer this message."
        cast = self.visible_cast(self._path_to(node.id))
        character = next((c for c in cast if c.id == character_id), None)
        if character is None:
            return f"{answer.name} is no longer in the story."
        if not (character.own_model or "").strip():
            return (
                f"{character.name} isn't played by a model of their own any more "
                "(Played by, on their card)."
            )
        if character.id == self.story.held_character_id:
            # Their turns are what they do now, and their answer is never sent
            # with them (engine/prompt.py): an answer asked for would be paid
            # for and go nowhere.
            return f"{character.name} is yours to play now, so their model isn't asked for them."
        return None

    def regenerate_reaction(self, node_id: str, character_id: str) -> Iterator[SessionNotice]:
        """Ask one character's model again for their answer to a message: a
        passage they answered at the same time as the author, or the author's
        turn they answered after it. The request is the round's for them at
        that message (`_own_request`): the story as they saw it up to it,
        their card and notes, the public record there and what they remember,
        written first where the round would write it; never another's answer.
        Their answer alone is replaced, in its place, the others' untouched.
        A Stop, a failure, or a reply declined, cut off or empty leaves the
        answer as it was; logged and counted as the round's are.

        The record and who saw which part of a passage are read as the round
        left them on the messages: asking one character again is not a reason
        to read the story for everyone."""
        refusal = self.reaction_refusal(node_id, character_id)
        if refusal is not None:
            raise ValueError(refusal)
        node = next(n for n in self.nodes if n.id == node_id)
        history = self._path_to(node.id)
        cast = self.visible_cast(history)
        character = next(c for c in cast if c.id == character_id)
        # A turn's answers were given having seen it; a passage's, blind to
        # the turn after it: asked again the way they were asked.
        after_turn_of = self._turn_taker(node) if node.kind == "user" else None

        def count(text: str) -> int:
            return self.estimator.estimate(text)

        yield from self._recollect_due([character], history, cast, count)
        # What they remembered there, as the round then had it: never a
        # memory a later round wrote of what this one still sent whole.
        seen = seen_entries(
            history, character, cast, start=self.story.setup.starting_scene, texts=self.texts
        )
        then = memories_then(
            self.bundle.memories,
            character.id,
            seen,
            count=count,
            budget=reactions.HISTORY_TOKENS,
            size=reactions.RECOLLECT_MESSAGES,
        )
        try:
            request, payload = self._own_request(
                character, cast, history, count, after_turn_of, recollections=then
            )
        except (ProviderError, ValueError) as exc:
            raise ProviderError(f"{_not_asked(character, exc)} {ANSWER_KEPT}") from exc
        call = self._own_logged(character, request, payload, node)
        [box] = yield from self._answer_side_by_side(
            [request], f"Asking {character.name}'s model again…"
        )
        answer, notice, stopped = self._taken(*call, box, node)
        if stopped:
            raise StreamCancelled()
        if answer is None:
            raise ProviderError(f"{notice.text} {ANSWER_KEPT}")
        for index, given in enumerate(node.meta.reactions):
            if given.character_id == character_id:
                node.meta.reactions[index] = answer
                break
        else:
            raise ValueError("that character didn't answer this message")
        # Kept before it is said: a Stop landing on the notice ends the job there.
        self.save()
        yield SessionNotice(f"{character.name} answered again.")
