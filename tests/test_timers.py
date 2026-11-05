"""A delayed call names the object it works on, and a timer belongs to its
owner: Qt drops the call, and stops the timer, when that object is deleted.
Without them they ran on windows an earlier test had closed, raised there
("Internal C++ object already deleted"), and on Python 3.11 PySide crashed
handling the exception (CI, Nov 5 2026). The source is read, as
test_threads.py reads it for raw threads."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sealedlore"


def calls(name: str) -> list[tuple[str, ast.Call]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called == name:
                found.append((f"{path.relative_to(SRC)}:{node.lineno}", node))
    return found


def test_every_delayed_call_names_the_object_it_works_on():
    """`QTimer.singleShot(delay, context, callable)`, never (delay, callable)."""
    bare = [where for where, call in calls("singleShot") if len(call.args) < 3]
    assert bare == [], bare


def test_every_timer_has_an_owner():
    """`QTimer(owner)`: a timer of its own outlives the widget it serves."""
    orphans = [where for where, call in calls("QTimer") if not call.args and not call.keywords]
    assert orphans == [], orphans
