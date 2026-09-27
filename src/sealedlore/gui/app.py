"""GUI entry point.

Sets the Wayland app ID via setDesktopFileName so window matching works under
Wayland as well as X11.
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from sealedlore import __version__
from sealedlore.gui.fields import install_wheel_guard
from sealedlore.gui.main_window import MainWindow
from sealedlore.gui.text_size import load_stylesheet
from sealedlore.storage.atomic import open_private
from sealedlore.storage.paths import ensure_data_home

CRASH_LOG = "crash.log"

__all__ = ["build_app", "load_stylesheet", "main"]

APP_ID = "sealedlore"

# Preference order; the first family actually installed wins. Noto Sans leads
# because that is what the app was designed against — leaving a family ahead of
# it that happens to be absent would change the look the day someone installs
# it. Nothing is vendored, so this falls back to whatever the system provides.
FONT_CANDIDATES = ("Noto Sans", "DejaVu Sans", "Liberation Sans", "Cantarell")


def pick_font(app: QApplication) -> None:
    from PySide6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    for candidate in FONT_CANDIDATES:
        if candidate in families:
            font = app.font()
            font.setFamily(candidate)
            app.setFont(font)
            return


def build_app(argv: Sequence[str]) -> QApplication:
    QCoreApplication.setApplicationName("SealedLore")
    QCoreApplication.setOrganizationName("SealedLore")
    # Drives the Wayland app_id and the X11 WM_CLASS.
    QApplication.setDesktopFileName(APP_ID)

    app = QApplication(list(argv))
    # Before any widget and before the stylesheet (fields.install_wheel_guard).
    install_wheel_guard(app)
    app.setStyleSheet(load_stylesheet())
    pick_font(app)
    return app


PR_SET_DUMPABLE = 4
# Windows: no critical-error box, and no Windows Error Reporting dialog.
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002


def _no_core_dumps() -> None:
    """A native crash must not write the process's memory to disk: a
    memory-only chat or scene lives nowhere else. `RLIMIT_CORE` 0 stops a
    core file; not dumpable stops the system's collector (systemd-coredump
    reads the flag, not the limit) and, as a side effect, a debugger
    attaching from another process. faulthandler is unaffected: it runs
    inside the process (crash.log).

    Windows has neither. Its error reporting may still keep or send a
    minidump whatever the app asks, which the manual says; this only keeps
    its dialog away. (`ctypes.CDLL(None)` raises TypeError there.)"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
        except (OSError, AttributeError):
            pass
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _log_crashes(root: Path | None) -> None:
    """A native crash (a segfault in Qt) leaves no Python traceback.
    faulthandler appends every thread's Python stack to crash.log in the
    data folder: file and function names only, nothing from a story. The
    file is the author's alone, like the rest of the folder."""
    try:
        folder = ensure_data_home(root)
        # Kept open for the life of the process, as faulthandler needs.
        fd = open_private(folder / CRASH_LOG, os.O_WRONLY | os.O_APPEND)
        log = os.fdopen(fd, "a", encoding="utf-8")
    except OSError:
        return
    faulthandler.enable(file=log, all_threads=True)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="sealedlore-gui")
    parser.add_argument("--version", action="version", version=f"sealedlore {__version__}")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--mock", action="store_true", help="use the scripted mock provider (no endpoint needed)"
    )
    parser.add_argument("--story", default=None, help="open this story id on start")
    options, qt_arguments = parser.parse_known_args(arguments[1:])
    _no_core_dumps()
    _log_crashes(options.data_dir)

    app = build_app([arguments[0], *qt_arguments])
    window = MainWindow(root=options.data_dir, use_mock=options.mock)
    if options.story:
        window.open_story(options.story)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
