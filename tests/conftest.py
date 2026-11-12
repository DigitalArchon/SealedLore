from __future__ import annotations

import os

import pytest

from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.node import Node, NodeMeta, Usage
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story, StyleDirectives
from sealedlore.tree import link_child


@pytest.fixture(scope="session", autouse=True)
def _wheel_guard_style_first():
    """The app installs the wheel guard's style before any widget exists
    (gui/app.build_app); tests do the same, once, at the start of the run.
    Installed by whichever test first made a window, it swapped the app's
    style under earlier tests' live widgets and crashed the run."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # a machine without Qt still runs the headless tests
        yield
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from sealedlore.gui.collector import install_collector_pause
    from sealedlore.gui.fields import install_wheel_guard

    app = QApplication.instance() or QApplication([])
    install_wheel_guard(app)
    # As the app does: the collector waits while background threads run.
    install_collector_pause(app)
    yield
    # Offscreen on Windows, mime data left on the clipboard (Copy prompt,
    # winprivacy.copy_text) crashes the interpreter's exit with an access
    # violation after every test has passed, so the run fails. The real
    # Windows platform doesn't; any QMimeData does, not only ours.
    app.clipboard().clear()
    _delete_widgets(app.topLevelWidgets())


def _delete_widgets(widgets) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    for widget in widgets:
        widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture(autouse=True)
def _the_apps_look_as_it_was():
    """The app's look put back as the test found it, so every test starts in
    the look the run began with: no stylesheet, Qt's own palette and font,
    the default theme and fonts, 100% text. A text size or a theme set
    through the window applies the whole stylesheet, and a test that put
    back only 100% (test_ui_review's rebuilds, Nov 2026) left every later
    test laid out in it: the Lore page's list and the cast board's measured
    fewer rows than they were sized for, but only after it. Put back once
    the test's windows are deleted (`_delete_the_tests_windows` asks for
    this first), so a restyle reaches no more than older widgets."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        yield
        return
    app = QApplication.instance()
    if app is None:
        yield
        return
    look = app_look(app)
    yield
    put_back_look(app, look)


def app_look(app) -> tuple:
    """The app's look as it is: its stylesheet, palette and font, the theme,
    the fonts chosen and the text size (`_the_apps_look_as_it_was`)."""
    from sealedlore.gui import text_size, theme

    return (
        app.styleSheet(),
        app.palette(),
        app.font(),
        theme.current().key,
        theme.fonts(),
        text_size.scale(),
    )


def put_back_look(app, look: tuple) -> None:
    """The app's look as `app_look` found it, each part only if it changed:
    a stylesheet or palette set restyles every widget alive."""
    from sealedlore.gui import text_size, theme

    sheet, palette, font, chosen, fonts, scale = look
    if theme.current().key != chosen:
        theme.set_theme(chosen)
    if theme.fonts() != fonts:
        theme.set_fonts(fonts.ui, fonts.story, fonts.mono)
    text_size._scale = scale
    if app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
    if app.palette() != palette:
        app.setPalette(palette)
    if app.font() != font:
        app.setFont(font)


@pytest.fixture(autouse=True)
def _delete_the_tests_windows(_the_apps_look_as_it_was):
    """Deletes the windows and dialogs a test made and left behind. Tests
    build them with no parent, and one kept alive by a connected lambda
    outlives the test: PySide6 6.12.0 then deletes it twice at interpreter
    exit and the run segfaults after every test has passed (CI, Oct 2026).
    The app gives every dialog its window as parent. Only what the test
    made: a module fixture's window (test_readme_labels) is older."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        yield
        return
    from shiboken6 import getCppPointer

    def address(widget) -> int:
        return getCppPointer(widget)[0]

    app = QApplication.instance()
    before = {address(w) for w in app.topLevelWidgets()} if app else set()
    yield
    app = QApplication.instance()
    if app is not None:
        _delete_widgets([w for w in app.topLevelWidgets() if address(w) not in before])


class MemoryKeychain:
    """A keychain in memory (storage/keychain.py): no test ever reads or
    writes the real one."""

    priority = 5

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.store[(service, name)]


@pytest.fixture(autouse=True)
def _follow_ups_go_on(request, monkeypatch):
    """A premise draft opens the follow-up dialog before Setup, and it is
    modal: here it goes straight on, as if nothing was asked.
    tests/test_followup_gui.py drives it itself (marked `real_follow_up`)."""
    if request.node.get_closest_marker("real_follow_up"):
        yield
        return
    try:
        from PySide6.QtWidgets import QDialog

        from sealedlore.gui.followup_dialog import FollowUpDialog
    except ImportError:
        yield
        return
    monkeypatch.setattr(FollowUpDialog, "exec", lambda dialog: QDialog.Accepted)
    yield


@pytest.fixture(autouse=True)
def _crops_take_the_whole_picture(request, monkeypatch):
    """Every picture added from disk opens the crop dialog, which is modal:
    left alone it would wait for a click that never comes. Here it answers
    "Use the whole picture", as adding did before there was a crop.
    tests/test_crop.py drives the dialog itself (marked `real_crop`)."""
    if request.node.get_closest_marker("real_crop"):
        yield
        return
    try:
        from PySide6.QtWidgets import QDialog

        from sealedlore.gui.crop_dialog import CropDialog
    except ImportError:
        yield
        return

    def take_whole(dialog) -> int:
        dialog.whole = True
        return QDialog.Accepted

    monkeypatch.setattr(CropDialog, "exec", take_whole)
    yield


class RefusingImages:
    """The image client a test gets unless it stands in one of its own: map
    pictures are on by default (`Config.map_pictures_mode`), and any test
    with a NanoGPT chat endpoint and a key has an image endpoint too. Every
    picture is refused, as an endpoint that is down would; nothing reaches
    the network."""

    def __init__(self, base_url: str = "", api_key: str = "", **kwargs) -> None:
        self.base_url, self.api = base_url, kwargs.get("api", "")

    def generate(self, payload):
        from sealedlore.providers.base import ProviderError

        raise ProviderError("tests never reach an image model")

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _unanswered_boxes_fail(monkeypatch):
    """A message box no test answers waits forever offscreen, and the whole
    run hangs with nothing said. Here it fails at once instead, naming the
    box; a test that means to answer one patches it as before."""
    try:
        from PySide6.QtWidgets import QMessageBox
    except ImportError:
        yield
        return

    def static(kind: str):
        def ask(_parent, title, text, *_args, **_kwargs):
            raise AssertionError(f"unanswered {kind}: {title!r}: {text!r}")

        return staticmethod(ask)

    def exec_(box):
        raise AssertionError(f"unanswered message box: {box.windowTitle()!r}: {box.text()!r}")

    for kind in ("question", "information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, static(kind))
    monkeypatch.setattr(QMessageBox, "exec", exec_)
    yield


@pytest.fixture(autouse=True)
def _writes_left_to_the_cache(monkeypatch):
    """Every save flushes to the disk before it is swapped in
    (`storage.atomic.DURABLE`), several milliseconds a file: across the
    suite's thousands of saves that is many minutes. The tests of it turn it
    back on (tests/test_saving.py)."""
    from sealedlore.storage import atomic

    monkeypatch.setattr(atomic, "DURABLE", False, raising=False)
    yield


@pytest.fixture(autouse=True)
def memory_keychain():
    from sealedlore.storage import keychain

    backend = MemoryKeychain()
    keychain.use_backend(backend)
    keychain.last_problem = None
    yield backend
    keychain.use_backend(MemoryKeychain())


@pytest.fixture
def estimator() -> TokenEstimator:
    """Deterministic and offline: tests must never reach for tiktoken's download."""
    return TokenEstimator(counter=fallback_counter)


@pytest.fixture
def cast() -> list[Character]:
    return [
        Character(
            id="char-serrik",
            name="Serrik Vaun",
            aliases=["the Grey Wolf"],
            summary="A veteran duellist with a debt he cannot repay.",
        ),
        Character(id="char-maela", name="Maela Orr", summary="A locksmith who talks too much."),
        Character(id="char-idris", name="Captain Idris", summary="Three days' ride north."),
    ]


@pytest.fixture
def story() -> Story:
    return Story(
        id="story-1",
        title="The Sundering",
        world_bible="Calder Keep has stood empty for a hundred years.",
        style=StyleDirectives(
            response_style="custom",
            length_target="roughly 150-250 words, two to four paragraphs",
            person="third",
            tense="past",
        ),
        scene=SceneState(
            location="The undercroft beneath Calder Keep",
            time_of_day="past midnight",
            situation="Torchlight, and something moving in the dark.",
            present_character_ids=["char-serrik", "char-maela"],
        ),
    )


def make_exchange(count: int, *, speaker_id: str = "char-serrik") -> list[Node]:
    """`count` linked user/assistant pairs, oldest first."""
    nodes: list[Node] = []
    previous: Node | None = None
    for index in range(count):
        user = Node(
            id=f"u{index}",
            kind="user",
            speaker_id=speaker_id,
            content=f"Author turn {index}.",
            meta=NodeMeta(controlled_character_id=speaker_id),
        )
        assistant = Node(
            id=f"a{index}",
            kind="assistant",
            speaker_id="__narrator__",
            content=f"The world answers, turn {index}.",
            meta=NodeMeta(model="anthropic/claude-3.5-sonnet", usage=Usage(prompt_tokens=100)),
        )
        if previous is not None:
            link_child(previous, user)
        link_child(user, assistant)
        nodes.extend([user, assistant])
        previous = assistant
    return nodes
