"""Rebuilding chapters in the background, when the author asks. A mixin for `StorySession`.

Nothing is rebuilt on its own. An edit to an archived message, or another take
swapped in under a chapter, flags the chapter stale; the author rebuilds when
they choose, every flagged chapter at once, since they may have several small
edits to make first (the author's rule, Oct 2026). What changed is where the
rebuild runs: it used to hold the window behind a progress dialog, one chapter
after another. Now the calls go out on a thread of their own with `detached()`
clients, as background archival's do, and the author plays on meanwhile; the
old chapter is sent until its new text is adopted.

Adoption keeps the session's one writer: the worker at the start of a turn,
or the window when nothing is running (`adopt_rebuild`). A rebuild whose
messages changed while it was out (edited again, another take swapped in), or
whose chapter the author edited by hand meanwhile, is not used and the flag
stays. Paid for all the same, so it is logged and counted.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.archival import (
    SUMMARY_CUT_OFF,
    build_merge_messages,
    build_summary_messages,
)
from sealedlore.engine.session_archival import _fingerprint
from sealedlore.engine.session_merge import _close
from sealedlore.ids import utc_now_iso
from sealedlore.models.node import Node
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatRequest


def _texts_print(texts: Sequence[str]) -> list[str]:
    return [hashlib.sha256(text.encode()).hexdigest() for text in texts]


@dataclass
class _Rebuild:
    """One chapter or part being written again, and what it was written from."""

    summary_id: str
    # The summary's text when asked: the author's own edit meanwhile wins.
    content: str
    covered: list[str]
    # A chapter's messages, or a part's chapters' texts, as they were.
    fingerprint: list[str]
    model: str
    request: ChatRequest
    is_part: bool = False
    # Built on the thread: the chapter it continues may be rebuilt first.
    chunk: list[Node] = field(default_factory=list)
    previous_id: str | None = None
    previous_text: str | None = None
    target_words: int = 0
    reply: tuple[str, Any] | None = None
    error: str | None = None


@dataclass
class _RebuildJob:
    items: list[_Rebuild]
    done: threading.Event = field(default_factory=threading.Event)


class RebuildRuntime:
    _rebuild_job: _RebuildJob | None = None

    def can_rebuild_in_background(self) -> bool:
        return callable(getattr(self.provider, "detached", None))

    def rebuilding_ids(self) -> set[str]:
        """The chapters being rebuilt now, for the panel and the banner."""
        job = self._rebuild_job
        return {item.summary_id for item in job.items} if job is not None else set()

    def rebuild_ready(self) -> bool:
        job = self._rebuild_job
        return job is not None and job.done.is_set()

    def start_rebuild(self, summary_ids: Sequence[str]) -> bool:
        """Send the chapters off to be rebuilt; never blocks. The author's
        confirmation for a hand-edited one is the caller's (the window asks).

        Raises ValueError in a private scene (it would send the story to
        another model) or while a rebuild is still out.
        """
        self._refuse_in_private("Rebuilding a summary")
        if self._rebuild_job is not None:
            raise ValueError("chapters are already being rebuilt; try again once they are in")
        by_id = {summary.id: summary for summary in self.bundle.summaries}
        wanted = [by_id[i] for i in dict.fromkeys(summary_ids) if i in by_id]
        index = {node.id: node for node in self.nodes}
        depth = {
            s.id: len(self._path_to(s.covered_node_ids[0])) if s.covered_node_ids else 0
            for s in wanted
        }
        # Chapters first, oldest first, so a chapter continues from its
        # predecessor's new text; parts last, merged from their chapters' new text.
        wanted.sort(key=lambda s: (bool(s.merged_from), depth[s.id]))
        model = self.summarization_model
        route = self.route_for("summarisation")
        params = self.side_params(model)
        cast = self.visible_cast()
        texts = self.texts
        chat = self.story.chat
        target_words = self.config.summary_target_words
        merge_share = self.config.summary_merge_share
        items: list[_Rebuild] = []
        for summary in wanted:
            if summary.merged_from:
                chapters = [by_id[i] for i in summary.merged_from if i in by_id]
                if len(chapters) != len(summary.merged_from):
                    continue
                items.append(
                    _Rebuild(
                        summary_id=summary.id,
                        content=summary.content,
                        covered=list(summary.covered_node_ids),
                        fingerprint=_texts_print([c.content for c in chapters]),
                        model=model,
                        request=ChatRequest(
                            model=model, messages=[], params=params, extra_body=route
                        ),
                        is_part=True,
                    )
                )
                continue
            nodes = [index.get(node_id) for node_id in summary.covered_node_ids]
            if not nodes or any(node is None for node in nodes):
                continue
            previous = self.previous_on_branch(summary)
            items.append(
                _Rebuild(
                    summary_id=summary.id,
                    content=summary.content,
                    covered=list(summary.covered_node_ids),
                    fingerprint=_fingerprint(nodes),  # type: ignore[arg-type]
                    model=model,
                    request=ChatRequest(model=model, messages=[], params=params, extra_body=route),
                    # The thread reads them as they stood when asked.
                    chunk=[node.model_copy(deep=True) for node in nodes],  # type: ignore[union-attr]
                    previous_id=previous.id if previous is not None else None,
                    previous_text=previous.content if previous is not None else None,
                    target_words=target_words,
                )
            )
        if not items:
            return False
        chapter_texts = {s.id: s.content for s in self.bundle.summaries}
        merged_from = {item.summary_id: list(by_id[item.summary_id].merged_from) for item in items}
        job = _RebuildJob(items=items)
        client = self.provider.detached()

        def run() -> None:
            written: dict[str, str] = {}
            try:
                for item in items:
                    if item.is_part:
                        run_texts = [
                            Summary(content=written.get(i, chapter_texts[i]))
                            for i in merged_from[item.summary_id]
                        ]
                        words = sum(len(s.content.split()) for s in run_texts)
                        messages = build_merge_messages(
                            run_texts,
                            cast,
                            target_words=int(merge_share * words),
                            chat=chat,
                            texts=texts,
                        )
                    else:
                        previous = written.get(item.previous_id or "", item.previous_text)
                        messages = build_summary_messages(
                            item.chunk,
                            cast,
                            previous=Summary(content=previous) if previous else None,
                            target_words=item.target_words,
                            chat=chat,
                            texts=texts,
                        )
                    item.request = ChatRequest(
                        model=item.request.model,
                        messages=messages,
                        params=item.request.params,
                        extra_body=item.request.extra_body,
                    )
                    try:
                        item.reply = client.complete(item.request)
                    except Exception as exc:  # noqa: BLE001 - reported at adoption
                        item.error = str(exc) or type(exc).__name__
                        continue
                    text, completed = item.reply
                    if text.strip() and completed.finish_reason != "length":
                        written[item.summary_id] = text.strip()
            finally:
                job.done.set()
                _close(client)

        self._rebuild_job = job
        threading.Thread(target=run, name="chapter-rebuild", daemon=True).start()
        return True

    def adopt_rebuild(self) -> list[str]:
        """Put finished rebuilds into the story; the notices to show.

        The worker calls it at the start of a turn, the window when nothing is
        running: never both at once. Waits for a private scene to end, since
        the summary block is frozen in one.
        """
        job = self._rebuild_job
        if job is None or not job.done.is_set() or self.in_private:
            return []
        self._rebuild_job = None
        by_id = {summary.id: summary for summary in self.bundle.summaries}
        index = {node.id: node for node in self.nodes}
        rebuilt: list[Summary] = []
        changed: list[str] = []
        failed: list[str] = []
        for item in job.items:
            log_ref = self._log(
                "merge_request" if item.is_part else "summary_request",
                {
                    "summary_id": item.summary_id,
                    "node_ids": item.covered,
                    "payload": self.provider.build_payload(item.request),
                    "background": True,
                    "rebuild": True,
                },
            )
            kind = "merge_response" if item.is_part else "summary_response"
            if item.reply is None:
                self._log(kind, {"request_log_ref": log_ref, "error": item.error})
                failed.append(item.error or "no reply")
                continue
            text, completed = item.reply
            usage = self.priced(completed.usage, item.model)
            self._log(
                kind, {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage}
            )
            text = text.strip()
            summary = by_id.get(item.summary_id)
            if not text or completed.finish_reason == "length":
                self.unreported_usage.append(usage)
                failed.append(SUMMARY_CUT_OFF if text else "the summariser returned no text")
                continue
            if summary is not None and not any(s is summary for s in self.bundle.summaries):
                # A part dropped by the rebuild of one of its own chapters.
                self.unreported_usage.append(usage)
                continue
            if summary is None or not self._rebuild_still_applies(item, summary, by_id, index):
                self.unreported_usage.append(usage)
                if summary is not None:
                    changed.append(summary.id)
                continue
            summary.content = text
            summary.model = item.model
            summary.usage = usage
            summary.hand_edited = False
            summary.stale = False
            summary.edited_at = utc_now_iso()
            if not item.is_part:
                self.drop_parts_over(summary.id)
            rebuilt.append(summary)
        self.save()
        notices: list[str] = []
        numbers = self.chapter_numbers_on_paths()
        if rebuilt:
            named = ", ".join(_named(s, numbers) for s in rebuilt)
            notices.append(f"Rebuilt {named} in the background.")
        if changed:
            named = ", ".join(_named(by_id[i], numbers) for i in changed if i in by_id)
            notices.append(
                f"{named} changed while being rebuilt, so the new summary wasn't used; "
                "it is still marked stale. Rebuild again when you're done editing."
            )
        if failed:
            notices.append(
                f"{len(failed)} summar{'y' if len(failed) == 1 else 'ies'} couldn't be rebuilt "
                f"({failed[0]}); still marked stale."
            )
        return notices

    def _rebuild_still_applies(
        self,
        item: _Rebuild,
        summary: Summary,
        by_id: dict[str, Summary],
        index: dict[str, Node],
    ) -> bool:
        if summary.content != item.content or summary.covered_node_ids != item.covered:
            return False
        if item.is_part:
            chapters = [by_id.get(i) for i in summary.merged_from]
            if any(c is None for c in chapters):
                return False
            return _texts_print([c.content for c in chapters]) == item.fingerprint  # type: ignore[union-attr]
        nodes = [index.get(node_id) for node_id in summary.covered_node_ids]
        if any(node is None for node in nodes):
            return False
        return _fingerprint(nodes) == item.fingerprint  # type: ignore[arg-type]

    def chapter_numbers_on_paths(self) -> dict[str, int]:
        """Chapter numbers as the reader counts them on the path being read,
        falling back to the place among all chapters for another branch's."""
        from sealedlore.engine.archival import chapter_number, chapter_numbers

        numbers = chapter_numbers(self.split().summaries)
        for summary in self.bundle.summaries:
            numbers.setdefault(summary.id, chapter_number(self.bundle.summaries, summary))
        return numbers

    def _drop_rebuild(self, reason: str) -> None:
        """The story is being left: whatever the job has is logged, not adopted."""
        job = self._rebuild_job
        if job is None:
            return
        self._rebuild_job = None
        for item in job.items:
            if item.reply is None and item.error is None:
                continue
            log_ref = self._log(
                "merge_request" if item.is_part else "summary_request",
                {
                    "summary_id": item.summary_id,
                    "payload": self.provider.build_payload(item.request),
                    "background": True,
                    "rebuild": True,
                },
            )
            kind = "merge_response" if item.is_part else "summary_response"
            if item.reply is None:
                self._log(kind, {"request_log_ref": log_ref, "error": item.error})
                continue
            text, completed = item.reply
            self._log(
                kind,
                {
                    "request_log_ref": log_ref,
                    "text": text,
                    "usage": completed.raw_usage,
                    "dropped": reason,
                },
            )
            self.unreported_usage.append(self.priced(completed.usage, item.model))


def _named(summary: Summary, numbers: dict[str, int]) -> str:
    first = numbers.get(summary.id, 0)
    last = first + summary.chapters - 1
    return f"Chapters {first}–{last}" if last > first else f"Chapter {first}"
