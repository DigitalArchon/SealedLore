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
    from sealedlore.gui.fields import install_wheel_guard

    app = QApplication.instance() or QApplication([])
    install_wheel_guard(app)
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
def _delete_the_tests_windows():
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
