"""Every file dialog of the app, and GTK's recent files list left as it was by them.

The functions are Qt's static ones (getOpenFileName, getOpenFileNames, getSaveFileName,
getExistingDirectory): the same arguments, the same answers, a QFileDialog made as Qt makes
one for them (qfiledialog.cpp: the folder or file the dialog starts at, the mode, the
options, file: URLs only, the filter chosen). They exist because a static call answers only
once it returns, and GTK may have written the list by then.

Under Qt's GTK 3 platform theme (GNOME, Cinnamon and the other GTK desktops) a native
QFileDialog is GTK's own GtkFileChooserDialog, run in the app's own process; the desktop
portal's dialog, out of process, refuses an app that isn't dumpable, and Qt falls back to
GTK's. GTK adds every file chosen in it to the desktop's recent files list
($XDG_DATA_HOME/recently-used.xbel, there for every other program to read) under the name
it has for the process ("python"), and a quarter of a second later writes the whole list
from its model: an entry older than 30 days dropped, the layout GTK's, the file left 0600.
Qt never runs GTK's main loop, so there is no write as the app quits (PySide6 6.11, measured
under Xvfb, Nov 15 2026).

recentguard.py, DA Assistant's canonical copy, puts back what those writes change. Its
Keeper is told here by Qt, since nothing in the app reaches GTK itself (no PyGObject):

  - as the dialog answers with files chosen (filesSelected and accepted, emitted from GTK's
    own response, before GTK's write: after a Save over a file GTK asks about, only some
    10 ms before it, measured, as both wait a quarter of a second and the answer's wait
    starts first), the list as it is - its bytes, or that there is none - is the clean
    copy, and a write is pending;
  - while one is, the list and its folder are watched (QFileSystemWatcher): a list that
    isn't the clean copy is put back, and the write stops being pending once a list that
    changed holds every file chosen as added since the choice, under whatever name added
    it, or HOLD_SECONDS (30 s) after the last choice where none does (the user's recent
    files switched off);
  - with none pending nothing is watched or put back: what another program writes before
    the next choice is the clean copy then.

Qt's own widget dialog (AA_DontUseNativeDialogs, as under firejail; DontUseNativeDialog; a
desktop that offers Qt no native dialog) writes no recent files, and nothing is kept for it.
On Windows and macOS the dialogs are shown as they are, nothing kept. All of it runs on the
GUI thread.

Qt's own settings are a second trace (`settings_aside`): every QFileDialog, GTK's and Qt's
own alike, writes the folders it visited (history, lastVisited, the sidebar's places; never
a file's name) to $XDG_CONFIG_HOME/QtProject.conf as it is deleted, a file every Qt program
the user runs reads. The app keeps no QSettings of its own, so on Linux Qt's user settings
go to a folder of the process's own in $XDG_RUNTIME_DIR (memory), removed as the app quits:
the user's file is neither read nor written, and the dialogs remember folders for the run.

The Qt DA apps' copies (SealedLore's gui/file_dialogs.py, the canonical one, and DA Media
Browser's) are the same file, but for the licence header DA Media Browser puts on every file.
"""

from __future__ import annotations

import atexit
import math
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from html import unescape
from urllib.parse import quote, unquote, urlsplit

import shiboken6
from PySide6.QtCore import (
    QCoreApplication,
    QFileInfo,
    QFileSystemWatcher,
    QObject,
    QSettings,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog

from . import recentguard

NO_OPTIONS = QFileDialog.Option(0)
# The schemes Qt's static functions allow: local files only.
LOCAL_FILES = ["file"]
# Whoever added a file chosen: GTK's name for the process is GTK's to choose.
ANY_NAME = "any"


def _say(text: str) -> None:
    try:
        print(f"[recent files] {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def keeps_the_list() -> bool:
    """Whether the dialogs keep GTK's recent files list as it was: on Linux."""
    return sys.platform.startswith("linux")


def _remove_aside(folder: str) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def settings_aside() -> str | None:
    """On Linux, Qt's user settings (QtProject.conf, where a dialog keeps the folders it
    visited) go to a new 0700 folder in $XDG_RUNTIME_DIR, else in the system's temporary
    folder, removed as the process ends; its path, or None where nothing was set aside.
    Before the first dialog (gui/app.py build_app). Never where the app keeps QSettings of
    its own: they would go there too."""
    if not keeps_the_list():
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    base = runtime if runtime and os.path.isdir(runtime) else None
    try:
        folder = tempfile.mkdtemp(prefix="qt-settings-", dir=base)
    except OSError as error:
        _say(f"Qt's settings were left where they are: {error}")
        return None
    for kind in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(kind, QSettings.Scope.UserScope, folder)
    atexit.register(_remove_aside, folder)
    return folder


def _uri(path: str) -> str:
    return "file://" + quote(os.path.abspath(path), errors="surrogateescape")


def _path_of(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.scheme != "file":
        return uri
    return os.path.normpath(unquote(parts.path, errors="surrogateescape"))


_ATTRIBUTE = {name: re.compile(rf'\s{name}="([^"]*)"') for name in ("href", "modified")}


def _attribute(tag: str, name: str) -> str | None:
    found = _ATTRIBUTE[name].search(tag)
    return unescape(found.group(1)) if found else None


def _seconds(stamp: str | None) -> int | None:
    """GLib's ISO 8601 time ("2026-11-15T04:00:00.123456Z"), as whole seconds rounded up:
    GTK stamps a file as it adds it, which can be a moment before the dialog answers."""
    try:
        return math.ceil(datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def added(data: bytes | None) -> dict[str, int]:
    """What a recent files list holds: each file's path, with the last time any program
    added it. Read in one pass (a list is at most recentguard.MOST); anything that isn't
    GTK's own layout is left out, which only keeps a write pending longer."""
    found: dict[str, int] = {}
    if not data:
        return found
    text = data.decode("utf-8", "replace")
    for piece in text.split("</bookmark>"):
        start = piece.rfind("<bookmark ")
        if start < 0:
            continue
        piece = piece[start:]
        end = piece.find(">")
        href = _attribute(piece[: end + 1], "href") if end >= 0 else None
        if not href:
            continue
        stamps = [0]
        for tag in piece.split("<bookmark:application ")[1:]:
            close = tag.find(">")
            if close >= 0:
                stamps.append(_seconds(_attribute(" " + tag[: close + 1], "modified")) or 0)
        if max(stamps):
            path = _path_of(href)
            found[path] = max(max(stamps), found.get(path, 0))
    return found


def _written(now: tuple) -> bytes | None:
    """The bytes of the list as recentguard.look saw it, a link's file's included."""
    if now[0] == recentguard.FILE:
        return now[1]
    if now[0] == recentguard.LINK and now[2][0] == recentguard.FILE:
        return now[2][1]
    return None


class _Item:
    def __init__(self, stamp: int):
        self._stamp = stamp

    def has_application(self, _name: str) -> bool:
        return True

    def get_application_info(self, _name: str) -> tuple:
        return (None, 1, self._stamp)


class Written:
    """GTK's recent manager as recentguard's Keeper sees it, told by the list's own writes:
    "changed" after each, and its model, the files a list that changed holds (none while
    the list is the clean copy)."""

    def __init__(self):
        self._handlers: list = []
        self.held: dict[str, int] = {}

    def connect_after(self, _name: str, handler) -> None:
        self._handlers.append(handler)

    def lookup_item(self, uri: str) -> _Item:
        stamp = self.held.get(_path_of(uri))
        if stamp is None:
            raise LookupError(f"not in the list: {uri}")
        return _Item(stamp)

    def changed(self, data: bytes | None) -> None:
        self.held = added(data)
        for handler in self._handlers:
            handler(self)


class Guard(QObject):
    """The list at `path` kept as it was through the app's file dialogs (see the module):
    a recentguard.Keeper, told by Qt."""

    def __init__(self, path: str, parent: QObject):
        super().__init__(parent)
        self._written = Written()
        self.keeper = recentguard.Keeper(self._written, path, ANY_NAME, later=self._later)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self.look)
        self.watcher.directoryChanged.connect(self.look)
        self._timers: list[QTimer] = []
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._last_look)

    @property
    def pending(self) -> bool:
        return self.keeper.pending

    def chose(self, paths) -> None:
        """A dialog answered with these files chosen (paths): GTK's write of them is pending."""
        self.keeper.chose([_uri(path) for path in paths if isinstance(path, str) and path])
        self._watch()

    def look(self, _path: str = "") -> None:
        """The list or its folder changed: put back, or the write is no longer pending."""
        try:
            now = recentguard.look(self.keeper.path)
            self._written.changed(_written(now) if now != self.keeper.clean else None)
        except Exception as e:  # noqa: BLE001 - never a crash: the list is left as it is
            _say(f"{self.keeper.path} not looked at: {e}")
        self._watch()

    def _last_look(self) -> None:
        if self.keeper.pending:
            self.look()

    def _watch(self) -> None:
        """The list and its folder watched while a write is pending, nothing otherwise. A
        list renamed into place (GTK's write, recentguard's) is a new file to watch."""
        try:
            if not self.keeper.pending:
                watched = self.watcher.files() + self.watcher.directories()
                if watched:
                    self.watcher.removePaths(watched)
                return
            path = self.keeper.path
            folder = os.path.dirname(path)
            if folder not in self.watcher.directories() and os.path.isdir(folder):
                self.watcher.addPath(folder)
            if path not in self.watcher.files() and os.path.lexists(path):
                self.watcher.addPath(path)
        except RuntimeError as e:  # the watcher gone with the application
            _say(f"{self.keeper.path} not watched: {e}")

    def _later(self, seconds: float, call):
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._held(timer, call))
        self._timers.append(timer)
        timer.start(max(0, int(seconds * 1000)))
        return lambda: self._drop(timer)

    def _held(self, timer: QTimer, call) -> None:
        self._drop(timer)
        call()
        self._watch()

    def _drop(self, timer: QTimer) -> None:
        if timer in self._timers:
            self._timers.remove(timer)
        if shiboken6.isValid(timer):
            timer.stop()
            timer.deleteLater()


_NONE = object()
_guard: object | None = None


def guard() -> Guard | None:
    """The process's Guard, made the first time a dialog answers; None where the list can't
    be kept (said once)."""
    global _guard
    if _guard is None:
        _guard = _NONE
        try:
            app = QCoreApplication.instance()
            if app is None:
                raise RuntimeError("there is no Qt application")
            _guard = Guard(os.path.join(recentguard.data_home(), recentguard.RECENT_FILES), app)
        except Exception as e:  # noqa: BLE001 - the list isn't kept: said, never a crash
            _say(f"the recent files list isn't kept as it was: {e}")
    return None if _guard is _NONE else _guard


def qts_own(dialog: QFileDialog) -> bool:
    """Whether Qt's own widgets are the dialog, not GTK's: they never touch the list."""
    return bool(
        QCoreApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
        or dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
        or dialog.findChild(QDialogButtonBox, "buttonBox") is not None
    )


def run(dialog: QFileDialog) -> int:
    """dialog.exec(), the files it chose kept out of GTK's recent files list: its answer. The
    keeper is told as the dialog answers, not once exec() has returned."""
    if not keeps_the_list():
        return dialog.exec()

    def answered(paths) -> None:
        try:
            if qts_own(dialog):
                return
            keeping = guard()
            if keeping is not None:
                keeping.chose(list(paths))
        except Exception as e:  # noqa: BLE001 - never a crash: the dialog answers as ever
            _say(f"a choice not kept out of the list: {e}")

    told = [
        dialog.filesSelected.connect(answered),
        dialog.accepted.connect(lambda: answered(dialog.selectedFiles())),
    ]
    try:
        return dialog.exec()
    finally:
        if shiboken6.isValid(dialog):
            for connection in told:
                QObject.disconnect(connection)


def _selection(directory: str) -> str:
    """The file a static call starts with selected: the last part of `directory` where it
    names no folder (QFileDialogArgs)."""
    url = QUrl.fromLocalFile(directory)
    if url.isEmpty():
        return ""
    local = QFileInfo(url.toLocalFile())
    return "" if local.isDir() else local.fileName()


def _made(parent, caption, directory, filters, mode, options) -> QFileDialog:
    dialog = QFileDialog(parent, caption, directory, filters)
    dialog.setFileMode(mode)
    dialog.setOptions(options)
    selection = _selection(directory)
    if selection:
        dialog.selectFile(selection)
    dialog.setSupportedSchemes(LOCAL_FILES)
    return dialog


def _chosen(dialog: QFileDialog, selected_filter: str) -> tuple[list[QUrl] | None, str]:
    """Run the dialog and free it: what it chose (None when left) and the filter."""
    try:
        if run(dialog) == QDialog.DialogCode.Accepted and shiboken6.isValid(dialog):
            return dialog.selectedUrls(), dialog.selectedNameFilter()
        return None, selected_filter
    finally:
        if shiboken6.isValid(dialog):
            dialog.deleteLater()


def _local(url: QUrl) -> str:
    return url.toLocalFile() if url.isLocalFile() or url.isEmpty() else url.toString()


def _one(parent, caption, directory, filters, selected_filter, options, mode, save=False):
    dialog = _made(parent, caption, directory, filters, mode, options)
    if save:
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    if selected_filter:
        dialog.selectNameFilter(selected_filter)
    urls, chosen_filter = _chosen(dialog, selected_filter)
    return (_local(urls[0]) if urls else ""), chosen_filter


def open_file_name(
    parent=None, caption="", directory="", filters="", selected_filter="", options=NO_OPTIONS
) -> tuple[str, str]:
    """QFileDialog.getOpenFileName: (the file, the filter chosen); ("", the filter given)
    when left."""
    mode = QFileDialog.FileMode.ExistingFile
    return _one(parent, caption, directory, filters, selected_filter, options, mode)


def open_file_names(
    parent=None, caption="", directory="", filters="", selected_filter="", options=NO_OPTIONS
) -> tuple[list[str], str]:
    """QFileDialog.getOpenFileNames: ([the files], the filter chosen)."""
    dialog = _made(parent, caption, directory, filters, QFileDialog.FileMode.ExistingFiles, options)
    if selected_filter:
        dialog.selectNameFilter(selected_filter)
    urls, chosen_filter = _chosen(dialog, selected_filter)
    return [_local(url) for url in urls or []], chosen_filter


def save_file_name(
    parent=None, caption="", directory="", filters="", selected_filter="", options=NO_OPTIONS
) -> tuple[str, str]:
    """QFileDialog.getSaveFileName: (the file, the filter chosen)."""
    mode = QFileDialog.FileMode.AnyFile
    return _one(parent, caption, directory, filters, selected_filter, options, mode, save=True)


def existing_directory(
    parent=None, caption="", directory="", options=QFileDialog.Option.ShowDirsOnly
) -> str:
    """QFileDialog.getExistingDirectory: the folder, "" when left."""
    dialog = _made(parent, caption, directory, "", QFileDialog.FileMode.Directory, options)
    urls, _ = _chosen(dialog, "")
    return _local(urls[0]) if urls else ""
