"""A visual story's predesigned sheet beside its plot file (models/visual.py
`VisualDesign`): `<plot>.visual.json` next to `<plot>.md`, read when the
story is made and copied onto it. No Qt."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from sealedlore.models.character import Character
from sealedlore.models.visual import VisualDesign
from sealedlore.storage.atomic import read_json

SUFFIX = ".visual.json"


def design_path(plot_path: Path) -> Path:
    return plot_path.with_suffix("").with_name(plot_path.with_suffix("").name + SUFFIX)


def load_design(plot_path: Path) -> VisualDesign | None:
    """The sheet beside the plot file, or None when there is none or it
    can't be read (the game then has no locks, no dead and no flavour)."""
    try:
        data = read_json(design_path(plot_path), None)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return VisualDesign.model_validate(data)
    except ValidationError:
        return None


def cards_for_design(design: VisualDesign, names: list[str]) -> list[Character]:
    """Supporting cards for the design's people the story has no card for
    (its townspeople: the plot file carries only the cast), each from the
    sheet's `card` line, never playable, so the game can stand and talk to
    them (`has_card`). `names`: the cards the story has."""
    have = {n.strip().casefold() for n in names}
    out = []
    for name, sheet in design.people.items():
        if name.strip().casefold() in have or not sheet.card.strip():
            continue
        out.append(
            Character(
                name=name.strip(),
                summary=sheet.card.strip(),
                is_player_available=False,
                origin="author",
            )
        )
    return out
