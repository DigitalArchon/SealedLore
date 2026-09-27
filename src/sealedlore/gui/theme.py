"""The window's colours and fonts (View → Theme, View → Fonts…).

The stylesheet (`style.qss`) names its colours instead of writing them:
`@panel`, `@text_dim`, `@kind_director`. A theme is a table of those names,
and the stylesheet the window gets is the template with the current theme's
values filled in. The parts Qt draws in code (the story map, the budget
meter, the wash over a background picture) take theirs from the same table
through `colour`.

Dark is the look the app was designed in: filled in, the template is the
stylesheet exactly as it was written with its colours in place. Names that
share a value in Dark (a button's hover and the edge of a passage) are
separate because another theme may need them apart.

High contrast is for poor eyesight, beside Text size: black ground, white
text, borders that show, and yellow for focus and what is current. Colours
that tell kinds apart (the author's turns, directions, asides, chapters) stay
distinct but bright enough to read on black.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtGui import QColor

TEMPLATE = Path(__file__).with_name("style.qss")

# Dark, the app's own look. Grouped by what the colours are for.
DARK: dict[str, str] = {
    # grounds
    "window": "#16181d",
    "field": "#14161a",
    "panel": "#1b1e25",
    "separator": "#22252d",
    "field_disabled": "#181a20",
    "row_alternate": "#171a20",
    # borders
    "border": "#23262f",
    "control_border": "#2a2e39",
    "button_border": "#2f3441",
    "hover_border": "#3b4356",
    "hover_border_accent": "#3b4261",
    "list_divider": "#1d2027",
    "indicator_border": "#4a5163",
    # buttons
    "button": "#232733",
    "button_hover": "#2b3040",
    "button_pressed": "#1f2330",
    "button_disabled": "#1a1c22",
    "primary": "#3b5ea8",
    "primary_border": "#4a70bd",
    "primary_hover": "#4670c0",
    "primary_text": "#f2f5fb",
    "checked_border": "#3b5ea8",
    # text
    "text": "#d8dbe2",
    "text_bright": "#e4e7ee",
    "text_selected": "#eef2f9",
    "text_accent": "#c0caf5",
    "text_soft": "#b9bfcb",
    "text_heading": "#c9ceda",
    "text_payload": "#c3c9d4",
    "text_dim": "#8a91a0",
    "text_hint": "#757d8c",
    "text_placeholder": "#6b7383",
    "text_faint": "#5d6472",
    "text_disabled": "#565c69",
    "text_disabled_faint": "#3a3f4b",
    # accents and states
    "accent": "#7aa2f7",
    "selection": "#2b3a5c",
    "text_selection": "#33456b",
    "scrollbar": "#4a5165",
    "scrollbar_hover": "#626b84",
    "warning": "#e0af68",
    "warning_bg": "#241f16",
    "warning_border": "#4a3a20",
    "error": "#f7768e",
    # the transcript's messages, by kind
    "story_text": "#dfe2e9",
    "kind_turn": "#7aa2f7",
    "turn_bg": "#191d27",
    "kind_passage": "#2b3040",
    "kind_narration": "#565f73",
    "narration_bg": "#17191f",
    "narration_text": "#c2c7d2",
    "kind_director": "#bb9af7",
    "director_bg": "#1d1a22",
    "director_text": "#cbb6e8",
    "ooc_bg": "#1d1a22",
    "ooc_border": "#3a3145",
    "ooc_focus": "#bb9af7",
    "ooc_text": "#9aa2b1",
    "kind_aside": "#5fb3b3",
    "aside_bg": "#161b1c",
    "aside_border": "#2f4a4a",
    "aside_text": "#c4d6d6",
    "aside_question": "#8fb8b8",
    "kind_summary": "#9ece6a",
    "summary_bg": "#1a1c24",
    "summary_border": "#2a2e3b",
    "kind_picture": "#e0af68",
    "picture_bg": "#171a20",
    "archived_bg": "#181a1f",
    "archived_text": "#9aa1ae",
    # private scenes
    "private_mark": "#9d7cd8",
    "private_bg": "#1b1824",
    "private_text": "#cfc6e6",
    "private_input_bg": "#1c1826",
    "private_input_border": "#6e56ad",
    "private_toggle_bg": "#3a2d57",
    "private_toggle_border": "#7d5fc4",
    "private_toggle_text": "#e4d9ff",
    # drawn in code: the budget meter, the story map, the picture wash, the
    # plot editor's problems
    "meter_system": "#7aa2f7",
    "meter_summaries": "#9ece6a",
    "meter_history": "#bb9af7",
    "meter_tail": "#e0af68",
    "meter_over": "#f7768e",
    "meter_track": "#23262e",
    "map_lane": "#4a5165",
    "map_current": "#7aa2f7",
    "map_chapter": "#2b3a5c",
    "map_here": "#e0af68",
    "map_text": "#c0caf5",
    "map_dim": "#8a91a0",
    "map_grid": "#23262f",
    "wash": "#0c0d11",
    "plot_error": "#c0392b",
    "plot_note": "#b9770e",
}

# High contrast: every text at least 7:1 on its ground (WCAG AAA), every
# border and mark at least 3:1 against black.
HIGH_CONTRAST: dict[str, str] = {
    "window": "#000000",
    "field": "#000000",
    "panel": "#000000",
    "separator": "#808080",
    "field_disabled": "#000000",
    "row_alternate": "#161616",
    "border": "#b3b3b3",
    "control_border": "#ffffff",
    "button_border": "#ffffff",
    "hover_border": "#ffff00",
    "hover_border_accent": "#ffff00",
    "list_divider": "#5e5e5e",
    "indicator_border": "#ffffff",
    "button": "#000000",
    "button_hover": "#2e2e2e",
    "button_pressed": "#474747",
    "button_disabled": "#000000",
    "primary": "#0a4fc4",
    "primary_border": "#ffffff",
    "primary_hover": "#0847b0",
    "primary_text": "#ffffff",
    "checked_border": "#ffff00",
    "text": "#ffffff",
    "text_bright": "#ffffff",
    "text_selected": "#ffffff",
    "text_accent": "#ffff00",
    "text_soft": "#ffffff",
    "text_heading": "#ffffff",
    "text_payload": "#ffffff",
    "text_dim": "#e6e6e6",
    "text_hint": "#d9d9d9",
    "text_placeholder": "#bfbfbf",
    "text_faint": "#bfbfbf",
    "text_disabled": "#8c8c8c",
    "text_disabled_faint": "#8c8c8c",
    "accent": "#ffff00",
    "selection": "#0a3d91",
    "text_selection": "#0a4fc4",
    "scrollbar": "#bfbfbf",
    "scrollbar_hover": "#ffffff",
    "warning": "#ffd400",
    "warning_bg": "#000000",
    "warning_border": "#ffd400",
    "error": "#ff7070",
    "story_text": "#ffffff",
    "kind_turn": "#5cb3ff",
    "turn_bg": "#000000",
    "kind_passage": "#b3b3b3",
    "kind_narration": "#8c8c8c",
    "narration_bg": "#000000",
    "narration_text": "#ffffff",
    "kind_director": "#d9a6ff",
    "director_bg": "#000000",
    "director_text": "#f2e0ff",
    "ooc_bg": "#000000",
    "ooc_border": "#d9a6ff",
    "ooc_focus": "#ffff00",
    "ooc_text": "#ffffff",
    "kind_aside": "#40e0d0",
    "aside_bg": "#000000",
    "aside_border": "#40e0d0",
    "aside_text": "#ffffff",
    "aside_question": "#a6fff5",
    "kind_summary": "#80ff80",
    "summary_bg": "#000000",
    "summary_border": "#80ff80",
    "kind_picture": "#ffd400",
    "picture_bg": "#000000",
    "archived_bg": "#000000",
    "archived_text": "#d9d9d9",
    "private_mark": "#d9a6ff",
    "private_bg": "#000000",
    "private_text": "#ffffff",
    "private_input_bg": "#000000",
    "private_input_border": "#d9a6ff",
    "private_toggle_bg": "#3d1a66",
    "private_toggle_border": "#d9a6ff",
    "private_toggle_text": "#ffffff",
    "meter_system": "#5cb3ff",
    "meter_summaries": "#80ff80",
    "meter_history": "#d9a6ff",
    "meter_tail": "#ffd400",
    "meter_over": "#ff7070",
    "meter_track": "#333333",
    "map_lane": "#b3b3b3",
    "map_current": "#ffff00",
    "map_chapter": "#0a3d91",
    "map_here": "#ffd400",
    "map_text": "#ffffff",
    "map_dim": "#d9d9d9",
    "map_grid": "#333333",
    "wash": "#000000",
    "plot_error": "#ff7070",
    "plot_note": "#ffd400",
}


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    colours: dict[str, str] = field(repr=False)


THEMES: dict[str, Theme] = {
    theme.key: theme
    for theme in (
        Theme("dark", "Dark", DARK),
        Theme("high_contrast", "High contrast", HIGH_CONTRAST),
    )
}
DEFAULT_THEME = "dark"

# The monospace font the stylesheet asks for when the author hasn't chosen one.
DEFAULT_MONO = '"DejaVu Sans Mono", "Liberation Mono", monospace'
# The text of the story: passages, turns, and the boxes they are written in.
STORY_SELECTORS = "#messageBody, #messageEditor, #input, #oocInput"

_TOKEN = re.compile(r"@([a-z][a-z0-9_]*)")
# Comments are left as written: they may name a colour to explain it.
_COMMENT = re.compile(r"(/\*.*?\*/)", re.S)


@dataclass
class Fonts:
    """The author's font families; "" is the app's own."""

    ui: str = ""
    story: str = ""
    mono: str = ""


_theme: Theme = THEMES[DEFAULT_THEME]
_fonts = Fonts()


def current() -> Theme:
    return _theme


def fonts() -> Fonts:
    return _fonts


def known(key: str) -> str:
    """`key` if there is such a theme, else the default."""
    return key if key in THEMES else DEFAULT_THEME


def set_theme(key: str) -> Theme:
    """Choose the theme `stylesheet()` and `colour()` read. The window
    applies it (`text_size.apply`), since that restyles every widget."""
    global _theme
    _theme = THEMES[known(key)]
    return _theme


def set_fonts(ui: str = "", story: str = "", mono: str = "") -> None:
    global _fonts
    _fonts = Fonts(ui=ui.strip(), story=story.strip(), mono=mono.strip())


def colour(name: str, alpha: int = 255) -> QColor:
    """A colour of the current theme, for what is drawn in code."""
    value = QColor(_theme.colours[name])
    value.setAlpha(alpha)
    return value


def translucent(name: str, alpha: int) -> str:
    """A colour of the current theme as a stylesheet `rgba()`."""
    value = QColor(_theme.colours[name])
    return f"rgba({value.red()}, {value.green()}, {value.blue()}, {alpha})"


def _quoted(family: str) -> str:
    return '"' + family.replace("\\", "").replace('"', "") + '"'


def render(template: str, theme: Theme, chosen: Fonts | None = None) -> str:
    """The stylesheet: `template` with `theme`'s colours and the fonts in.

    A name the theme doesn't have is an error, not a blank: the stylesheet
    would silently drop the rule. The story font is a rule of its own at the
    end, only when one is chosen, so the default stylesheet is unchanged.
    """
    chosen = chosen or Fonts()
    values = {**theme.colours, "font_mono": _quoted(chosen.mono) if chosen.mono else DEFAULT_MONO}

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"style.qss names a colour the {theme.label} theme lacks: @{name}")
        return values[name]

    sheet = "".join(
        part if part.startswith("/*") else _TOKEN.sub(fill, part)
        for part in _COMMENT.split(template)
    )
    if chosen.story:
        sheet += f"\n{STORY_SELECTORS} {{\n  font-family: {_quoted(chosen.story)};\n}}\n"
    return sheet


def load_template() -> str:
    try:
        return TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        return ""


def stylesheet() -> str:
    """The stylesheet for the current theme and fonts, at its designed size."""
    template = load_template()
    return render(template, _theme, _fonts) if template else ""
