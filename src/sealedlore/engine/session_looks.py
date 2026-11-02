"""Looks on the session's side: which picture each character is shown with
as the story stands, and the author's choice of one. A mixin for
`StorySession`; the pure part is engine/looks.py.

A choice goes on the message at the end of the story, as a scene correction
does, so it lasts from there and follows branches and takes. In a private
scene it goes only on the scene's own messages, and counts only while the
scene is open: the looks are read from the path as the models see it, the
open scene's messages in and an ended one's out. On the public message
before it, or carried on by the next choice made after it, a look chosen in
private would outlast the scene and say something of what happened there.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from sealedlore.engine.archival import render_chunk
from sealedlore.engine.look_pick import LookSubject, build_pick_messages, look_subjects, parse_pick
from sealedlore.engine.looks import looks_at
from sealedlore.engine.scene_state import same_person, scene_at
from sealedlore.models.node import Node
from sealedlore.models.private import PrivateSpan
from sealedlore.providers.base import ChatRequest, ProviderError
from sealedlore.providers.tee import move_refusal
from sealedlore.tree import path_to

# A reasoning model spends its thinking from this too (the reads' room).
LOOK_MAX_TOKENS = 4000


@dataclass
class _LookRead:
    """A look pick's request, prepared before the call so the call can go
    out beside the reads (session_plot.py `_read_after_turn`)."""

    node: Node
    subjects: list[LookSubject]
    request: ChatRequest
    log_ref: str
    span: PrivateSpan | None = None
    before: dict[str, str] = field(default_factory=dict)


class LooksRuntime:
    def _looks_path(self) -> list:
        """The path looks are read from: an ended private scene's messages
        left out, the open one's in."""
        span = self.open_span
        return self.model_nodes(self.full_path(), include_span=span.id if span else None)

    def looks_now(self) -> dict[str, str]:
        """Who is shown with which picture at the end of the story
        (character id → picture id)."""
        return looks_at(self._looks_path())

    def looks_wanted_now(self) -> dict[str, str]:
        """For the latest passage, per character, the look and expression the
        choice found no picture for ("armour · angry"), or none."""
        path = self._looks_path()
        return dict(path[-1].meta.looks_wanted) if path else {}

    def show_look(self, character_id: str, ref_id: str | None) -> None:
        """Show a character with one of their pictures from here on; None
        puts them back to their portrait. ValueError, in words, when there is
        nowhere to keep it yet."""
        path = self.full_path()
        if not path:
            raise ValueError("A look can be chosen once the story has begun.")
        span = self.open_span
        if span is not None and path[-1].meta.private_span != span.id:
            raise ValueError(
                "In a private scene a look is chosen once the scene has a message of its own: "
                "on the story's message before it, it would outlast the scene."
            )
        character = next((c for c in (*self.cast, *self.supporting) if c.id == character_id), None)
        if character is None:
            raise ValueError("That character isn't in the story.")
        if ref_id is not None and all(ref.id != ref_id for ref in character.reference_images):
            raise ValueError(f"That picture isn't one of {character.name}'s any more.")
        looks = looks_at(self._looks_path())
        if ref_id is None:
            looks.pop(character_id, None)
        else:
            looks[character_id] = ref_id
        path[-1].meta.looks = looks
        self.save()

    # --- chosen after a passage (engine/look_pick.py) ------------------------------

    def _private_look_model(self, span: PrivateSpan) -> str:
        """The model a private scene's looks are chosen on: Settings' choice
        when the scene could move to it, else the scene's own."""
        chosen = self.config.private_look_model.strip()
        if chosen and move_refusal(span.model, chosen) is None:
            return chosen
        return span.model

    def _prepare_look_read(self, node: Node | None = None) -> _LookRead | None:
        """The look pick's request for the latest passage, logged; None when
        picks are off, nothing was written, or nobody it could concern has
        more than one picture. In a private scene, on the private model
        (logged as that scene's calls are)."""
        if self.config.look_picks != "every_turn" or self.story.chat:
            return None
        result = self.last_result
        if node is None:
            if result is None or result.node is None or result.stopped_early:
                return None
            node = result.node
        if not node.content.strip():
            return None
        span = self.open_span if node.meta.private_span else None
        if node.meta.private_span and (span is None or span.id != node.meta.private_span):
            return None  # a scene's passage is never read once the scene has ended
        if span is not None and self.private_provider is None:
            return None
        path = self.model_nodes(
            path_to(self.nodes, node.id), include_span=span.id if span else None
        )
        before = looks_at(path)
        scene = scene_at(path[:-1], self.story.scene)
        supporting = self.visible_supporting()
        present = set(scene.present_character_ids) | {
            c.id for c in supporting if any(same_person(n, c) for n in scene.present_others)
        }
        author = path[-2] if len(path) > 1 and path[-2].kind == "user" else None
        # The character the author plays is in the scene, whatever the card says.
        held = author.meta.controlled_character_id if author is not None else None
        present.add(held or self.story.held_character_id or "")
        cast = self.visible_cast(path)
        subjects = look_subjects([*cast, *supporting], before, present, node.content)
        if not subjects:
            return None
        author_turn = render_chunk([author], cast, texts=self.texts) if author else ""
        messages = build_pick_messages(
            subjects, author_turn=author_turn, passage=node.content, texts=self.texts
        )
        if span is None:
            model = self.scene_model
            request = ChatRequest(
                model=model,
                extra_body=self.route_for("scene"),
                messages=messages,
                params=self.side_params(model, max_tokens=LOOK_MAX_TOKENS),
            )
            body = {"node_id": node.id, "payload": self.provider.build_payload(request)}
            log_ref = self._log("look_request", body)
        else:
            model = self._private_look_model(span)
            request = ChatRequest(
                model=model,
                # Nothing in a private scene (`route_for`): its own endpoint.
                extra_body=self.route_for("scene"),
                messages=messages,
                params=self.side_params(model, max_tokens=LOOK_MAX_TOKENS),
            )
            body = {"look": True, "payload": self.private_provider.build_payload(request)}
            log_ref = self._log_turn("request", body, span)
        return _LookRead(node, subjects, request, log_ref, span, before)

    def update_looks_after_turn(
        self, pending: _LookRead | None = None, fetched: dict[str, object] | None = None
    ) -> Iterator:
        """Choose who is shown with which picture after the latest passage,
        and keep it on the passage. `fetched` is the reply (or the error) of
        a call already made on another thread. A failure leaves everyone as
        they were and says so; it never costs the passage."""
        from sealedlore.engine.session import SessionNotice

        pending = pending or self._prepare_look_read()
        if pending is None:
            return
        node, span = pending.node, pending.span
        provider = self.private_provider if span is not None else self.provider
        model = pending.request.model
        yield SessionNotice(
            f"Choosing looks with {model}…",
            waiting_since=time.monotonic() if fetched is None else None,
        )
        try:
            if fetched is None:
                text, completed = provider.complete(pending.request)
            elif "error" in fetched:
                raise fetched["error"]  # type: ignore[misc]
            else:
                text, completed = fetched["reply"]  # type: ignore[misc]
            answer = {
                "request_log_ref": pending.log_ref,
                "text": text,
                "usage": completed.raw_usage,
            }
            if span is not None:
                self._log_turn("response", {"look": True, **answer}, span)
            else:
                self._log("look_response", answer)
            self.unreported_usage.append(self.priced(completed.usage, model))
            picks = parse_pick(text, pending.subjects)
        except (ProviderError, ValueError) as exc:
            yield SessionNotice(f"Couldn't choose the looks for this passage ({exc}).")
            return
        yield from self._keep_picks(node, pending, picks)

    def _keep_picks(self, node: Node, pending: _LookRead, picks) -> Iterator:
        """The picks on the passage: a character back on their portrait has
        none chosen. Said in the status bar when someone's look changed."""
        from sealedlore.engine.session import SessionNotice

        looks = dict(pending.before)
        wanted: dict[str, str] = {}
        changed: list[str] = []
        subjects = {subject.character_id: subject for subject in pending.subjects}
        for pick in picks:
            subject = subjects[pick.character_id]
            first = subject.options[0].ref_id
            was = looks.get(pick.character_id) or first
            if pick.ref_id == first:
                looks.pop(pick.character_id, None)
            else:
                looks[pick.character_id] = pick.ref_id
            if pick.ref_id != was:
                words = next(o.words for o in subject.options if o.ref_id == pick.ref_id)
                changed.append(f"{subject.name}: {words}")
            if pick.missing:
                wanted[pick.character_id] = pick.missing
        node.meta.looks = looks
        node.meta.looks_wanted = wanted
        self.save()
        if changed:
            yield SessionNotice("Looks: " + "; ".join(changed))
