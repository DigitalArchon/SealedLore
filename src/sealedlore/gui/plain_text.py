"""Text shown as text, never as HTML, wherever it came from.

Qt draws a label's, a message box's and a tooltip's text as HTML when its
first line looks as if it holds a tag (`Qt.mightBeRichText`, Qt's AutoText).
Names and words from imported cards, from models and from endpoints reach
all three, so a card named `<img src="/a/local/file">` drew that file, and
`<a href=…>` a link. Two rules, installed once where the application is made
(`app.build_app`, the tests' conftest), hold everywhere rather than site by
site:

* **Labels are plain text** unless made rich on purpose: every QLabel still
  on AutoText as it is polished is set to PlainText (`plain_label`, from the
  app's proxy style, gui/fields.py). That takes in every message box, the
  static ones (QMessageBox.question and the rest) too, QInputDialog's and
  QProgressDialog's labels, and Qt's own dialogs. A label meant to show
  markup says so with `setTextFormat(Qt.RichText)`, and escapes whatever it
  holds from outside (`html.escape`).
* **Tooltips are shown as given**: they have no format of their own, so
  every way the app sets one (`TIP_SETTERS`) goes through `plain_tip`, which
  hands Qt a text that might be taken for HTML as HTML that shows exactly
  those characters. A tooltip meant as markup is wrapped in `Rich`.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QStandardItem
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtWidgets import (
    QLabel,
    QListWidgetItem,
    QTabBar,
    QTableWidgetItem,
    QTabWidget,
    QToolBox,
    QTreeWidgetItem,
    QWidget,
)


class Rich(str):
    """A tooltip that is markup on purpose (anything from outside in it
    escaped): `plain_tip` lets it through as it is."""


_SHOWN = ("<p style='white-space:pre-wrap'>", "</p>")


def plain_tip(text: str) -> str:
    """`text` as a tooltip shows it, character for character. Qt takes a
    tooltip for HTML by its first line; one that would be, is made HTML that
    shows the text itself (its spaces and lines kept). Anything else is
    left as it is, and so is a tooltip read back from a widget and set again
    (what this made, holding no tag of its own)."""
    if isinstance(text, Rich) or not isinstance(text, str) or not GuiQt.mightBeRichText(text):
        return text
    start, end = _SHOWN
    inner = text[len(start) : -len(end)]
    if text.startswith(start) and text.endswith(end) and "<" not in inner and ">" not in inner:
        return text
    return f"{start}{html.escape(text, quote=False)}{end}"


def plain_label(widget: object) -> None:
    """As a widget is polished: a label still on AutoText becomes plain
    text. Qt's own tooltip label sets its format on every tooltip, and
    tooltips go through `plain_tip`."""
    if (
        isinstance(widget, QLabel)
        and widget.textFormat() == Qt.AutoText
        and widget.metaObject().className() != "QTipLabel"
    ):
        widget.setTextFormat(Qt.PlainText)


# (class, method, which argument is the tooltip)
TIP_SETTERS = (
    (QWidget, "setToolTip", 0),
    (QAction, "setToolTip", 0),
    (QListWidgetItem, "setToolTip", 0),
    (QTableWidgetItem, "setToolTip", 0),
    (QStandardItem, "setToolTip", 0),
    (QTreeWidgetItem, "setToolTip", 1),
    (QTabBar, "setTabToolTip", 1),
    (QTabWidget, "setTabToolTip", 1),
    (QToolBox, "setItemToolTip", 1),
)

_installed: dict[tuple[type, str], object] = {}


def _wrapped(original, position: int):
    def set_tip(self, *args, **kwargs):
        if len(args) > position:
            args = (*args[:position], plain_tip(args[position]), *args[position + 1 :])
        return original(self, *args, **kwargs)

    set_tip.plain_tip = True
    set_tip.__name__ = getattr(original, "__name__", "setToolTip")
    set_tip.__doc__ = getattr(original, "__doc__", None)
    return set_tip


def install() -> None:
    """Once, as the application is made: every tooltip the app sets goes
    through `plain_tip` (labels are `plain_label`'s, from the style)."""
    for cls, name, position in TIP_SETTERS:
        current = cls.__dict__.get(name)
        if current is None or getattr(current, "plain_tip", False):
            continue
        _installed[(cls, name)] = current
        setattr(cls, name, _wrapped(current, position))
