"""Running from source on Windows (docs/manual.md, "Windows (from source)").

Most of these run everywhere: the Windows branches are chosen by
`sys.platform` at call time, so they can be tested with it patched, and the
Linux side is checked to be what it was. The ACL and the error mode need a
real Windows (the manually run `windows.yml` workflow)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from sealedlore.storage import atomic, paths
from sealedlore.storage.atomic import append_jsonl, atomic_write_json, open_private
from sealedlore.storage.images import is_safe_relative, write_bytes_atomic

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

# Every byte Windows' text mode would change: LF, CR, and Ctrl-Z (read as end
# of file in text mode).
AWKWARD = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def test_a_picture_is_written_byte_for_byte(tmp_path: Path):
    """Through a raw descriptor in Windows' default text mode, every 0x0A
    became 0x0D 0x0A and the picture was ruined."""
    picture = tmp_path / "images" / "p.png"
    write_bytes_atomic(picture, AWKWARD)
    assert picture.read_bytes() == AWKWARD


def test_open_private_writes_a_newline_as_one_byte(tmp_path: Path):
    path = tmp_path / "raw"
    fd = open_private(path, os.O_WRONLY)
    os.write(fd, b"a\nb")
    os.close(fd)
    assert path.read_bytes() == b"a\nb"


def test_json_and_logs_end_lines_the_same_on_every_system(tmp_path: Path):
    """The data folder is the same bytes whichever system wrote it."""
    atomic_write_json(tmp_path / "nodes.json", {"a": [1, 2]}, keep_backup=True)
    append_jsonl(tmp_path / "api_log.jsonl", {"kind": "request"})
    append_jsonl(tmp_path / "api_log.jsonl", {"kind": "response"})
    assert b"\r" not in (tmp_path / "nodes.json").read_bytes()
    log = (tmp_path / "api_log.jsonl").read_bytes()
    assert log == b'{"kind": "request"}\n{"kind": "response"}\n'
    assert json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8")) == {"a": [1, 2]}


def test_on_windows_a_replace_waits_out_a_file_held_open(tmp_path: Path, monkeypatch):
    """A virus scanner holding nodes.json for a moment used to fail the save."""
    calls: list[int] = []
    real = os.replace

    def held_twice(source, target):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "in use")
        real(source, target)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(atomic.os, "replace", held_twice)
    monkeypatch.setattr(atomic.time, "sleep", lambda _s: None)
    atomic_write_json(tmp_path / "story.json", {"title": "x"})
    assert len(calls) == 3
    assert json.loads((tmp_path / "story.json").read_text(encoding="utf-8")) == {"title": "x"}


def test_on_windows_a_file_held_open_for_good_still_fails(tmp_path: Path, monkeypatch):
    def always_held(source, target):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(atomic.os, "replace", always_held)
    monkeypatch.setattr(atomic.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "story.json", {"title": "x"})
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32", reason="the Linux side")
def test_elsewhere_a_replace_is_tried_once(tmp_path: Path, monkeypatch):
    calls: list[int] = []

    def held(source, target):
        calls.append(1)
        raise PermissionError(13, "in use")

    monkeypatch.setattr(atomic.os, "replace", held)
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "story.json", {"title": "x"})
    assert len(calls) == 1


def test_the_windows_data_folder_is_local_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "/users/me/AppData/Local")
    assert paths.data_home() == Path("/users/me/AppData/Local") / "SealedLore"
    monkeypatch.setenv("XDG_DATA_HOME", "/elsewhere")
    assert paths.data_home() == Path("/elsewhere") / "sealedlore"


@pytest.mark.skipif(sys.platform == "win32", reason="the Linux side")
def test_the_linux_data_folder_is_where_it_was(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "/should/not/matter")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths.data_home() == Path.home() / ".local" / "share" / "sealedlore"
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    assert paths.data_home() == Path("/xdg") / "sealedlore"


def test_on_windows_a_picture_path_with_a_colon_is_refused(monkeypatch):
    """On NTFS, "images/a:b.png" is stream b.png of a file "a"."""
    assert is_safe_relative("images/a:b.png") is (sys.platform != "win32")
    monkeypatch.setattr(sys, "platform", "win32")
    assert not is_safe_relative("images/a:b.png")
    assert not is_safe_relative("images/C:x/p.png")
    assert is_safe_relative("images/refs/p.png")


def test_the_crash_guard_never_stops_the_app_starting(monkeypatch):
    """`ctypes.CDLL(None)` raises TypeError on Windows; the guard must not
    reach it there, and must not raise on a system without `windll`."""
    from sealedlore.gui import app

    monkeypatch.setattr(sys, "platform", "win32")
    app._no_core_dumps()


# --- a real Windows ---------------------------------------------------------------


@windows_only
def test_the_data_folder_is_the_owners_and_systems_alone(tmp_path: Path):
    from sealedlore.storage import winacl

    held = tmp_path / "data"
    held.mkdir()
    (held / "config.json").write_text("{}", encoding="utf-8")  # there before the ACL
    folder = paths.ensure_data_home(held)
    sid = winacl.user_sid()
    assert winacl.is_owner_only(folder, sid)
    append_jsonl(folder / "stories" / "s" / "api_log.jsonl", {"kind": "request"})
    for path in (folder / "config.json", folder / "stories" / "s" / "api_log.jsonl"):
        trustees = {entry.rsplit(";", 1)[1] for entry in winacl.entries(path)}
        assert trustees == {sid, winacl.SYSTEM_SID}, winacl.folder_sddl(path)


@windows_only
def test_the_windows_crash_guard_runs():
    from sealedlore.gui import app

    app._no_core_dumps()


def test_on_windows_the_stylesheet_sits_on_fusion(monkeypatch):
    """style.qss was drawn on Fusion; Windows' own styles widened the
    inspector by 108px on GitHub's runner."""
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.fields import _base_style

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(sys, "platform", "win32")
    assert _base_style(app) == "fusion"


def test_copied_text_is_kept_out_of_windows_clipboard_history(monkeypatch):
    """Win+V keeps what was copied, and "Sync across devices" uploads it."""
    import struct

    from PySide6.QtWidgets import QApplication

    from sealedlore.gui import winprivacy

    QApplication.instance() or QApplication([])
    elsewhere = winprivacy.private_mime("the prompt")
    if sys.platform != "win32":
        assert elsewhere.formats() == ["text/plain"], "nothing added off Windows"
    monkeypatch.setattr(sys, "platform", "win32")
    data = winprivacy.private_mime("the prompt")
    assert data.text() == "the prompt"
    zero = struct.pack("<I", 0)
    for name in (
        "ExcludeClipboardContentFromMonitorProcessing",
        "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard",
    ):
        assert bytes(data.data(f'application/x-qt-windows-mime;value="{name}"')) == zero


def test_capture_exclusion_is_windows_only_and_never_raises(monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget

    from sealedlore.gui.winprivacy import exclude_from_capture

    QApplication.instance() or QApplication([])
    widget = QWidget()
    if sys.platform != "win32":
        assert exclude_from_capture(widget, True) is False
        monkeypatch.setattr(sys, "platform", "win32")  # and no windll here
        assert exclude_from_capture(widget, True) is False
