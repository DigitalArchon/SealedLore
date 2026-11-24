from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import uuid
from pathlib import Path

import pytest

from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.node import Node, NodeMeta, Usage
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story, StyleDirectives
from sealedlore.tree import link_child


@pytest.fixture(scope="session", autouse=True)
def _qts_settings_of_their_own(tmp_path_factory):
    """A QFileDialog keeps its folders in Qt's own settings (QtProject.conf) as it is
    deleted, the widgets' dialog and GTK's alike: the run's go to a folder of its own,
    never into the settings of whoever runs it."""
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        yield
        return
    folder = str(tmp_path_factory.mktemp("qt-settings"))
    for kind in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(kind, QSettings.Scope.UserScope, folder)
    yield


@pytest.fixture(scope="session", autouse=True)
def _wheel_guard_style_first(_qts_settings_of_their_own):
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
    from sealedlore.gui import plain_text, workers
    from sealedlore.gui.collector import install_collector_pause
    from sealedlore.gui.fields import install_wheel_guard

    app = QApplication.instance() or QApplication([])
    install_wheel_guard(app)
    # As the app does: labels plain unless rich on purpose, tooltips as given.
    plain_text.install()
    # As the app does: the collector waits while background threads run, and
    # fetches nobody waits for hand back through one relay.
    install_collector_pause(app)
    workers.install(app)
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
def _lorebooks_come_as_they_are(request, monkeypatch):
    """Bring their lorebook too opens a modal review of its entries: here
    every entry comes as it is. tests/test_lore_origin_gui.py drives it
    itself (marked `real_lore_pick`)."""
    if request.node.get_closest_marker("real_lore_pick"):
        yield
        return
    try:
        from PySide6.QtWidgets import QDialog

        from sealedlore.gui.lorebook_pick import LorebookPickDialog
    except ImportError:
        yield
        return
    monkeypatch.setattr(LorebookPickDialog, "exec", lambda dialog: QDialog.Accepted)
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
def _trash_of_its_own(monkeypatch, tmp_path_factory):
    """Nothing a test deletes reaches the desktop's trash. QFile.moveToTrash puts a
    test's folder in the real one wherever the test's temp folder shares the home
    folder's file system: a story every run, 391 of them in one trash before this.
    So it moves to a trash of the run's own, under pytest's temp folder."""
    try:
        from PySide6.QtCore import QFile
    except ImportError:
        yield
        return
    trash = tmp_path_factory.getbasetemp() / "trash"

    def to_trash(path: str) -> tuple[bool, str]:
        source = Path(path)
        if not source.exists() and not source.is_symlink():
            return (False, "")
        trash.mkdir(exist_ok=True)
        moved = trash / f"{uuid.uuid4().hex}-{source.name}"
        shutil.move(str(source), moved)
        return (True, str(moved))

    monkeypatch.setattr(QFile, "moveToTrash", staticmethod(to_trash))
    yield


@pytest.fixture(autouse=True)
def _unanswered_file_dialogs_fail(monkeypatch):
    """Every file dialog of the app is run by gui/file_dialogs.py; one no test answers
    (by patching file_dialogs.open_file_name and the rest) fails at once, naming it,
    rather than waiting forever offscreen."""
    try:
        from PySide6.QtWidgets import QFileDialog
    except ImportError:
        yield
        return

    def exec_(dialog):
        raise AssertionError(f"unanswered file dialog: {dialog.windowTitle()!r}")

    monkeypatch.setattr(QFileDialog, "exec", exec_)
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
def _every_save_checked(monkeypatch):
    """A story's files are made again only as far as their records changed
    (storage/record_files.py). Across the suite, every save then reads each
    file back and holds it to what writing it whole would have written: any
    change a save missed, by any path, fails the test that made it."""
    from sealedlore.storage import record_files

    monkeypatch.setattr(record_files, "CHECKED", True)
    yield


@pytest.fixture(autouse=True)
def _the_test_process_stays_dumpable(monkeypatch):
    """The command line's `main` makes its process undumpable, first thing
    (sealedlore/core_dumps.py): run in this process by the CLI's tests, it
    would leave the rest of the run so too, and a crash in it with no core
    for gdb. Its own tests run it in a process of their own
    (tests/test_core_dumps.py)."""
    from sealedlore import cli

    monkeypatch.setattr(cli, "no_core_dumps", lambda: None)
    yield


class NetworkInTest(OSError):
    """What a test that reaches past this computer meets (`_no_network`)."""


# Every outside connection and name lookup a test tries, for `_no_network`
# to fail the test with: one refused inside code that falls back quietly
# (a listing whose ProviderError is caught) would otherwise pass unseen.
_REACHED: list[str] = []


def _local_address(address) -> bool:
    if not isinstance(address, tuple):
        return True  # a unix socket's path
    host = str(address[0])
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _refused(what: str) -> NetworkInTest:
    _REACHED.append(what)
    return NetworkInTest(f"a test tried to reach {what}: tests never use the network")


def _guarded_connect(original):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _local_address(address):
            raise _refused(repr(address))
        return original(self, address)

    return connect


def _guarded_lookup(original):
    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _local_address((host,)):
            raise _refused(f"the name {host!r}")
        return original(host, *args, **kwargs)

    return getaddrinfo


# Installed when the tests are collected, so session fixtures and imports
# are held too; programs a test starts in a process of their own are not.
socket.socket.connect = _guarded_connect(socket.socket.connect)
socket.socket.connect_ex = _guarded_connect(socket.socket.connect_ex)
socket.getaddrinfo = _guarded_lookup(socket.getaddrinfo)


def _tokenizer_never_fetched() -> None:
    """tiktoken downloads its vocabulary the first time it is used where it
    has none cached (GitHub's runners, a fresh machine), and the window warms
    it for any test that shows one: the guard above then failed every such
    test. Tests read it only from a cache (CI fills one from the AppImage's
    pinned file, `tools/fetch_tokenizer.py`); with none, the estimator falls
    back to its approximate count, as the app does offline."""
    try:
        import tiktoken.load
    except ImportError:
        return
    reading = tiktoken.load.read_file

    def read_file(blobpath: str) -> bytes:
        if "://" in blobpath:
            raise OSError(f"tests never download tiktoken's files: {blobpath}")
        return reading(blobpath)

    tiktoken.load.read_file = read_file


_tokenizer_never_fetched()


@pytest.fixture(autouse=True)
def _no_network():
    """Tests never use the network (the project's rule, held here rather than
    by review): a test that tried, even where the code under it caught the
    refusal and went on, fails naming where it tried to go."""
    _REACHED.clear()
    yield
    reached, _REACHED[:] = list(_REACHED), []
    assert not reached, "this test tried to use the network: " + ", ".join(reached)


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


def pictures_read(widget, timeout: float = 20.0) -> None:
    """Every page and dialog under `widget` has read the pictures it lists.
    They are read a few a tick once it is built (gui/cast_board.py `Thumbs`),
    so a test that counts decodes starts once they are in."""
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while any(t.isActive() and t.interval() == 0 for t in widget.findChildren(QTimer)):
        assert time.monotonic() < deadline, "pictures still being read"
        app.processEvents()


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


def merge_old_chapters(session):  # noqa: ANN001, ANN201 - a StorySession; its new part or None
    """A merge of old chapters into a part, as the turns make one: sent off on
    its own thread (`start_background_merge`, a scripted provider standing in
    for its own detached client for this call alone) and taken in as the next
    archival takes it (`adopt_merge`). None when none is due or it failed
    (`merge_problem` says why)."""
    provider = session.provider
    lent = not callable(getattr(provider, "detached", None))
    if lent:
        provider.detached = lambda: provider
    try:
        if not session.start_background_merge():
            return None
    finally:
        if lent:
            del provider.detached
    assert session._merge_job.done.wait(5)
    return session.adopt_merge()
