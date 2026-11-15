"""The app's file dialogs leave GTK's recent files list as it was (gui/file_dialogs.py, with
gui/recentguard.py, DA Assistant's copy): under Qt's GTK 3 platform theme a native
QFileDialog is GTK's own chooser, in this process, which adds every file chosen to
$XDG_DATA_HOME/recently-used.xbel and writes the whole list from its model a quarter of a
second later (an entry older than 30 days dropped, 0600); the guard puts back what that
write changed.

The timing is held here with a stand-in dialog and a list in a folder of the test's own;
then GTK's real dialog, under Xvfb in a process of its own (HOME and the XDG folders fresh
temporary ones, a session bus of its own), answered through the GTK that Qt loaded: the
list must be byte for byte as seeded a second after the choice and after the process has
ended - and, with the guard off, isn't. No test reads or writes the list of whoever runs
the suite."""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from urllib.parse import unquote

import pytest
from PySide6.QtCore import SIGNAL, QEventLoop, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QWidget

from sealedlore.gui import file_dialogs, recentguard

REPO = Path(__file__).resolve().parents[1]
GUI = REPO / "src" / "sealedlore" / "gui"
CANONICAL = REPO.parent / "DAAssistant" / "assistant" / "recentguard.py"


def now() -> str:
    """GLib's stamp for this moment, as GTK writes it when it adds a file."""
    moment = time.time()
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment)) + f".{int(moment % 1 * 1e6):06d}Z"
    )


posix = pytest.mark.skipif(sys.platform == "win32", reason="the list is GTK's: Linux")


def entry(href: str, stamp: str = "2026-09-01T10:00:00.000000Z", app: str = "org.gnome.TextEditor"):
    return (
        f'  <bookmark href="{href}" added="{stamp}" modified="{stamp}" visited="{stamp}">\n'
        f"    <info>\n"
        f'      <metadata owner="http://freedesktop.org">\n'
        f'        <mime:mime-type type="text/plain"/>\n'
        f"        <bookmark:applications>\n"
        f'          <bookmark:application name="{app}" exec="&apos;{app} %u&apos;" '
        f'modified="{stamp}" count="1"/>\n'
        f"        </bookmark:applications>\n"
        f"      </metadata>\n"
        f"    </info>\n"
        f"  </bookmark>\n"
    ).encode()


def xbel(*entries: bytes) -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<xbel version="1.0"\n'
        b'      xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"\n'
        b'      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info"\n'
        b">\n" + b"".join(entries) + b"</xbel>"
    )


def gtk_writes(path: Path, data: bytes) -> None:
    """As GLib's g_file_set_contents writes the list: a file of its own renamed over it,
    then 0600."""
    temp = path.with_name(path.name + ".XXXXXX")
    temp.write_bytes(data)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop, loop.quit)
    loop.exec()


def settle(guard, ms: int = 1500) -> None:
    """Until the guard is no longer pending, `ms` at most."""
    waited = 0
    while guard.pending and waited < ms:
        spin(50)
        waited += 50


class Dialog(QWidget):
    """A file dialog as run() sees one: exec() answers as GTK's does, the selection signals
    first, then accepted; `write` is GTK's write, before exec() returns or a quarter of a
    second after."""

    filesSelected = Signal(list)
    accepted = Signal()

    def __init__(self, paths, answer=QDialog.DialogCode.Accepted, write=None, at_once=False):
        super().__init__()
        self.paths, self.answer, self.write, self.at_once = paths, answer, write, at_once
        self.options: list = []

    def testOption(self, option):  # noqa: N802 - Qt's name
        return option in self.options

    def selectedFiles(self):  # noqa: N802 - Qt's name
        return list(self.paths)

    def told(self) -> int:
        """How many are connected to its answer."""
        return self.receivers(SIGNAL("filesSelected(QVariantList)")) + self.receivers(
            SIGNAL("accepted()")
        )

    def exec(self):
        if self.answer == QDialog.DialogCode.Accepted:
            self.filesSelected.emit(list(self.paths))
            self.accepted.emit()
            if self.write is not None:
                if self.at_once:
                    self.write()
                else:
                    QTimer.singleShot(250, self, self.write)
        return int(self.answer.value)


@pytest.fixture
def listed(tmp_path: Path, monkeypatch):
    """A data folder of the test's own with a seeded list, and the process's guard for it."""
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(file_dialogs, "_guard", None)
    monkeypatch.setattr(sys, "platform", "linux")
    recent = data / "recently-used.xbel"
    chosen = tmp_path / "home" / "notes.txt"
    chosen.parent.mkdir()
    chosen.write_text("notes\n", encoding="utf-8")
    seed = xbel(
        entry("file:///srv/report-from-august.txt", "2026-08-01T09:00:00.000000Z"),
        entry(chosen.as_uri(), "2026-11-05T12:00:00.000000Z"),
    )
    recent.write_bytes(seed)
    os.chmod(recent, 0o644)
    yield recent, chosen, seed
    made = file_dialogs._guard
    if isinstance(made, file_dialogs.Guard):
        made.watcher.removePaths(made.watcher.files() + made.watcher.directories())
        made.deleteLater()


def gtks_list(chosen: Path) -> bytes:
    """The list as GTK writes it after the choice: the old entry dropped, the file chosen
    added under GTK's name for the process."""
    return xbel(
        entry(chosen.as_uri(), "2026-11-05T12:00:00.000000Z"),
        entry(chosen.as_uri(), now(), "python"),
    )


@posix
def test_the_list_gtk_writes_after_a_dialog_answers_is_put_back_byte_for_byte(listed):
    recent, chosen, seed = listed
    dialog = Dialog([str(chosen)], write=lambda: gtk_writes(recent, gtks_list(chosen)))
    assert file_dialogs.run(dialog) == QDialog.DialogCode.Accepted
    guard = file_dialogs.guard()
    assert guard.pending
    assert guard.watcher.directories() == [str(recent.parent)]
    spin(1000)
    assert recent.read_bytes() == seed
    assert stat.S_IMODE(os.lstat(recent).st_mode) == 0o644
    assert not guard.pending, "GTK's write held the file chosen: nothing more is awaited"
    assert guard.watcher.files() + guard.watcher.directories() == []
    assert guard._timers == []


@posix
def test_the_clean_copy_is_the_list_as_the_dialog_answers_not_as_exec_returns(listed):
    """A Save over a file GTK asked about answers only some 10 ms before GTK's write: a list
    read once exec() had returned could already be GTK's."""
    recent, chosen, seed = listed
    dialog = Dialog(
        [str(chosen)], write=lambda: gtk_writes(recent, gtks_list(chosen)), at_once=True
    )
    file_dialogs.run(dialog)
    assert recent.read_bytes() != seed  # GTK's write, before exec() came back
    settle(file_dialogs.guard())
    assert recent.read_bytes() == seed


@posix
def test_another_programs_change_before_the_choice_is_kept(listed):
    recent, chosen, seed = listed
    theirs = seed.replace(b"</xbel>", entry("file:///srv/theirs.odt") + b"</xbel>")
    gtk_writes(recent, theirs)
    file_dialogs.run(Dialog([str(chosen)], write=lambda: gtk_writes(recent, gtks_list(chosen))))
    settle(file_dialogs.guard())
    assert recent.read_bytes() == theirs


@posix
def test_a_list_that_was_not_there_is_taken_away_again(listed):
    recent, chosen, _seed = listed
    recent.unlink()
    file_dialogs.run(Dialog([str(chosen)], write=lambda: gtk_writes(recent, gtks_list(chosen))))
    settle(file_dialogs.guard())
    assert not os.path.lexists(recent)


@posix
def test_a_write_holding_only_some_of_the_files_keeps_the_rest_awaited(listed, tmp_path):
    recent, chosen, seed = listed
    other = tmp_path / "home" / "other.txt"
    other.write_text("other\n", encoding="utf-8")
    dialog = Dialog([str(chosen), str(other)])
    file_dialogs.run(dialog)
    guard = file_dialogs.guard()
    gtk_writes(recent, gtks_list(chosen))
    spin(300)
    assert recent.read_bytes() == seed
    assert guard.pending
    gtk_writes(recent, xbel(entry(other.as_uri(), now(), "python")))
    settle(guard)
    assert recent.read_bytes() == seed
    assert not guard.pending


@posix
def test_where_gtk_never_writes_the_wait_ends_and_others_changes_count_again(listed, monkeypatch):
    """The user's recent files switched off: GTK writes an empty list over theirs, and adds
    nothing."""
    recent, chosen, seed = listed
    monkeypatch.setattr(recentguard, "HOLD_SECONDS", 0.4)
    file_dialogs.run(Dialog([str(chosen)], write=lambda: gtk_writes(recent, xbel())))
    guard = file_dialogs.guard()
    spin(300)
    assert recent.read_bytes() == seed
    assert guard.pending
    settle(guard)
    assert not guard.pending
    assert guard.watcher.files() + guard.watcher.directories() == []
    gtk_writes(recent, b"theirs")
    spin(100)
    assert recent.read_bytes() == b"theirs"


@posix
def test_a_dialog_left_or_qts_own_keeps_nothing(listed, monkeypatch):
    recent, chosen, _seed = listed
    made = []
    monkeypatch.setattr(file_dialogs, "guard", lambda: made.append(1))
    file_dialogs.run(Dialog([str(chosen)], answer=QDialog.DialogCode.Rejected))
    own = Dialog([str(chosen)])
    own.options.append(QFileDialog.Option.DontUseNativeDialog)
    file_dialogs.run(own)
    widgets = Dialog([str(chosen)])
    QDialogButtonBox(widgets).setObjectName("buttonBox")  # Qt's own dialog, its widgets
    file_dialogs.run(widgets)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    try:
        file_dialogs.run(Dialog([str(chosen)]))
    finally:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, False)
    assert made == []


def test_on_windows_the_dialog_is_only_shown(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(file_dialogs, "guard", lambda: pytest.fail("a guard made on Windows"))
    dialog = Dialog(["C:/x.txt"])
    assert file_dialogs.run(dialog) == QDialog.DialogCode.Accepted
    assert dialog.told() == 0  # nothing was connected


def test_on_linux_the_choice_is_told_and_nothing_stays_connected(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    told = []
    keeper = type("Keeper", (), {"chose": lambda self, paths: told.append(paths)})()
    monkeypatch.setattr(file_dialogs, "guard", lambda: keeper)
    dialog = Dialog(["/home/u/a.txt"])
    file_dialogs.run(dialog)
    assert told == [["/home/u/a.txt"], ["/home/u/a.txt"]]  # filesSelected, then accepted
    assert dialog.told() == 0


@posix
def test_what_a_list_holds_is_read_from_gtks_layout():
    stamp = "2026-11-15T04:00:00.250000Z"
    data = xbel(
        entry("file:///home/u/My%20Notes.txt", "2026-11-05T12:00:00.000000Z"),
        entry("file:///home/u/My%20Notes.txt", stamp, "python"),
        entry("file:///home/u/a&amp;b.txt", stamp, "python"),
    )
    held = file_dialogs.added(data)
    assert set(held) == {"/home/u/My Notes.txt", "/home/u/a&b.txt"}
    assert held["/home/u/My Notes.txt"] == 1_794_715_201  # 04:00:00.25 rounded up: GTK stamps
    # a file as it adds it, which can be a moment before the dialog answers
    assert file_dialogs.added(b'<bookmark href="file:///x" ' * 1000) == {}
    assert file_dialogs.added(None) == {}


# Qt's static functions, and what each is given and answers: the helper must make the same
# dialog and give the same answer.
CALLS = [
    ("getOpenFileName", "open_file_name", ("Open plot", "{home}", "Plot files (*.md);;All (*)")),
    ("getOpenFileName", "open_file_name", ("Open", "{home}/notes.txt", "", "All (*)")),
    ("getOpenFileNames", "open_file_names", ("Add pictures", "{home}", "Pictures (*.png)")),
    ("getSaveFileName", "save_file_name", ("Export", "{home}/story.json", "A (*.json);;B (*.md)")),
    (
        "getSaveFileName",
        "save_file_name",
        ("Export", "{home}/x.md", "A (*.json);;B (*.md)", "B (*.md)"),
    ),
    ("getExistingDirectory", "existing_directory", ("Save all pictures", "{home}")),
    ("getExistingDirectory", "existing_directory", ("Choose a folder",)),
]


REAL_EXEC = QFileDialog.exec  # before conftest makes an unanswered one fail


def _state(dialog: QFileDialog) -> dict:
    return {
        "title": dialog.windowTitle(),
        "parent": dialog.parent(),
        "mode": dialog.fileMode(),
        "accept": dialog.acceptMode(),
        "options": dialog.options(),
        "filters": dialog.nameFilters(),
        "filter": dialog.selectedNameFilter(),
        "folder": dialog.directoryUrl(),
        "selected": dialog.selectedFiles(),
        "schemes": dialog.supportedSchemes(),
    }


def answer_when_shown(target: Path | None, seen: list, tries: int = 200) -> None:
    """Once a file dialog is up (Qt's own widgets, offscreen): note how it was made, then
    choose `target` and accept, or leave it (None)."""
    dialog = QApplication.activeModalWidget()
    if not isinstance(dialog, QFileDialog):
        if tries:
            QTimer.singleShot(
                20, QApplication.instance(), lambda: answer_when_shown(target, seen, tries - 1)
            )
        return
    seen.append(_state(dialog))
    if target is None:
        dialog.reject()
    else:
        dialog.selectFile(str(target))
        dialog.accept()


@pytest.mark.parametrize("accept", [True, False])
@pytest.mark.parametrize(("static", "helper", "args"), CALLS)
def test_each_function_makes_and_answers_as_qts_static_one(
    static, helper, args, accept, tmp_path, monkeypatch
):
    """Qt's static function and the helper, each answered the same way: the same dialog
    (title, parent, mode, options, filters, folder, selection, schemes) and the same answer."""
    home = tmp_path / "home"
    (home / "pictures").mkdir(parents=True)
    (home / "notes.txt").write_text("notes\n", encoding="utf-8")
    args = tuple(arg.replace("{home}", str(home)) for arg in args)
    target = (
        {
            "getOpenFileName": home / "notes.txt",
            "getOpenFileNames": home / "notes.txt",
            "getSaveFileName": home / "new.md",
            "getExistingDirectory": home / "pictures",
        }[static]
        if accept
        else None
    )
    monkeypatch.setattr(QFileDialog, "exec", REAL_EXEC)
    monkeypatch.setattr(file_dialogs, "keeps_the_list", lambda: False)
    monkeypatch.chdir(home)  # where a dialog given no folder starts
    parent = QDialog()
    started: list = []  # Qt's last visited folder, where one given none starts: here
    QTimer.singleShot(0, parent, lambda: answer_when_shown(home / "notes.txt", started))
    QFileDialog.getOpenFileName(None, "", str(home))
    seen: list = []
    QTimer.singleShot(0, parent, lambda: answer_when_shown(target, seen))
    theirs = getattr(QFileDialog, static)(parent, *args)
    QTimer.singleShot(0, parent, lambda: answer_when_shown(target, seen))
    ours = getattr(file_dialogs, helper)(parent, *args)
    assert len(seen) == 2
    assert ours == theirs
    assert seen[0] == seen[1]
    if accept:
        assert ours and ours != ("", "")
    parent.deleteLater()


def called(function: ast.FunctionDef) -> set[str]:
    """The names a function calls, attributes (dialog.exec) by their last part."""
    return {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }


def test_no_file_dialog_of_the_app_goes_round_the_helper():
    """Only file_dialogs.py names QFileDialog, and every dialog it makes is shown by run(),
    which tells the guard as the dialog answers: Qt's static functions are never called."""
    for path in sorted(GUI.rglob("*.py")):
        if path.name != "file_dialogs.py":
            assert "QFileDialog" not in path.read_text(encoding="utf-8"), path.name
    tree = ast.parse((GUI / "file_dialogs.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    calls = {name: called(function) for name, function in functions.items()}
    assert [name for name, made in calls.items() if "exec" in made] == ["run"]
    assert [name for name, made in calls.items() if "run" in made] == ["_chosen"]
    assert [name for name, made in calls.items() if "QFileDialog" in made] == ["_made"]
    statics = ("getOpenFileName", "getOpenFileNames", "getSaveFileName", "getExistingDirectory")
    assert not any(made & set(statics) for made in calls.values())
    for name in ("open_file_name", "open_file_names", "save_file_name", "existing_directory"):
        through = calls[name] | (calls["_one"] if "_one" in calls[name] else set())
        assert {"_made", "_chosen"} <= through, name


@pytest.mark.skipif(
    not CANONICAL.is_file(), reason="DA Assistant's repository isn't beside this one"
)
def test_recentguard_is_da_assistants_copy_but_for_its_docstrings_last_paragraph():
    """recentguard.py is DA Assistant's (the DA apps' security standard), byte for byte but for
    the module docstring's last paragraph. A change is made there first, then copied here."""

    def parts(text: str) -> tuple[str, str]:
        first = ast.parse(text).body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
        lines = text.splitlines(keepends=True)
        docstring = "".join(lines[first.lineno - 1 : first.end_lineno])
        return docstring.rsplit("\n\n", 1)[0], "".join(lines[first.end_lineno :])

    ours = parts((GUI / "recentguard.py").read_text(encoding="utf-8"))
    theirs = parts(CANONICAL.read_text(encoding="utf-8"))
    assert ours[0] == theirs[0], (
        "the docstring differs from DA Assistant's before its last paragraph"
    )
    if ours[1] != theirs[1]:
        mine, canonical = ours[1].splitlines(), theirs[1].splitlines()
        first = next(
            (i for i, (a, b) in enumerate(zip(mine, canonical, strict=False)) if a != b),
            min(len(mine), len(canonical)),
        )
        pytest.fail(
            f"recentguard.py differs from DA Assistant's at line {first + 1} after the "
            f"docstring: {mine[first : first + 1]!r} here, {canonical[first : first + 1]!r} "
            "there. Change DA Assistant's copy first, then copy it here."
        )


SCRIPT = textwrap.dedent(
    """
    import ctypes, json, os, sys
    sys.path.insert(0, {src!r})
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from sealedlore.gui import file_dialogs, recentguard

    how, chosen, guarded = sys.argv[1], sys.argv[2], sys.argv[3] == "guarded"
    if not guarded:
        file_dialogs._guard = file_dialogs._NONE
    seen = {{"put_back": 0}}
    putting = recentguard.Keeper._put


    def counted(keeper, now):
        seen["put_back"] += 1
        putting(keeper, now)


    recentguard.Keeper._put = counted
    recent = os.path.join(os.environ["XDG_DATA_HOME"], "recently-used.xbel")

    def now():
        with open(recent, "rb") as f:
            return f.read().decode()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)


    class GList(ctypes.Structure):
        pass


    GList._fields_ = [("data", ctypes.c_void_p), ("next", ctypes.POINTER(GList)),
                      ("prev", ctypes.POINTER(GList))]
    tries = [0]


    folder = os.path.dirname(chosen)


    def answer():
        # GTK's own dialog, through the GTK Qt loaded: the file chosen, OK, and Replace
        tries[0] += 1
        if tries[0] > 200:
            seen["gave_up"] = True
            return
        try:
            gtk = ctypes.CDLL("libgtk-3.so.0", mode=os.RTLD_NOLOAD)
        except OSError:
            QTimer.singleShot(100, app, answer)
            return
        gobject = ctypes.CDLL("libgobject-2.0.so.0")
        gtk.gtk_window_list_toplevels.restype = ctypes.POINTER(GList)
        gtk.gtk_file_chooser_dialog_get_type.restype = ctypes.c_size_t
        gtk.gtk_message_dialog_get_type.restype = ctypes.c_size_t
        gobject.g_type_check_instance_is_a.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        gtk.gtk_widget_get_visible.argtypes = [ctypes.c_void_p]
        for name in ("select_filename", "set_filename", "set_current_folder"):
            getattr(gtk, "gtk_file_chooser_" + name).argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        gtk.gtk_file_chooser_get_filename.argtypes = [ctypes.c_void_p]
        gtk.gtk_file_chooser_get_filename.restype = ctypes.c_char_p
        gtk.gtk_dialog_response.argtypes = [ctypes.c_void_p, ctypes.c_int]
        node = gtk.gtk_window_list_toplevels()
        while node:
            widget = node.contents.data
            if gtk.gtk_widget_get_visible(widget):
                if gobject.g_type_check_instance_is_a(widget, gtk.gtk_message_dialog_get_type()):
                    seen["replace_asked"] = True
                    gtk.gtk_dialog_response(widget, -3)        # Replace
                elif ("answered" not in seen and gobject.g_type_check_instance_is_a(
                        widget, gtk.gtk_file_chooser_dialog_get_type())):
                    if how == "save":
                        gtk.gtk_file_chooser_set_filename(widget, chosen.encode())
                    elif how == "folder":
                        gtk.gtk_file_chooser_set_current_folder(widget, folder.encode())
                        gtk.gtk_file_chooser_select_filename(widget, chosen.encode())
                    else:
                        gtk.gtk_file_chooser_select_filename(widget, chosen.encode())
                    if gtk.gtk_file_chooser_get_filename(widget) == chosen.encode():
                        seen["answered"] = True
                        gtk.gtk_dialog_response(widget, -5)    # OK: Qt's accept
            node = node.contents.next
        QTimer.singleShot(100, app, answer)


    def choose():
        QTimer.singleShot(300, app, answer)
        if how == "open":
            seen["chosen"], _ = file_dialogs.open_file_name(None, "Open", folder, "Text (*.txt)")
        elif how == "save":
            seen["chosen"], _ = file_dialogs.save_file_name(None, "Save", chosen, "Text (*.txt)")
        else:
            seen["chosen"] = file_dialogs.existing_directory(None, "Folder", folder)
        seen["gtk"] = "answered" in seen
        QTimer.singleShot(1000, app, written)


    def written():
        seen["after_a_second"] = now()
        seen["pending"] = guarded and file_dialogs.guard().pending
        app.quit()


    QTimer.singleShot(0, app, choose)
    app.exec()
    seen["after_the_loop"] = now()
    print("RESULT", json.dumps(seen), flush=True)
    """
)


def _xcb_missing() -> list[str]:
    """The X11 libraries Qt's xcb plugin needs that this system lacks (a stock Ubuntu
    26.04 has seven of them missing, Nov 2026): GTK's dialog under Xvfb can't be shown."""
    if sys.platform != "linux":
        return []
    from sealedlore.gui.x11_check import missing_libraries

    return missing_libraries()


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("xvfb-run"), reason="needs Xvfb")
@pytest.mark.skipif(bool(_xcb_missing()), reason="Qt's xcb plugin can't load here")
class TestGtksOwnDialog:
    def choose(self, how: str, guarded: bool):
        top = tempfile.mkdtemp()
        try:
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "QT_QPA_PLATFORM": "xcb",
                "QT_QPA_PLATFORMTHEME": "gtk3",
                "XDG_CURRENT_DESKTOP": "GNOME",
                "NO_AT_BRIDGE": "1",
                "GSETTINGS_BACKEND": "memory",
                "GIO_USE_VFS": "local",
            }
            for name, sub in (
                ("HOME", "home"),
                ("XDG_DATA_HOME", "data"),
                ("XDG_CONFIG_HOME", "config"),
                ("XDG_CACHE_HOME", "cache"),
                ("XDG_STATE_HOME", "state"),
                ("XDG_RUNTIME_DIR", "run"),
            ):
                env[name] = os.path.join(top, sub)
                os.mkdir(env[name], 0o700)
            chosen = os.path.join(env["HOME"], "Projects" if how == "folder" else "notes.txt")
            if how == "folder":
                os.mkdir(chosen)
            else:
                Path(chosen).write_text("notes\n", encoding="utf-8")
            seed = xbel(
                entry("file:///srv/report-from-august.txt", "2026-08-01T09:00:00.000000Z"),
                entry("file:///srv/agenda.txt", now()),
                entry("file://" + chosen, "2026-11-05T12:00:00.000000Z"),
            )
            recent = Path(env["XDG_DATA_HOME"]) / "recently-used.xbel"
            recent.write_bytes(seed)
            os.chmod(recent, 0o644)
            script = Path(top) / "choose.py"
            script.write_text(SCRIPT.format(src=str(REPO / "src")), encoding="utf-8")
            command = ["xvfb-run", "-a"]
            if shutil.which("dbus-run-session"):
                command += ["dbus-run-session", "--"]
            command += [sys.executable, "-I", str(script), how, chosen]
            command.append("guarded" if guarded else "unguarded")
            done = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=180, check=False
            )
            results = [
                line[len("RESULT ") :]
                for line in done.stdout.splitlines()
                if line.startswith("RESULT ")
            ]
            assert done.returncode == 0 and len(results) == 1, (
                done.stdout[-3000:] + done.stderr[-3000:]
            )
            seen = json.loads(results[0])
            assert seen["gtk"], f"GTK's own dialog wasn't the one shown: {done.stderr[-3000:]}"
            assert seen["chosen"] == chosen
            seen["after_the_process"] = recent.read_bytes().decode()
            seen["mode"] = stat.S_IMODE(os.lstat(recent).st_mode)
            return seed.decode(), seen
        finally:
            shutil.rmtree(top, ignore_errors=True)

    def test_an_open_dialog_leaves_the_list_byte_for_byte_as_it_was(self):
        seed, seen = self.choose("open", guarded=True)
        for when in ("after_a_second", "after_the_loop", "after_the_process"):
            assert seen[when] == seed, when
        assert seen["put_back"] >= 1, "GTK wrote the list, and the guard put it back"
        assert not seen["pending"], "GTK's write held the file chosen: nothing more awaited"
        assert seen["mode"] == 0o644

    def test_a_save_over_a_file_gtk_asks_about_leaves_the_list_as_it_was(self):
        """GTK asks Replace it? and answers Qt only some 10 ms before it writes the list."""
        seed, seen = self.choose("save", guarded=True)
        assert seen["replace_asked"]
        for when in ("after_a_second", "after_the_loop", "after_the_process"):
            assert seen[when] == seed, when
        assert seen["put_back"] >= 1, "GTK wrote the list, and the guard put it back"
        assert not seen["pending"], "GTK's write held the file chosen: nothing more awaited"

    def test_a_folder_chosen_leaves_the_list_as_it_was(self):
        seed, seen = self.choose("folder", guarded=True)
        for when in ("after_a_second", "after_the_loop", "after_the_process"):
            assert seen[when] == seed, when
        assert seen["put_back"] >= 1, "GTK wrote the list, and the guard put it back"
        assert not seen["pending"], "GTK's write held the file chosen: nothing more awaited"

    def test_without_the_guard_gtk_changes_the_list(self):
        """What the guard undoes: the old entry dropped, the file chosen under "python"."""
        seed, seen = self.choose("open", guarded=False)
        for when in ("after_a_second", "after_the_loop", "after_the_process"):
            assert seen[when] != seed, when
            assert "report-from-august" not in seen[when]
            assert "agenda.txt" in seen[when]
            assert 'bookmark:application name="python"' in seen[when]
        assert seen["mode"] == 0o600
        assert seen["put_back"] == 0


QT_SETTINGS_CHILD = textwrap.dedent(
    """
    import os, sys
    import shiboken6
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QFileDialog
    from {package}.gui import file_dialogs

    aside = file_dialogs.settings_aside() if sys.argv[1] == "aside" else None
    print("ASIDE", aside or "", flush=True)
    app = QApplication(sys.argv)
    dialog = QFileDialog(None, "Open", sys.argv[2])
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    QTimer.singleShot(200, dialog.reject)
    dialog.exec()
    shiboken6.delete(dialog)        # Qt's widget dialog writes its folders as it is deleted
    """
)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
@pytest.mark.parametrize("aside", [True, False])
def test_qts_own_settings_are_kept_out_of_the_users_and_removed_after(tmp_path, aside):
    """With the settings set aside the user's QtProject.conf is byte for byte as it was
    and the folder set aside is gone once the process ends; without, Qt writes the folders
    the dialog visited into it (proving the test sees it)."""
    config, run, visited = tmp_path / "config", tmp_path / "run", tmp_path / "Private notes"
    for folder in (config, run, visited):
        folder.mkdir()
    run.chmod(0o700)
    users = config / "QtProject.conf"
    users.write_text("[FileDialog]\nlastVisited=file:///somewhere/of/the/users\n")
    before = users.read_bytes()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(config),
        "XDG_RUNTIME_DIR": str(run),
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    package = file_dialogs.__name__.split(".")[0]
    script = QT_SETTINGS_CHILD.format(package=package)
    done = subprocess.run(
        [sys.executable, "-c", script, "aside" if aside else "plain", str(visited)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    set_aside = next(
        line.split(" ", 1)[1] for line in done.stdout.splitlines() if line.startswith("ASIDE")
    )
    if aside:
        assert users.read_bytes() == before
        assert set_aside.startswith(str(run)) and not os.path.exists(set_aside)
    else:
        assert set_aside == ""
        assert users.read_bytes() != before and str(visited) in unquote(users.read_text())
