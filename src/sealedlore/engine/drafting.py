"""Drafting a whole story from a premise: the call, and turning its reply into a story.

There is no story — and so no session — until the reply is in, which is why
this isn't a `StorySession` method. The call is logged once the story exists,
into that story's own API log, so its cost counts toward the story like any
other.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sealedlore.engine.authoring import (
    GENERATION_MAX_TOKENS,
    build_generation_messages,
    parse_generated,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.generation import GenerationParams
from sealedlore.models.story import NarrativePerson, NarrativeTense
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    StreamCompleted,
    StreamEvent,
    TextDelta,
)
from sealedlore.storage.repository import StoryBundle, append_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario


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
    ) -> None:
        """`route`: the authoring role's (`routing.config_route`)."""
        if not premise.strip():
            raise ValueError("write a premise first")
        self.provider = provider
        self.premise = premise
        self.model = model
        # Chosen before the draft, so its opening is written in them; they
        # stand whatever the reply's style says.
        self.person = person
        self.tense = tense
        self.request = ChatRequest(
            model=model,
            extra_body=dict(route or {}),
            messages=build_generation_messages(premise, person=person, tense=tense, texts=texts),
            params=GenerationParams(max_tokens=GENERATION_MAX_TOKENS),
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
        self, *, main_model: str | None = None, root: Path | None = None
    ) -> tuple[StoryBundle, list[str]]:
        """Save the drafted story and log the call to it. Raises ValueError if unusable.

        `main_model` is what the story will be played with, which needn't be
        the (perhaps stronger) model that drafted it.
        """
        scenario, warnings = parse_generated(self.text)
        bundle = bundle_from_scenario(scenario, main_model=main_model)
        style = bundle.story.style
        style.person = self.person or style.person
        style.tense = self.tense or style.tense
        save_story_bundle(bundle, root=root)
        request_id = new_id()
        append_api_log(
            bundle.story.id,
            {
                "id": request_id,
                "kind": "draft_request",
                "at": self._requested_at,
                "premise": self.premise,
                "payload": self.provider.build_payload(self.request),
            },
            root=root,
        )
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
