"""Private scenes: the session's side. A mixin for `StorySession`.

A private scene is a span of the story played on a private model — local, or
a nano-gpt TEE model — that nothing else hears about. The rules, in order of
how much they matter:

1. **Nothing from the scene reaches any other model.** Its messages carry
   `meta.private_span`; `path()`, `_path_to` and `render_chunk` leave them out,
   and every model-facing read goes through those. The approved summary stands
   in for the scene from then on. `tests/test_private.py` plays a scene with a
   marker in it and then every side call, and checks no payload but the
   private model's ever holds the marker.
2. **While the scene is open, nothing else is called.** No director, archival,
   network lore retrieval (keywords only), scene or plot reads, lore picks,
   merges or scans. Afterwards the scene and plot reads run on the summary.
3. **A memory-only scene never reaches the disk** (`bundle_to_save`): its
   messages are dropped from what is written and its calls are logged without
   content, so the story's cost still adds up.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from sealedlore.engine.archival import render_chunk, split_path, summary_params
from sealedlore.engine.private_prompt import (
    build_handoff_messages,
    build_private_condense_messages,
    build_private_summary_messages,
    private_rules_sections,
)
from sealedlore.engine.prompt import AssembledPrompt, assemble_prompt, story_rules_sections
from sealedlore.engine.retrieval import lore_layout
from sealedlore.engine.scene_state import log_on_path
from sealedlore.ids import utc_now_iso
from sealedlore.models.generation import GenerationParams
from sealedlore.models.node import NARRATOR_SPEAKER_ID, Node, NodeMeta, Usage
from sealedlore.models.private import PrivateKeep, PrivateSpan
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError

# Fewer than this many recent messages left in a private prompt, and the
# story is too big for the private model: offer the handoff. Live, a 16k
# budget on the 126k-token test story kept 8 of 118 (six was too lenient).
FIT_MIN_MESSAGES = 20
# The handoff's share of the private budget.
HANDOFF_SHARE = 0.3
# Room for a reasoning model (GLM thinks first); a cap costs nothing unused.
PRIVATE_SUMMARY_MAX_TOKENS = 4000
HANDOFF_MAX_TOKENS = 6000
# A scene that outgrows the private budget is condensed by the private model
# (`_condense`): once the next prompt would drop one of the scene's own
# messages, or pass CONDENSE_AT of the budget, the oldest whole exchanges are
# rolled into `span.scene_summary` until the prompt is at or below the target.
PRIVATE_CONDENSE_AT = 0.9
PRIVATE_CONDENSE_TARGET = 0.6
PRIVATE_CONDENSE_MAX_TOKENS = 4000
PRIVATE_CONDENSE_WORDS = 600
# How the condensed part reads where it stands in the history.
CONDENSED_LEAD = "Earlier in this scene (condensed):"


@dataclass(frozen=True)
class PrivateFit:
    """How the story fits the private model's budget."""

    budget: int
    total: int  # what the whole story would take
    kept_messages: int  # recent messages that still fit
    dropped_messages: int

    @property
    def fits(self) -> bool:
        return self.dropped_messages == 0 or self.kept_messages >= FIT_MIN_MESSAGES


class PrivateRuntime:
    # Set by the window when a scene opens; never the story's own provider.
    private_provider: ChatProvider | None = None
    # Checks each reply's signature on a TEE model (providers/tee.py); None
    # when the model isn't one or checking is off.
    private_tee: Any = None
    # Set by the window while a TEE/ model is being attested, or with the
    # reason it was refused: every call that would reach that model refuses
    # before sending (_tee_guard). None when there's nothing to wait for.
    tee_block: str | None = None
    # What happened to a scene the app closed in the middle of, for a notice.
    private_notice: str | None = None

    def _tee_guard(self) -> None:
        """Nothing goes to a TEE/ model whose enclave is still being attested,
        or was refused (providers/tee.TeeClient.attest)."""
        if self.tee_block:
            from sealedlore.providers.tee import TeeRefused

            raise TeeRefused(f"{self.tee_block}; nothing was sent")

    # --- the span ------------------------------------------------------------

    @property
    def open_span(self) -> PrivateSpan | None:
        return next((s for s in reversed(self.story.private_spans) if s.status == "open"), None)

    @property
    def in_private(self) -> bool:
        return self.open_span is not None

    def span_nodes(self, span: PrivateSpan) -> list[Node]:
        """The scene's messages on the active path, in order."""
        return [node for node in self.full_path() if node.meta.private_span == span.id]

    def enter_private(
        self, *, keep: PrivateKeep, provider: ChatProvider, model: str, base_url: str = ""
    ) -> PrivateSpan:
        if self.in_private:
            raise ValueError("a private scene is already open")
        if self.story.chat:
            raise ValueError(
                "a simple chat has no private scenes: start one on a TEE model instead"
            )
        full = self.full_path()
        span = PrivateSpan(
            start_parent_id=full[-1].id if full else None,
            model=model,
            host=urlparse(base_url).hostname or "",
            keep=keep,
        )
        self.story.private_spans.append(span)
        self.private_provider = provider
        self.save()
        return span

    def resume_private(self, provider: ChatProvider) -> None:
        """Back into a scene kept on disk, after the app was closed in it."""
        self.private_provider = provider

    def recover_private_spans(self) -> None:
        """On load: a memory-only scene still open was never written down.

        Its messages are gone with the app that held them; the story goes on
        from where the scene began."""
        for span in self.story.private_spans:
            if span.status == "open" and span.keep == "memory":
                span.status = "discarded"
                span.ended_at = span.ended_at or utc_now_iso()
                self.private_notice = (
                    "A private scene wasn't kept: the app closed before it ended, and it "
                    "was held in memory only. The story goes on from where it began."
                )
                if span.start_parent_id is not None and not any(
                    node.id == self.story.active_leaf_id for node in self.nodes
                ):
                    self.story.active_leaf_id = span.start_parent_id

    def _refuse_in_private(self, what: str) -> None:
        """For calls that would send the story to another model."""
        if self.in_private:
            raise ValueError(
                f"{what} waits until the private scene ends: it would send the scene's "
                "story to another model."
            )

    # --- the scene's prompt ------------------------------------------------------

    def private_options(self):
        full = story_rules_sections(self.story, texts=self.texts)
        return replace(
            self.assembly_options(),
            token_budget=self.config.private_budget,
            rules_override=tuple(private_rules_sections(self.story, full, texts=self.texts)),
        )

    def private_history(self, span: PrivateSpan) -> list[Node]:
        return self.model_nodes(self.full_path(), include_span=span.id)

    def private_assemble(self, turn, history: Sequence[Node], span: PrivateSpan) -> AssembledPrompt:
        """The story's prompt, fitted to the private model: its budget, its
        rules, the handoff in place of the chapters when there is one, and
        lore by keywords only (no embeddings or Jev: they would hear the scene)."""
        options = self.private_options()
        layout = lore_layout(
            self.visible_lore(),
            budget=options.token_budget,
            share=self.config.lore_whole_share,
            count_tokens=self._count_lore_tokens,
        )
        lore = () if layout.mode == "whole" else self._keyword_lore(turn, history)
        if span.handoff:
            ids = [node.id for node in history]
            start = ids.index(span.handoff_through) + 1 if span.handoff_through in ids else 0
            verbatim = list(history[start:])
            summaries = [Summary(content=span.handoff, covered_node_ids=ids[:start])]
        else:
            split = split_path(self.bundle.summaries, history)
            verbatim, summaries = list(split.verbatim), list(split.summaries)
        covered = set(self._condensed_ids(span, history))
        if covered:
            # The scene's own oldest messages, condensed by the private model,
            # stand where they stood: after whatever led into the scene. Up
            # with the chapters (the system block) the scene's start read
            # before its own lead-in, and every condense cost a cache write.
            first = next((i for i, node in enumerate(verbatim) if node.id in covered), None)
            verbatim = [node for node in verbatim if node.id not in covered]
            if first is not None:
                verbatim.insert(first, self._condensed_node(span))
        prompt = assemble_prompt(
            story=self.story,
            cast=self.visible_cast(history),
            history_nodes=verbatim,
            turn=turn,
            summaries=summaries,
            lore=lore,
            standing_lore=layout.standing,
            supporting=self.supporting_for(turn.user_text, history),
            on_file=self.visible_supporting(),
            scene_log=log_on_path(self.story.scene_log, history),
            estimator=self.estimator,
            options=options,
        )
        self.last_prompt = prompt
        return prompt

    def _keyword_lore(self, turn, history: Sequence[Node]):
        report = self.retrieve(turn, history, allow_network=False)
        self.last_retrieval = report
        return report.entries

    # --- a scene that outgrows the private model ---------------------------------

    @staticmethod
    def _condensed_node(span: PrivateSpan) -> Node:
        """The condensed part as one narrator message, for the prompt only:
        never attached to the story."""
        return Node(
            id=f"condensed-{span.id}",
            kind="assistant",
            speaker_id=NARRATOR_SPEAKER_ID,
            content=f"{CONDENSED_LEAD}\n\n{span.scene_summary or ''}",
            meta=NodeMeta(private_span=span.id),
        )

    @staticmethod
    def _condensed_ids(span: PrivateSpan, history: Sequence[Node]) -> list[str]:
        """The scene's messages `span.scene_summary` stands in for, in order."""
        if not span.scene_summary or not span.scene_summary_through:
            return []
        ids: list[str] = []
        for node in history:
            if node.meta.private_span == span.id:
                ids.append(node.id)
            if node.id == span.scene_summary_through:
                return ids
        # The covered messages aren't on this path (taken back, or a retake
        # among them): the summary describes messages that are gone.
        return []

    def _condense_if_needed(self, turn, history: Sequence[Node], span: PrivateSpan) -> Iterator:
        """Before a private turn: if the scene's own messages no longer fit the
        private budget, the oldest whole exchanges are condensed, on the
        private model, until the prompt is at or below the target."""
        budget = self.config.private_budget
        for _round in range(4):
            prompt = self.private_assemble(turn, history, span)
            covered = set(self._condensed_ids(span, history))
            scene = [n for n in history if n.meta.private_span == span.id and n.id not in covered]
            # Only a summary of messages still on the path carries on.
            previous = span.scene_summary if covered else None
            dropped = set(prompt.excluded_node_ids)
            over = prompt.full_total > PRIVATE_CONDENSE_AT * budget
            if not (over or any(n.id in dropped for n in scene)):
                return
            excess = max(prompt.full_total - int(PRIVATE_CONDENSE_TARGET * budget), 1)
            chunk = self._oldest_exchanges(scene, excess, span.model)
            if not chunk:
                return  # one exchange left: it stays verbatim, whatever the budget
            if not (yield from self._condense(span, chunk, previous)):
                return  # not kept: this turn's window drops the oldest instead

    def _oldest_exchanges(self, scene: Sequence[Node], excess: int, model: str) -> list[Node]:
        """The oldest whole exchanges worth at least `excess` tokens, always
        leaving the newest exchange as it is."""
        chunk: list[Node] = []
        taken = 0
        last_reply = max(
            (i for i, node in enumerate(scene) if node.kind == "assistant"), default=-1
        )
        for index, node in enumerate(scene):
            if index >= last_reply:
                break  # the newest exchange stays
            chunk.append(node)
            taken += self.estimator.estimate(
                render_chunk([node], self.cast, include_private=True, texts=self.texts), model
            )
            if node.kind == "assistant" and taken >= excess:
                break
        while chunk and chunk[-1].kind != "assistant":
            chunk.pop()  # end on a reply, so the window opens on an author turn
        return chunk

    def _condense(self, span: PrivateSpan, chunk: Sequence[Node], previous: str | None) -> Iterator:
        """Roll `chunk` into `previous`, the summary of the scene's messages
        before it on this path (None: it starts the summary). The private
        model only.

        A reply cut off by its token limit, or empty, is not kept (live, GLM
        5.3 Flash spent its 2,000 on reasoning and a summary stopped
        mid-sentence, standing in for the messages it had lost): the scene
        stays as it was, and its oldest messages fall out of this turn's
        window instead. Returns whether the summary was kept."""
        if self.private_provider is None:
            raise ValueError("the private scene has no model to condense with")
        names = [c.name for c in self.visible_cast()] + [c.name for c in self.visible_supporting()]
        held = self._character(self.story.held_character_id)
        request = ChatRequest(
            model=span.model,
            messages=build_private_condense_messages(
                previous,
                render_chunk(chunk, self.cast, include_private=True, texts=self.texts),
                names,
                self.story.style.person or "third",
                held=held.name if held else None,
                words=PRIVATE_CONDENSE_WORDS,
                tense=self.story.style.tense,
                texts=self.texts,
            ),
            params=summary_params(span.model, max_tokens=PRIVATE_CONDENSE_MAX_TOKENS),
        )
        log_ref = self._log_turn(
            "request",
            {"condense": True, "payload": self.private_provider.build_payload(request)},
            span,
        )
        text, completed = self.private_provider.complete(request)
        self._log_turn(
            "response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
            span,
        )
        self.unreported_usage.append(self.priced(completed.usage, span.model))
        text = text.strip()
        from sealedlore.engine.session import SessionNotice

        if not text or completed.finish_reason == "length":
            yield SessionNotice(
                "The private scene outgrew the private model's context, but its summary of "
                "the oldest messages came back "
                + ("cut off by its token limit" if text else "empty")
                + ", so it wasn't kept: those messages fall out of this turn's window."
            )
            return False
        span.scene_summary = text
        span.scene_summary_through = chunk[-1].id
        self.save()
        yield SessionNotice(
            f"The private scene outgrew the private model's context: its oldest "
            f"{len(chunk)} messages were condensed by the private model."
        )
        return True

    def private_fit(self, turn) -> PrivateFit:
        """How much of the story the private model can take, before the scene."""
        span = self.open_span or PrivateSpan(model="", keep="memory")
        history = self.path()
        prompt = self.private_assemble(turn, history, span)
        messages = len(
            [node for node in split_path(self.bundle.summaries, history).verbatim if node.content]
        )
        dropped = len(prompt.excluded_node_ids)
        return PrivateFit(
            budget=self.config.private_budget,
            total=prompt.full_total,
            kept_messages=max(messages - dropped, 0),
            dropped_messages=dropped,
        )

    def write_handoff(self) -> str:
        """The story so far, condensed by the story's own model to fit the
        private one. Pre-scene material only: this runs before the scene has
        a single message, and reads `path()`, which never holds one."""
        span = self.open_span
        if span is None:
            raise ValueError("no private scene is open")
        history = self.path()
        split = split_path(self.bundle.summaries, history)
        parts = [f"Chapter {i}: {s.content}" for i, s in enumerate(split.summaries, 1)]
        parts.append(render_chunk(list(split.verbatim), self.cast, texts=self.texts))
        words = max(150, int(self.config.private_budget * HANDOFF_SHARE / 1.35))
        request = ChatRequest(
            model=self.model,
            messages=build_handoff_messages(
                "\n\n".join(p for p in parts if p), words, texts=self.texts
            ),
            params=GenerationParams(max_tokens=HANDOFF_MAX_TOKENS),
        )
        log_ref = self._log("handoff_request", {"payload": self.provider.build_payload(request)})
        text, completed = self.provider.complete(request)
        self._log(
            "handoff_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, self.model))
        text = text.strip()
        if not text:
            raise ProviderError("the summary for the private model came back empty")
        span.handoff = text
        span.handoff_through = history[-1].id if history else None
        self.save()
        return text

    # --- turns in the scene ---------------------------------------------------------

    def _private_turn(
        self, turn, user_node: Node, *, history: Sequence[Node] | None = None
    ) -> Iterator:
        span = self.open_span
        if span is None or self.private_provider is None:
            raise ValueError("the private scene has no model to send to")
        user_node.meta.private_span = span.id
        if history is None:
            history = [node for node in self.private_history(span) if node.id != user_node.id]
        yield from self._condense_if_needed(turn, history, span)
        prompt = self.private_assemble(turn, history, span)
        yield from self._generate(
            prompt,
            turn,
            parent=user_node,
            provider=self.private_provider,
            model=span.model,
            span=span,
        )
        yield self._passage_done()
        self._check_tee(self.last_result.node if self.last_result else None)
        self.save()

    @staticmethod
    def _passage_done():
        from sealedlore.engine.session import PassageDone

        return PassageDone()

    def _check_tee(self, node: Node | None) -> None:
        """A TEE model's reply: was it signed by the enclave that was attested?
        An end-to-end encrypted one needs no check: only the attested enclave's
        key could have sealed it (providers/private_mode.py)."""
        if node is not None and getattr(self, "_last_sealed", False):
            node.meta.tee = "encrypted"
            self._mark_sent(node, "encrypted")
            return
        if node is None or self.private_tee is None:
            return
        # The author's message went to the enclave that attested (the guard
        # refuses otherwise): marked too, or it looked less protected than the
        # reply (the author, Sept 2026). Not "signed": that is the reply's.
        self._mark_sent(node, "attested")
        response_id = getattr(self, "_last_response_id", None)
        try:
            node.meta.tee = "verified" if self.private_tee.verify_reply(response_id) else "failed"
        except Exception:  # an unreachable check is not a failed one
            node.meta.tee = "unchecked"

    def _mark_sent(self, reply: Node, how: str) -> None:
        """Mark the author's message a reply answers with how it was sent."""
        parent = next((n for n in self.nodes if n.id == reply.parent_id), None)
        if parent is not None and parent.kind == "user":
            parent.meta.tee = how

    def _log_turn(self, kind: str, body: dict[str, Any], span: PrivateSpan | None) -> str:
        """The API log for a turn: as usual, but a memory-only scene's calls
        leave only what they cost."""
        if span is None:
            return self._log(kind, body)
        if span.keep == "disk":
            return self._log(kind, {**body, "private": True})
        if kind == "request":
            model = (body.get("payload") or {}).get("model")
            return self._log("private_request", {"payload": {"model": model}})
        return self._log(
            "private_response",
            {"request_log_ref": body.get("request_log_ref"), "usage": body.get("usage") or {}},
        )

    # --- leaving -------------------------------------------------------------------

    def summarise_private(self) -> str:
        """The private model's summary of the scene's own messages, and only them."""
        span = self.open_span
        if span is None or self.private_provider is None:
            raise ValueError("no private scene is open")
        self._tee_guard()
        nodes = self.span_nodes(span)
        if not nodes:
            return ""
        scene_text = self._scene_text_for_summary(span, nodes)
        names = [c.name for c in self.visible_cast()] + [c.name for c in self.visible_supporting()]
        request = ChatRequest(
            model=span.model,
            messages=build_private_summary_messages(
                scene_text,
                names,
                self.story.style.person or "third",
                held=held.name if (held := self._character(self.story.held_character_id)) else None,
                tense=self.story.style.tense,
                texts=self.texts,
            ),
            params=summary_params(span.model, max_tokens=PRIVATE_SUMMARY_MAX_TOKENS),
        )
        log_ref = self._log_turn(
            "request",
            {"summary": True, "payload": self.private_provider.build_payload(request)},
            span,
        )
        text, completed = self.private_provider.complete(request)
        self._log_turn(
            "response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
            span,
        )
        self.unreported_usage.append(self.priced(completed.usage, span.model))
        text = text.strip()
        if not text:
            raise ProviderError("the private model returned an empty summary")
        if completed.finish_reason == "length":
            # As on every summary path: half a summary never passes as whole.
            raise ProviderError(
                "the private model's summary was cut off by its token limit; end the "
                "scene again to ask for another"
            )
        if completed.sealed:
            span.summary_tee = "encrypted"
        elif self.private_tee is not None:
            # The summary is the one reply that goes back into the story: was
            # it signed by the enclave that was attested?
            try:
                verified = self.private_tee.verify_reply(completed.response_id)
                span.summary_tee = "verified" if verified else "failed"
            except Exception:  # an unreachable check is not a failed one
                span.summary_tee = "unchecked"
        return text

    def _scene_text_for_summary(self, span: PrivateSpan, nodes: Sequence[Node]) -> str:
        """The scene as the exit summary reads it: the condensed part, then
        the rest verbatim. A scene too long even for that is condensed
        further first, so the summary request fits the private model."""
        limit = int(self.config.private_budget * PRIVATE_CONDENSE_AT)
        for _round in range(4):
            covered = set(self._condensed_ids(span, nodes))
            rest = [node for node in nodes if node.id not in covered]
            text = render_chunk(rest, self.cast, include_private=True, texts=self.texts)
            previous = span.scene_summary if covered else None
            if previous:
                text = f"WHAT CAME EARLIER IN THE SCENE (condensed):\n{previous}\n\n{text}"
            if self.estimator.estimate(text, span.model) <= limit:
                return text
            chunk = self._oldest_exchanges(rest, max(len(rest) // 2, 1) * 400, span.model)
            if not chunk:
                return text
            through = span.scene_summary_through
            for _event in self._condense(span, chunk, previous):
                pass
            if span.scene_summary_through == through:
                return text  # not kept: the summary reads the scene as it is
        return text

    def close_private(self, summary: str) -> Iterator:
        """End the scene with the author's approved summary.

        The summary joins the story after the scene's last message; from here
        on it is all any other model knows of the scene. The scene and plot
        reads then run on it, so the roster and the clock catch up.
        """
        span = self.open_span
        if span is None:
            raise ValueError("no private scene is open")
        nodes = self.span_nodes(span)
        full = self.full_path()
        parent = nodes[-1] if nodes else (full[-1] if full else None)
        span.status, span.ended_at = "closed", utc_now_iso()
        self._release_private()
        if summary.strip() and nodes:
            node = Node(
                kind="assistant",
                speaker_id=NARRATOR_SPEAKER_ID,
                content=summary.strip(),
                meta=NodeMeta(
                    model=span.model,
                    private_summary_of=span.id,
                    controlled_character_id=self.story.held_character_id,
                ),
            )
            self._attach(node, parent=parent)
            span.summary_node_id = node.id
            self._sync_scene()
            from sealedlore.engine.session import GenerationResult

            self.last_result = GenerationResult(
                node=node,
                text=node.content,
                reasoning=None,
                usage=Usage(),
                finish_reason="stop",
                stopped_early=False,
            )
            self.save()
            yield from self.read_after_turn()
        self.save()

    def discard_private(self) -> None:
        """End the scene with nothing carried over; the story goes on from
        where it began. On disk its messages stay as a side branch that no
        model is ever sent; in memory they go."""
        span = self.open_span
        if span is None:
            return
        doomed = {node.id for node in self.nodes if node.meta.private_span == span.id}
        if span.keep == "memory" and doomed:
            for node in self.nodes:
                node.children = [child for child in node.children if child not in doomed]
            self.bundle.nodes[:] = [node for node in self.nodes if node.id not in doomed]
        if span.keep == "memory":
            self.bundle.asides[:] = [
                aside
                for aside in self.bundle.asides
                if aside.anchor_node_id not in doomed and aside.private_span != span.id
            ]
        span.status, span.ended_at = "discarded", utc_now_iso()
        self.story.active_leaf_id = span.start_parent_id
        self._release_private()
        # The last real turn was the scene's; nothing after it may read it.
        self.last_result = None
        self._sync_scene()
        self.save()

    def _release_private(self) -> None:
        """Close the scene's clients, and forget what was worked out from
        the scene: the lore selection made from its keywords would otherwise
        serve the next image prompt."""
        for client in (self.private_provider, self.private_tee):
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - a socket already gone
                    pass
        self.private_provider = None
        self.private_tee = None
        self.tee_block = None
        self.last_retrieval = None

    # --- what reaches the disk ----------------------------------------------------------

    def bundle_to_save(self):
        """The bundle as it may be written: a memory-only scene's messages,
        and anything anchored to them, left out; the summary re-attached to
        where the scene began."""
        memory = {span.id: span for span in self.story.private_spans if span.keep == "memory"}
        doomed = (
            {node.id for node in self.nodes if node.meta.private_span in memory}
            if memory
            else set()
        )
        private_asides = any(a.private_span in memory for a in self.bundle.asides)
        condensed = any(span.scene_summary for span in memory.values())
        if not doomed and not private_asides and not condensed:
            return self.bundle
        bundle = self.bundle
        summary_parent = {
            span.summary_node_id: span.start_parent_id
            for span in memory.values()
            if span.summary_node_id
        }
        nodes: list[Node] = []
        for node in bundle.nodes:
            if node.id in doomed:
                continue
            changed: dict[str, Any] = {}
            children = [child for child in node.children if child not in doomed]
            children += [summary for summary, parent in summary_parent.items() if parent == node.id]
            if children != node.children:
                changed["children"] = children
            if node.id in summary_parent:
                changed["parent_id"] = summary_parent[node.id]
            nodes.append(node.model_copy(update=changed) if changed else node)
        story = bundle.story
        story_update: dict[str, Any] = {}
        if story.active_leaf_id in doomed:
            # Back to where that node's scene began, whether it is still open
            # or was closed with the messages left in memory: a branch onto
            # one of them must never save a leaf that isn't in the file.
            leaf_span = next(
                (n.meta.private_span for n in bundle.nodes if n.id == story.active_leaf_id),
                None,
            )
            span = memory.get(leaf_span) if leaf_span else None
            story_update["active_leaf_id"] = span.start_parent_id if span else None
        kept_log = [entry for entry in story.scene_log if entry.closed_at_node_id not in doomed]
        if len(kept_log) != len(story.scene_log):
            story_update["scene_log"] = kept_log
        if any(span.scene_summary for span in memory.values()):
            # The rolling summary is scene text: never written for a memory span.
            story_update["private_spans"] = [
                span.model_copy(update={"scene_summary": None, "scene_summary_through": None})
                if span.id in memory
                else span
                for span in story.private_spans
            ]
        if story_update:
            story = story.model_copy(update=story_update)
        return bundle.model_copy(
            update={
                "nodes": nodes,
                "story": story,
                "asides": [
                    a
                    for a in bundle.asides
                    if a.anchor_node_id not in doomed and a.private_span not in memory
                ],
            }
        )
