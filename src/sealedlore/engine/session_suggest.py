"""Suggest my turn on the session's side: the call that asks the storyteller
what the author's character might do next. A mixin for `StorySession`; the
pure part is engine/suggest.py.

Asked the way a Question is: the story's own prompt up to the tail, so it
rides the cached prefix, with the request in the question's place. In a
private scene it goes to the scene's model, with the scene's window and
budget, and nowhere else. Nothing is stored in the story: no message, no
aside. The call is logged so its cost counts (`suggest_request` /
`suggest_response`; a private scene's as that scene's calls are logged).
"""

from __future__ import annotations

from collections.abc import Generator

from sealedlore.engine.prompt import assemble_chat
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.suggest import (
    DEFAULT_COUNT,
    NOBODY_HELD,
    how_many,
    parse_suggestions,
    suggest_request,
)
from sealedlore.providers.base import ChatRequest
from sealedlore.tree import index_nodes


class SuggestRuntime:
    def can_suggest(self) -> bool:
        """Whether there is someone to suggest a turn for: the character the
        author plays, in a story; a chat's user always."""
        return self.story.chat or self._character(self.story.held_character_id) is not None

    def suggest_turns(
        self, count: int = DEFAULT_COUNT
    ) -> Generator[SessionNotice, None, list[str]]:
        """A few things the author's character might do or say next (a chat:
        possible next messages of the user's), for the author to choose from
        outside the story. Returns them; nothing is kept.

        Refused as a Question is: while a TEE model is not attested, and in
        a private scene with no model to ask. A reply that can't be read is
        a ValueError in plain words.
        """
        self._tee_guard()
        count = how_many(count)
        chat = self.story.chat
        held = None if chat else self._character(self.story.held_character_id)
        if not chat and held is None:
            raise ValueError(NOBODY_HELD)
        # In a private scene, the private model and what it reads, as for
        # the scene's turns and Questions.
        span = self.open_span
        provider = self.private_provider if span is not None else self.provider
        if provider is None:
            raise ValueError("the private scene has no model to ask")
        model = span.model if span is not None else self.model
        history = self.private_history(span) if span is not None else self.path()
        asked = suggest_request(
            self.texts, held=held.name if held is not None else "", count=count, chat=chat
        )
        # The Prompt page keeps the last turn's prompt, not this one.
        shown = self.last_prompt
        try:
            if chat:
                if span is not None:
                    verbatim, summaries = self._private_window(history, span)
                else:
                    split = self.split(history)
                    verbatim, summaries = list(split.verbatim), list(split.summaries)
                prompt = assemble_chat(
                    story=self.story,
                    history_nodes=verbatim,
                    tail=asked,
                    summaries=summaries,
                    nodes_by_id=index_nodes(self.nodes),
                    estimator=self.estimator,
                    options=self.private_options() if span is not None else self.assembly_options(),
                    # The chat's tail is for its replies, not for this.
                    with_chat_tail=False,
                )
            else:
                # Lore by what is at hand: assembling this never embeds.
                latest = history[-1].content if history else ""
                lore = self.retrieve(
                    self._question_as_turn(latest), history, allow_network=False
                ).entries
                prompt = self.assemble_question(
                    "",
                    lore=lore,
                    history=history,
                    options=self.private_options() if span is not None else None,
                    span=span,
                    rendered=asked,
                )
        finally:
            self.last_prompt = shown
        request = ChatRequest(
            model=model,
            # None in a private scene, or for a TEE or encrypted model.
            extra_body=self.route_for("story"),
            messages=prompt.messages,
            params=self.story_params(model),
            use_cache_control=self._cache_control_for(model),
            cache_ttl=self.config.cache_ttl,
        )
        body = {
            "node_id": history[-1].id if history else None,
            "payload": provider.build_payload(request),
        }
        log_ref = (
            self._log_turn("request", {"suggest": True, **body}, span)
            if span is not None
            else self._log("suggest_request", body)
        )
        yield SessionNotice(
            "Asking for a few possible next messages…"
            if chat
            else f"Asking what {held.name} might do next…"
        )
        text, completed = provider.complete(request)
        answer = {
            "request_log_ref": log_ref,
            "text": text,
            "finish_reason": completed.finish_reason,
            "usage": completed.raw_usage,
        }
        if span is not None:
            self._log_turn("response", {"suggest": True, **answer}, span)
        else:
            self._log("suggest_response", answer)
        usage = self.priced(completed.usage, model)
        self.unreported_usage.append(usage)
        self._update_correction_factor(prompt, usage, span)
        return parse_suggestions(text, count)
