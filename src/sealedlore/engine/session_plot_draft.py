"""A plot written by the model, on the session's side. A mixin for `StorySession`.

The pure part is engine/plot_draft.py. Here: the story as the model is shown
it (and, for one under way, where it stands), the call on the authoring model
as a premise draft is made, logged in the story's API log so its cost counts,
at most one more to put the reply right, and the plot given to the story
(`set_plot_document`). Never in a simple chat or a private scene: there is no
plot in the one, and the other freezes it and would send the story to another
model. The call runs on the worker, which is the story's one writer meanwhile.
"""

from __future__ import annotations

import time
from collections.abc import Generator, Sequence
from dataclasses import asdict

from sealedlore.engine.archival import render_chunk
from sealedlore.engine.plot_draft import (
    PLOT_MAX_TOKENS,
    PlotDraft,
    PlotWishes,
    build_plot_messages,
    build_repair_messages,
    drop_broken,
    merge_reply,
    plot_problems,
    read_reply,
    story_text,
)
from sealedlore.engine.scene_update import render_card
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.story_plot import document_from_story, without_plot
from sealedlore.messages import PromptMessage
from sealedlore.models.plot_file import PlotDocument
from sealedlore.providers.base import ChatRequest, ReasoningDelta, StreamCompleted, TextDelta

# How often a long reply says how far it has got.
PROGRESS_SECONDS = 2.0
# A story under way: the chapters so far, the latest at most this many, and
# its latest messages.
SO_FAR_CHAPTERS = 12
SO_FAR_MESSAGES = 6


class PlotDraftRuntime:
    def draft_plot(self, wishes: PlotWishes) -> Generator[SessionNotice, None, PlotDraft]:
        """Have the authoring model write the story's plot, and give it to
        the story in place of any it had. Yields progress; returns the plot
        as the story has it now, with what was left out. Raises ValueError
        (or ProviderError) when nothing usable came back; Stop
        (StreamCancelled) leaves the story as it was."""
        self._refuse_in_chat("A plot")
        self._refuse_in_private("Writing a plot")
        base = without_plot(document_from_story(self.bundle))
        shown = without_plot(document_from_story(self.bundle, for_model=True))
        messages = build_plot_messages(
            story_text(shown),
            wishes,
            plays=self._plot_plays(),
            so_far=self._plot_so_far(),
            texts=self.texts,
        )
        return (
            yield from self._write_plot(
                messages, base, newcomers=wishes.newcomers, details={"wishes": asdict(wishes)}
            )
        )

    def _write_plot(
        self,
        messages: Sequence[PromptMessage],
        base: PlotDocument,
        *,
        newcomers: str = "",
        revise: bool = False,
        details: dict,
    ) -> Generator[SessionNotice, None, PlotDraft]:
        """The call, at most one more to put it right, and the plot given to
        the story: a first plot (`draft_plot`) or one written again at the
        author's request (`revise`, engine/session_followup.py). `details`
        go in the API log beside each request."""
        model = self.authoring_model
        text, completed = yield from self._plot_call(messages, model, "first", details)
        merged, notes = merge_reply(
            base, read_reply(text).document, newcomers=newcomers, revise=revise
        )
        problems = plot_problems(merged)
        if completed.finish_reason == "length":
            problems.insert(0, "The reply was cut off before its end: write the plot shorter.")
        repaired = False
        if problems:
            yield SessionNotice("Writing the plot: putting right what it can't use as written…")
            again = build_repair_messages(messages, text, problems, self.texts)
            text, completed = yield from self._plot_call(again, model, "repair", details)
            fixed, fixed_notes = merge_reply(
                base, read_reply(text).document, newcomers=newcomers, revise=revise
            )
            if fixed.events:
                merged, notes, repaired = fixed, fixed_notes, True
        document, dropped = drop_broken(merged)
        if not document.events:
            raise ValueError("the model wrote no event the story could use: ask again")
        notes += dropped
        # What it couldn't put right but the story can play past: said, kept.
        notes += plot_problems(document)
        if completed.finish_reason == "length":
            notes.append("The model ran out of room: the plot may be missing its last part.")
        notes += self.set_plot_document(document)
        return PlotDraft(document=document_from_story(self.bundle), notes=notes, repaired=repaired)

    def _plot_call(
        self, messages: Sequence[PromptMessage], model: str, step: str, details: dict
    ) -> Generator[SessionNotice, None, tuple[str, StreamCompleted]]:
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("authoring"),
            messages=list(messages),
            params=self.side_params(model, max_tokens=PLOT_MAX_TOKENS),
        )
        return (
            yield from self._authoring_stream(
                request,
                kind="plot_draft",
                details={"step": step, **details},
                writing="Writing the plot",
                thinking="Thinking the plot through",
            )
        )

    def _authoring_stream(
        self, request: ChatRequest, *, kind: str, details: dict, writing: str, thinking: str
    ) -> Generator[SessionNotice, None, tuple[str, StreamCompleted]]:
        """One call on the authoring model, streamed so a reply that takes
        minutes says how far it has got, and logged as `<kind>_request` and
        `<kind>_response`. A reply left empty by a model that reasoned to
        the limit is asked for once more, as `ChatProvider.complete` does."""
        log_ref = self._log(
            f"{kind}_request", {**details, "payload": self.provider.build_payload(request)}
        )
        text, completed = "", StreamCompleted()
        for _attempt in range(2):
            chunks: list[str] = []
            written = thought = 0
            said = time.monotonic()
            completed = StreamCompleted()
            for event in self.provider.stream(request):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                    written += len(event.text)
                elif isinstance(event, ReasoningDelta):
                    # Live (GLM 5.3), a first reply reasoned 20,000 tokens, five
                    # minutes in which nothing else said it was working.
                    thought += len(event.text)
                elif isinstance(event, StreamCompleted):
                    completed = event
                    continue
                if time.monotonic() - said >= PROGRESS_SECONDS and (written or thought):
                    said = time.monotonic()
                    # About six characters to a word, the Markdown's marks included.
                    yield SessionNotice(
                        f"{writing}… about {written // 6:,} words."
                        if written
                        else f"{thinking}… about {thought // 6:,} words so far."
                    )
            text = "".join(chunks)
            if text.strip() or completed.finish_reason != "length":
                break
        self._log(
            f"{kind}_response",
            {
                "request_log_ref": log_ref,
                "text": text,
                "usage": completed.raw_usage,
                "finish_reason": completed.finish_reason,
            },
        )
        self.unreported_usage.append(self.priced(completed.usage, request.model))
        return text, completed

    def _plot_plays(self) -> str:
        """Whom the author plays, for the plot's model: the plot's "the
        player's character" is whoever they hold."""
        cast = {character.id: character for character in self.visible_cast()}
        setup = self.story.setup
        held = cast.get(self.story.held_character_id) if self.nodes else None
        if held is not None:
            return self.texts.fill("authoring.plot.plays", name=held.name)
        chosen = [cast[i].name for i in setup.choose_one_of if i in cast]
        if len(chosen) > 1:
            return self.texts.fill("authoring.plot.chooses", names=", ".join(chosen))
        suggested = cast.get(setup.suggested_character_id or "") or cast.get(
            self.story.held_character_id or ""
        )
        if suggested is not None:
            return self.texts.fill("authoring.plot.plays", name=suggested.name)
        if not any(character.is_player_available for character in cast.values()):
            return self.texts["authoring.plot.directs"]
        return ""

    def _plot_so_far(self) -> str:
        """Where a story under way stands: the scene, the chapters so far and
        the latest messages, as models may see them (`path()`: a private
        scene's messages are left out)."""
        path = self.path()
        if not any(node.kind == "assistant" for node in path):
            return ""
        split = self.split(path)
        parts = [
            render_card(
                self.story.scene, cast=self.visible_cast(), held_id=self.story.held_character_id
            )
        ]
        chapters = [summary.content.strip() for summary in split.summaries]
        chapters = [chapter for chapter in chapters if chapter][-SO_FAR_CHAPTERS:]
        if chapters:
            parts.append("The story so far, chapter by chapter:\n\n" + "\n\n".join(chapters))
        latest = render_chunk(split.verbatim[-SO_FAR_MESSAGES:], self.cast, texts=self.texts)
        if latest:
            parts.append("The latest messages:\n\n" + latest)
        return "\n\n".join(parts)
