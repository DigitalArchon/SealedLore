"""A row of controls that wraps onto another line when the width runs out.

The composer's rows set the window's minimum width, and they grow with the
text size (View → Text size): at 150% the window could not be narrower than
1,556px, past a laptop's 1,366. A `WrapRow` lays its groups out like a
`QHBoxLayout` while they fit and moves whole groups down a line when they
don't, so a caption never leaves its control.

Within a line it behaves like a box layout: spare width goes to members whose
size policy expands (a compact combo, up to its maximum width), a short line
squeezes them toward their minimum, and groups added with `right=True` sit
at the line's right end.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget, QWidgetItem


@dataclass
class _Group:
    items: list[QLayoutItem]
    right: bool = False
    # Moved down a line whole rather than squeezed beside others (a widget
    # that wraps inside itself, like the branch bar).
    whole: bool = False
    # Filled in each pass: the members being laid out, and their widths.
    shown: list[QLayoutItem] = field(default_factory=list)


class WrapRow(QLayout):
    def __init__(self, parent: QWidget | None = None, *, spacing: int = 8, line_spacing: int = 6):
        super().__init__(parent)
        self._groups: list[_Group] = []
        self._spacing = spacing
        self._line_spacing = line_spacing
        # The width the last layout pass had, and the height it took.
        self._width_given = 0
        self._height_given = 0
        self.setContentsMargins(0, 0, 0, 0)

    # --- building ----------------------------------------------------------------

    def add_group(
        self, widgets: Sequence[QWidget], *, right: bool = False, whole: bool = False
    ) -> None:
        """Widgets kept together on one line (a caption and its control)."""
        items: list[QLayoutItem] = []
        for widget in widgets:
            self.addChildWidget(widget)
            items.append(QWidgetItem(widget))
        self._groups.append(_Group(items, right, whole))
        self.invalidate()

    # --- QLayout ------------------------------------------------------------------

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt naming
        self._groups.append(_Group([item]))

    def count(self) -> int:
        return sum(len(group.items) for group in self._groups)

    def _flat(self) -> list[QLayoutItem]:
        return [item for group in self._groups for item in group.items]

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt naming
        items = self._flat()
        return items[index] if 0 <= index < len(items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt naming
        for group in self._groups:
            if index < len(group.items):
                item = group.items.pop(index)
                if not group.items:
                    self._groups.remove(group)
                return item
            index -= len(group.items)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt naming
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """Everything on one line."""
        groups = self._visible()
        width = sum(self._width(g, "hint") for g in groups) + self._spacing * max(
            0, len(groups) - 1
        )
        return QSize(width, self._line_height(groups)) + self._margins()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        """The widest group, alone on a line, squeezed; as tall as the lines
        take at the width it has, so the rows below never overlap them."""
        groups = self._visible()
        width = max((self._width(g, "min") for g in groups), default=0)
        height = self._line_height(groups) + self._margins().height()
        if self._width_given:
            height = max(height, self.heightForWidth(self._width_given))
        return QSize(width + self._margins().width(), height)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        height = self._arrange(rect, apply=True)
        if rect.width() != self._width_given:
            self._width_given = rect.width()
            if height != self._height_given:
                # Lines were added or taken away: the minimum height changed.
                self._height_given = height
                self.invalidate()

    # --- layout -------------------------------------------------------------------

    def _margins(self) -> QSize:
        m = self.contentsMargins()
        return QSize(m.left() + m.right(), m.top() + m.bottom())

    def _visible(self) -> list[_Group]:
        shown = []
        for group in self._groups:
            group.shown = [item for item in group.items if not item.isEmpty()]
            if group.shown:
                shown.append(group)
        return shown

    def _width(self, group: _Group, kind: str) -> int:
        sizes = [
            (item.minimumSize() if kind == "min" else item.sizeHint()).width()
            for item in group.shown
        ]
        return sum(sizes) + self._spacing * max(0, len(sizes) - 1)

    @staticmethod
    def _line_height(groups: Sequence[_Group]) -> int:
        return max((item.sizeHint().height() for g in groups for item in g.shown), default=0)

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        """Lay the groups out in `rect`; returns the height they take."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        width = max(0, area.width())
        lines: list[list[_Group]] = []
        used = 0
        # A group joins a line if the line fits at its members' minimum
        # widths, as a box layout would squeeze them: packing by preferred
        # widths wrapped the composer at 100% on a 1366px window, where one
        # squeezed line each had always fitted.
        for group in self._visible():
            need = self._width(group, "hint" if group.whole else "min")
            if lines and used + self._spacing + need <= width:
                lines[-1].append(group)
                used += self._spacing + need
            else:
                lines.append([group])
                used = need
        y = area.y()
        for index, line in enumerate(lines):
            height = self._line_height(line)
            if apply:
                self._place_line(line, area.x(), y, width, height)
            y += height + (self._line_spacing if index < len(lines) - 1 else 0)
        return y - area.y() + m.top() + m.bottom()

    def _place_line(self, line: list[_Group], x: int, y: int, width: int, height: int) -> None:
        items = [item for group in line for item in group.shown]
        gaps = self._spacing * (len(items) - 1)
        widths = [item.sizeHint().width() for item in items]
        extra = width - gaps - sum(widths)
        grows = [
            i
            for i, item in enumerate(items)
            if item.widget() is not None and item.widget().sizePolicy().horizontalPolicy() in _GROWS
        ]
        if extra < 0:
            # Too little room: squeeze toward minimum widths, as a box would.
            for i in sorted(range(len(items)), key=lambda i: -(widths[i] - _min(items[i]))):
                give = min(-extra, widths[i] - _min(items[i]))
                widths[i] -= give
                extra += give
                if extra >= 0:
                    break
        else:
            # Spare room to the controls that expand, each up to its maximum.
            open_ = [i for i in grows if widths[i] < items[i].maximumSize().width()]
            while extra > 0 and open_:
                share = max(1, extra // len(open_))
                for i in list(open_):
                    give = min(share, extra, items[i].maximumSize().width() - widths[i])
                    widths[i] += give
                    extra -= give
                    if widths[i] >= items[i].maximumSize().width():
                        open_.remove(i)
                    if extra <= 0:
                        break
        extra = max(0, extra)
        first_right = next(
            (sum(len(g.shown) for g in line[:n]) for n, g in enumerate(line) if g.right), None
        )
        for i, item in enumerate(items):
            if i == first_right:
                x += extra
            hint = item.sizeHint().height()
            h = min(height, max(hint, item.minimumSize().height()))
            item.setGeometry(QRect(x, y + (height - h) // 2, widths[i], h))
            x += widths[i] + self._spacing


_GROWS = (QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)


def _min(item: QLayoutItem) -> int:
    return item.minimumSize().width()
