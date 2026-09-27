"""The size of every piece of text in the window (View → Text size).

For anyone, and above all for poor eyesight. The stylesheet sets its sizes
in pixels, so a larger size is the same stylesheet with every `font-size`
scaled; the application font (what widgets the stylesheet doesn't reach, and
`QFont()`, start from) is scaled with it, and the few fonts set in code go
through `points`. Spacing and padding stay as they are: they already fit
text of any size.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import QApplication

from sealedlore.gui import theme

# The choices offered, as a share of the designed size.
SCALES = (0.9, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0)
MIN_SCALE, MAX_SCALE = SCALES[0], SCALES[-1]

_FONT_SIZE = re.compile(r"(font-size:\s*)(\d+(?:\.\d+)?)px")

_scale = 1.0
# The application font's size before any scaling, in points, and its family
# before the author chose one (`app.pick_font`).
_base_points: float | None = None
_base_family: str | None = None


def load_stylesheet() -> str:
    """The stylesheet at its designed size, in the current theme and fonts."""
    return theme.stylesheet()


def scale() -> float:
    return _scale


def clamp(value: float) -> float:
    return max(MIN_SCALE, min(MAX_SCALE, round(value, 2)))


def points(size: float) -> float:
    """A font size set in code, at the current text size."""
    return size * _scale


def scaled_stylesheet(qss: str, factor: float) -> str:
    """The stylesheet with every pixel font size multiplied by `factor`."""
    if factor == 1.0:
        return qss

    def bigger(match: re.Match[str]) -> str:
        return f"{match.group(1)}{max(1, round(float(match.group(2)) * factor))}px"

    return _FONT_SIZE.sub(bigger, qss)


def step(current: float, direction: int) -> float:
    """The next size up (1) or down (-1) from `current`."""
    if direction > 0:
        return next((s for s in SCALES if s > current + 1e-6), MAX_SCALE)
    return next((s for s in reversed(SCALES) if s < current - 1e-6), MIN_SCALE)


def apply(app: QApplication, factor: float, base_stylesheet: str | None = None) -> None:
    """Every widget's text at `factor` of its designed size, in the current
    theme and fonts (gui/theme.py): changing any of the three restyles
    everything, so all three go through here."""
    if base_stylesheet is None:
        base_stylesheet = load_stylesheet()
    global _scale, _base_points, _base_family
    _scale = clamp(factor)
    font = app.font()
    if _base_points is None:
        _base_points = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
    if _base_family is None:
        _base_family = font.family()
    font.setPointSizeF(_base_points * _scale)
    font.setFamily(theme.fonts().ui or _base_family)
    app.setFont(font)
    app.setStyleSheet(scaled_stylesheet(base_stylesheet, _scale))
