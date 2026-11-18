"""The session's side of who saw which part of a passage that moves
(engine/reactions.py `passage_views`): at the start of the round, after the
public record, each passage of the newest `VIEW_CATCH_UP` that moves for a
character played by their own model (in the scene at one end only, or cut
away inside) is read once on the scene model, and the paragraphs each was
there for kept on it. A failed or unreadable reply leaves the passage whole
for them, as before, and says so. Never in a chat or a private scene.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from sealedlore.engine.reactions import apply_views, independent_cast, passage_views, view_prompt
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.node import Node
from sealedlore.providers.base import ChatRequest, ProviderError


class ViewsRuntime:
    def mark_views(self, history: Sequence[Node]) -> Iterator:
        from sealedlore.engine.session import SessionNotice

        if self.story.chat or self.in_private:
            return
        cast = self.visible_cast(history)
        characters = independent_cast(cast)
        if not characters:
            return
        model = self.scene_model
        for node, before, after, ask in passage_views(
            history, characters, start=self.story.setup.starting_scene
        ):
            yield SessionNotice(f"Working out who saw what with {model}…")
            request = ChatRequest(
                model=model,
                messages=[
                    PromptMessage(
                        role="user",
                        parts=(
                            ContentPart(
                                text=view_prompt(node, before, after, ask, cast, self.texts)
                            ),
                        ),
                    )
                ],
                params=self.side_params(model),
                extra_body=self.route_for("scene", model),
            )
            log_ref = self._log(
                "views_request",
                {"node_id": node.id, "payload": self.provider.build_payload(request)},
            )
            try:
                text, completed = self.provider.complete(request)
            except ProviderError as exc:
                self._log("views_response", {"request_log_ref": log_ref, "error": str(exc)})
                yield SessionNotice(f"Couldn't work out who saw which part of a passage ({exc}).")
                return
            self._log(
                "views_response",
                {
                    "request_log_ref": log_ref,
                    "node_id": node.id,
                    "text": text,
                    "usage": completed.raw_usage,
                    "finish_reason": completed.finish_reason,
                },
            )
            self.unreported_usage.append(self.priced(completed.usage, model))
            if completed.finish_reason == "length" or not apply_views(text, node, ask):
                yield SessionNotice(
                    "Who saw which part of a passage couldn't be read; "
                    "it counts whole for those in it."
                )
