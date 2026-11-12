"""Text from outside the app is shown as text, never as HTML (gui/plain_text.py).

Qt draws a label's, a message box's and a tooltip's text as HTML when its first
line looks as if it holds a tag. A card named `<img src="/a/local/file">` drew
that file, `<a href=…>` a link, wherever the name was shown.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QAction, QStandardItem  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QListWidgetItem,
    QMessageBox,
    QTabWidget,
    QTreeWidgetItem,
    QWidget,
)

from sealedlore.gui import plain_text  # noqa: E402
from sealedlore.gui.plain_text import TIP_SETTERS, Rich, plain_tip  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1] / "src" / "sealedlore" / "gui"
NAME = '<img src="/etc/hostname"> Jane <a href="https://example.org">Moss</a>'


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _shown(app, widget: QWidget) -> QWidget:
    widget.show()
    app.processEvents()
    return widget


def test_a_label_showing_a_name_shows_it_as_text(app):
    label = _shown(app, QLabel(NAME))
    assert label.textFormat() == Qt.PlainText
    assert label.text() == NAME
    label.deleteLater()


def test_a_label_set_afterwards_and_restyled_stays_plain(app):
    label = _shown(app, QLabel())
    label.setStyleSheet("color: red")  # its own style: polished again
    label.setText(NAME)
    app.processEvents()
    assert label.textFormat() == Qt.PlainText
    label.deleteLater()


def test_a_label_made_rich_on_purpose_stays_rich(app):
    label = QLabel("<b>Portraits</b>")
    label.setTextFormat(Qt.RichText)
    _shown(app, label)
    assert label.textFormat() == Qt.RichText
    label.deleteLater()


def _boxes_text_formats(app, show) -> dict[str, Qt.TextFormat]:
    """Every label of the message box `show()` opens, by name, then the box
    closed."""
    seen: dict[str, Qt.TextFormat] = {}

    def look() -> None:
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox), "no message box came"
        for label in box.findChildren(QLabel):
            if label.text():
                seen[label.objectName()] = label.textFormat()
        box.done(QMessageBox.Cancel)

    QTimer.singleShot(0, look)
    show()
    return seen


@pytest.mark.parametrize("kind", ["question", "information", "warning", "critical"])
def test_a_box_built_by_the_real_statics_shows_plain_text(app, monkeypatch, kind):
    """App code calls QMessageBox's statics with card and story names in
    them; conftest stands in for them, so here they are Qt's own again."""
    monkeypatch.undo()
    static = getattr(QMessageBox, kind)
    seen = _boxes_text_formats(app, lambda: static(None, "Remove Jane?", f"Remove {NAME}?"))
    assert seen.get("qt_msgbox_label") == Qt.PlainText


def test_a_box_made_by_hand_shows_its_informative_text_plain_too(app, monkeypatch):
    monkeypatch.undo()
    box = QMessageBox(QMessageBox.Warning, "Delete", f"Delete {NAME}?", QMessageBox.Cancel)
    box.setInformativeText(f"{NAME}'s pictures go with them.")
    seen = _boxes_text_formats(app, box.exec)
    assert seen == {
        "qt_msgbox_label": Qt.PlainText,
        "qt_msgbox_informativelabel": Qt.PlainText,
    }
    box.deleteLater()


def test_a_tooltip_shows_what_it_was_given_character_for_character(app):
    from PySide6.QtGui import QTextDocument

    widget = QWidget()
    widget.setToolTip(NAME)
    shown = QTextDocument()
    shown.setHtml(widget.toolTip())
    assert shown.toPlainText() == NAME
    assert "<img" not in widget.toolTip() and "<a " not in widget.toolTip()
    widget.setToolTip("Jane Moss\nA scout")
    assert widget.toolTip() == "Jane Moss\nA scout", "a plain tooltip is left as it is"
    widget.setToolTip(Rich("<b>on purpose</b>"))
    assert widget.toolTip() == "<b>on purpose</b>"
    widget.deleteLater()


def test_every_way_of_setting_a_tooltip_shows_it_as_text(app):
    action = QAction("Draw")
    action.setToolTip(NAME)
    tree = QTreeWidgetItem()
    tree.setToolTip(1, NAME)
    item = QListWidgetItem("x")
    item.setToolTip(NAME)
    standard = QStandardItem("x")
    standard.setToolTip(NAME)
    tabs = QTabWidget()
    tabs.addTab(QWidget(), "Card")
    tabs.setTabToolTip(0, NAME)
    for tip in (
        action.toolTip(),
        tree.toolTip(1),
        item.toolTip(),
        standard.toolTip(),
        tabs.tabToolTip(0),
    ):
        assert tip == plain_tip(NAME) and "<img" not in tip
    tabs.deleteLater()


def _tip_calls(path: Path) -> list[str]:
    """Tooltip setters the app calls that `plain_text.install` doesn't
    cover, and tooltip-role data not plainly the app's own words."""
    covered = {name for _cls, name, _position in TIP_SETTERS}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def own_words(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "plain_tip":
            return True
        while isinstance(node, ast.Attribute | ast.Subscript):
            node = node.value
        return isinstance(node, ast.Name) and node.id.isupper()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        if name.endswith("ToolTip") and name.startswith("set") and name not in covered:
            found.append(f"{path.name}:{node.lineno} {name}")
        others = [
            arg
            for arg in node.args
            if not (isinstance(arg, ast.Attribute) and arg.attr == "ToolTipRole")
        ]
        if len(others) == len(node.args) or name not in ("setItemData", "setData"):
            continue
        # setItemData(index, value, role); setData(value, role), (role,
        # value) or (column, role, value).
        value = others[1] if name == "setItemData" and len(others) > 1 else others[-1]
        if not own_words(value):
            found.append(f"{path.name}:{node.lineno} {name}(…, ToolTipRole)")
    return found


def test_every_tooltip_the_app_sets_goes_through_plain_tip():
    offenders = [found for path in sorted(SOURCE.rglob("*.py")) for found in _tip_calls(path)]
    assert offenders == []


def test_the_rules_are_installed_once_and_hold():
    plain_text.install()
    plain_text.install()
    widget = QWidget()
    widget.setToolTip("<b>x</b>")
    assert widget.toolTip() == plain_tip("<b>x</b>")
    # Read back and set again (a tooltip built from another's): shown the same.
    widget.setToolTip(widget.toolTip())
    assert widget.toolTip() == plain_tip("<b>x</b>")
    # Made to look like what plain_tip makes, with a tag inside: escaped all the same.
    forged = "<p style='white-space:pre-wrap'><img src=x></p>"
    assert "<img" not in plain_tip(forged)
    widget.deleteLater()
