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

A simple chat has private parts on the same terms (a beta tester asked to
switch mid-chat, as a story can): the same span, the same three rules, with a
conversation's prompts in place of a story's (`engine/chat.py`). A chat on a
TEE model is private throughout and has none.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from sealedlore.engine.archival import render_chunk, split_path
from sealedlore.engine.chat import CONDENSED_LEAD as CHAT_CONDENSED_LEAD
from sealedlore.engine.chat import (
    build_chat_handoff_messages,
    build_chat_private_condense_messages,
    build_chat_private_summary_messages,
    render_chat_chunk,
)
from sealedlore.engine.private_prompt import (
    build_handoff_messages,
    build_private_condense_messages,
    build_private_summary_messages,
    private_rules_sections,
)
from sealedlore.engine.prompt import (
    AssembledPrompt,
    assemble_chat,
    assemble_prompt,
    story_rules_sections,
)
from sealedlore.engine.retrieval import lore_layout
from sealedlore.engine.scene_state import log_on_path
from sealedlore.ids import utc_now_iso
from sealedlore.models.config import split_model
from sealedlore.models.node import NARRATOR_SPEAKER_ID, Node, NodeMeta, Usage
from sealedlore.models.private import PrivateKeep, PrivateSpan, TeeUpstream
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError, why_not_whole
from sealedlore.providers.tee import is_private_mode

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
# A reply's TEE marks (NodeMeta.tee), the most proven first.
_MARK_ORDER = ("encrypted", "verified", "signed", "unchecked", "failed")
# Held while a job decides whether it attests a TEE/ model again
# (`_attest_again`), so that two never do it at once.
_REATTESTING = threading.Lock()
# Set on a thread while it makes a call whose reply is checked where it is
# marked (a passage, an aside, the scene's summary: `_checked_where_marked`),
# so the watch every other reply goes through (`_watch_tee`) leaves it.
_MARKED_HERE = threading.local()


@contextmanager
def _checked_where_marked() -> Iterator[None]:
    before = getattr(_MARKED_HERE, "active", False)
    _MARKED_HERE.active = True
    try:
        yield
    finally:
        _MARKED_HERE.active = before


def checked_where_marked(events: Iterator[Any]) -> Iterator[Any]:
    """A stream whose reply is checked where it is marked: the watch leaves it
    (`_checked_where_marked`), only while the provider runs, never while the
    caller holds an event."""
    iterator = iter(events)
    while True:
        with _checked_where_marked():
            try:
                event = next(iterator)
            except StopIteration:
                return
        yield event


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
    _private_provider: ChatProvider | None = None
    # A reply of the attested TEE/ model that failed its check where nothing
    # shows a mark (a chapter, a condensed part, a picture's prompt): said by
    # the window once the job is done (`take_tee_notices`).
    _tee_notices: list[str] | None = None
    _tee_notices_lock = threading.Lock()

    @property
    def private_provider(self) -> ChatProvider | None:
        return self._private_provider

    @private_provider.setter
    def private_provider(self, provider: ChatProvider | None) -> None:
        """The scene's provider reports every reply for its TEE check, as the
        story's does (`_watch_tee`)."""
        from sealedlore.engine.session import _weak_watch

        self._private_provider = provider
        if provider is not None:
            try:
                provider.tee_watch = _weak_watch(self._watch_tee)
            except AttributeError:  # a stand-in that takes no attributes
                pass

    # Checks each reply's signature on a TEE model (providers/tee.py); None
    # when the model isn't one or checking is off.
    private_tee: Any = None
    # Set by the window while a TEE/ model is being attested, or with the
    # reason it was refused: every call that would reach that model refuses
    # before sending (_tee_guard). None when there's nothing to wait for.
    # The session sets it too while it attests the model again (_attest_again).
    tee_block: str | None = None
    # True while the session attests a TEE/ model again.
    tee_attesting: bool = False
    # What that last found, for the window once the job is done
    # (`take_reattestation`): ("attested", the attestation) or ("refused", why).
    _reattested: tuple[str, Any] | None = None
    # What happened to a scene the app closed in the middle of, for a notice.
    private_notice: str | None = None

    def _tee_guard(self) -> None:
        """Nothing goes to a TEE/ model whose enclave is still being attested,
        or was refused (providers/tee.TeeClient.attest); and one whose
        attestation is REATTEST_SECONDS old (providers/tee.py) is attested
        again first. Every call that reaches the model comes here first, on
        its job's thread."""
        from sealedlore.providers.tee import TeeClient, TeeRefused

        if self.tee_block:
            raise TeeRefused(f"{self.tee_block}; nothing was sent")
        tee = self.private_tee
        if isinstance(tee, TeeClient) and tee.due():
            self._attest_again(tee)

    def _attest_again(self, tee: Any) -> None:
        """Attest a TEE/ model again before the request that found its
        attestation old, on that request's thread (a job's, never the
        window's), as the window attested it first: everything else that
        would reach the model refuses meanwhile (`tee_block`), and a refusal
        stands until the window attests it anew (Try again). Without this a
        TEE/ model was attested only as a scene or chat began or its model
        changed: an instance Chutes started later, whose replies are signed
        by none, was never checked, nor a revocation seen."""
        from sealedlore.providers.tee import TeeRefused

        with _REATTESTING:
            if self.tee_block:
                raise TeeRefused(f"{self.tee_block}; nothing was sent")
            if self.private_tee is not tee or not tee.due():
                return  # attested again, or replaced, meanwhile
            self.tee_block = f"{tee.model}'s enclave is still being attested"
            self.tee_attesting = True
        try:
            attestation = tee.attest()
        except Exception as exc:  # refused, or out of reach: either way nothing goes
            reason = str(exc) or "the enclave's attestation didn't hold"
            self.tee_block = f"The attestation was refused: {reason}"
            self._reattested = ("refused", reason)
            raise TeeRefused(f"{self.tee_block}; nothing was sent") from exc
        finally:
            self.tee_attesting = False
        span = self.open_span
        if span is not None:
            span.attestation = attestation
        else:
            self.story.chat_attestation = attestation
        self._reattested = ("attested", attestation)
        self.tee_block = None

    def take_reattestation(self) -> tuple[str, Any] | None:
        """What attesting a TEE/ model again last found, once: ("attested",
        the attestation) or ("refused", why); None when it hasn't since."""
        found, self._reattested = self._reattested, None
        return found

    # --- every reply of the attested TEE/ model -------------------------------------

    def _tee_mark(self, completed: Any, model: str | None = None) -> str | None:
        """How a TEE/ model's reply checks out against the record its attested
        key signs (providers/tee.py `check_reply`): "verified", "signed",
        "failed", or "unchecked" when no record could be read. None when there
        is nothing attested to check it with: no TEE/ model attested (or Check
        TEE models off), the reply of a `model` other than the attested one,
        or a private/ model's, sealed and so proved by its seal. A failed one
        makes the model attested again before its next request (`_tee_guard`:
        TeeClient.doubt)."""
        return self._tee_check(completed, model)[0]

    def _tee_check(
        self, completed: Any, model: str | None = None, *, name: bool = False
    ) -> tuple[str | None, TeeUpstream | None]:
        """`_tee_mark`, and with `name` (where the reply shows its mark, and
        Settings → Private asks for it: `Config.tee_name_upstreams`) who ran
        its model, by RedPill's record (providers/tee.py `served_by`)."""
        tee = self.private_tee
        if tee is None or completed is None or getattr(completed, "sealed", False):
            return None, None
        if model is not None and split_model(model)[1] != getattr(tee, "model", None):
            return None, None
        named = name and self.config.tee_name_upstreams and callable(getattr(tee, "check", None))
        upstream = None
        try:
            if named:
                found = tee.check(
                    getattr(completed, "response_id", None),
                    **_reply_record(completed),
                    name_upstream=True,
                )
                mark, upstream = found.mark, found.upstream
            else:
                mark = tee.check_reply(
                    getattr(completed, "response_id", None), **_reply_record(completed)
                )
        except Exception:  # noqa: BLE001 - an unreachable check is not a failed one
            mark = "unchecked"
        if mark == "failed":
            doubt = getattr(tee, "doubt", None)
            if callable(doubt):
                doubt()
        return mark, upstream

    def _watch_tee(self, request: ChatRequest, completed: Any) -> None:
        """Every reply comes here, on the thread it came back on and before
        its caller has it (the providers' `tee_watch`). One the attested TEE/
        model wrote that isn't checked where it is marked (a passage, an aside,
        the scene's summary) is checked as those are: a chapter or a merge, a
        card condensed, the condensed part of a scene, a picture's or a
        video's prompt, the looks, the handoff, whatever else reaches the
        model. Its mark goes into the API log (`tee_check`; never in a scene
        kept in memory only, whose log holds nothing but its calls'), and a
        failure is said once the job is done (`take_tee_notices`)."""
        if getattr(_MARKED_HERE, "active", False):
            return
        mark = self._tee_mark(completed, request.model)
        if mark is None:
            return
        span = self.open_span
        if span is None or span.keep == "disk":
            entry: dict[str, Any] = {"model": request.model, "mark": mark}
            if span is not None:
                entry["private"] = True
            self._log("tee_check", entry)
        if mark == "failed":
            with self._tee_notices_lock:
                if self._tee_notices is None:
                    self._tee_notices = []
                self._tee_notices.append(
                    f"A reply from {request.model} failed its TEE check: it doesn't match "
                    "the record the attested key signed. The model is attested again before "
                    "anything more is sent to it."
                )

    def take_tee_notices(self) -> list[str]:
        """What the TEE checks of replies with no mark of their own found
        wrong since this was last asked, once."""
        with self._tee_notices_lock:
            found, self._tee_notices = list(self._tee_notices or []), None
        return found

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
        if self.tee_chat:
            raise ValueError(
                "this chat is on a TEE model, so all of it is private already: there is "
                "no other model to keep a part of it from"
            )
        full = self.full_path()
        span = PrivateSpan(
            # After the last message the author sees, an ended scene's
            # included: its messages stay in the story as read (a memory
            # scene's are saved under the nearest written one, `bundle_to_save`).
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

    def switch_private_model(self, *, provider: ChatProvider, model: str) -> None:
        """Carry the open scene on, and write its summary, with another model:
        nano-gpt retired the one it began on (Oct 2026), and without this a
        scene with messages could only end with nothing carried over. Only to a
        model as private as the scene's (`tee.move_refusal`): the scene's text
        goes to it. Earlier messages keep their own `meta.model`."""
        from sealedlore.providers.tee import move_refusal

        span = self.open_span
        if span is None:
            raise ValueError("no private scene is open")
        if move_refusal(span.model, model):
            raise ValueError(f"{model} isn't as private as {span.model}, the scene's model")
        old, old_tee = self.private_provider, self.private_tee
        for client in (old, old_tee):
            close = getattr(client, "close", None)
            if callable(close) and client is not provider:
                try:
                    close()
                except Exception:  # noqa: BLE001 - a socket already gone
                    pass
        span.model = model
        # The old model's enclave proved nothing about the new one's.
        span.attestation = None
        self.private_provider = provider
        self.private_tee = None
        self.tee_block = None
        self.save()

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

    def private_model(self) -> str | None:
        """The model a private scene's prompts go to: the open scene's, else
        the one Settings → Private names (before the scene has begun)."""
        span = self.open_span
        if span is not None and span.model:
            return span.model
        settings = self.config.private_provider
        return settings.model if settings is not None and settings.model else None

    def private_options(self):
        # Counted for the private model, and what its replies teach the
        # estimator is its own (`_update_correction_factor`): with the story
        # model's, a private model that counts differently moved the story's.
        model = self.private_model()
        if self.story.chat:
            # A chat has no rules to replace: its own system prompt goes, at
            # the private model's budget.
            return replace(
                self.assembly_options(), token_budget=self.config.private_budget, model=model
            )
        full = story_rules_sections(self.story, texts=self.texts)
        return replace(
            self.assembly_options(),
            model=model,
            token_budget=self.config.private_budget,
            rules_override=tuple(private_rules_sections(self.story, full, texts=self.texts)),
            # The cast's room at the private budget; a card with no condensed
            # text yet goes short, since nothing is written while a scene is
            # open (CastRuntime condenses for this room beforehand).
            cast_token_cap=self.config.cast_cap(self.config.private_budget),
            cast_waiting_form="short",
        )

    def private_history(self, span: PrivateSpan) -> list[Node]:
        return self.model_nodes(self.full_path(), include_span=span.id)

    def _private_text(self, nodes: Sequence[Node]) -> str:
        """Messages as the private model is given them outside a prompt (to
        summarise, to condense, to count), the scene's own included."""
        if self.story.chat:
            return render_chat_chunk(nodes, include_private=True, texts=self.texts)
        return render_chunk(nodes, self.cast, include_private=True, texts=self.texts)

    def private_assemble(self, turn, history: Sequence[Node], span: PrivateSpan) -> AssembledPrompt:
        """The story's prompt, fitted to the private model: its budget, its
        rules, the handoff in place of the chapters when there is one, and
        lore by keywords only (no embeddings or Jev: they would hear the scene)."""
        options = self.private_options()
        if self.story.chat:
            return self._private_assemble_chat(turn, history, span, options)
        layout = lore_layout(
            self.visible_lore(),
            budget=options.token_budget,
            share=self.config.lore_whole_share,
            count_tokens=self._count_lore_tokens,
        )
        lore = () if layout.mode == "whole" else self._keyword_lore(turn, history)
        verbatim, summaries = self._private_window(history, span)
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

    def _private_window(
        self, history: Sequence[Node], span: PrivateSpan
    ) -> tuple[list[Node], list[Summary]]:
        """What the private model reads word for word, and what stands for the
        rest: the handoff in place of the chapters when there is one, and the
        scene's own condensed part where its messages stood."""
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
        return verbatim, summaries

    def _private_assemble_chat(
        self, turn, history: Sequence[Node], span: PrivateSpan, options
    ) -> AssembledPrompt:
        """A chat's request, fitted to the private model: the same system
        prompt and conversation, at the private budget. Nothing else of the
        app is in a chat's prompt to begin with."""
        verbatim, summaries = self._private_window(history, span)
        prompt = assemble_chat(
            story=self.story,
            history_nodes=verbatim,
            tail=turn.user_text,
            summaries=summaries,
            nodes_by_id={node.id: node for node in self.nodes},
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

    def _condensed_node(self, span: PrivateSpan) -> Node:
        """The condensed part as one narrator message, for the prompt only:
        never attached to the story."""
        lead = CHAT_CONDENSED_LEAD if self.story.chat else CONDENSED_LEAD
        return Node(
            id=f"condensed-{span.id}",
            kind="assistant",
            speaker_id=NARRATOR_SPEAKER_ID,
            content=f"{lead}\n\n{span.scene_summary or ''}",
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
            taken += self.estimator.estimate(self._private_text([node]), model)
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
        request = ChatRequest(
            model=span.model,
            messages=self._condense_messages(previous, self._private_text(chunk)),
            params=self.side_params(span.model, max_tokens=PRIVATE_CONDENSE_MAX_TOKENS),
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

        unfit = why_not_whole(completed)
        if not text or unfit is not None:
            # Kept under the scene's own latest message (`_kept_notice`): a
            # memory scene's never reaches the disk.
            yield self._kept_notice(
                "The private scene outgrew the private model's context, but its summary of "
                "the oldest messages came back "
                + (f"not whole: {unfit}" if text else "empty")
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

    def _condense_messages(self, previous: str | None, text: str):
        if self.story.chat:
            return build_chat_private_condense_messages(
                previous, text, words=PRIVATE_CONDENSE_WORDS, texts=self.texts
            )
        names = [c.name for c in self.visible_cast()] + [c.name for c in self.visible_supporting()]
        held = self._character(self.story.held_character_id)
        return build_private_condense_messages(
            previous,
            text,
            names,
            self.story.style.person or "third",
            held=held.name if held else None,
            words=PRIVATE_CONDENSE_WORDS,
            tense=self.story.style.tense,
            texts=self.texts,
        )

    def _summary_messages(self, text: str):
        if self.story.chat:
            return build_chat_private_summary_messages(text, texts=self.texts)
        names = [c.name for c in self.visible_cast()] + [c.name for c in self.visible_supporting()]
        held = self._character(self.story.held_character_id)
        return build_private_summary_messages(
            text,
            names,
            self.story.style.person or "third",
            held=held.name if held else None,
            tense=self.story.style.tense,
            texts=self.texts,
        )

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
        chat = self.story.chat
        label = "Part" if chat else "Chapter"
        parts = [f"{label} {i}: {s.content}" for i, s in enumerate(split.summaries, 1)]
        parts.append(
            render_chat_chunk(list(split.verbatim), texts=self.texts)
            if chat
            else render_chunk(list(split.verbatim), self.cast, texts=self.texts)
        )
        words = max(150, int(self.config.private_budget * HANDOFF_SHARE / 1.35))
        build = build_chat_handoff_messages if chat else build_handoff_messages
        request = ChatRequest(
            model=self.model,
            extra_body=self.route_for("story"),
            messages=build("\n\n".join(p for p in parts if p), words, texts=self.texts),
            params=self.side_params(self.model, max_tokens=HANDOFF_MAX_TOKENS),
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
        # The one read a scene keeps: its looks, chosen on a private model
        # (session_looks.py), the scene's own messages only.
        yield from self.update_looks_after_turn()

    @staticmethod
    def _passage_done():
        from sealedlore.engine.session import PassageDone

        return PassageDone()

    def _written_mark(self, model: str, span: PrivateSpan | None) -> str | None:
        """The TEE mark a private scene's or a chat's passage carries from the
        moment it is written, before its reply is checked (`_check_tee`). The
        check runs once the passage is in, and a Stop on its last chunk, or a
        reply cut off partway, closed the turn first: a TEE model's passage was
        left with no mark, drawn as any other model's. An end-to-end encrypted
        model's is "encrypted": every request for one goes sealed, and its
        reply is read only once the attested enclave's seal has opened it
        (providers/private_mode.py `_accept`), so what came is proved however
        much of it came. A TEE/ model whose replies are checked is "unchecked"
        until its check says otherwise. A TEE/ model with "Check TEE models"
        off is just a model, and its passages unmarked, as before."""
        if span is None and not self.story.chat:
            return None
        if is_private_mode(model):
            return "encrypted"
        return "unchecked" if self.private_tee is not None else None

    def _check_tee(self, node: Node | None) -> None:
        """A TEE model's reply: was it signed by the enclave that was attested?
        An end-to-end encrypted one needs no check: only the attested enclave's
        key could have sealed it (providers/private_mode.py)."""
        if node is not None and getattr(self, "_last_sealed", False):
            node.meta.tee = self._with_carried(node, "encrypted")
            self._mark_sent(node, "encrypted")
            return
        if node is None or self.private_tee is None:
            return
        mark, upstream = self._tee_check(getattr(self, "_last_completed", None), name=True)
        mark = mark or "unchecked"
        node.meta.tee = self._with_carried(node, mark)
        node.meta.tee_upstream = self._upstream_carried(node, upstream)
        # The author's message is marked too, or it looked less protected than
        # the reply (the author, Sept 2026), and only as far as the reply shows
        # (the author, Nov 11 2026: "always make clear to the user what is
        # actually happening"): the guard sent it only while the model was
        # attested, but nano-gpt routes it, and only a record the attested key
        # signed shows that enclave received it. Chutes signs none, and a model
        # may be served elsewhere (TEE/gemma4-31b falls back to Tinfoil).
        # Never "signed": that is the reply's. Once shown received, a take
        # that shows nothing doesn't take it back: the same text was received.
        received = mark in ("verified", "signed")
        self._mark_sent(node, "received" if received else "unproven", keep_received=True)

    def _upstream_carried(self, node: Node, upstream: TeeUpstream | None) -> TeeUpstream | None:
        """Who ran a passage just carried on: named only when both parts
        name the same provider, or the first named none."""
        carried = getattr(self, "_carried_upstream", None)
        self._carried_upstream = None
        if carried is None or carried[0] != node.id or carried[1] is None:
            return upstream
        before = carried[1]
        if upstream is None or not before.name:
            return upstream
        if before.name != upstream.name:
            return TeeUpstream(note="its parts were run by different providers")
        return upstream

    def _with_carried(self, node: Node, mark: str) -> str | None:
        """A passage just carried on (Continue) is only as proven as its less
        proven part: what it was before and what the continuation's check
        found. A first part with no mark proves nothing, nor does the whole."""
        carried = getattr(self, "_carried_mark", None)
        self._carried_mark = None
        if carried is None or carried[0] != node.id:
            return mark
        before = carried[1]
        if before not in _MARK_ORDER or mark not in _MARK_ORDER:
            return None
        return max(before, mark, key=_MARK_ORDER.index)

    def _mark_sent(self, reply: Node, how: str, *, keep_received: bool = False) -> None:
        """Mark the author's message a reply answers with how it was sent."""
        parent = next((n for n in self.nodes if n.id == reply.parent_id), None)
        if parent is None or parent.kind != "user":
            return
        if keep_received and parent.meta.tee == "received":
            return
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
        request = ChatRequest(
            model=span.model,
            messages=self._summary_messages(scene_text),
            params=self.side_params(span.model, max_tokens=PRIVATE_SUMMARY_MAX_TOKENS),
        )
        log_ref = self._log_turn(
            "request",
            {"summary": True, "payload": self.private_provider.build_payload(request)},
            span,
        )
        with _checked_where_marked():  # span.summary_tee, below
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
        unfit = why_not_whole(completed)
        if unfit is not None:
            # As on every summary path: half a summary never passes as whole.
            raise ProviderError(
                f"the private model's summary wasn't whole: {unfit}; end the scene again to "
                "ask for another"
            )
        if completed.sealed:
            span.summary_tee = "encrypted"
        elif self.private_tee is not None:
            # The summary is the one reply that goes back into the story: was
            # it signed by the enclave that was attested?
            span.summary_tee = self._tee_mark(completed) or "unchecked"
        return text

    def _scene_text_for_summary(self, span: PrivateSpan, nodes: Sequence[Node]) -> str:
        """The scene as the exit summary reads it: the condensed part, then
        the rest verbatim. A scene too long even for that is condensed
        further first, so the summary request fits the private model."""
        limit = int(self.config.private_budget * PRIVATE_CONDENSE_AT)
        for _round in range(4):
            covered = set(self._condensed_ids(span, nodes))
            rest = [node for node in nodes if node.id not in covered]
            text = self._private_text(rest)
            previous = span.scene_summary if covered else None
            if previous:
                earlier = "IN THIS PART" if self.story.chat else "IN THE SCENE"
                text = f"WHAT CAME EARLIER {earlier} (condensed):\n{previous}\n\n{text}"
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
        serve the next image prompt, and its last prompt be shown or sent."""
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
        # The scene's last prompt is scene text: nothing after the scene may
        # read it (the inspector, Help → Model trouble's "the story's prompt").
        self.last_prompt = None

    # --- what reaches the disk ----------------------------------------------------------

    def bundle_to_save(self):
        """The bundle as it may be written: a memory-only scene's messages,
        and anything anchored to them, left out. Whatever hung from them (the
        summary; a branch rewritten from it; a scene begun after one ended
        with no summary) hangs from the nearest message above them that is
        written, as do the leaf and a scene's start: the path as it stands,
        so a take swapped in under the scene's start since is the one saved."""
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
        index = {node.id: node for node in bundle.nodes}

        def kept_above(node_id: str | None) -> str | None:
            """`node_id`, or its nearest ancestor that is written."""
            seen: set[str] = set()
            while node_id is not None and node_id in doomed and node_id not in seen:
                seen.add(node_id)
                node_id = index[node_id].parent_id
            return None if node_id in doomed else node_id

        moved = {
            node.id: kept_above(node.parent_id)
            for node in bundle.nodes
            if node.id not in doomed and node.parent_id in doomed
        }
        nodes: list[Node] = []
        for node in bundle.nodes:
            if node.id in doomed:
                continue
            changed: dict[str, Any] = {}
            children = [child for child in node.children if child not in doomed]
            children += [
                child
                for child, parent in moved.items()
                if parent == node.id and child not in children
            ]
            if children != node.children:
                changed["children"] = children
            if node.id in moved:
                changed["parent_id"] = moved[node.id]
            nodes.append(node.model_copy(update=changed) if changed else node)
        story = bundle.story
        story_update: dict[str, Any] = {}
        if story.active_leaf_id in doomed:
            # Whether its scene is still open or was closed with the messages
            # left in memory: a leaf that isn't in the file can't be opened.
            story_update["active_leaf_id"] = kept_above(story.active_leaf_id)
        kept_log = [entry for entry in story.scene_log if entry.closed_at_node_id not in doomed]
        if len(kept_log) != len(story.scene_log):
            story_update["scene_log"] = kept_log
        spans: list[PrivateSpan] = []
        for span in story.private_spans:
            update = self._span_as_saved(span, memory, doomed, kept_above)
            spans.append(span.model_copy(update=update) if update else span)
        if any(saved is not span for saved, span in zip(spans, story.private_spans, strict=True)):
            story_update["private_spans"] = spans
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

    @staticmethod
    def _span_as_saved(
        span: PrivateSpan, memory: dict[str, PrivateSpan], doomed: set[str], kept_above
    ) -> dict[str, Any]:
        """What changes in a span as it is written: a memory span's rolling
        summary goes (it is scene text), and a start among messages that
        aren't written moves to the nearest one above them that is."""
        update: dict[str, Any] = {}
        if span.id in memory and (span.scene_summary or span.scene_summary_through):
            update.update(scene_summary=None, scene_summary_through=None)
        if span.start_parent_id in doomed:
            update["start_parent_id"] = kept_above(span.start_parent_id)
        return update


def _reply_record(completed: Any) -> dict[str, Any]:
    """What a TEE reply's check needs of its stream (providers/tee.py
    `check`): its receipt id, the hash of its bytes, nano-gpt's route, and
    when its request was sent."""
    if completed is None:
        return {}
    return {
        "receipt_id": getattr(completed, "receipt_id", None),
        "body_hash": getattr(completed, "body_hash", None),
        "route": getattr(completed, "tee_route", None),
        "sent_at": getattr(completed, "sent_at", None),
    }
