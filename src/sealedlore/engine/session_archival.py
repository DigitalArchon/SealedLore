"""Archival in the background: chapters prepared off the turn, adopted when due.

Archival used to run on the turn's path: the author sent, the summariser ran
for every chunk needed, then the passage. A first archival run takes several
chunks, and with the ledger beside each summary a chunk took 25-55s on Sonnet
4.6 (measured), so one turn could wait minutes.

Now the chapters are written on a thread of their own, from a snapshot, while
the author plays: started after a passage once the next prompt would be near
the budget (`PREPARE_AT`), or at the latest when a turn finds it over. Nothing
in the session changes until `adopt_archival`, on the worker thread, when
archival is due; a chapter whose nodes are no longer the next run on the path,
or whose text was edited meanwhile, is dropped. A turn that finds the budget
over and the chapters not ready does not wait: its oldest prose falls out of
the window for that turn, as it always has when archival couldn't help.

The work uses the provider's `detached()` clients, never the shared sibling:
the reads after the next passage use that, and a provider tracks one socket.
A provider without `detached` (the scripted mock) archives inline as before.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.archival import (
    SUMMARY_CUT_OFF,
    build_summary_messages,
    characters_in,
    plan_chunk,
    render_chunk,
    summary_params,
    turns_in,
)
from sealedlore.engine.characters import build_extraction_messages, known_names, parse_suggestions
from sealedlore.engine.ledger import build_ledger_messages, check_ledger, ledger_on, normalised
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session_merge import _close
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.models.generation import GenerationParams
from sealedlore.models.node import NARRATOR_SPEAKER_ID, Node
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatRequest, ProviderError

# Start preparing once the next prompt would be this share of the budget: a
# turn's own text is a few hundred tokens, so the chapters are usually ready
# before any turn goes over.
PREPARE_AT = 0.9
# What a chapter costs in the prompt when there are none yet to measure:
# ~250-400 words of summary plus the ledger's growth (measured: +100-150).
DEFAULT_CHAPTER_TOKENS = 600
# Room for a reasoning model: live, GLM 5.3 spent 101s thinking and the
# reply was cut off at 8,000. A cap only limits; it costs nothing unused.
LEDGER_MAX_TOKENS = 16_000


def _fingerprint(nodes: Sequence[Node]) -> list[str]:
    return [
        hashlib.sha256(f"{node.id}\0{node.content}\0{node.ooc or ''}".encode()).hexdigest()
        for node in nodes
    ]


@dataclass
class _Call:
    request: ChatRequest
    reply: tuple[str, Any] | None = None
    error: str | None = None


@dataclass
class _Chapter:
    node_ids: list[str]
    fingerprint: list[str]
    summary: _Call
    ledger: _Call | None
    ledger_text: str | None = None
    ledger_problem: str | None = None


@dataclass
class _ArchiveJob:
    chapters: list[_Chapter]
    scan: _Call | None
    done: threading.Event = field(default_factory=threading.Event)
    ready: list[_Chapter] = field(default_factory=list)
    error: str | None = None


def plan_chunks(
    verbatim: Sequence[Node],
    *,
    full_total: int,
    target: int,
    chunk_tokens,  # noqa: ANN001 - Callable[[list[Node]], int]
    chapter_tokens: int,
    turns: int,
    max_rounds: int,
) -> list[list[Node]]:
    """The chunks the next archival will take, estimated without writing them.

    The inline loop measures the prompt after each real chapter; here each
    chapter is costed at `chapter_tokens`, and one more is taken if in doubt.
    """
    chunks: list[list[Node]] = []
    offset = 0
    total = full_total
    while total > target and len(chunks) < max_rounds:
        chunk = plan_chunk(verbatim[offset:], turns=turns)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        total -= chunk_tokens(chunk) - chapter_tokens
    return chunks


class ArchivalRuntime:
    _archive_job: _ArchiveJob | None = None

    def can_archive_in_background(self) -> bool:
        return self.config.auto_archive and callable(getattr(self.provider, "detached", None))

    def _next_turn(self) -> TurnRequest:
        held = self.story.held_character_id
        return TurnRequest(
            speaker_id=held or NARRATOR_SPEAKER_ID, user_text="", controlled_character_id=held
        )

    def _measure(self, history: Sequence[Node]):  # noqa: ANN202 - AssembledPrompt
        """The next turn's prompt size, without replacing `last_prompt`: the
        inspector and the review show the prompt the last turn really sent."""
        kept = self.last_prompt
        try:
            return self.assemble(self._next_turn(), history_nodes=history)
        finally:
            self.last_prompt = kept

    def _chapter_tokens(self) -> int:
        chapters = [s for s in self.split().summaries if not s.merged_from]
        if not chapters:
            return DEFAULT_CHAPTER_TOKENS
        sizes = [self.estimator.estimate(s.content, self.model) for s in chapters[-5:]]
        return sum(sizes) // len(sizes) + 150

    def prepare_archival(self, *, force: bool = False) -> bool:
        """After a passage: start writing the chapters the next turns will need.

        `force` starts it whatever the prompt's size (a turn found it over).
        Never blocks, and does nothing while a job is out.
        """
        if not self.can_archive_in_background() or self._archive_job is not None:
            return False
        history = self.path()
        prompt = self._measure(history)
        budget = self.story.defaults.context_token_budget
        if not force and prompt.full_total < PREPARE_AT * budget:
            return False
        cast = self.visible_cast()
        chunks = plan_chunks(
            list(self.split(history).verbatim),
            full_total=prompt.full_total,
            target=self.archive_target_tokens(),
            chunk_tokens=lambda chunk: self.estimator.estimate(
                render_chunk(chunk, cast, texts=self.texts), self.model
            ),
            chapter_tokens=self._chapter_tokens(),
            turns=self.config.archive_chunk_turns,
            max_rounds=self._max_rounds(),
        )
        if not chunks:
            return False
        self._archive_job = self._start_job(chunks, history)
        return True

    def _max_rounds(self) -> int:
        from sealedlore.engine.session import MAX_AUTO_ARCHIVE_ROUNDS

        return MAX_AUTO_ARCHIVE_ROUNDS

    def _start_job(self, chunks: list[list[Node]], history: Sequence[Node]) -> _ArchiveJob:
        """Build every request now, on this thread; only the calls leave it."""
        cast = self.visible_cast()
        texts = self.texts
        model = self.summarization_model
        on_path = list(self.split(history).summaries)
        previous_text = on_path[-1].content if on_path else None
        chapters = [
            _Chapter(
                node_ids=[node.id for node in chunk],
                fingerprint=_fingerprint(chunk),
                summary=_Call(
                    request=ChatRequest(model=model, messages=[], params=GenerationParams())
                ),
                ledger=None,
            )
            for chunk in chunks
        ]
        # The thread reads its chunks as they stood when planned: an edit the
        # author makes meanwhile must not reach the summariser (the fingerprint
        # drops the chapter at adoption, but the call would already be out).
        chunks = [[node.model_copy(deep=True) for node in chunk] for chunk in chunks]
        prose = [render_chunk(chunk, cast, texts=texts) for chunk in chunks]
        scan = None
        if self.config.suggest_characters and not self.story.chat:
            supporting = self.bundle.supporting
            on_file = [c.name for c in (*self.cast, *supporting.characters)]
            on_file += [s.name for s in supporting.suggestions]
            scan = _Call(
                request=ChatRequest(
                    model=model,
                    messages=build_extraction_messages(
                        "\n\n".join(prose),
                        on_file=on_file,
                        declined=supporting.dismissed,
                        texts=texts,
                    ),
                    params=GenerationParams(),
                )
            )
        job = _ArchiveJob(chapters=chapters, scan=scan)
        first_ledger = ledger_on(on_path)
        earlier = "" if first_ledger else "\n\n".join(s.content for s in on_path)
        target_words = self.config.summary_target_words
        use_ledger = self.config.story_ledger and not self.story.chat
        clients = [self.provider.detached(), self.provider.detached()]

        def run() -> None:
            """Each chapter's summary and ledger side by side, one chapter after another:
            the next needs this one's summary and ledger to continue from."""
            summary_text, ledger_text = previous_text, first_ledger
            try:
                for chapter, chunk, text in zip(chapters, chunks, prose, strict=True):
                    previous = Summary(content=summary_text) if summary_text else None
                    chapter.summary.request = ChatRequest(
                        model=model,
                        messages=build_summary_messages(
                            chunk,
                            cast,
                            previous=previous,
                            target_words=target_words,
                            chat=self.story.chat,
                            texts=texts,
                        ),
                        params=summary_params(model),
                    )
                    worker = None
                    if use_ledger:
                        chapter.ledger = _Call(
                            request=ChatRequest(
                                model=model,
                                messages=build_ledger_messages(
                                    ledger_text,
                                    text,
                                    names=[c.name for c in characters_in(text, cast)],
                                    earlier=earlier if ledger_text is None else "",
                                    texts=texts,
                                ),
                                params=GenerationParams(max_tokens=LEDGER_MAX_TOKENS),
                            )
                        )
                        worker = threading.Thread(
                            target=_call, args=(clients[1], chapter.ledger), daemon=True
                        )
                        worker.start()
                    _call(clients[0], chapter.summary)
                    if worker is not None:
                        worker.join()
                    if chapter.summary.reply is None or not chapter.summary.reply[0].strip():
                        job.error = chapter.summary.error or "the summariser returned no text"
                        break
                    if chapter.summary.reply[1].finish_reason == "length":
                        job.error = SUMMARY_CUT_OFF
                        break
                    summary_text = chapter.summary.reply[0].strip()
                    if chapter.ledger is not None:
                        chapter.ledger_text, chapter.ledger_problem = _accepted(
                            chapter.ledger, ledger_text
                        )
                        ledger_text = chapter.ledger_text
                    job.ready.append(chapter)
                if scan is not None and job.ready:
                    _call(clients[0], scan)
            finally:
                job.done.set()
                for client in clients:
                    _close(client)

        threading.Thread(target=run, name="archival", daemon=True).start()
        return job

    def adopt_archival(self, history: Sequence[Node]) -> Iterator[SessionNotice]:
        """Put the chapters written in the background into the story. Worker thread only.

        Each must still be the next run on the path, unedited; the first that
        isn't ends it, and the rest are dropped (they continued from it).
        """
        job = self._archive_job
        if job is None or not job.done.is_set():
            return
        self._archive_job = None
        by_id = {node.id: node for node in self.nodes}
        adopted: list[Summary] = []
        archived: list[Node] = []
        for chapter in job.ready:
            verbatim = list(self.split(history).verbatim)
            run = verbatim[: len(chapter.node_ids)]
            nodes = [by_id.get(i) for i in chapter.node_ids]
            if [n.id for n in run] != chapter.node_ids or _fingerprint(run) != chapter.fingerprint:
                break
            summary = self._record_chapter(chapter)
            adopted.append(summary)
            archived.extend(n for n in nodes if n is not None)
        if adopted:
            prompt = self._measure(history)
            yield SessionNotice(
                f"Archived {turns_in(archived)} turns into {len(adopted)} "
                f"chapter{'s' if len(adopted) != 1 else ''}, written in the background — "
                f"prompt now ~{prompt.full_total:,} tokens."
            )
            problems = [c.ledger_problem for c in job.ready[: len(adopted)] if c.ledger_problem]
            if problems:
                yield SessionNotice(
                    f"The story ledger wasn't updated for {len(problems)} of them "
                    f"({problems[0]}); it carries on from before, and their summaries hold "
                    "what it would have added."
                )
        elif job.ready or job.error:
            reason = job.error or "the story changed while they were written"
            yield SessionNotice(f"The chapters written in the background weren't used ({reason}).")
        # Paid calls that weren't used still go in the log and the cost: the
        # chapters after the first mismatch, the one that failed, and the scan
        # when nothing was adopted.
        reason = job.error or "not adopted"
        for chapter in job.chapters[len(adopted) :]:
            self._log_dropped("summary", chapter.summary, reason, node_ids=chapter.node_ids)
            if chapter.ledger is not None:
                self._log_dropped("ledger", chapter.ledger, reason)
        if job.scan is not None and adopted:
            yield from self._adopt_scan(job.scan, archived)
        elif job.scan is not None:
            self._log_dropped("characters", job.scan, reason)

    def _log_dropped(self, kind: str, call: _Call, reason: str, **extra: Any) -> None:
        """A background reply that isn't used was paid for all the same."""
        if call.reply is None and call.error is None:
            return  # never sent
        log_ref = self._log(
            f"{kind}_request",
            {**extra, "payload": self.provider.build_payload(call.request), "background": True},
        )
        if call.reply is None:
            self._log(f"{kind}_response", {"request_log_ref": log_ref, "error": call.error})
            return
        text, completed = call.reply
        self._log(
            f"{kind}_response",
            {
                "request_log_ref": log_ref,
                "text": text,
                "usage": completed.raw_usage,
                "dropped": reason,
            },
        )
        self.unreported_usage.append(self.priced(completed.usage, call.request.model))

    def _record_chapter(self, chapter: _Chapter) -> Summary:
        assert chapter.summary.reply is not None
        text, completed = chapter.summary.reply
        model = chapter.summary.request.model
        log_ref = self._log(
            "summary_request",
            {
                "node_ids": chapter.node_ids,
                "payload": self.provider.build_payload(chapter.summary.request),
                "background": True,
            },
        )
        self._log(
            "summary_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        ledger = None
        if chapter.ledger is not None:
            ledger_ref = self._log(
                "ledger_request", {"payload": self.provider.build_payload(chapter.ledger.request)}
            )
            if chapter.ledger.reply is not None:
                ledger_reply, ledger_completed = chapter.ledger.reply
                self._log(
                    "ledger_response",
                    {
                        "request_log_ref": ledger_ref,
                        "text": ledger_reply,
                        "usage": ledger_completed.raw_usage,
                    },
                )
                self.unreported_usage.append(self.priced(ledger_completed.usage, model))
            else:
                self._log(
                    "ledger_response",
                    {"request_log_ref": ledger_ref, "error": chapter.ledger.error},
                )
            if chapter.ledger_problem:
                self._log(
                    "ledger_refused",
                    {"request_log_ref": ledger_ref, "reason": chapter.ledger_problem},
                )
            ledger = chapter.ledger_text
        summary = Summary(
            covered_node_ids=list(chapter.node_ids),
            content=text.strip(),
            model=model,
            usage=self.priced(completed.usage, model),
            ledger=ledger,
        )
        self.bundle.summaries.append(summary)
        self.save()
        return summary

    def _adopt_scan(self, scan: _Call, archived: Sequence[Node]) -> Iterator[SessionNotice]:
        log_ref = self._log(
            "characters_request",
            {
                "node_ids": [node.id for node in archived],
                "payload": self.provider.build_payload(scan.request),
                "background": True,
            },
        )
        if scan.reply is None:
            self._log("characters_response", {"request_log_ref": log_ref, "error": scan.error})
            yield SessionNotice(
                f"Couldn't check the archived turns for new characters ({scan.error})."
            )
            return
        text, completed = scan.reply
        self._log(
            "characters_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, scan.request.model))
        supporting = self.bundle.supporting
        known = known_names(
            (*self.cast, *supporting.characters),
            [*supporting.dismissed, *(s.name for s in supporting.suggestions)],
        )
        try:
            found = parse_suggestions(text, known=known)
        except ValueError as exc:
            yield SessionNotice(f"Couldn't check the archived turns for new characters ({exc}).")
            return
        supporting.suggestions.extend(found)
        self.save()
        if found:
            names = ", ".join(suggestion.name for suggestion in found)
            yield SessionNotice(f"New characters to consider, in the Cast tab: {names}.")


def _call(client, call: _Call) -> None:  # noqa: ANN001
    try:
        call.reply = client.complete(call.request)
    except ProviderError as exc:
        call.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a background call must never escape
        call.error = str(exc) or type(exc).__name__


def _accepted(call: _Call, previous: str | None) -> tuple[str | None, str | None]:
    """(the ledger to carry on with, why the reply was refused or None)."""
    if call.reply is None:
        return previous, call.error or "no reply"
    text, completed = call.reply
    problem = check_ledger(text, previous, finish_reason=completed.finish_reason)
    return (previous, problem) if problem else (normalised(text), None)
