"""The board: the story's areas laid out by direction, not to scale
(the author, Oct 23 2026: players are always somewhere, in an area; the
map joins the areas "by directional non scale placement", each a scene of
its own, and the country between them is nobody's).

A node is an area that stands on its own: a place that is its own local
area (`local_area`), never a route (a road or a tramway joins areas, it is
not one). Each node takes a cell on a grid, one step from the node it was
first reached from in the direction the story gave the way between them
(a link's words, else the bearing between the two on the map, else the
next free cell round it). The cell grid is read into a display map by
`board_world`: every node the same width, its buildings laid inside it as
its plan draws them, the routes between nodes, so the map view draws the
board with its own machinery."""

from __future__ import annotations

import math
from collections.abc import Iterable

from sealedlore.engine.world_art import LAYOUT_KINDS, local_area
from sealedlore.engine.world_layout import bearing, position, size_of
from sealedlore.models.world import WorldMap, WorldPlace

# A cell's pitch and a node's drawn width, in the display map's metres:
# the gap between is where the ways between areas are drawn.
CELL_M = 400.0
NODE_M = 300.0
# The eight steps round a cell, by compass bearing.
STEPS: dict[float, tuple[int, int]] = {
    0: (0, -1),
    45: (1, -1),
    90: (1, 0),
    135: (1, 1),
    180: (0, 1),
    225: (-1, 1),
    270: (-1, 0),
    315: (-1, -1),
}


def is_node(world: WorldMap, place: WorldPlace) -> bool:
    """Whether a place is an area of the board's own: its own local area,
    of a kind that gets a plan, not a route."""
    if place.kind == "route" or place.kind not in LAYOUT_KINDS:
        return False
    local = local_area(world, place.id)
    return local is not None and local.id == place.id


def node_of(world: WorldMap, place_id: str | None) -> str | None:
    """The node a place belongs to: its local area when that is a node,
    else None (a route, a region, nowhere)."""
    local = local_area(world, place_id)
    if local is None or not is_node(world, local):
        return None
    return local.id


def _step(degrees: float) -> tuple[int, int]:
    """The grid step nearest a bearing."""
    nearest = min(STEPS, key=lambda d: min(abs(degrees - d), 360 - abs(degrees - d)))
    return STEPS[nearest]


def _bearing_between(world: WorldMap, a: str, b: str) -> float | None:
    at, to = position(world, a), position(world, b)
    if at is None or to is None or (at[0] == to[0] and at[1] == to[1]):
        return None
    return math.degrees(math.atan2(to[0] - at[0], to[1] - at[1])) % 360


def ways(world: WorldMap) -> dict[str, list[tuple[str, float | None, str | None]]]:
    """Each node's ways to other nodes: (node, bearing from this one or
    None, the route they go by or None). A link straight between two nodes
    is a way each; a link to a route joins the node to every other node the
    route links, in the direction the route's own way to that node names."""
    nodes = {pid for pid, place in world.places.items() if is_node(world, place)}
    out: dict[str, list[tuple[str, float | None, str | None]]] = {n: [] for n in nodes}
    by_route: dict[str, list[tuple[str, float | None]]] = {}
    for link in world.links:
        a, b = node_of(world, link.a), node_of(world, link.b)
        heading = bearing(link.direction)
        ra = world.places.get(link.a)
        rb = world.places.get(link.b)
        if a is not None and b is not None and a != b:
            out[a].append((b, heading, None))
            out[b].append((a, (heading + 180) % 360 if heading is not None else None, None))
        elif a is not None and rb is not None and rb.kind == "route":
            # From the node, the route lies this way; the route's own ways
            # (below) say where it leads.
            by_route.setdefault(rb.id, []).append((a, heading))
        elif b is not None and ra is not None and ra.kind == "route":
            # The route's way to the node: from the route, the node lies
            # `heading` away; from the node, the route lies the other way.
            by_route.setdefault(ra.id, []).append(
                (b, (heading + 180) % 360 if heading is not None else None)
            )
    for route_id, ends in by_route.items():
        # Along a route, each node is joined to each other node on it. The
        # way from a to b is the way the route leads to b (the route's own
        # way to b, turned round: from b the route lies south, so the route
        # leads north to b), else the way the route lies from a.
        for i, (a, toward_route) in enumerate(ends):
            for j, (b, toward_b) in enumerate(ends):
                if i == j or a == b:
                    continue
                heading = (toward_b + 180) % 360 if toward_b is not None else toward_route
                way = (b, heading, route_id)
                if way not in out[a]:
                    out[a].append(way)
    return out


def board_cells(world: WorldMap) -> dict[str, tuple[int, int]]:
    """Each node's cell (column east, row south) on the board. The first
    node is the story's start (else the first node by id) at (0, 0); the
    rest are placed breadth first, a step from the node they are reached
    from, in the way's direction (else the bearing between them on the
    map, else round it), pushed on along that step until the cell is free.
    Nodes no way reaches are placed by bearing from the start."""
    nodes = sorted(pid for pid, place in world.places.items() if is_node(world, place))
    if not nodes:
        return {}
    start = node_of(world, world.start) if world.start else None
    if start is None:
        start = nodes[0]
    links = ways(world)
    cells: dict[str, tuple[int, int]] = {start: (0, 0)}
    taken = {(0, 0)}
    queue = [start]

    def place_from(origin: str, target: str, heading: float | None) -> None:
        if target in cells:
            return
        if heading is None:
            heading = _bearing_between(world, origin, target)
        ox, oy = cells[origin]
        if heading is None:
            step = next((s for s in STEPS.values() if (ox + s[0], oy + s[1]) not in taken), (1, 0))
        else:
            step = _step(heading)
        x, y = ox + step[0], oy + step[1]
        while (x, y) in taken:
            x, y = x + step[0], y + step[1]
        cells[target] = (x, y)
        taken.add((x, y))
        queue.append(target)

    while queue:
        origin = queue.pop(0)
        for target, heading, _route in sorted(links.get(origin, []), key=lambda w: w[0]):
            place_from(origin, target, heading)
    for pid in nodes:  # out of every way's reach: by where the map puts it
        if pid not in cells:
            place_from(start, pid, None)
    return cells


def board_world(world: WorldMap) -> WorldMap:
    """The map as the board shows it: a copy with every node the same
    width at its cell, what is inside a node scaled into it where its plan
    draws it, and each route at the middle of the nodes it joins, so the
    ways draw as lines between cells. Nothing else moves; the story's own
    map, with its distances, is untouched."""
    cells = board_cells(world)
    shown = world.model_copy(deep=True)
    for pid, (col, row) in cells.items():
        node = shown.places[pid]
        was = position(world, pid) or (0.0, 0.0)
        scale = NODE_M / max(size_of(node), 1.0)
        node.x, node.y, node.size = col * CELL_M, -row * CELL_M, NODE_M
        for child in shown.places.values():
            if child.id == pid or node_of(world, child.id) != pid:
                continue
            spot = position(world, child.id)
            if spot is None:
                continue
            child.x = node.x + (spot[0] - was[0]) * scale
            child.y = node.y + (spot[1] - was[1]) * scale
            child.size = max(size_of(child) * scale, 1.0)
    for place in shown.places.values():
        if place.kind != "route":
            continue
        ends = [cells[n] for n in _route_nodes(world, place.id) if n in cells]
        if ends:
            place.x = sum(c for c, _r in ends) / len(ends) * CELL_M
            place.y = -sum(r for _c, r in ends) / len(ends) * CELL_M
            place.inside = None
    return shown


def _route_nodes(world: WorldMap, route_id: str) -> list[str]:
    found: list[str] = []
    for link in world.links:
        other = link.b if link.a == route_id else link.a if link.b == route_id else None
        node = node_of(world, other) if other else None
        if node is not None and node not in found:
            found.append(node)
    return found


def nodes_of(world: WorldMap, ids: Iterable[str]) -> set[str]:
    """The nodes the places belong to."""
    return {n for n in (node_of(world, pid) for pid in ids) if n is not None}
