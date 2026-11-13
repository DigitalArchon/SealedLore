"""Follow-ups on the session's side: the author asking for changes to a story
or a plot set up for them, and undoing them. A mixin for `StorySession`.

The pure part is engine/followup.py. Here: the call on the authoring model,
logged so its cost counts; a story's changes applied at once (each checked
as a review's is, and only on a story not yet begun, so cards and entries can
be taken out); a plot written again through the plot writer's own steps; and
every follow-up that changed something kept for undo, newest first. Undo puts
back what the follow-up could change: the setup for a story's, the plot and
the cards and entries it adds or keeps hidden for a plot's. Never in a simple
chat or a private scene. The calls run on the worker, the story's one writer.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from sealedlore.engine.authoring import (
    FOLLOW_UP_KINDS,
    REVIEW_MAX_TOKENS,
    apply_change,
    describe,
    ordered_for_apply,
    parse_review,
)
from sealedlore.engine.followup import (
    NOTES_REWRITTEN,
    PLOT,
    STORY,
    FollowUpTurn,
    build_followup_messages,
    plot_request_text,
    setup_fingerprint,
    setup_view,
)
from sealedlore.engine.plot_draft import (
    PlotDraft,
    build_plot_revision_messages,
    plot_reply_text,
    story_text,
)
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.story_plot import (
    document_from_story,
    notes_from_plot,
    plot_uses,
    take_out_character,
    without_plot,
)
from sealedlore.ids import utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.plot import Chronicle
from sealedlore.models.story import Story
from sealedlore.providers.base import ChatRequest

# A follow-up rewrites only what it changes, but a whole cast made "more
# badass" is every card's text again.
FOLLOW_UP_MAX_TOKENS = REVIEW_MAX_TOKENS


@dataclass
class FollowUpUndo:
    """The story as it was before a follow-up, kept to put it back."""

    turn: FollowUpTurn
    story: Story
    cast: list[Character]
    supporting: list[Character]
    lore: list[LoreEntry]
    # A plot given to a story under way stamps its state on the leaf.
    leaf_id: str | None = None
    leaf_chronicle: Chronicle | None = None
    # `setup_fingerprint` just after: anything else changed since, undo asks.
    after: str = ""


class FollowUpRuntime:
    def follow_up_history(self, target: str) -> list[FollowUpTurn]:
        """What the author has asked of the story or its plot this session."""
        return self.follow_ups.setdefault(target, [])

    def follow_up_story(self, request: str) -> Generator[SessionNotice, None, FollowUpTurn]:
        """Change the story's setup as the author asks. Yields progress;
        returns what was changed and what couldn't be. Raises ValueError for
        a story that has begun (Review settings changes one under way), a
        reply that can't be read or was cut off, and ProviderError."""
        self._refuse_in_chat("Changing the story")
        self._refuse_in_private("Changing the story")
        if self.nodes:
            raise ValueError(
                "the story has begun: Story → Review settings with the model… changes a story "
                "under way"
            )
        if not request.strip():
            raise ValueError("say what you'd like changed first")
        model = self.authoring_model
        messages = build_followup_messages(
            setup_view(self.bundle, self.hidden_ids()),
            request,
            self.follow_up_history(STORY),
            self.texts,
        )
        chat = ChatRequest(
            model=model,
            extra_body=self.route_for("authoring"),
            messages=messages,
            params=self.side_params(model, max_tokens=FOLLOW_UP_MAX_TOKENS),
        )
        text, completed = yield from self._authoring_stream(
            chat,
            kind="followup",
            details={"target": STORY, "request": request},
            writing="Changing the story",
            thinking="Thinking it through",
        )
        if completed.finish_reason == "length":
            raise ValueError("the reply was cut off before its end: ask for less at once")
        review = parse_review(text, self.bundle, kinds=FOLLOW_UP_KINDS)
        turn = FollowUpTurn(target=STORY, request=request.strip(), summary=review.summary)
        undo = self._kept_for_undo(turn)
        notes_before = self.story.storyteller_notes
        for change in ordered_for_apply(review.changes):
            try:
                apply_change(self.bundle, change)
            except ValueError as exc:
                turn.refused.append(f"{describe(change)}: {exc}")
                continue
            if change.kind != "storyteller_notes":
                turn.changed.append(describe(change))
        if self.story.storyteller_notes != notes_before:
            # Said last, as one line: that they changed, never what they say.
            turn.changed.append(NOTES_REWRITTEN)
        turn.refused[:0] = review.cannot_fix
        self.follow_up_history(STORY).append(turn)
        if turn.changed:
            self.sync_unstarted_scene()
            self._changed_by_follow_up(undo)
        return turn

    def follow_up_plot(
        self, request: str
    ) -> Generator[SessionNotice, None, tuple[FollowUpTurn, PlotDraft]]:
        """Write the story's plot again as the author asks, through the plot
        writer's own steps (`_write_plot`). Yields progress; returns the turn
        and the plot as the story has it now. Raises ValueError when nothing
        usable came back (the plot then stays as it was), and ProviderError."""
        self._refuse_in_chat("A plot")
        self._refuse_in_private("Changing the plot")
        if self.story.plot is None:
            raise ValueError("the story has no plot to change: write one first")
        if not request.strip():
            raise ValueError("say what you'd like changed first")
        base = without_plot(document_from_story(self.bundle))
        shown = without_plot(document_from_story(self.bundle, for_model=True))
        # Notes the plot's own writer gave the story are part of its last
        # reply, and its to write again; any others are the story's, told to
        # it as the story's. Never both.
        own = notes_from_plot(self.story)
        notes_before = self.story.storyteller_notes
        messages = build_plot_revision_messages(
            story_text(shown),
            plot_reply_text(document_from_story(self.bundle), own_notes=own),
            plot_request_text(request, self.follow_up_history(PLOT), self.texts),
            plays=self._plot_plays(),
            so_far=self._plot_so_far(),
            notes="" if own else self.story.storyteller_notes or "",
            texts=self.texts,
        )
        turn = FollowUpTurn(target=PLOT, request=request.strip())
        undo = self._kept_for_undo(turn)
        draft = yield from self._write_plot(
            messages, base, revise=True, notes_own=own, details={"request": request}
        )
        if not self.nodes:
            self._drop_plot_orphans()
            draft.document = document_from_story(self.bundle)
        turn.changed = [f"The plot: {len(draft.document.events)} events"]
        if self.story.storyteller_notes != notes_before:
            # That they changed, never what they say.
            turn.changed.append(NOTES_REWRITTEN)
        turn.refused = list(draft.notes)
        self.follow_up_history(PLOT).append(turn)
        self._changed_by_follow_up(undo)
        return turn, draft

    def follow_up_to_undo(self, target: str) -> FollowUpTurn | None:
        """The follow-up undo would take back: the newest, if it was this
        target's (one before it can't be undone past it)."""
        if self.follow_up_undo and self.follow_up_undo[-1].turn.target == target:
            return self.follow_up_undo[-1].turn
        return None

    def follow_up_changed_since(self) -> bool:
        """Whether the story changed after the newest follow-up: undoing it
        would take those changes back too."""
        if not self.follow_up_undo:
            return False
        return setup_fingerprint(self.bundle) != self.follow_up_undo[-1].after

    def undo_follow_up(self, target: str) -> bool:
        """Put the story back as it was before the newest follow-up, if it
        was this target's."""
        if self.follow_up_to_undo(target) is None:
            return False
        self._refuse_in_private("Undoing a change")
        undo = self.follow_up_undo.pop()
        live, kept = self.story, undo.story
        if target == STORY:
            live.title = kept.title
            live.world_bible = kept.world_bible
            live.storyteller_notes = kept.storyteller_notes
            live.storyteller_notes_by = kept.storyteller_notes_by
            live.setup = kept.setup
            live.defaults = kept.defaults
            live.style = kept.style
            live.picture_style = kept.picture_style
            live.image_style = kept.image_style
            if not self.nodes:
                live.scene = kept.scene
        else:
            live.plot = kept.plot
            live.plot_schedule = kept.plot_schedule
            # A plot written again may have written its own notes again.
            live.storyteller_notes = kept.storyteller_notes
            live.storyteller_notes_by = kept.storyteller_notes_by
            for node in self.nodes:
                if node.id == undo.leaf_id:
                    node.meta.chronicle = undo.leaf_chronicle
        self.bundle.cast[:] = undo.cast
        self.bundle.supporting.characters[:] = undo.supporting
        self.bundle.lore[:] = undo.lore
        undo.turn.undone = True
        self.forget_review_undo()
        self._sync_scene()
        self.story.updated_at = utc_now_iso()
        self.save()
        return True

    def _kept_for_undo(self, turn: FollowUpTurn) -> FollowUpUndo:
        path = self.path()
        leaf = path[-1] if path else None
        chronicle = leaf.meta.chronicle if leaf is not None else None
        return FollowUpUndo(
            turn=turn,
            story=self.story.model_copy(deep=True),
            cast=[c.model_copy(deep=True) for c in self.bundle.cast],
            supporting=[c.model_copy(deep=True) for c in self.bundle.supporting.characters],
            lore=[e.model_copy(deep=True) for e in self.bundle.lore],
            leaf_id=leaf.id if leaf is not None else None,
            leaf_chronicle=chronicle.model_copy(deep=True) if chronicle is not None else None,
        )

    def _changed_by_follow_up(self, undo: FollowUpUndo) -> None:
        # A review's undo would put back the story from before this change.
        self.forget_review_undo()
        self.story.updated_at = utc_now_iso()
        self.save()
        undo.after = setup_fingerprint(self.bundle)
        self.follow_up_undo.append(undo)

    def _drop_plot_orphans(self) -> None:
        """Before the story begins, the people and places a plot written again
        no longer has go with the old plot: hidden, nobody has met them."""
        plot = self.story.plot
        for card in [*self.bundle.cast, *self.bundle.supporting.characters]:
            if card.plot_hidden and not plot_uses(plot, card.id):
                take_out_character(self.bundle, card)
        self.bundle.lore[:] = [
            entry
            for entry in self.bundle.lore
            if not (entry.plot_hidden and not plot_uses(plot, entry.id))
        ]
        self.save()
