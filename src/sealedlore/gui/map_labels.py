"""Place names on the Region and World maps, kept from lying over each
other (the author, Oct 18 2026: "make labels snap to each other in a
sensible order so they aren't overlapping"). Live, a toll bridge, its
boundary stone, a cairn and a marker post within a few metres of each other
were four names written on top of one another, with the people there over
them.

Pure: the view measures each name's block in screen pixels and gives them
in the order they should be read (down the map, and where places share a
spot the one the character is at first, then the larger), with the
pictures they mustn't cover; this says how far down each must move. Each
block keeps its own spot if it is clear, else it goes just under what it
would cover, so a cluster's names stand in a column."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# The space left between two names stacked under each other, in pixels.
STACK_GAP = 1.0
# The buckets blocks are sorted into, so a map with hundreds of places
# compares each name only with those near it.
CELL = 128.0


@dataclass(frozen=True)
class Block:
    """A name's block where it would be drawn: centred on `x`, its top at
    `top`, in screen pixels."""

    key: str
    x: float
    top: float
    width: float
    height: float


def _cells(left: float, top: float, right: float, bottom: float) -> list[tuple[int, int]]:
    return [
        (cx, cy)
        for cx in range(int(left // CELL), int(right // CELL) + 1)
        for cy in range(int(top // CELL), int(bottom // CELL) + 1)
    ]


def _covered(
    placed: dict[tuple[int, int], list[tuple[float, float, float, float, str]]],
    block: Block,
    top: float,
) -> float | None:
    """The lowest bottom of what `block` would cover with its top at `top`,
    never counting its own picture; None when it covers nothing."""
    left, right, bottom = block.x - block.width / 2, block.x + block.width / 2, top + block.height
    lowest = None
    for cell in _cells(left, top, right, bottom):
        for l2, t2, r2, b2, whose in placed.get(cell, ()):
            if l2 < right and left < r2 and t2 < bottom and top < b2 and whose != block.key:
                lowest = b2 if lowest is None else max(lowest, b2)
    return lowest


def stack_labels(blocks: Sequence[Block], pictures: Sequence[Block] = ()) -> dict[str, float]:
    """How far down each block must move so no two overlap, nor cover a
    picture other than its own (`pictures`, keyed as the blocks), by key.
    The first keeps its place; each after it keeps its own unless that
    covers something placed before it, and then takes the nearest spot
    below that is clear, snapped to the bottom of what it would cover."""
    # Each rect is (left, top, right, bottom, whose).
    placed: dict[tuple[int, int], list[tuple[float, float, float, float, str]]] = {}

    def place(block: Block, top: float) -> None:
        left = block.x - block.width / 2
        rect = (left, top, left + block.width, top + block.height, block.key)
        for cell in _cells(*rect[:4]):
            placed.setdefault(cell, []).append(rect)

    for picture in pictures:
        place(picture, picture.top)
    shifts: dict[str, float] = {}
    for block in blocks:
        top = block.top
        for _ in range(len(blocks) + len(pictures) + 1):
            lowest = _covered(placed, block, top)
            if lowest is None:
                break
            top = lowest + STACK_GAP
        shifts[block.key] = top - block.top
        place(block, top)
    return shifts
