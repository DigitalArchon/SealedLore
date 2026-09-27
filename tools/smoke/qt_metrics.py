"""What Qt measures with on this machine: the style, the screen, the fonts,
and each inspector page's width with a story open.

For comparing a platform against Linux when a layout test fails there
(`.github/workflows/windows.yml` runs it after the tests). Offscreen, with
the mock provider and a throwaway data folder; nothing leaves the machine.

    QT_QPA_PLATFORM=offscreen python tools/smoke/qt_metrics.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtGui import QFontDatabase, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleFactory  # noqa: E402

LABEL = "“Author turn 1, another way"


def main() -> None:
    app = QApplication([])
    screen = app.primaryScreen()
    font = app.font()
    print(f"platform       {sys.platform}, Qt platform {app.platformName()}")
    print(f"native style   {app.style().name()}  (available: {QStyleFactory.keys()})")
    print(
        f"screen         logical {screen.logicalDotsPerInch()} dpi, physical "
        f"{screen.physicalDotsPerInch():.0f} dpi, ratio {screen.devicePixelRatio()}"
    )
    print(
        f"app font       {font.family()!r} {font.pointSizeF()}pt, "
        f"{QFontMetrics(font).height()}px high"
    )
    print(f"general font   {QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()!r}")
    plain = QFontMetrics(font)
    boxes = plain.horizontalAdvance("i") == plain.horizontalAdvance("W")
    print(
        f"fonts          {len(QFontDatabase.families())} families; "
        + ("every glyph a box (no font database)" if boxes else "real glyphs")
    )

    from sealedlore.gui.fields import install_wheel_guard

    install_wheel_guard(app)
    style = app.style()
    base = style.baseStyle() if hasattr(style, "baseStyle") else style
    print(f"style in use   {base.name()} (under the wheel guard)")

    from sealedlore.gui import story_map

    small = story_map._font(8)
    metrics = QFontMetrics(small)
    print(
        f"map label      {small.family()!r} {small.pointSizeF()}pt: {LABEL!r} is "
        f"{metrics.horizontalAdvance(LABEL)}px of {story_map.label_width():.0f}px"
    )

    from sealedlore.gui.main_window import MainWindow
    from sealedlore.models.character import Character
    from sealedlore.models.story import Story
    from sealedlore.storage.repository import StoryBundle, save_story_bundle
    from tests.conftest import make_exchange

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        story = Story(id="story-1", title="Metrics", held_character_id="char-serrik")
        cast = [Character(id="char-serrik", name="Serrik Vaun")]
        save_story_bundle(StoryBundle(story=story, cast=cast, nodes=make_exchange(2)), root=root)
        window = MainWindow(root=root, use_mock=True)
        window.resize(1280, 860)
        window.show()
        for _ in range(10):
            app.processEvents()
        tabs = window.right_tabs
        print(f"inspector      empty: {tabs.width()}px")
        window.open_story(story.id)
        for _ in range(10):
            app.processEvents()
        print(f"inspector      open: {tabs.width()}px, minimum {tabs.minimumSizeHint().width()}px")
        for index in range(tabs.count()):
            page = tabs.widget(index)
            print(
                f"  {tabs.tabText(index):<14} minimum {page.minimumSizeHint().width()}px, "
                f"preferred {page.sizeHint().width()}px"
            )
        print(f"window         minimum {window.minimumSizeHint().width()}px wide")
        window.close()


if __name__ == "__main__":
    main()
