"""Before Qt starts on Linux: whether the X11 libraries Qt's xcb platform
plugin needs are installed, and if not, what to install.

PySide6's wheels don't bundle them. Qt 6.5+ then aborts at startup naming
only the first one it misses ("xcb-cursor0 or libxcb-cursor0 is needed"),
and once that one is installed it names none at all. Tested in the lab
(Oct 28 2026): a stock Linux Mint 22.3 on X11 lacks `libxcb-cursor0` alone;
Ubuntu 26.04 forced onto X11 lacks seven of these, and `libxcb-cursor0`
brings only three of them. A Wayland session doesn't load the plugin, so
nothing is checked there. The AppImage carries the libraries and puts them
on the library path, so the check passes there too.

Nothing here imports Qt: it runs before the first window is made.
"""

from __future__ import annotations

import ctypes
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

# Every library Qt's xcb plugin loads (its own needs and theirs), with the
# package that has it on Debian, Ubuntu and Mint; Fedora; and Arch (read from
# the package databases of the lab's machines).
LIBRARIES: dict[str, tuple[str, str, str]] = {
    "libxcb.so.1": ("libxcb1", "libxcb", "libxcb"),
    "libX11.so.6": ("libx11-6", "libX11", "libx11"),
    "libX11-xcb.so.1": ("libx11-xcb1", "libX11-xcb", "libx11"),
    "libxkbcommon.so.0": ("libxkbcommon0", "libxkbcommon", "libxkbcommon"),
    "libxkbcommon-x11.so.0": ("libxkbcommon-x11-0", "libxkbcommon-x11", "libxkbcommon-x11"),
    "libxcb-cursor.so.0": ("libxcb-cursor0", "xcb-util-cursor", "xcb-util-cursor"),
    "libxcb-icccm.so.4": ("libxcb-icccm4", "xcb-util-wm", "xcb-util-wm"),
    "libxcb-image.so.0": ("libxcb-image0", "xcb-util-image", "xcb-util-image"),
    "libxcb-keysyms.so.1": ("libxcb-keysyms1", "xcb-util-keysyms", "xcb-util-keysyms"),
    "libxcb-render-util.so.0": (
        "libxcb-render-util0",
        "xcb-util-renderutil",
        "xcb-util-renderutil",
    ),
    "libxcb-util.so.1": ("libxcb-util1", "xcb-util", "xcb-util"),
    "libxcb-xkb.so.1": ("libxcb-xkb1", "libxcb", "libxcb"),
    "libxcb-randr.so.0": ("libxcb-randr0", "libxcb", "libxcb"),
    "libxcb-render.so.0": ("libxcb-render0", "libxcb", "libxcb"),
    "libxcb-shape.so.0": ("libxcb-shape0", "libxcb", "libxcb"),
    "libxcb-shm.so.0": ("libxcb-shm0", "libxcb", "libxcb"),
    "libxcb-sync.so.1": ("libxcb-sync1", "libxcb", "libxcb"),
    "libxcb-xfixes.so.0": ("libxcb-xfixes0", "libxcb", "libxcb"),
}

# How each family installs, and which column of LIBRARIES is its.
INSTALLERS: dict[str, tuple[str, int]] = {
    "debian": ("sudo apt install", 0),
    "fedora": ("sudo dnf install", 1),
    "arch": ("sudo pacman -S", 2),
}
# The names /etc/os-release gives (ID, ID_LIKE) for each family.
FAMILIES: dict[str, str] = {
    "debian": "debian",
    "ubuntu": "debian",
    "linuxmint": "debian",
    "pop": "debian",
    "elementary": "debian",
    "zorin": "debian",
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "rocky": "fedora",
    "almalinux": "fedora",
    "nobara": "fedora",
    "arch": "arch",
    "cachyos": "arch",
    "manjaro": "arch",
    "endeavouros": "arch",
}


def platform_chosen(env: Mapping[str, str], qt_arguments: Sequence[str] = ()) -> str:
    """The platform Qt was told to use first (`-platform name` among Qt's
    arguments, else QT_QPA_PLATFORM: "wayland;xcb" tries wayland first;
    "xcb:option" is xcb), or "" when nobody said."""
    named = ""
    for index, argument in enumerate(qt_arguments):
        if argument == "-platform" and index + 1 < len(qt_arguments):
            named = qt_arguments[index + 1]
        elif argument.startswith("-platform="):
            named = argument.split("=", 1)[1]
    named = named or env.get("QT_QPA_PLATFORM", "")
    first = named.split(";")[0].strip()
    return first.split(":")[0].strip()


def uses_x11(env: Mapping[str, str], qt_arguments: Sequence[str] = ()) -> bool:
    """Whether Qt will load its xcb plugin: told to, or told nothing on a
    session that isn't Wayland (Qt picks Wayland on a Wayland session, as it
    did on every Wayland desktop in the lab, and xcb otherwise)."""
    chosen = platform_chosen(env, qt_arguments)
    if chosen:
        return chosen == "xcb"
    session = env.get("XDG_SESSION_TYPE", "")
    if session == "wayland" or (not session and env.get("WAYLAND_DISPLAY")):
        return False
    return True


def missing_libraries(load: Callable[[str], object] = ctypes.CDLL) -> list[str]:
    """The libraries the dynamic loader can't load, as Qt's plugin would
    load them (the library path the AppImage sets included)."""
    missing = []
    for soname in LIBRARIES:
        try:
            load(soname)
        except OSError:
            missing.append(soname)
    return missing


def family_of(os_release: str) -> str | None:
    """The system's family ("debian", "fedora", "arch") from the text of
    /etc/os-release: its ID, else the first of its ID_LIKE we know."""
    fields: dict[str, list[str]] = {}
    for line in os_release.splitlines():
        found = re.match(r"\s*(ID|ID_LIKE)\s*=\s*(.*)", line)
        if found:
            fields[found.group(1)] = found.group(2).strip().strip("\"'").lower().split()
    for name in (*fields.get("ID", []), *fields.get("ID_LIKE", [])):
        if name in FAMILIES:
            return FAMILIES[name]
    return None


def install_command(family: str | None, missing: Sequence[str]) -> str | None:
    """The one command that installs every missing library, or None for a
    system we don't know the packages of."""
    if family not in INSTALLERS:
        return None
    command, column = INSTALLERS[family]
    packages = list(dict.fromkeys(LIBRARIES[soname][column] for soname in missing))
    return f"{command} {' '.join(packages)}"


def message(missing: Sequence[str], family: str | None, env: Mapping[str, str]) -> str:
    """What to tell someone whose Qt can't start on X11."""
    lines = [
        "SealedLore can't start: Qt needs these X11 libraries, and they aren't installed:",
        "  " + ", ".join(missing),
    ]
    command = install_command(family, missing)
    if command:
        lines += ["Install them with:", f"  {command}"]
    else:
        lines.append("Install them with your system's package manager.")
    if env.get("WAYLAND_DISPLAY"):
        lines += ["Or start it on Wayland instead:", "  QT_QPA_PLATFORM=wayland sealedlore-gui"]
    lines.append("The AppImage carries these libraries, if you'd rather not install them.")
    return "\n".join(lines)


def check(
    env: Mapping[str, str],
    qt_arguments: Sequence[str] = (),
    *,
    load: Callable[[str], object] = ctypes.CDLL,
    os_release: str | None = None,
    platform: str = sys.platform,
) -> str | None:
    """The message to print and stop with, or None when Qt can start: not
    Linux, not X11, or every library there."""
    if not platform.startswith("linux") or not uses_x11(env, qt_arguments):
        return None
    missing = missing_libraries(load)
    if not missing:
        return None
    if os_release is None:
        try:
            os_release = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
        except OSError:
            os_release = ""
    return message(missing, family_of(os_release), env)
