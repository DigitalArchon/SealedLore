"""The story's branches, for the branch bar and the story map (§10).

Two words, the author's rule (Sept 2026):

- A **take** is another version of one passage (‹ 2 / 3 ›). It swaps in place
  (`tree.takes_of`, `StorySession.switch_take`) and is never a branch.
- A **branch** is a line of the story the author split off by rewriting an
  earlier message (⋯ → Rewrite from here…). Its first message carries its
  name (`NodeMeta.branch_name`); the first line is `Story.main_branch_name`.

Before this, every fork in the tree was shown as a branch, and nearly all of
them were takes nobody continued (12 of 12 in the long test story): even the author
couldn't keep track. A fork made before the rule, where more than one child
went on and none is named, is read as a branch named "Branch from message N"
(the first child stays on its parent's line). Nothing is written for it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sealedlore.models.node import Node
from sealedlore.tree import index_nodes, roots


@dataclass(frozen=True)
class Branch:
    # The branch's first message; None for the first line ("Main").
    start_id: str | None
    name: str
    # The message number its first message has (1 for the first line).
    start_at: int
    end_id: str
    # Messages from the start of the story to the branch's end.
    length: int
    is_current: bool
    # The branch it split from (None: the first line, or this is it).
    parent_start_id: str | None
    # How many branches deep: 0 for the first line.
    depth: int

    @property
    def is_main(self) -> bool:
        return self.start_id is None


class _Tree:
    """The node tree with the branch starts worked out, for one pass."""

    def __init__(
        self, nodes: Sequence[Node], excluded: Collection[str], active: Collection[str] = ()
    ) -> None:
        self.index = {i: n for i, n in index_nodes(nodes).items() if i not in excluded}
        self.active = set(active)
        self._depth: dict[str, int] = {}
        self.starts: dict[str, str] = {}
        self.main_root: Node | None = None
        self._find_starts(nodes)

    def kids(self, node: Node) -> list[Node]:
        return [self.index[c] for c in node.children if c in self.index]

    def depth(self, node: Node) -> int:
        """Its message number: how many messages from the start to it."""
        if node.id not in self._depth:
            chain: list[Node] = []
            current: Node | None = node
            while current is not None and current.id not in self._depth:
                chain.append(current)
                current = self.index.get(current.parent_id) if current.parent_id else None
            base = self._depth[current.id] if current is not None else 0
            for offset, item in enumerate(reversed(chain), start=1):
                self._depth[item.id] = base + offset
        return self._depth[node.id]

    def _lines(self, parent: Node | None, children: list[Node]) -> list[Node]:
        """The children that begin or carry on a line: not a take that ended."""
        return [
            child
            for child in children
            if child.meta.branch_name
            or not (
                parent is not None
                and parent.kind == "user"
                and child.kind == "assistant"
                and not self.kids(child)
            )
        ]

    def _find_starts(self, nodes: Sequence[Node]) -> None:
        top = [node for node in roots(nodes) if node.id in self.index]
        groups: list[tuple[Node | None, list[Node]]] = [(None, top)]
        groups += [(node, self.kids(node)) for node in self.index.values()]
        for parent, children in groups:
            lines = self._lines(parent, children)
            unnamed = [child for child in lines if not child.meta.branch_name]
            # A branch's first passage can have takes (Regenerate on it): they
            # share its name, and the one read, or carrying it on, stands for it.
            named: dict[str, list[Node]] = {}
            for child in children:
                if child.meta.branch_name:
                    named.setdefault(child.meta.branch_name, []).append(child)
            for name, takes in named.items():
                chosen = next(
                    (take for take in takes if take.id in self.active),
                    next((take for take in takes if self.kids(take)), takes[-1]),
                )
                self.starts[chosen.id] = name
            # The first unnamed one carries its parent's line on; any other
            # is a fork from before branches had names.
            for child in unnamed[1:]:
                self.starts[child.id] = f"Branch from message {self.depth(child)}"
            if parent is None:
                self.main_root = unnamed[0] if unnamed else (top[0] if top else None)

    def end(self, start: Node, active: Collection[str]) -> Node:
        """Where a branch ends: along its own line, through the take being
        read, else the one carrying it on, else the newest."""
        node = start
        seen = {node.id}
        while True:
            # Never into another branch, nor one of its first passage's takes.
            kids = [kid for kid in self.kids(node) if not kid.meta.branch_name]
            kids = [kid for kid in kids if kid.id not in self.starts]
            if not kids:
                return node
            node = next(
                (kid for kid in kids if kid.id in active),
                next((kid for kid in kids if self.kids(kid)), kids[-1]),
            )
            if node.id in seen:
                return node
            seen.add(node.id)

    def branch_of(self, node: Node) -> str | None:
        """The start of the branch a message is on (None: the first line)."""
        current: Node | None = node
        while current is not None:
            if current.id in self.starts:
                return current.id
            current = self.index.get(current.parent_id) if current.parent_id else None
        return None


def branch_starts(
    nodes: Sequence[Node], excluded: Collection[str] = (), active: Collection[str] = ()
) -> dict[str, str]:
    """Each branch's first message, and its name."""
    return dict(_Tree(nodes, excluded, active).starts)


def branches(
    nodes: Sequence[Node],
    active_path_ids: Sequence[str],
    *,
    main_name: str = "Main",
    excluded: Collection[str] = (),
) -> list[Branch]:
    """Every branch, the first line first and each followed by its own
    branches (in the order they split off): the story map's lanes, in order.

    `excluded` are messages that belong to no branch: a discarded private
    scene's, kept on disk as a side line no model is sent.
    """
    tree = _Tree(nodes, excluded, active_path_ids)
    if tree.main_root is None:
        return []
    active = set(active_path_ids)
    leaf = tree.index.get(active_path_ids[-1]) if active_path_ids else None
    current = tree.branch_of(leaf) if leaf is not None else None

    def make(start: Node, start_id: str | None, name: str, parent: str | None, depth: int):
        end = tree.end(start, active)
        return Branch(
            start_id=start_id,
            name=name,
            start_at=tree.depth(start),
            end_id=end.id,
            length=tree.depth(end),
            is_current=(start_id == current) if leaf is not None else start_id is None,
            parent_start_id=parent,
            depth=depth,
        )

    children: dict[str | None, list[str]] = {}
    for start_id in tree.starts:
        parent = tree.index[start_id].parent_id
        owner = tree.branch_of(tree.index[parent]) if parent in tree.index else None
        children.setdefault(owner, []).append(start_id)

    found: list[Branch] = []

    def visit(start_id: str | None, parent: str | None, depth: int) -> None:
        if start_id is None:
            found.append(make(tree.main_root, None, main_name, None, 0))
        else:
            found.append(make(tree.index[start_id], start_id, tree.starts[start_id], parent, depth))
        for child in sorted(children.get(start_id, []), key=lambda i: tree.depth(tree.index[i])):
            visit(child, start_id, depth + 1)

    visit(None, None, 0)
    return found


def next_branch_name(existing: Sequence[Branch]) -> str:
    """ "Branch 2", "Branch 3"…: the first free one (the first line is 1)."""
    taken = {branch.name for branch in existing}
    number = len(existing) + 1
    while f"Branch {number}" in taken:
        number += 1
    return f"Branch {number}"


def unique_branch_name(name: str, existing: Sequence[Branch], *, keep: str | None = "") -> str:
    """`name`, or "name (2)", "name (3)"… when a branch already has it.

    Two messages side by side with one name are read as takes of one branch
    start (`_Tree._find_starts`), so a second "Ending" beside the first would
    vanish into it. `keep`: the start id of a branch being renamed, whose own
    name doesn't count.
    """
    taken = {b.name.casefold() for b in existing if keep == "" or b.start_id != keep}
    if name.casefold() not in taken:
        return name
    number = 2
    while f"{name} ({number})".casefold() in taken:
        number += 1
    return f"{name} ({number})"


@dataclass(frozen=True)
class MapLane:
    """One branch on the story map: a lane of its own messages, joined to the
    lane it split from."""

    branch: Branch
    # Its column across the map (the first line is 0).
    column: int
    # The column of the lane it leaves from (None: the first line).
    parent_column: int | None
    # Every message on its line, from the start of the story to its end.
    node_ids: tuple[str, ...]
    # Message ranges (first, last; 1-based) summarised into chapters on its line.
    chapters: tuple[tuple[int, int], ...] = ()

    @property
    def first(self) -> int:
        """The first message the lane draws: its own, not what it shares."""
        return self.branch.start_at

    def node_at(self, message: int) -> str | None:
        return self.node_ids[message - 1] if 1 <= message <= len(self.node_ids) else None


def map_columns(listed: Sequence[Branch]) -> list[tuple[int, int | None]]:
    """Each branch's column on the map and its parent's, in `listed` order.

    The first line keeps column 0. The others, in the order they begin, take
    the first column free by then, one whose lane ended before this one
    leaves its parent (as a git graph does): given one column each, a branch
    late in the story crossed the empty columns of branches that had ended
    long before.
    """
    column: dict[int, int] = {}
    # The last message of the lane in each column so far; 0 is the first line's.
    busy_until: list[int] = [0]
    for index in sorted(range(len(listed)), key=lambda i: (listed[i].start_at, i)):
        branch = listed[index]
        if branch.is_main:
            column[index] = 0
            busy_until[0] = branch.length
            continue
        free = next(
            (c for c in range(1, len(busy_until)) if busy_until[c] < branch.start_at - 1),
            None,
        )
        if free is None:
            busy_until.append(branch.length)
            free = len(busy_until) - 1
        else:
            busy_until[free] = branch.length
        column[index] = free
    by_start = {branch.start_id: column[i] for i, branch in enumerate(listed)}
    return [
        (column[i], None if branch.is_main else by_start.get(branch.parent_start_id, 0))
        for i, branch in enumerate(listed)
    ]
