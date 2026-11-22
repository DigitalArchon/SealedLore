"""The session's side of lore fired as SillyTavern fires it
(engine/tavern_firing.py): a story's own choice (`StoryDefaults.lore_firing`,
the Lore page), its settings SillyTavern's (`tavern_world_info`).

Its entries are the lore the models may see (never one the plot keeps
hidden, nor one held back until mentioned). A constant entry nothing can
stop stands in the cached system block; the rest are fired each turn and
sent with it, in SillyTavern's order, in place of SealedLore's own choice
(the room `fit_lore` gives doesn't apply: SillyTavern's budget does).
Those standing are fired with the rest, as SillyTavern fires them (the author,
Nov 21 2026): what they say is read for the others' keys and they count
toward min activations; they are sent once, where they stand. They stand
outside SillyTavern's budget, the one way these rules differ from its own:
SillyTavern leaves out a constant the budget can't hold, SealedLore sends it
every turn, and counted, it took room it never used (a card's 19,000 tokens
of constants left every keyed entry over a 15,000 budget, and recursion
stopped). In a private scene the budget is a share of the private model's.

No entry can cost a turn: one whose settings can't be read fires by its
keywords as an entry written here does, and a firing that fails altogether
sends the standing entries alone and says so (`retrieve`).

Each author turn records what fired and the chat's length then
(`NodeMeta.tavern_fired`, `tavern_length`), fired or not, and the timed
effects are replayed along the path from those records: every branch, take
and turn taken back has its own. Regenerate fires afresh (a SillyTavern
swipe scans again, and rolls again); Continue scans again too, and what it
fires is added to the turn's record."""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.retrieval import Injection, LoreLayout, RetrievalReport, render_entry
from sealedlore.engine.tavern_firing import (
    FireEntry,
    TavernSettings,
    fire,
    fire_entry,
    replay,
)
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node

# What a passage is scanned as, "name: text": the storyteller's.
STORYTELLER_NAME = "Narrator"
NEW_TURN = ("normal",)
# SealedLore's Regenerate keeps the take it replaces, as a SillyTavern swipe does.
RETAKE = ("regenerate", "swipe")
CONTINUE = ("continue",)


class TavernRuntime:
    # A retake's firing, recorded on its turn only once the take has text
    # (`settle_tavern`): a take stopped or failed before any left the passage
    # standing with a firing that wasn't its own.
    _tavern_pending: tuple | None = None

    @property
    def fires_like_tavern(self) -> bool:
        """The story's lore fires as SillyTavern's does (never a chat's)."""
        return self.story.defaults.lore_firing == "sillytavern" and not self.story.chat

    def tavern_settings(self) -> TavernSettings:
        return TavernSettings(**self.story.defaults.tavern_world_info.model_dump())

    def _fire_entries(self, entries: Sequence[LoreEntry]) -> list[FireEntry]:
        return [self._fire_entry(entry) for entry in entries]

    def _fire_entry(self, entry: LoreEntry) -> FireEntry:
        own = {
            "id": entry.id,
            "title": entry.title,
            "content": entry.content,
            "keywords": entry.keywords,
            "always_on": entry.always_on,
            "enabled": entry.enabled,
        }
        if entry.tavern is None:
            return fire_entry(**own, raw=None)
        try:
            return fire_entry(
                **own,
                raw=entry.tavern.fields,
                numbered=entry.tavern.numbered,
                decorators=entry.tavern.decorators,
            )
        except Exception:  # noqa: BLE001 - a stranger's entry, in any shape
            # Settings nothing here can read: the entry fires by its keywords,
            # as one written in SealedLore does, and the story goes on.
            return fire_entry(**own, raw=None)

    def tavern_layout(self) -> LoreLayout:
        """Standing: the constant entries nothing can stop, cached with the
        world; the rest fire each turn."""
        live = [entry for entry in self.visible_lore() if entry.enabled]
        standing_ids = {f.id for f in self._fire_entries(live) if f.standing}
        total = sum(self._count_lore_tokens(render_entry(entry)) for entry in live)
        return LoreLayout(
            "select",
            tuple(e for e in live if e.id in standing_ids),
            tuple(e for e in live if e.id not in standing_ids),
            total,
            0,
        )

    def _tavern_name(self, node: Node) -> str:
        if node.kind == "assistant":
            return STORYTELLER_NAME
        if node.speaker_id == DIRECTOR_SPEAKER_ID:
            return "Direction"
        if node.speaker_id == NARRATOR_SPEAKER_ID:
            return "Narration"
        character = next((c for c in self.cast if c.id == node.speaker_id), None)
        return character.name if character is not None else "You"

    def _tavern_messages(self, turn, history: Sequence[Node]) -> list[str]:  # noqa: ANN001
        """The chat as SillyTavern scans it: newest first, "name: text" when
        its settings include names."""
        names = self.story.defaults.tavern_world_info.include_names
        said = [node for node in history if node.kind in ("user", "assistant")]
        speaker = Node(kind="user", speaker_id=turn.speaker_id, content=turn.user_text)
        messages = []
        for node in [speaker, *reversed(said)]:
            text = node.content or ""
            messages.append(f"{self._tavern_name(node)}: {text}" if names else text)
        return messages

    def tavern_select(
        self,
        entries: Sequence[LoreEntry],
        turn,  # noqa: ANN001 - TurnRequest
        history: Sequence[Node],
        *,
        trigger: Sequence[str] = NEW_TURN,
        standing: Sequence[LoreEntry] = (),
    ) -> RetrievalReport:
        """This turn's lore, fired as SillyTavern fires it. `standing`: the
        entries in the cached block, fired with the rest and sent there."""
        place = {entry.id: at for at, entry in enumerate(self.bundle.lore)}
        together = sorted([*standing, *entries], key=lambda e: place.get(e.id, len(place)))
        fire_entries = self._fire_entries(together)
        known = {f.id: f for f in fire_entries}
        records = [
            (node.meta.tavern_length, node.meta.tavern_fired or [])
            for node in history
            if node.kind == "user" and node.meta.tavern_length is not None
        ]
        length = sum(1 for node in history if node.kind in ("user", "assistant")) + 1
        scene = self.story.scene
        present = [c.name for c in self.cast if c.id in scene.present_character_ids]
        result = fire(
            fire_entries,
            self._tavern_messages(turn, history),
            settings=self.tavern_settings(),
            # A private scene's model has a budget of its own: by the story's,
            # a quarter of 60k went into a 16k prompt.
            context=(
                self.config.private_budget
                if self.in_private
                else self.story.defaults.context_token_budget
            ),
            count_tokens=self._count_lore_tokens,
            timed=replay(records, known),
            length=length,
            trigger=tuple(trigger),
            present=[*present, *scene.present_others],
            # Sent every turn, wherever the budget stands: outside it.
            free={entry.id for entry in standing},
        )
        by_id = {entry.id: entry for entry in entries}
        injected = tuple(
            Injection(
                entry=by_id[fired.id],
                reason="tavern",
                tokens=self._count_lore_tokens(render_entry(by_id[fired.id])),
                why=result.why.get(fired.id, ""),
            )
            for fired in result.fired
            if fired.id in by_id
        )
        not_fired = tuple((by_id[i], why) for i, why in result.not_fired.items() if i in by_id)
        return RetrievalReport(
            injected=injected,
            tokens=sum(i.tokens for i in injected),
            selector="sillytavern",
            not_fired=not_fired,
            tavern_budget=result.budget,
            query_text=turn.user_text,
            # What fired and the chat's length, for the turn's record (not
            # the standing entries: nothing of theirs is timed).
            tavern_record=(length, tuple(i for i in result.ids if i in by_id)),
        )

    def settle_tavern(self, came: bool) -> None:
        """A retake's firing, once its take has text (`came`) or hasn't."""
        pending, self._tavern_pending = self._tavern_pending, None
        if pending is not None and came:
            self.record_tavern(*pending)

    def record_tavern(self, turn, report: RetrievalReport, added: bool = False) -> None:  # noqa: ANN001
        """What fired for a turn sent, on its author's node. `added`: for
        more of its passage (Continue), which keeps what fired for the
        passage so far, at the turn's own length: replaced by Continue's own
        scan, an entry that fired for the passage lost its sticky, and one
        cooling down fired again the next turn."""
        if report.selector != "sillytavern" or report.tavern_record is None:
            return
        node = next((n for n in self.nodes if n.id == turn.turn_node_id), None)
        if node is None:
            return
        length, fired = report.tavern_record
        if added and node.meta.tavern_length is not None:
            before = node.meta.tavern_fired or []
            node.meta.tavern_fired = [*before, *(i for i in fired if i not in before)]
            return
        node.meta.tavern_length = length
        node.meta.tavern_fired = list(fired)
