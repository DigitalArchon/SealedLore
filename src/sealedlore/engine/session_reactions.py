"""StorySession's side of the reaction round (engine/reactions.py).

After a passage lands, every character whose card names a model answers it
on the worker, one after the other, each prompt built before any call so no
answer can reach another's. A character's model is reached through the
session's provider like any other (`EndpointsProvider` resolves
"Endpoint::model", so a fine-tuned local model on its own endpoint works);
each answer is kept on the passage (`NodeMeta.reactions`), where the history
renders it to the storyteller, the reads and the summariser as that
character's intention.

The round never costs the turn: a failed or refused answer is a notice and
nothing more, and a passage that didn't land (stopped, refused, empty) gets
no round. Direction and Narration turns are the storyteller's alone (the
author's answer 3), so a passage answering one of those draws no round
either.
"""

from __future__ import annotations

from collections.abc import Iterator

from sealedlore.engine.reactions import reacting_characters, reaction_messages
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.ids import utc_now_iso
from sealedlore.models.config import split_model
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Reaction
from sealedlore.providers.base import ChatRequest, ProviderError, why_not_whole


class ReactionRuntime:
    def react_after_turn(self) -> Iterator[SessionNotice]:
        """The round after a passage: each character played by their own model
        answers it, blind to the others and to what the author will write."""
        if self.story.chat or self.in_private:
            return
        result = self.last_result
        if result is None or result.unread:
            # Nothing landed to answer: stopped, refused or empty.
            return
        passage = result.node
        if passage is None or passage.kind != "assistant":
            return
        parent = next((n for n in self.nodes if n.id == passage.parent_id), None)
        if (
            parent is None
            or parent.kind != "user"
            or parent.speaker_id in (DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID)
        ):
            # Direction is to the storyteller, and each own model takes its
            # writing as fact: they don't answer a Director turn.
            return
        characters = reacting_characters(
            self.cast, self.story.scene, held_character_id=self.story.held_character_id
        )
        if not characters:
            return
        # The round answers the passage as it now stands: a continuation or a
        # revision has made it longer since any earlier round, and those
        # answers are replaced rather than stacked beside their own updates.
        passage.meta.reactions = []
        history = self.path()
        # Every prompt is built before any call goes out, so each character
        # answers the passage alone: the blindness is in what each is given.
        built = [
            (
                character,
                reaction_messages(
                    story=self.story,
                    cast=self.cast,
                    character=character,
                    history=history,
                    texts=self.texts,
                ),
            )
            for character in characters
        ]
        answered: list[str] = []
        for character, messages in built:
            _, model = split_model(character.own_model or "")
            request = ChatRequest(
                model=model,
                messages=messages,
                params=self.side_params(model),
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
            try:
                text, completed = self.provider.complete(request)
            except (ProviderError, ValueError) as error:
                yield SessionNotice(f"{character.name}'s model couldn't answer: {error}")
                self._log("own_response", {"request_log_ref": log_ref, "error": str(error)})
                continue
            # The watches (route, reasoning, TEE) are the provider's own: it
            # reports every finished reply itself.
            reason = why_not_whole(completed)
            if reason or not text.strip():
                yield SessionNotice(f"{character.name}'s model didn't answer: {reason}")
                continue
            usage = self.priced(completed.usage, model)
            passage.meta.reactions.append(
                Reaction(
                    character_id=character.id,
                    name=character.name,
                    text=text.strip(),
                    model=model,
                    usage=usage,
                )
            )
            answered.append(character.name)
            self._log(
                "own_response",
                {
                    "request_log_ref": log_ref,
                    "node_id": passage.id,
                    "character": character.name,
                    "usage": completed.raw_usage,
                    "finish_reason": completed.finish_reason,
                },
            )
        if answered:
            self.save()
            yield SessionNotice(
                f"{', '.join(answered)} answered at once — the storyteller gets "
                "their answers with your next turn, as intentions."
            )

    def edit_reaction(self, node_id: str, character_id: str, text: str) -> None:
        """Edit what a character's own model answered a passage with (the
        author's answer 4). Regenerating the passage that follows reuses the
        edited answer; nothing else changes."""
        node = next((n for n in self.nodes if n.id == node_id), None)
        if node is None:
            raise ValueError("no such message")
        for reaction in node.meta.reactions:
            if reaction.character_id == character_id:
                reaction.text = text.strip()
                reaction.edited_at = utc_now_iso()
                break
        else:
            raise ValueError("that character didn't answer this passage")
        self.save()
