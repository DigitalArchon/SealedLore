"""One SealedLore at a time on a data folder: a second window, or a
command that writes, is refused while another process has the folder."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from sealedlore.storage import folder_lock

ROOT = Path(__file__).resolve().parents[1]

HOLDER = """
import sys
from pathlib import Path
from sealedlore.storage import folder_lock
folder_lock.hold(Path(sys.argv[1]))
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def held_elsewhere(tmp_path):
    """The data folder held by another process until the test ends."""
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "held"
        yield process
    finally:
        process.stdin.close()
        process.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="flock: Linux and macOS")
def test_a_folder_another_process_holds_is_refused_until_it_lets_go(tmp_path, held_elsewhere):
    with pytest.raises(folder_lock.FolderInUse, match="already running"):
        folder_lock.hold(tmp_path)
    held_elsewhere.stdin.close()
    held_elsewhere.wait(timeout=10)
    folder_lock.hold(tmp_path).release()


def test_another_folder_has_a_lock_of_its_own(tmp_path):
    first = folder_lock.hold(tmp_path / "one")
    second = folder_lock.hold(tmp_path / "two")
    assert first is not second
    first.release()
    second.release()


def test_held_twice_in_one_process_it_is_let_go_by_the_last(tmp_path):
    first = folder_lock.hold(tmp_path)
    again = folder_lock.hold(tmp_path)
    assert again is first
    again.release()
    assert first.fd is not None  # still held: the first holder hasn't let go
    first.release()
    assert first.fd is None


def test_a_folder_that_cant_be_locked_is_used_as_it_always_was(tmp_path, monkeypatch):
    def no_locks(fd: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(folder_lock, "_lock", no_locks)
    lock = folder_lock.hold(tmp_path)
    assert lock.fd is None
    lock.release()


@pytest.mark.skipif(sys.platform == "win32", reason="flock: Linux and macOS")
def test_a_command_that_writes_says_so_and_stops(tmp_path, held_elsewhere, capsys):
    from sealedlore.cli import main as cli_main

    assert cli_main(["--data-dir", str(tmp_path), "new", "Doomsville"]) == 1
    assert "already running" in capsys.readouterr().err
    assert not (tmp_path / "stories").exists()
    assert cli_main(["--data-dir", str(tmp_path), "list"]) == 0  # reading goes ahead


def test_a_command_lets_go_when_it_is_done(tmp_path):
    from sealedlore.cli import main as cli_main

    assert cli_main(["--data-dir", str(tmp_path), "new", "Doomsville"]) == 0
    assert os.path.realpath(tmp_path) not in folder_lock._held


@pytest.mark.skipif(sys.platform == "win32", reason="flock: Linux and macOS")
def test_a_second_window_says_the_app_is_running_and_never_opens(
    tmp_path, held_elsewhere, monkeypatch
):
    pytest.importorskip("PySide6")
    from sealedlore.gui import app

    told: list[tuple[str, str]] = []
    monkeypatch.setattr(app.x11_check, "check", lambda env, arguments: None)
    monkeypatch.setattr(app, "_no_core_dumps", lambda: None)
    monkeypatch.setattr(app, "_log_crashes", lambda root: None)
    monkeypatch.setattr(app, "build_app", lambda argv: None)
    monkeypatch.setattr(
        app.QMessageBox, "warning", lambda parent, title, text: told.append((title, text))
    )

    def no_window(**_):
        raise AssertionError("a second window was made")

    monkeypatch.setattr(app, "MainWindow", no_window)
    assert app.main(["sealedlore-gui", "--data-dir", str(tmp_path)]) == 1
    assert told and told[0][0] == "SealedLore is already running"
    assert str(tmp_path) in told[0][1]


def test_on_windows_the_first_byte_is_locked_with_msvcrt(tmp_path, monkeypatch):
    calls: list[tuple[int, int]] = []
    fake = types.SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, size: calls.append((mode, size))
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    fd = os.open(tmp_path / folder_lock.LOCK_FILE, os.O_RDWR | os.O_CREAT)
    try:
        folder_lock._lock(fd)
        folder_lock._unlock(fd)
    finally:
        os.close(fd)
    assert calls == [(fake.LK_NBLCK, 1), (fake.LK_UNLCK, 1)]


def test_on_windows_a_byte_locked_elsewhere_is_a_folder_in_use(tmp_path, monkeypatch):
    def locked(fd: int) -> None:
        raise OSError(errno.EACCES, "Permission denied")  # msvcrt's LK_NBLCK

    monkeypatch.setattr(folder_lock, "_lock", locked)
    with pytest.raises(folder_lock.FolderInUse):
        folder_lock.hold(tmp_path)
    assert os.path.realpath(tmp_path) not in folder_lock._held
