"""The scene card as the map has it, for the storyteller in a story played on
the map: pure. The author, after the Sonnet 4.6 playtest (Oct 17 2026): the
prompt was built for narrative stories before the map, and a map story's
tail had three location authorities and two presence records that never met
(SealedLoreDocs design/map-prompt.md): the card's Location and roster from
the scene read, which never sees the map, and the positions from the map
read, which never sees the card. Live, the roster said "NOT IN THE SCENE
YET: Evan Stevens" while the positions put an unnamed young man with a rifle
in sight, and asked his name Sonnet made one up.

So, in a map story, the card the storyteller is sent follows the map where
the map knows: its Location gives way to the map's chain of places, anyone
the map stands in the author's character's area is listed as here (someone
not named yet by the passage's description of them), and anyone it stands
in another area is listed as elsewhere, with where. Who the map doesn't
place is as the card says. The story's own card is never changed: this is
what the prompt shows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sealedlore.engine.scene_state import same_person
from sealedlore.models.character import Character
from sealedlore.models.scene import OffstageCharacter, SceneState

# How the card notes someone the map has elsewhere.
MAP_HAS_THEM = "the map has them at "


def reconcile_scene(
    scene: SceneState,
    *,
    here: Sequence[str],
    away: Mapping[str, str],
    cast: Sequence[Character],
    unknowns: Mapping[str, str],
    held_id: str | None,
) -> SceneState:
    """`scene` with the map's word on who is where: `here` are the names the
    map stands in the author's character's area (labels of people not named
    yet among them, `unknowns` giving their descriptions), `away` the names
    it stands in other areas with the place they are at; `cast` is the main
    cast, listed by id (anyone else by name). The card's Location is left
    out (the map's chain is where the scene is)."""
    out = scene.model_copy(deep=True)
    out.location = None

    def card_of(name: str) -> Character | None:
        return next((c for c in cast if same_person(name, c)), None)

    present = list(out.present_character_ids)
    notes = {e.character_id: e.note for e in out.offstage_but_nearby}
    for name in here:
        card = card_of(name)
        if card is not None and card.id not in present:
            present.append(card.id)
            notes.pop(card.id, None)
    for name, where in away.items():
        card = card_of(name)
        if card is None or card.id == held_id:
            continue
        if card.id in present:
            present.remove(card.id)
        notes[card.id] = MAP_HAS_THEM + where
    out.present_character_ids = present
    out.offstage_but_nearby = [
        OffstageCharacter(character_id=cid, note=note)
        for cid, note in notes.items()
        if cid not in present
    ]

    others = [
        name
        for name in out.present_others
        if not any(same_person(name, Character(id="", name=gone)) for gone in away)
    ]
    for name in here:
        if card_of(name) is not None:
            continue  # the cast is listed by id, above
        shown = f"{unknowns[name]} (not named yet)" if name in unknowns else name
        if not any(shown == o or same_person(shown, Character(id="", name=o)) for o in others):
            others.append(shown)
    out.present_others = others
    return out
