"""The README and the manual name menus, tabs, fields and buttons. Each must
exist in the real window under that exact label, so a renamed control fails
here rather than in a beta tester's hands.

Paths ("Story → Export → Story backup…", "Settings → Lore → Large
lorebooks") are walked through the live menu bar, the Settings and Setup
dialogs' tabs and the inspector's pages, down to the labels on a page. The
first version matched any string literal anywhere in gui/, so "Story →
Export story backup…" passed on a save dialog's title while the menu item
itself still said "Export story archive…".
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QLabel,
    QMenu,
    QTabWidget,
    QWidget,
)

ROOT = Path(__file__).resolve().parent.parent
GUI = ROOT / "src" / "sealedlore" / "gui"
# The user-facing docs whose labels are checked; most of them are in the manual.
DOCS = (ROOT / "README.md", ROOT / "docs" / "manual.md")


def _docs() -> str:
    return "\n\n".join(path.read_text(encoding="utf-8") for path in DOCS)


# Bold labels that end in an ellipsis are controls ("**Copy review**" is not
# checked this way: many bold phrases are concepts, not labels).
BOLD_CONTROL = re.compile(r"\*\*([^*\n]{2,60}…)\*\*")
# Bold chains whose head isn't a menu, dialog or page ("Speaking as →
# Question"): each segment must at least be a label somewhere in gui/.
BOLD_CHAIN = re.compile(r"\*\*([^*\n]*?→[^*\n]*?)\*\*")

Tree = dict[str, "Tree"]
# Submenus (and the menus themselves): a path may pass through one, never
# end on one. "Story → Export story backup…" matched the Export submenu and
# stopped there, unnoticed.
SUBMENUS: set[int] = set()


def _clean(text: str) -> str:
    return text.replace("&", "").strip()


def _menu_tree(menu: QMenu) -> Tree:
    tree: Tree = {}
    SUBMENUS.add(id(tree))
    for action in menu.actions():
        if action.isSeparator() or not action.text():
            continue
        tree[_clean(action.text())] = _menu_tree(action.menu()) if action.menu() else {}
    return tree


def _labels(page: QWidget) -> Tree:
    """Every label, checkbox and button text on a page."""
    found: Tree = {}
    for widget in page.findChildren(QWidget):
        if isinstance(widget, QLabel | QAbstractButton) and widget.text():
            text = _clean(widget.text())
            if text and len(text) <= 60:
                found[text] = {}
    return found


def _tabs(tabs: QTabWidget) -> Tree:
    return {_clean(tabs.tabText(i)): _labels(tabs.widget(i)) for i in range(tabs.count())}


@pytest.fixture(scope="module")
def tree(tmp_path_factory) -> Tree:
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.gui.setup_dialog import SetupDialog
    from sealedlore.models.story import Story

    _app = QApplication.instance() or QApplication([])
    window = MainWindow(root=tmp_path_factory.mktemp("readme"), use_mock=True)
    story = Story(title="README")
    settings = _tabs(SettingsDialog(window.config, story, window).tabs)
    setup = _tabs(SetupDialog(story, [], parent=window).findChild(QTabWidget))
    menus = {
        _clean(action.text()): _menu_tree(action.menu())
        for action in window.menuBar().actions()
        if action.menu()
    }
    menus["File"]["Settings…"] = settings
    menus["Story"]["Setup…"] = setup
    pages = window.right_tabs
    inspector = {_clean(pages.tabText(i)): _labels(pages.widget(i)) for i in range(pages.count())}
    window.close()
    return {**inspector, **menus, "Settings": settings, "Setup": setup}


def _readme_text() -> str:
    """The docs as running text: markup gone, lines joined."""
    text = _docs().replace("**", "").replace("`", "")
    return re.sub(r"\s*\n\s*", " ", text)


def _child(text: str, children: Tree) -> str | None:
    """The longest child label `text` begins with, as a whole label.

    Its "…" may be left off only where the sentence ends or the path goes
    on ("File → Settings."), never before more words: "Import scenario…"
    must not pass as "Import…".
    """
    best = None
    for label in children:
        for form in (label, label.rstrip("…")):
            if not form or not text.startswith(form):
                continue
            after = text[len(form) :]
            if after[:1].isalnum():
                continue  # part of a longer word
            if form != label and not re.match(r"\s*(→|[.,;:)|]|$)", after):
                continue
            if best is None or len(form) > len(best[1]):
                best = (label, form)
    return best[0] if best is not None else None


def _chains(text: str, tree: Tree):
    """Each place a known head is followed by an arrow: (start, head)."""
    heads = sorted(tree, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w→])(" + "|".join(re.escape(h) for h in heads) + r")\s*→")
    for match in pattern.finditer(text):
        before = text[: match.start()].rstrip()
        if before.endswith("→"):
            continue  # the middle of a chain already walked
        yield match.start(), match.group(1)


def test_every_path_in_the_readme_is_in_the_window(tree):
    text = _readme_text()
    missing: list[str] = []
    checked = 0
    for start, head in _chains(text, tree):
        checked += 1
        node, position = tree[head], start + len(head)
        path = [head]
        while True:
            rest = text[position:]
            arrow = re.match(r"\s*→\s*", rest)
            if arrow is None:
                break
            position += arrow.end()
            label = _child(text[position:], node)
            if label is None:
                missing.append(f"{' → '.join(path)} → {text[position : position + 40]!r}")
                break
            path.append(label)
            position += len(label) if text[position:].startswith(label) else len(label) - 1
            node = node[label]
        if id(node) in SUBMENUS:
            missing.append(
                f"{' → '.join(path)} (a menu, not an item): {text[start : position + 30]!r}"
            )
    assert checked >= 30, f"only {checked} paths found: the docs changed shape, or the regex did"
    assert not missing, "The docs name paths the window doesn't have:\n" + "\n".join(missing)


def gui_strings() -> set[str]:
    found: set[str] = set()
    for path in GUI.glob("*.py"):
        syntax = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(syntax):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(_clean(node.value))
    return found


def _known(label: str, strings: set[str]) -> bool:
    label = _clean(label)
    return label in strings or any(
        s.startswith(label) or s.startswith(label.rstrip("…")) for s in strings if s
    )


def test_every_other_control_the_readme_names_exists(tree):
    """Bold controls and chains outside the menus and dialogs: a label
    somewhere in gui/ is the most that can be checked for them."""
    text = _docs()
    strings = gui_strings()
    missing: list[str] = []
    for match in BOLD_CHAIN.finditer(text):
        segments = [s.strip(" *`").rstrip(":.,;") for s in match.group(1).split("→")]
        if segments[0] in tree:
            continue  # walked through the window above
        missing += [f"{match.group(1)}: {s!r}" for s in segments if s and not _known(s, strings)]
    for match in BOLD_CONTROL.finditer(text):
        if "→" not in match.group(1) and not _known(match.group(1), strings):
            missing.append(match.group(1))
    assert not missing, "The docs name controls the GUI doesn't have:\n" + "\n".join(missing)
