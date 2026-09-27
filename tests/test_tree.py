"""Tree navigation and branching."""

from __future__ import annotations

import pytest

from sealedlore.models.node import Node
from sealedlore.tree import (
    active_path,
    children_of,
    leaves,
    link_child,
    path_to,
    roots,
    sibling_position,
    siblings_of,
    subtree_ids,
)
from tests.conftest import make_exchange


def test_active_path_walks_root_to_leaf():
    nodes = make_exchange(3)
    path = active_path(nodes, "a2")
    assert [node.id for node in path] == ["u0", "a0", "u1", "a1", "u2", "a2"]


def test_active_path_is_empty_without_a_leaf():
    assert active_path(make_exchange(1), None) == []


def test_path_to_unknown_node_raises():
    with pytest.raises(KeyError):
        path_to(make_exchange(1), "nope")


def test_path_detects_a_cycle():
    first = Node(id="a", kind="user", speaker_id="s", content="one")
    second = Node(id="b", kind="assistant", speaker_id="s", content="two")
    first.parent_id, second.parent_id = "b", "a"
    with pytest.raises(ValueError, match="cycle"):
        path_to([first, second], "a")


def test_regenerating_creates_a_sibling_not_a_replacement():
    nodes = make_exchange(1)
    user = nodes[0]
    second_take = Node(id="a0b", kind="assistant", speaker_id="__narrator__", content="Another.")
    link_child(user, second_take)
    nodes.append(second_take)

    assert [node.id for node in children_of(nodes, "u0")] == ["a0", "a0b"]
    assert [node.id for node in active_path(nodes, "a0")] == ["u0", "a0"]
    assert [node.id for node in active_path(nodes, "a0b")] == ["u0", "a0b"]


def test_sibling_position_reports_the_variant_cycler():
    nodes = make_exchange(1)
    for index in range(3):
        take = Node(id=f"alt{index}", kind="assistant", speaker_id="__narrator__", content="x")
        link_child(nodes[0], take)
        nodes.append(take)

    assert sibling_position(nodes, "a0") == (1, 4)
    assert sibling_position(nodes, "alt2") == (4, 4)


def test_roots_and_siblings_of_a_root():
    nodes = make_exchange(1)
    other_root = Node(id="r2", kind="user", speaker_id="s", content="a second opening")
    nodes.append(other_root)

    assert [node.id for node in roots(nodes)] == ["u0", "r2"]
    assert [node.id for node in siblings_of(nodes, "r2")] == ["u0", "r2"]


def test_leaves_lists_every_branch_tip():
    nodes = make_exchange(2)
    fork = Node(id="fork", kind="assistant", speaker_id="__narrator__", content="a fork")
    link_child(nodes[2], fork)
    nodes.append(fork)

    assert sorted(node.id for node in leaves(nodes)) == ["a1", "fork"]


def test_subtree_ids_collects_descendants_depth_first():
    nodes = make_exchange(2)
    assert subtree_ids(nodes, "u1") == ["u1", "a1"]
    assert subtree_ids(nodes, "u0") == ["u0", "a0", "u1", "a1"]


def test_link_child_keeps_both_sides_consistent():
    parent = Node(id="p", kind="user", speaker_id="s", content="p")
    child = Node(id="c", kind="assistant", speaker_id="s", content="c")
    link_child(parent, child)
    link_child(parent, child)  # idempotent

    assert child.parent_id == "p"
    assert parent.children == ["c"]
