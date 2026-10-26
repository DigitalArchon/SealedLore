"""A visual story's wider world (the author, Oct 26 2026: "a conceptual
overall map size, with a general idea of what is in each area", travel
between known places that "crosses every territory between", any part
explorable, its areas "dynamically generated ... based on the general
instructions of the overmap"). The design is SealedLoreDocs
design/visual-stories.md §19-20. Pydantic only.

The overmap is a fixed frame in kilometres (the plot file's: x east, y
north, the farm at 0, 0), cut into a grid of cells. Each region says what
its country is like, how often something happens to a traveller there (a
chance an hour of travel, split between danger, opportunity and something
of interest, with examples for the keeper), what exploring it finds (outdoor
templates by weight), how warm a traveller must be and how fast they go
across it. Roads, rivers and the rail line are lines, so neighbouring cells
join. Places stand where they belong: the plot file's own, by name, and
new ones made from a template the first time they are entered.

It is copied onto the story with the rest of the design
(`VisualDesign.overmap`), so a story is whole on its own; what play changes
(places found, cells explored, the traveller's needs) is on the live state
(`AreaState.overmap`, models/area.py).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LineKind = Literal["road", "track", "river", "creek", "rail"]
# What a region's country is, for the cells made in it and the map's look.
Terrain = Literal["fields", "woods", "hills", "marsh", "plains", "ruins", "scrub", "lake"]


class OverRegion(BaseModel):
    key: str
    name: str
    # What it is, in a sentence or two, for the keeper and the map.
    words: str = ""
    # Where it is: boxes in km (west, east, south, north). The first region
    # in the overmap's list whose box holds a point is the region there.
    boxes: list[tuple[float, float, float, float]] = Field(default_factory=list)
    terrain: Terrain = "fields"
    # The ground underfoot ("snow", "grass", "dirt"…), as a plan's.
    ground: str = "snow"
    # The dead wandering a cell when it is first made, on average.
    dead: float = 0.0
    # The chance in a hundred that something happens in an hour of travel
    # by day, and how it splits: danger, opportunity, something of interest.
    chance: float = 5.0
    split: tuple[int, int, int] = (40, 30, 30)
    danger: list[str] = Field(default_factory=list)
    opportunity: list[str] = Field(default_factory=list)
    interest: list[str] = Field(default_factory=list)
    # What exploring a cell finds: outdoor templates (`Kit.outdoor`) by weight.
    finds: list[tuple[str, int]] = Field(default_factory=list)
    # Warm things a traveller needs here (a coat, gloves, a blanket).
    warm: int = 1
    # Across country, a share of the pace on a road.
    pace: float = 0.6


class OverLine(BaseModel):
    kind: LineKind
    name: str
    # In km, in order along it.
    points: list[tuple[float, float]] = Field(default_factory=list)


class OverPlace(BaseModel):
    """A place on the overmap: one of the plot file's map (by name), or one
    made from `template` the first time it is entered."""

    name: str
    # Where it is, in km.
    x: float
    y: float
    # Known from the start (the farm, the town everyone knows of); the rest
    # appear on the overmap once found.
    known: bool = False
    # For a place the plot file's map doesn't have: how it is made.
    template: str = ""
    what: str = ""


class Overmap(BaseModel):
    # The frame, in km.
    west: float = -35.0
    east: float = 65.0
    south: float = -25.0
    north: float = 35.0
    # A cell, in km: 5:3, as an area's plan is.
    cell_w: float = 0.4
    cell_h: float = 0.24
    regions: list[OverRegion] = Field(default_factory=list)
    lines: list[OverLine] = Field(default_factory=list)
    places: list[OverPlace] = Field(default_factory=list)
    # Walking pace on a road, km an hour, rests and all.
    road_kmh: float = 3.5


class Needs(BaseModel):
    """What the traveller last had, on the clock (seconds since the story
    began), and the hurts already counted for going without."""

    ate: int = 0
    drank: int = 0
    slept: int = 0
    # Seconds travelled short of warmth since the cold last hurt.
    cold: int = 0
    # Wounds already given for hunger and thirst (one a day past the worst).
    starved: int = 0
    parched: int = 0


class OvermapState(BaseModel):
    """What play changes on the overmap."""

    # Places found (by name, as the overmap names them).
    known: list[str] = Field(default_factory=list)
    # Cells entered, by key "i,j".
    explored: list[str] = Field(default_factory=list)
    needs: Needs = Field(default_factory=Needs)
    # Set once the known places and the needs are set up from the design.
    begun: bool = False
