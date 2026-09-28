"""Merging old chapters into parts, in the background. A mixin for `StorySession`.

Chapters never shrink on their own: one per ten exchanges, all of them in every
prompt, so a long enough story fills its budget with summaries alone. Past
`Config.summary_merge_ratio` of the budget, the oldest chapters are merged
into one part (`archival.plan_merge`).

Nothing here makes the author wait. The call goes out on its own thread with
a `detached()` client of its own and touches no session state; the reply is
kept until `adopt_merge`, on the worker thread, at the next moment the summary
block changes anyway (an archival), so the part costs no cache write of its
own and the session keeps one writer. A reply whose chapters changed while it
was out is thrown away.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.archival import (
    SUMMARY_CUT_OFF,
    build_merge_messages,
    plan_merge,
    source_chapters,
    summaries_tokens,
    summary_params,
)
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatRequest, ProviderError


@dataclass
class _MergeJob:
    """One merge call in flight, and what it was asked about."""

    run_ids: list[str]
    # The text each summary had when asked: a changed one voids the reply.
    contents: list[str]
    request: ChatRequest
    model: str
    thread: threading.Thread | None = None
    reply: tuple[str, Any] | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class MergeRuntime:
    _merge_job: _MergeJob | None = None

    def merge_run(self) -> list[Summary]:
        """What would be merged now, on the active path; empty if nothing."""
        path_summaries = list(self.split().summaries)
        budget = self.story.defaults.context_token_budget
        size = summaries_tokens(
            path_summaries, lambda text: self.estimator.estimate(text, self.model), texts=self.texts
        )
        return plan_merge(
            path_summaries,
            over_threshold=size > self.config.summary_merge_ratio * budget,
            keep=self.config.summary_merge_keep,
        )

    def start_background_merge(self) -> bool:
        """Send a merge off on its own thread, if one is due. Never blocks.

        Needs a provider that can make a call alongside the worker's
        (`detached()`); a scripted one can't, and merges only when asked.
        """
        if not self.config.auto_archive or self._merge_job is not None:
            return False
        make_client = getattr(self.provider, "detached", None)
        if not callable(make_client):
            return False
        run = self.merge_run()
        if not run:
            return False
        job = self._merge_job_for(run)
        side = make_client()

        def call() -> None:
            try:
                job.reply = side.complete(job.request)
            except Exception as exc:  # noqa: BLE001 - reported at adoption
                job.error = str(exc) or type(exc).__name__
            finally:
                job.done.set()
                _close(side)

        job.thread = threading.Thread(target=call, name="chapter-merge", daemon=True)
        self._merge_job = job
        job.thread.start()
        return True

    def merge_now(self) -> Summary | None:
        """Merge on this thread, and adopt it: for the author asking, and tests."""
        self._refuse_in_private("Merging chapters")
        run = self.merge_run()
        if not run:
            return None
        job = self._merge_job_for(run)
        job.reply = self.provider.complete(job.request)
        job.done.set()
        self._merge_job = job
        return self.adopt_merge()

    def _merge_job_for(self, run: list[Summary]) -> _MergeJob:
        model = self.summarization_model
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("summarisation"),
            messages=build_merge_messages(
                run,
                self.visible_cast(),
                chat=self.story.chat,
                target_words=int(
                    self.config.summary_merge_share
                    * sum(len(summary.content.split()) for summary in run)
                ),
                texts=self.texts,
            ),
            params=summary_params(model),
        )
        return _MergeJob(
            run_ids=[summary.id for summary in run],
            contents=[summary.content for summary in run],
            request=request,
            model=model,
        )

    def adopt_merge(self) -> Summary | None:
        """Put a finished merge into the story. Worker thread only; never waits.

        Returns the new part, or None if there was none ready or the reply no
        longer applies (a chapter edited, rebuilt or deleted meanwhile).
        """
        job = self._merge_job
        if job is None or not job.done.is_set():
            return None
        self._merge_job = None
        log_ref = self._log(
            "merge_request",
            {"summary_ids": job.run_ids, "payload": self.provider.build_payload(job.request)},
        )
        if job.error is not None or job.reply is None:
            self._log("merge_response", {"request_log_ref": log_ref, "error": job.error})
            return None
        text, completed = job.reply
        self._log(
            "merge_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        usage = self.priced(completed.usage, job.model)
        text = text.strip()
        by_id = {summary.id: summary for summary in self.bundle.summaries}
        run = [by_id.get(summary_id) for summary_id in job.run_ids]
        if (
            not text
            or completed.finish_reason == "length"
            or any(summary is None for summary in run)
            or [summary.content for summary in run if summary] != job.contents
            or any(summary.stale for summary in run if summary)
        ):
            # Paid for all the same, so it counts.
            self.unreported_usage.append(usage)
            return None
        run = [summary for summary in run if summary is not None]
        part = Summary(
            covered_node_ids=[node_id for summary in run for node_id in summary.covered_node_ids],
            content=text,
            model=job.model,
            usage=usage,
            merged_from=source_chapters(run),
            # The ledger is not merged: it is as of the part's end either way.
            ledger=run[-1].ledger,
        )
        # An older part this one takes in is superseded; its chapters stay.
        replaced = {summary.id for summary in run if summary.merged_from}
        self.bundle.summaries[:] = [s for s in self.bundle.summaries if s.id not in replaced]
        self.bundle.summaries.append(part)
        self.save()
        return part

    def drop_stale_parts(self) -> list[Summary]:
        """Parts made stale by an edit go; their chapters stand in until the next merge.

        A part is derived from its chapters, so it is merged again rather than
        rebuilt. One the author has edited is theirs, and stays (stale).
        """
        dropped = [
            s for s in self.bundle.summaries if s.merged_from and s.stale and not s.hand_edited
        ]
        if dropped:
            gone = {summary.id for summary in dropped}
            self.bundle.summaries[:] = [s for s in self.bundle.summaries if s.id not in gone]
        return dropped

    def rebuild_part(self, part: Summary) -> Summary:
        """Merge a part's chapters again, in place. Raises ProviderError."""
        self._refuse_in_private("Rebuilding a part")
        by_id = {summary.id: summary for summary in self.bundle.summaries}
        chapters = [by_id[i] for i in part.merged_from if i in by_id]
        if len(chapters) != len(part.merged_from):
            raise ValueError("the chapters this part was merged from are no longer in the story")
        job = self._merge_job_for(chapters)
        text, completed = self.provider.complete(job.request)
        log_ref = self._log(
            "merge_request",
            {"summary_ids": job.run_ids, "payload": self.provider.build_payload(job.request)},
        )
        self._log(
            "merge_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        if not text.strip():
            raise ProviderError("the summariser returned no text")
        if completed.finish_reason == "length":
            raise ProviderError(SUMMARY_CUT_OFF)
        part.content = text.strip()
        part.model = job.model
        part.usage = self.priced(completed.usage, job.model)
        part.stale = False
        part.hand_edited = False
        self.save()
        return part


def _close(client) -> None:  # noqa: ANN001
    """Close a background client; nothing about closing may reach the story."""
    try:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except Exception:  # noqa: BLE001, S110 - best effort
        pass
