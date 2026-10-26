"""A world kit (the author, Oct 26 2026: "use the DDA method of designing
insides of places"): what a setting's places are made of, written once and
put together by the program, as Cataclysm: Dark Days Ahead builds its world
from hand-written pieces. The method, never its data (that game is under a
share-alike licence; every kit here is our own writing).

- Item tables (`ItemTable`): what a cupboard, a fridge or a dresser holds,
  by chance, rolled once when its area is first entered and kept.
- Templates (`Template`): a building's inside as rows of tiles with a
  legend for the furniture in it, several variants of each kind of
  building, picked by weight, turned and mirrored to put the door where the
  map has it.
- Chunks (`Chunk`): small stamped pieces (a bedroom, a pantry corner)
  placed into a template by weight, so one template makes many insides.
- Yard pieces (`YardPiece`): things stamped on the ground round a building
  out of doors (a woodpile, a wreck, a dog kennel).
- Containers (`ContainerRule`): which table stocks a thing a template did
  not place (a hand-drawn plan's dresser), by the words of its name.

A kit lives beside its tile set (`SampleStories/tilesets/<set>/kit.json`,
storage/kits.py), since its things are drawn in that set. Pydantic only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Side = Literal["north", "east", "south", "west"]


class ItemEntry(BaseModel):
    """One line of an item table: an item, another table, or (with neither,
    in a distribution) nothing at all."""

    item: str = ""
    table: str = ""
    # A collection: the chance in a hundred that this entry comes. A
    # distribution: its weight against the others.
    chance: int = 100
    count: int | tuple[int, int] = 1
    kind: str = ""
    note: str = ""


class ItemTable(BaseModel):
    """A collection rolls each entry on its own chance; a distribution picks
    exactly one entry by weight (CDDA's item groups)."""

    type: Literal["collection", "distribution"] = "collection"
    entries: list[ItemEntry] = Field(default_factory=list)


class LegendEntry(BaseModel):
    """What a template's character stands for: a thing (a piece of
    furniture, a block of the character one piece as big as the block), or
    a floor tile with a roll of `loot` lying on it."""

    thing: str = ""
    # The table that stocks it, and the chance in a hundred that it holds
    # anything at all.
    stock: str = ""
    chance: int = 100
    loot: str = ""
    # The tile under it: floor indoors ("_"), ground out of doors (".").
    floor: str = "_"

    @model_validator(mode="after")
    def _one_of(self) -> LegendEntry:
        if not self.thing and not self.loot:
            raise ValueError("a legend entry names a thing or a loot table")
        return self


class Nest(BaseModel):
    """A chunk stamped into rows at (x, y), its top-left corner, one of
    `chunks` by weight (a chunk "" stamps nothing)."""

    x: int
    y: int
    chunks: list[tuple[str, int]]


class Chunk(BaseModel):
    """A small stamped piece: rows over the rows it lands on (a space keeps
    what is under it), its own legend, its own chunks."""

    rows: list[str]
    legend: dict[str, LegendEntry] = Field(default_factory=dict)
    labels: dict[str, tuple[int, int]] = Field(default_factory=dict)
    nested: list[Nest] = Field(default_factory=list)


class TemplateLevel(BaseModel):
    """A floor above (1, 2…) or below (-1…) the template's own, on its grid,
    its stairs ("&") over the floor's below."""

    index: int
    name: str = ""
    rows: list[str]
    legend: dict[str, LegendEntry] = Field(default_factory=dict)
    labels: dict[str, tuple[int, int]] = Field(default_factory=dict)
    nested: list[Nest] = Field(default_factory=list)


class Template(BaseModel):
    """One variant of a kind of building's inside, drawn with its main door
    on `doors[0]` (the sides it has doors out on, as drawn)."""

    id: str
    weight: int = 100
    doors: list[Side] = Field(default_factory=lambda: ["south"])
    # Metres a tile, as drawn.
    tile_m: float = 1.0
    rows: list[str]
    legend: dict[str, LegendEntry] = Field(default_factory=dict)
    # Shared legends by name (`Kit.palettes`), under the template's own.
    palettes: list[str] = Field(default_factory=list)
    labels: dict[str, tuple[int, int]] = Field(default_factory=dict)
    nested: list[Nest] = Field(default_factory=list)
    levels: list[TemplateLevel] = Field(default_factory=list)


class BuildingKind(BaseModel):
    """A kind of building and its variants: a place whose name (else its
    words) holds one of `match`, as whole words, is built from one."""

    match: list[str]
    variants: list[Template]


class ContainerRule(BaseModel):
    """A thing whose name holds one of `match` (whole words) is stocked from
    `table`, with `chance` in a hundred of holding anything."""

    match: list[str]
    table: str
    chance: int = 100


class YardPiece(BaseModel):
    """A thing stamped on the ground near a building out of doors, its size
    in metres; only near buildings whose name holds one of `near` (any
    building when empty)."""

    thing: str
    w: float = 1.0
    h: float = 1.0
    weight: int = 100
    near: list[str] = Field(default_factory=list)
    stock: str = ""
    chance: int = 100


class Kit(BaseModel):
    name: str = ""
    tables: dict[str, ItemTable] = Field(default_factory=dict)
    containers: list[ContainerRule] = Field(default_factory=list)
    palettes: dict[str, dict[str, LegendEntry]] = Field(default_factory=dict)
    chunks: dict[str, Chunk] = Field(default_factory=dict)
    buildings: dict[str, BuildingKind] = Field(default_factory=dict)
    yard: list[YardPiece] = Field(default_factory=list)
    # How many yard pieces round each building out of doors, at most.
    yard_per_building: int = 3
