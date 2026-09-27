"""The scene at any point in the story, and how it changed. Pure: no I/O.

A scene is a snapshot on the node it describes (`NodeMeta.scene`), and the
scene at any point is the nearest snapshot at or above it on the path. So a
branch switch, a retake or a deletion lands on the right roster with nothing
to keep in sync: the roster used to be one story-level record, and after a
take-back or a branch switch it described a scene the path no longer held.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sealedlore.engine.validators import mentions
from sealedlore.models.character import Character
from sealedlore.models.node import Node
from sealedlore.models.scene import SceneChange, SceneLogEntry, SceneState

# How many ended scenes the prompt carries: enough to say who was in the room
# for the last few things that mattered, few enough to stay a short list.
SCENE_LOG_SHOWN = 6
# Kept on the story; older ones have long since reached the chapter summaries.
SCENE_LOG_KEPT = 50


def snapshot_index(path: Sequence[Node]) -> int | None:
    """Index of the nearest node on `path` carrying a scene, from the end."""
    for index in range(len(path) - 1, -1, -1):
        if path[index].meta.scene is not None:
            return index
    return None


def scene_at(
    path: Sequence[Node],
    fallback: SceneState,
    *,
    cast_ids: Collection[str] | None = None,
) -> SceneState:
    """A copy of the scene as it stands at the end of `path`.

    `fallback` is used when nothing on the path carries a scene: an unstarted
    story, or one written before snapshots. Ids outside `cast_ids` are dropped,
    so a character demoted since reads as nobody rather than as a raw id.
    """
    index = snapshot_index(path)
    source = path[index].meta.scene if index is not None else fallback
    assert source is not None
    scene = source.model_copy(deep=True)
    if cast_ids is not None:
        scene.present_character_ids = [i for i in scene.present_character_ids if i in cast_ids]
        scene.offstage_but_nearby = [
            entry for entry in scene.offstage_but_nearby if entry.character_id in cast_ids
        ]
    return scene


def same_person(name: str, character: Character) -> bool:
    """Whether a written name refers to this character ("Hollis" and "Constable Hollis")."""
    if not name.strip():
        return False
    return mentions(name, character) or mentions(
        character.name, Character(id="", name=name.strip())
    )


def roster_names(scene: SceneState, cast_by_id: dict[str, Character]) -> list[str]:
    """Everyone in the scene by name: the cast first, then everyone else."""
    names = [
        cast_by_id[character_id].name
        for character_id in scene.present_character_ids
        if character_id in cast_by_id
    ]
    return names + list(scene.present_others)


def diff(
    before: SceneState,
    after: SceneState,
    cast_by_id: dict[str, Character],
    *,
    how: dict[str, str] | None = None,
) -> list[SceneChange]:
    """What changed between two scenes, for the panel and the notice.

    `how` maps a lower-cased name to how that person came or went, as the
    scene read put it.
    """
    how = how or {}
    changes: list[SceneChange] = []

    def note(name: str) -> str | None:
        return how.get(name.lower())

    for character_id in after.present_character_ids:
        if character_id not in before.present_character_ids:
            name = cast_by_id[character_id].name if character_id in cast_by_id else character_id
            changes.append(
                SceneChange(kind="arrived", name=name, character_id=character_id, detail=note(name))
            )
    before_others = {name.lower() for name in before.present_others}
    for name in after.present_others:
        if name.lower() not in before_others:
            changes.append(SceneChange(kind="arrived", name=name, detail=note(name)))

    for character_id in before.present_character_ids:
        if character_id not in after.present_character_ids:
            name = cast_by_id[character_id].name if character_id in cast_by_id else character_id
            changes.append(
                SceneChange(kind="left", name=name, character_id=character_id, detail=note(name))
            )
    after_others = {name.lower() for name in after.present_others}
    for name in before.present_others:
        if name.lower() not in after_others:
            changes.append(SceneChange(kind="left", name=name, detail=note(name)))

    if after.location != before.location and after.location:
        changes.append(SceneChange(kind="location", detail=after.location))
    if after.time_of_day != before.time_of_day and after.time_of_day:
        changes.append(SceneChange(kind="time", detail=after.time_of_day))
    if after.privacy != before.privacy and after.privacy:
        changes.append(SceneChange(kind="privacy", detail=after.privacy))
    if after.situation != before.situation and after.situation:
        changes.append(SceneChange(kind="situation", detail=after.situation))
    return changes


def describe_changes(changes: Sequence[SceneChange]) -> str:
    """One line: "+Hollis (came in from the security office), −Ruiz, now in the infirmary"."""
    parts: list[str] = []
    for change in changes:
        if change.kind in ("arrived", "left"):
            sign = "+" if change.kind == "arrived" else "−"
            parts.append(f"{sign}{change.name}" + (f" ({change.detail})" if change.detail else ""))
        elif change.kind == "location":
            parts.append(f"now: {change.detail}")
        elif change.kind == "time":
            parts.append(f"time: {change.detail}")
        elif change.kind == "privacy":
            parts.append(f"{change.detail} scene")
        elif change.kind == "situation":
            parts.append("situation updated")
    return ", ".join(parts)


def log_on_path(
    entries: Sequence[SceneLogEntry], path: Sequence[Node], *, limit: int = SCENE_LOG_SHOWN
) -> list[SceneLogEntry]:
    """The ended scenes this path passed through, oldest first, the last `limit`."""
    on_path = {node.id for node in path}
    kept = [entry for entry in entries if entry.closed_at_node_id in on_path]
    return kept[-limit:] if limit else kept


def closed_entry(
    scene: SceneState,
    cast_by_id: dict[str, Character],
    *,
    gist: str | None,
    node_id: str,
) -> SceneLogEntry:
    """The log entry for a scene that just ended at `node_id`."""
    return SceneLogEntry(
        location=scene.location,
        present_names=roster_names(scene, cast_by_id),
        privacy=scene.privacy,
        gist=(gist or scene.situation or "").strip(),
        closed_at_node_id=node_id,
    )


def move_into_cast(scene: SceneState, character: Character) -> None:
    """A supporting character promoted to the cast keeps their place in the scene."""
    matching = [name for name in scene.present_others if same_person(name, character)]
    if not matching:
        return
    scene.present_others = [name for name in scene.present_others if name not in matching]
    if character.id not in scene.present_character_ids:
        scene.present_character_ids.append(character.id)


def rename_in_scene(scene: SceneState, character: Character, new_name: str) -> bool:
    """A supporting character renamed keeps their place on the roster.

    `present_others` holds names, not ids, so a rename would otherwise leave
    the old name in every snapshot: someone in the room the card no longer
    answers to. Cast members are on the roster by id and need nothing.
    """
    changed = False
    renamed: list[str] = []
    for name in scene.present_others:
        if same_person(name, character) and name != new_name:
            if new_name not in renamed:
                renamed.append(new_name)
            changed = True
        elif name not in renamed:
            renamed.append(name)
    if changed:
        scene.present_others = renamed
    return changed


def move_out_of_cast(scene: SceneState, character: Character) -> None:
    """A cast member demoted to supporting stays in the scene, by name."""
    was_present = character.id in scene.present_character_ids
    scene.present_character_ids = [i for i in scene.present_character_ids if i != character.id]
    scene.offstage_but_nearby = [
        entry for entry in scene.offstage_but_nearby if entry.character_id != character.id
    ]
    if was_present and not any(same_person(name, character) for name in scene.present_others):
        scene.present_others.append(character.name)
