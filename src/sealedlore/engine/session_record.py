"""The session's side of the public record (engine/public_record.py): kept
after each passage while the story has a character played by their own model,
on `record_model`, and given to each of them with their round.

Kept at the start of the round (`react_after_turn`, `react_to_turn`), once the
reads are done, so the keeper reads each passage with the scene it left: one
call a passage, catching up any passage on the path the record hasn't read (a
Stop, a failure, a stretch with nobody in the scene to answer). Far behind (a
story the trial was switched on for late), the older passages go in batches
of about `BATCH_WORDS`, then the last `ONE_BY_ONE` one at a time. Never in a
private scene: its messages aren't on the path, and the round doesn't run.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from sealedlore.engine.public_record import RecordState, apply_reply, keeper_prompt, record_at
from sealedlore.engine.reactions import independent_cast
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.node import Node
from sealedlore.providers.base import ChatRequest, ProviderError

ONE_BY_ONE = 12
BATCH_WORDS = 6000


class RecordRuntime:
    @property
    def record_model(self) -> str:
        """Who keeps the public record: the story's own, Settings', else the
        scene model."""
        return self.story.defaults.record_model or self.config.record_model or self.scene_model

    def keeps_record(self, history: Sequence[Node]) -> bool:
        return (
            self.config.public_record
            and not self.story.chat
            and not self.in_private
            and bool(independent_cast(self.visible_cast(history)))
        )

    def public_record(self, history: Sequence[Node]) -> tuple[str, ...]:
        """The record a character answering at the end of `history` is given."""
        return record_at(history).facts if self.config.public_record else ()

    def keep_public_record(self, history: Sequence[Node]) -> Iterator:
        """Bring the record up to the end of `history`, a passage at a time.
        A failed call says so and stops there, the record as it was: the
        round goes on without what that passage made public, and the next
        round tries again."""
        from sealedlore.engine.session import SessionNotice

        if not self.keeps_record(history):
            return
        last = max(
            (i for i, node in enumerate(history) if node.meta.public_record is not None),
            default=-1,
        )
        behind = [
            node
            for node in history[last + 1 :]
            if node.kind == "assistant" and node.content.strip()
        ]
        if not behind:
            return
        state = record_at(history[: last + 1])
        older, recent = behind[:-ONE_BY_ONE], behind[-ONE_BY_ONE:]
        steps: list[list[Node]] = []
        chunk: list[Node] = []
        words = 0
        for node in older:
            size = len(node.content.split())
            if chunk and words + size > BATCH_WORDS:
                steps.append(chunk)
                chunk, words = [], 0
            chunk.append(node)
            words += size
        if chunk:
            steps.append(chunk)
        steps += [[node] for node in recent]
        model = self.record_model
        for passages in steps:
            yield SessionNotice(f"Keeping the public record with {model}…")
            request = ChatRequest(
                model=model,
                messages=[
                    PromptMessage(
                        role="user",
                        parts=(ContentPart(text=keeper_prompt(state, passages, self.texts)),),
                    )
                ],
                params=self.side_params(model),
                extra_body=self.route_for("record", model),
            )
            log_ref = self._log(
                "record_request",
                {
                    "node_id": passages[-1].id,
                    "payload": self.provider.build_payload(request),
                },
            )
            try:
                text, completed = self.provider.complete(request)
            except ProviderError as exc:
                self._log("record_response", {"request_log_ref": log_ref, "error": str(exc)})
                yield SessionNotice(f"Couldn't keep the public record ({exc}).")
                return
            self._log(
                "record_response",
                {
                    "request_log_ref": log_ref,
                    "node_id": passages[-1].id,
                    "text": text,
                    "usage": completed.raw_usage,
                    "finish_reason": completed.finish_reason,
                },
            )
            self.unreported_usage.append(self.priced(completed.usage, model))
            new = None if completed.finish_reason == "length" else apply_reply(text, state)
            if new is None:
                # Carried forward, as the ledger's refused update is: asking
                # again every round would pay for the same reply.
                yield SessionNotice(
                    "The public record keeper's reply couldn't be used; the record stays as it was."
                )
            else:
                state = new
            self._keep_record(passages[-1], state)

    def _keep_record(self, node: Node, state: RecordState) -> None:
        node.meta.public_record = list(state.facts)
        node.meta.public_notes = list(state.notes)
