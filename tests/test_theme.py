"""Themes and fonts: the stylesheet names its colours, a theme fills them in."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from sealedlore.gui import theme
from sealedlore.gui.main_window import MainWindow
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle
from tests.conftest import make_exchange

TEMPLATE = theme.load_template()
NAMES = set(re.findall(r"@([a-z][a-z0-9_]*)", re.sub(r"/\*.*?\*/", "", TEMPLATE, flags=re.S)))


@pytest.fixture(autouse=True)
def _default_look():
    yield
    theme.set_theme(theme.DEFAULT_THEME)
    theme.set_fonts()


def test_every_theme_has_every_colour_and_no_other():
    """A colour added to the stylesheet or to one theme must be added to all."""
    code_drawn = set(theme.DARK) - NAMES
    assert NAMES - {"font_mono"} <= set(theme.DARK)
    for choice in theme.THEMES.values():
        assert set(choice.colours) == set(theme.DARK), choice.key
        assert all(re.fullmatch(r"#[0-9a-f]{6}", v) for v in choice.colours.values())
    assert {"wash", "map_lane", "meter_track"} <= code_drawn


def test_each_theme_fills_in_every_name():
    for choice in theme.THEMES.values():
        sheet = theme.render(TEMPLATE, choice)
        outside_comments = re.sub(r"/\*.*?\*/", "", sheet, flags=re.S)
        assert "@" not in outside_comments, choice.key


def test_a_name_no_theme_has_is_an_error_not_a_blank():
    with pytest.raises(KeyError, match="@nowhere"):
        theme.render("QWidget { color: @nowhere; }", theme.THEMES["dark"])
    # Comments may name anything.
    assert theme.render("/* @nowhere */", theme.THEMES["dark"]) == "/* @nowhere */"


def test_the_default_fonts_add_nothing_and_a_choice_adds_one_rule():
    dark = theme.THEMES["dark"]
    plain = theme.render(TEMPLATE, dark)
    assert theme.DEFAULT_MONO in plain
    assert theme.STORY_SELECTORS not in plain
    chosen = theme.render(TEMPLATE, dark, theme.Fonts(story="Noto Serif", mono='Odd "Mono"'))
    assert chosen.startswith(plain.replace(theme.DEFAULT_MONO, '"Odd Mono"'))
    assert chosen.endswith(f'{theme.STORY_SELECTORS} {{\n  font-family: "Noto Serif";\n}}\n')


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


HC = theme.HIGH_CONTRAST
TEXTS_ON_BLACK = [
    "text",
    "text_bright",
    "text_accent",
    "text_soft",
    "text_heading",
    "text_payload",
    "text_dim",
    "text_hint",
    "text_placeholder",
    "text_faint",
    "story_text",
    "narration_text",
    "director_text",
    "ooc_text",
    "aside_text",
    "aside_question",
    "archived_text",
    "private_text",
    "warning",
    "error",
    "map_text",
    "map_dim",
    "plot_error",
    "plot_note",
]
TEXTS_ON_COLOUR = [
    ("text_selected", "selection"),
    ("primary_text", "primary"),
    ("primary_text", "primary_hover"),
    ("private_toggle_text", "private_toggle_bg"),
    ("text", "button_hover"),
    ("text", "button_pressed"),
    ("text", "row_alternate"),
    ("text_bright", "text_selection"),
]
MARKS_ON_BLACK = [
    "border",
    "control_border",
    "button_border",
    "hover_border",
    "indicator_border",
    "separator",
    "list_divider",
    "accent",
    "checked_border",
    "scrollbar",
    "kind_turn",
    "kind_passage",
    "kind_narration",
    "kind_director",
    "kind_aside",
    "kind_summary",
    "kind_picture",
    "ooc_border",
    "aside_border",
    "summary_border",
    "private_mark",
    "private_input_border",
    "meter_system",
    "meter_summaries",
    "meter_history",
    "meter_tail",
    "meter_over",
    "map_lane",
    "map_current",
    "map_here",
]


def test_high_contrast_reads_at_seven_to_one_and_marks_show_at_three():
    """WCAG AAA for text (7:1), and 3:1 for borders and marks, so what the
    theme is for holds wherever a colour is used."""
    black = "#000000"
    for name in TEXTS_ON_BLACK:
        assert _contrast(HC[name], black) >= 7, name
    for text, ground in TEXTS_ON_COLOUR:
        assert _contrast(HC[text], HC[ground]) >= 7, (text, ground)
    for name in MARKS_ON_BLACK:
        assert _contrast(HC[name], black) >= 3, name
    # Disabled text is exempt from WCAG, but must still be read.
    assert _contrast(HC["text_disabled"], black) >= 4.5


def test_what_is_drawn_in_code_follows_the_theme():
    theme.set_theme("high_contrast")
    assert theme.colour("map_current").name() == HC["map_current"]
    assert theme.colour("map_chapter", 170).alpha() == 170
    assert theme.translucent("panel", 220) == "rgba(0, 0, 0, 220)"
    theme.set_theme("no such theme")
    assert theme.current().key == "dark"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_window_switches_theme_and_fonts_and_keeps_them(
    app, tmp_path: Path, story, cast, monkeypatch
):
    from sealedlore.gui import fonts_dialog

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    # A restyle reaches every widget alive: earlier tests' windows go first.
    for other in app.topLevelWidgets():
        if isinstance(other, MainWindow):
            other.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    window = MainWindow(root=tmp_path, use_mock=True)
    try:
        window.open_story(story.id)
        window.theme_choices["high_contrast"].trigger()
        assert f"background-color: {HC['window']}" in app.styleSheet()
        assert load_config(root=tmp_path).theme == "high_contrast"
        assert window.theme_choices["high_contrast"].isChecked()

        family = app.font().family()
        monkeypatch.setattr(fonts_dialog.FontsDialog, "exec", lambda self: 1)
        monkeypatch.setattr(
            fonts_dialog.FontsDialog,
            "chosen",
            lambda self: theme.Fonts(ui=family, story="Serif Of Nowhere"),
        )
        window.choose_fonts()
        assert '"Serif Of Nowhere"' in app.styleSheet()
        config = load_config(root=tmp_path)
        assert (config.ui_font, config.story_font) == (family, "Serif Of Nowhere")

        # A new window reads them back.
        theme.set_theme("dark")
        theme.set_fonts()
        again = MainWindow(root=tmp_path, use_mock=True)
        assert theme.current().key == "high_contrast"
        assert theme.fonts().story == "Serif Of Nowhere"
        again.close()
    finally:
        window.close()
        theme.set_theme("dark")
        theme.set_fonts()
        app.setStyleSheet("")
