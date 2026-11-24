"""Take out of PySide6 what SealedLore never loads.

Run by build.sh with the bundled Python, after the add-ons' trim:

    python trim_qt.py <site-packages>

What stays:
- the bindings the app imports (BINDINGS), QtTest beside them: the lab walk
  drives the AppImage's own window with it (SealedLoreDocs tools/lab_walk);
- every plugin of the kinds Qt loads for a desktop window, its look and input,
  pictures, the network and video (PLUGINS), less those whose libraries the
  wheels don't carry, which could never load (UNLOADABLE);
- the libraries those need, by their ELF NEEDED entries, and theirs in turn.
  A plugin is dlopen()ed, so nothing names it: the plugins kept are roots of
  that closure beside the bindings and shiboken, never found by it.

Everything else goes: the other bindings and their libraries, the type stubs,
QML's modules (nothing creates a QML engine), Qt's translations (the app
installs no translator), the metatypes the bindings were built with, and the
libraries outside the closure. Then every ELF file left is read again, and the
build stops if one needs a Qt library, or one the wheels carry, that isn't there.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# What the app imports (gui/), and QtTest for the lab walk.
BINDINGS = {"QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtMultimedia", "QtTest"}
PLUGINS = {
    # A window: on X11, Wayland, offscreen and Qt's other platforms, with their
    # GL and EGL integrations, Wayland's shells and decorations, and the input
    # devices the framebuffer platforms read.
    "platforms",
    "xcbglintegrations",
    "egldeviceintegrations",
    "generic",
    "wayland-shell-integration",
    "wayland-decoration-client",
    "wayland-graphics-integration-client",
    # The desktop's look and dialogs (GTK, the portal), input methods.
    "platformthemes",
    "platforminputcontexts",
    # Pictures and icons, SVG among them.
    "imageformats",
    "iconengines",
    # The network: TLS, and whether it is up.
    "tls",
    "networkinformation",
    # Video (gui/video_view.py): FFmpeg.
    "multimedia",
}
# Plugins of those kinds that need a library neither wheel carries in full:
# Qt could never load them, so they go too.
UNLOADABLE = {
    "imageformats/libqpdf.so": "libQt6Pdf, an add-on's",
    "platforminputcontexts/libqtvirtualkeyboardplugin.so": "libQt6VirtualKeyboard, an add-on's",
    "egldeviceintegrations/libqeglfs-kms-integration.so": "libQt6EglFsKmsGbmSupport, in neither",
}
# Folders under PySide6/Qt nothing reads at run time.
UNUSED = ("qml", "translations", "metatypes")
WHEELS = ("pyside6_essentials", "pyside6_addons", "shiboken6")


def is_elf(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(4) == b"\x7fELF"


def objdump_needed(path: Path) -> list[str]:
    """The libraries an ELF file names as NEEDED (`objdump -p`)."""
    said = subprocess.run(
        ["objdump", "-p", str(path)], capture_output=True, text=True, check=True
    ).stdout
    return [line.split()[1] for line in said.splitlines() if line.split()[:1] == ["NEEDED"]]


def size(path: Path) -> int:
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return path.stat().st_size if path.exists() else 0


def wheel_libraries(site: Path) -> set[str]:
    """The name of every library the Qt wheels install, by their RECORDs: what
    a kept file's NEEDED may name only if it is still there."""
    names = set()
    for wheel in WHEELS:
        for record in site.glob(f"{wheel}-*.dist-info/RECORD"):
            for row in csv.reader(record.read_text(encoding="utf-8").splitlines()):
                name = Path(row[0]).name
                if ".so" in name and not name.endswith((".py", ".pyc")):
                    names.add(name)
    return names


def trim(
    site: Path, needed: Callable[[Path], list[str]] = objdump_needed
) -> tuple[list[tuple[str, int]], list[str]]:
    """Trim PySide6 in `site`. Returns what went (a line and its bytes per
    group), and what is kept that needs a library that isn't there."""
    pyside, qt, shiboken = site / "PySide6", site / "PySide6" / "Qt", site / "shiboken6"
    plugins = qt / "plugins"
    carried = wheel_libraries(site)
    went: list[tuple[str, int]] = []

    def remove(label: str, paths: list[Path]) -> None:
        paths = sorted(p for p in paths if p.exists())
        total = sum(size(p) for p in paths)
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        went.append((f"{label}: {len(paths)}", total))

    remove(
        "bindings the app never imports",
        [p for p in pyside.glob("Qt*.abi3.so") if p.name.split(".")[0] not in BINDINGS],
    )
    remove("type stubs (.pyi)", list(pyside.glob("*.pyi")))
    for folder in UNUSED:
        remove(f"Qt/{folder}", [qt / folder])
    remove(
        "plugin kinds Qt never loads here",
        [p for p in plugins.iterdir() if p.is_dir() and p.name not in PLUGINS],
    )
    remove("plugins that could never load", [plugins / name for name in UNLOADABLE])

    libraries = {
        p.name: p
        for p in [*(qt / "lib").iterdir(), *pyside.glob("lib*.so*"), *shiboken.glob("lib*.so*")]
        if p.is_file()
    }
    roots = [
        *pyside.glob("Qt*.abi3.so"),
        *shiboken.glob("*.abi3.so"),
        *(p for p in plugins.rglob("*") if p.is_file() and is_elf(p)),
    ]
    wanted: set[str] = set()
    queue = sorted(roots)
    while queue:
        for name in needed(queue.pop()):
            if name in libraries and name not in wanted:
                wanted.add(name)
                queue.append(libraries[name])
    remove(
        "libraries nothing kept needs",
        [path for name, path in libraries.items() if name not in wanted],
    )
    for folder in sorted((p for p in pyside.rglob("*") if p.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()

    there = {p.name for p in [*pyside.rglob("*"), *shiboken.rglob("*")] if p.is_file()}
    missing = []
    for path in sorted(p for p in [*pyside.rglob("*"), *shiboken.rglob("*")] if p.is_file()):
        if not is_elf(path):
            continue
        for name in needed(path):
            if (name in carried or name.startswith("libQt6")) and name not in there:
                missing.append(f"{path.relative_to(site)} needs {name}")
    return went, missing


def main() -> None:
    site = Path(sys.argv[1])
    went, missing = trim(site)
    for line, total in went:
        print(f"  {line}, {total / 1e6:.1f} MB")
    print(f"PySide6 trimmed by {sum(total for _, total in went) / 1e6:.1f} MB")
    if missing:
        sys.exit(
            "kept, but needing a library that isn't there:\n  "
            + "\n  ".join(missing)
            + "\n(keep the library in packaging/appimage/trim_qt.py, or what needs it out)"
        )


if __name__ == "__main__":
    main()
