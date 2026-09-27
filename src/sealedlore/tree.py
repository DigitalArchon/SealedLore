"""Navigation over the message tree.

Every node has one parent and any number of children; regenerating creates a
sibling rather than replacing. The active path is the walk from the active
leaf back to the root, reversed — and that path alone is what gets sent to the
model.

All functions take a flat sequence of nodes (as stored in nodes.json) and are
pure. Node lists are small at this scale, so rebuilding the index per call is
cheaper than keeping a second structure in sync.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sealedlore.models.node import Node


def index_nodes(nodes: Sequence[Node]) -> dict[str, Node]:
    return {node.id: node for node in nodes}


def path_to(nodes: Sequence[Node], leaf_id: str) -> list[Node]:
    """Root-to-leaf path ending at `leaf_id`."""
    index = index_nodes(nodes)
    if leaf_id not in index:
        raise KeyError(f"unknown node id: {leaf_id!r}")

    reversed_path: list[Node] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError(f"cycle in node tree at {current!r}")
        seen.add(current)
        node = index.get(current)
        if node is None:
            raise ValueError(f"node {current!r} has a missing parent reference")
        reversed_path.append(node)
        current = node.parent_id

    reversed_path.reverse()
    return reversed_path


def active_path(nodes: Sequence[Node], active_leaf_id: str | None) -> list[Node]:
    if active_leaf_id is None:
        return []
    return path_to(nodes, active_leaf_id)


def roots(nodes: Sequence[Node]) -> list[Node]:
    return [node for node in nodes if node.parent_id is None]


def children_of(nodes: Sequence[Node], node_id: str) -> list[Node]:
    index = index_nodes(nodes)
    parent = index.get(node_id)
    if parent is None:
        raise KeyError(f"unknown node id: {node_id!r}")
    return [index[child_id] for child_id in parent.children if child_id in index]


def siblings_of(nodes: Sequence[Node], node_id: str) -> list[Node]:
    """All nodes sharing a parent with `node_id`, including it, in order."""
    index = index_nodes(nodes)
    node = index.get(node_id)
    if node is None:
        raise KeyError(f"unknown node id: {node_id!r}")
    if node.parent_id is None:
        return roots(nodes)
    return children_of(nodes, node.parent_id)


def sibling_position(nodes: Sequence[Node], node_id: str) -> tuple[int, int]:
    """1-based position and sibling count, for the variant cycler (`‹ 2 / 4 ›`)."""
    group = siblings_of(nodes, node_id)
    ids = [node.id for node in group]
    return ids.index(node_id) + 1, len(ids)


def leaves(nodes: Sequence[Node]) -> list[Node]:
    return [node for node in nodes if not node.children]


def subtree_ids(nodes: Sequence[Node], node_id: str) -> list[str]:
    """`node_id` and every descendant, depth-first."""
    index = index_nodes(nodes)
    if node_id not in index:
        raise KeyError(f"unknown node id: {node_id!r}")

    collected: list[str] = []
    stack = [node_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        collected.append(current)
        node = index.get(current)
        if node is not None:
            stack.extend(reversed(node.children))
    return collected


def link_child(parent: Node, child: Node) -> None:
    """Attach `child` to `parent`, keeping both sides of the link consistent."""
    child.parent_id = parent.id
    if child.id not in parent.children:
        parent.children.append(child.id)


def takes_of(
    nodes: Sequence[Node],
    node_id: str,
    active_ids: set[str],
    index: Mapping[str, Node] | None = None,
) -> list[Node]:
    """The takes of a passage, in order: the passages answering the same
    author turn. A take swaps in place, so at most one of them carries the
    story on. A sibling carrying a story of its own that isn't the one being
    read (a fork made before takes swapped in place) is a line of its own,
    not a take; so is anything that isn't a passage. `index` is
    `index_nodes(nodes)`, for callers asking about many passages at once."""
    index = index if index is not None else index_nodes(nodes)
    node = index.get(node_id)
    if node is None:
        raise KeyError(f"unknown node id: {node_id!r}")
    parent = index.get(node.parent_id) if node.parent_id else None
    if node.kind != "assistant" or parent is None or parent.kind != "user":
        return [node]
    # A branch's own first passage has takes of its own: the same name.
    group = [
        index[c]
        for c in parent.children
        if c in index
        and index[c].kind == "assistant"
        and index[c].meta.branch_name == node.meta.branch_name
    ]
    return [
        take
        for take in group
        if take.id == node_id
        or take.id in active_ids
        or not any(child in index for child in take.children)
    ]


def move_continuation(nodes: Sequence[Node], from_node: Node, to_node: Node) -> None:
    """What followed `from_node` follows `to_node` instead (a take swapped in)."""
    index = index_nodes(nodes)
    for child_id in from_node.children:
        child = index.get(child_id)
        if child is not None:
            child.parent_id = to_node.id
        if child_id not in to_node.children:
            to_node.children.append(child_id)
    from_node.children = []


def latest_leaf(nodes: Sequence[Node], node_id: str) -> Node:
    """Walk down from `node_id` through the newest child each time, to a leaf.

    Switching to another take lands at the end of that take's own story. The
    newest child is the best guess at "where I left off" without keeping a
    second structure of per-branch bookmarks in sync.
    """
    index = index_nodes(nodes)
    if node_id not in index:
        raise KeyError(f"unknown node id: {node_id!r}")
    node = index[node_id]
    seen = {node.id}
    while node.children:
        child = next((index[c] for c in reversed(node.children) if c in index), None)
        if child is None or child.id in seen:
            break
        seen.add(child.id)
        node = child
    return node
