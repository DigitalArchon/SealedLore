"""The check before Qt starts on Linux (gui/x11_check.py): on X11 with any of
Qt's xcb libraries missing, the app stops with what to install instead of
Qt's abort naming only the first."""

from __future__ import annotations

import pytest

from sealedlore.gui import x11_check
from sealedlore.gui.x11_check import (
    LIBRARIES,
    check,
    family_of,
    install_command,
    missing_libraries,
    uses_x11,
)

MINT = 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'
UBUNTU = "ID=ubuntu\nID_LIKE=debian\n"
FEDORA = 'NAME="Fedora Linux"\nID=fedora\n'
CACHYOS = "ID=cachyos\nID_LIKE=arch\n"
OMARCHY = "ID=omarchy\nID_LIKE=arch\n"
GENTOO = "ID=gentoo\n"
X11 = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
WAYLAND = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}


def loader(missing: set[str]):
    def load(soname: str) -> object:
        if soname in missing:
            raise OSError(f"{soname}: cannot open shared object file")
        return object()

    return load


@pytest.mark.parametrize(
    ("env", "arguments", "x11"),
    [
        (X11, (), True),
        (WAYLAND, (), False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, (), False),  # no session type: Qt goes by the display
        ({}, (), True),  # nothing said: Qt's default is xcb
        ({**WAYLAND, "QT_QPA_PLATFORM": "xcb"}, (), True),
        ({**WAYLAND, "QT_QPA_PLATFORM": "xcb:nograb"}, (), True),
        ({**X11, "QT_QPA_PLATFORM": "wayland;xcb"}, (), False),  # Omarchy's: wayland first
        ({**X11, "QT_QPA_PLATFORM": "offscreen"}, (), False),
        (WAYLAND, ("-platform", "xcb"), True),
        (X11, ("-platform=wayland",), False),
    ],
)
def test_x11_is_what_qt_will_load(env, arguments, x11):
    assert uses_x11(env, arguments) is x11


def test_a_missing_library_is_one_the_loader_refuses():
    assert missing_libraries(loader(set())) == []
    assert missing_libraries(loader({"libxcb-cursor.so.0", "libxcb-xkb.so.1"})) == [
        "libxcb-cursor.so.0",
        "libxcb-xkb.so.1",
    ]


def test_the_family_comes_from_os_release():
    assert family_of(MINT) == "debian"
    assert family_of(UBUNTU) == "debian"
    assert family_of(FEDORA) == "fedora"
    assert family_of(CACHYOS) == "arch"
    assert family_of(OMARCHY) == "arch"  # by its ID_LIKE
    assert family_of(GENTOO) is None
    assert family_of("") is None


def test_the_install_command_names_each_package_once():
    seven = [
        "libxcb-cursor.so.0",
        "libxkbcommon-x11.so.0",
        "libxcb-icccm.so.4",
        "libxcb-keysyms.so.1",
        "libxcb-xkb.so.1",
        "libxcb-image.so.0",
        "libxcb-render-util.so.0",
    ]
    # What a stock Ubuntu 26.04 lacked on X11, in the lab.
    assert install_command("debian", seven) == (
        "sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1 "
        "libxcb-xkb1 libxcb-image0 libxcb-render-util0"
    )
    assert install_command("fedora", ["libxcb-xkb.so.1", "libxcb-shm.so.0"]) == (
        "sudo dnf install libxcb"
    )
    assert install_command("arch", ["libxcb-cursor.so.0"]) == "sudo pacman -S xcb-util-cursor"
    assert install_command(None, seven) is None


def test_stock_mint_on_x11_is_told_to_install_libxcb_cursor0():
    told = check(X11, load=loader({"libxcb-cursor.so.0"}), os_release=MINT, platform="linux")
    assert told is not None
    assert "libxcb-cursor.so.0" in told and "sudo apt install libxcb-cursor0" in told
    assert "Wayland" not in told  # no Wayland display to offer
    assert "AppImage" in told


def test_an_x11_forced_on_a_wayland_session_is_offered_wayland_too():
    told = check(
        {**WAYLAND, "QT_QPA_PLATFORM": "xcb"},
        load=loader({"libxcb-cursor.so.0"}),
        os_release=FEDORA,
        platform="linux",
    )
    assert "sudo dnf install xcb-util-cursor" in told
    assert "QT_QPA_PLATFORM=wayland sealedlore-gui" in told


def test_an_unknown_system_is_told_the_libraries():
    told = check(X11, load=loader({"libxcb-icccm.so.4"}), os_release=GENTOO, platform="linux")
    assert "libxcb-icccm.so.4" in told and "package manager" in told


@pytest.mark.parametrize(
    ("env", "missing", "platform"),
    [
        (X11, set(), "linux"),  # everything there
        (WAYLAND, set(LIBRARIES), "linux"),  # Wayland needs none of them
        (X11, set(LIBRARIES), "win32"),
        (X11, set(LIBRARIES), "darwin"),
    ],
)
def test_nothing_is_said_when_qt_can_start(env, missing, platform):
    assert check(env, load=loader(missing), os_release=MINT, platform=platform) is None


def test_the_app_stops_before_qt_with_the_message(monkeypatch, capsys):
    pytest.importorskip("PySide6")
    from sealedlore.gui import app

    started = []
    monkeypatch.setattr(x11_check, "check", lambda env, arguments: "install libxcb-cursor0")
    monkeypatch.setattr(app, "build_app", lambda argv: started.append(argv))
    assert app.main(["sealedlore-gui", "--data-dir", "/nonexistent"]) == 1
    assert started == []  # Qt never started
    assert "install libxcb-cursor0" in capsys.readouterr().err


def test_this_machine_has_every_library_or_the_check_says_which():
    """The real loader: on a machine with Qt's X11 libraries (the developers'
    and CI's) nothing is missing; otherwise the list names real files."""
    missing = missing_libraries()
    assert set(missing) <= set(LIBRARIES)


def test_the_manuals_example_is_what_the_app_prints():
    from pathlib import Path

    from sealedlore.gui.x11_check import message

    manual = (Path(__file__).parent.parent / "docs" / "manual.md").read_text()
    printed = message(["libxcb-cursor.so.0"], "debian", {})
    assert f"```text\n{printed}\n```" in manual
