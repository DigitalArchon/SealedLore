"""The session's side of lore fired as SillyTavern fires it
(engine/tavern_firing.py): a story's own choice (`StoryDefaults.lore_firing`,
the Lore page), its settings SillyTavern's (`tavern_world_info`).

Its entries are the lore the models may see (never one the plot keeps
hidden, nor one held back until mentioned). A constant entry nothing can
stop stands in the cached system block; the rest are fired each turn and
sent with it, in SillyTavern's order, in place of SealedLore's own choice
(the room `fit_lore` gives doesn't apply: SillyTavern's budget does).

Each author turn records what fired and the chat's length then
(`NodeMeta.tavern_fired`, `tavern_length`), fired or not, and the timed
effects are replayed along the path from those records: every branch, take
and turn taken back has its own. Regenerate fires afresh (a SillyTavern
swipe scans again, and rolls again)."""

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
    @property
    def fires_like_tavern(self) -> bool:
        """The story's lore fires as SillyTavern's does (never a chat's)."""
        return self.story.defaults.lore_firing == "sillytavern" and not self.story.chat

    def tavern_settings(self) -> TavernSettings:
        return TavernSettings(**self.story.defaults.tavern_world_info.model_dump())

    def _fire_entries(self, entries: Sequence[LoreEntry]) -> list[FireEntry]:
        return [
            fire_entry(
                id=entry.id,
                title=entry.title,
                content=entry.content,
                keywords=entry.keywords,
                always_on=entry.always_on,
                enabled=entry.enabled,
                raw=entry.tavern.fields if entry.tavern is not None else None,
                numbered=entry.tavern.numbered if entry.tavern is not None else False,
                decorators=entry.tavern.decorators if entry.tavern is not None else (),
            )
            for entry in entries
        ]

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
    ) -> RetrievalReport:
        """This turn's lore, fired as SillyTavern fires it."""
        fire_entries = self._fire_entries(entries)
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
            context=self.story.defaults.context_token_budget,
            count_tokens=self._count_lore_tokens,
            timed=replay(records, known),
            length=length,
            trigger=tuple(trigger),
            present=[*present, *scene.present_others],
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
        )
        not_fired = tuple((by_id[i], why) for i, why in result.not_fired.items() if i in by_id)
        return RetrievalReport(
            injected=injected,
            tokens=sum(i.tokens for i in injected),
            selector="sillytavern",
            not_fired=not_fired,
            tavern_budget=result.budget,
            query_text=turn.user_text,
            # What fired and the chat's length, for the turn's record.
            tavern_record=(length, tuple(result.ids)),
        )

    def record_tavern(self, turn, report: RetrievalReport) -> None:  # noqa: ANN001
        """What fired for a turn sent, on its author's node."""
        if report.selector != "sillytavern" or report.tavern_record is None:
            return
        node = next((n for n in self.nodes if n.id == turn.turn_node_id), None)
        if node is None:
            return
        length, fired = report.tavern_record
        node.meta.tavern_length = length
        node.meta.tavern_fired = list(fired)
