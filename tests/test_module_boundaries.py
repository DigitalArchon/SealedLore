"""The engine must stay headless.

Prompt assembly, context management and provider I/O have to run with no GUI
present — the CLI, the tests and any future headless use depend on it. An
accidental `from PySide6 import ...` in those packages would only surface as
an ImportError on a machine without Qt, so assert it here instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sealedlore"
HEADLESS_PACKAGES = ("engine", "models", "providers", "storage")
HEADLESS_MODULES = ("tree.py", "messages.py", "ids.py", "cli.py")

FORBIDDEN_PREFIXES = ("PySide6", "sealedlore.gui")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def headless_sources() -> list[Path]:
    paths = [SOURCE_ROOT / name for name in HEADLESS_MODULES]
    for package in HEADLESS_PACKAGES:
        paths.extend(sorted((SOURCE_ROOT / package).rglob("*.py")))
    return paths


def test_headless_sources_exist():
    paths = headless_sources()
    assert len(paths) > 10
    assert all(path.exists() for path in paths)


def test_no_gui_imports_outside_the_gui_package():
    offenders: dict[str, set[str]] = {}
    for path in headless_sources():
        bad = {name for name in imported_modules(path) if name.startswith(FORBIDDEN_PREFIXES)}
        if bad:
            offenders[str(path.relative_to(SOURCE_ROOT))] = bad
    assert offenders == {}


def test_gui_package_is_the_only_qt_dependent_one():
    gui_sources = sorted((SOURCE_ROOT / "gui").rglob("*.py"))
    assert gui_sources
    assert any(
        any(name.startswith("PySide6") for name in imported_modules(path)) for path in gui_sources
    )
