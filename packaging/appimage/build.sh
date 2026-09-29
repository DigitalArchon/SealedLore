#!/usr/bin/env bash
# Build SealedLore as a single-file AppImage: dist/SealedLore-<version>-x86_64.AppImage
# and its checksum beside it (….AppImage.sha256).
#
# Runs on any x86_64 Linux with bash, git, curl, tar, dpkg-deb, sha256sum,
# file, objdump (binutils) and ldd. Downloads (HTTPS only, every one checked
# against a pinned sum or hash) are cached in build/appimage/cache, so a
# rebuild after a code change is quick.
#
# The build is reproducible: the same commit gives the same bytes, on any
# machine, in any folder, so anyone can rebuild a release and compare its
# checksum with the published one. What makes it so:
#   - it builds the commit, not the working tree (git archive; uncommitted
#     changes to tracked files stop the build), and every timestamp inside is
#     the commit's time (SOURCE_DATE_EPOCH);
#   - every input is pinned by checksum: the Python base, the Debian
#     libraries, uv, appimagetool and the AppImage runtime (tools.sh), every
#     package by hash (requirements.lock, from requirements.in by lock.sh),
#     and the build backend (build-requirements.lock);
#   - nothing of the builder's machine gets in: no file owners, modes or
#     extended attributes, no pip or uv settings, no build-folder paths (pip
#     writes them into console scripts and direct_url.json; both go, and the
#     build fails if any path is left). Compiled .pyc files are hash-checked,
#     not dated, and compiled in one process with a fixed hash seed.
#
# What goes in:
#   - a relocatable CPython 3.12 built for glibc 2.28 (python-appimage). The
#     floor that matters is PySide6's: 6.10 and later are built for glibc 2.34,
#     so the result runs on Ubuntu 22.04 / Debian 12 / Fedora 35 / RHEL 9 and
#     newer. PySide6 6.9 would reach glibc 2.28, but it isn't what the tests
#     run against;
#   - SealedLore and its packages, at the versions this repo is tested with,
#     and PySide6-Essentials rather than PySide6: the add-ons are hundreds of
#     megabytes the app never imports;
#   - the X11 helper libraries Qt's xcb plugin needs and many systems lack,
#     taken from Debian 11 (built for glibc <= 2.17) rather than from whatever
#     machine runs this script — a host's copy can need a newer glibc than the
#     systems the AppImage is for (Ubuntu 24.04's libxcb-cursor needs 2.38);
#   - the tokenizer's data file, so the first run on a new machine works offline;
#   - SealedLore's licence and the third-party notices (THIRD-PARTY-NOTICES.md,
#     with the licence texts in licenses/ and each Debian library's copyright
#     file) in usr/share/licenses/sealedlore. The build fails if a Python
#     package ships without a licence text (notices.py).
#
# Bumping the Python base, a Debian library or the runtime: update the versions
# THIRD-PARTY-NOTICES.md names, and the texts in licenses/ if they changed.
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/appimage/tools.sh
source "$HERE/tools.sh"
APPDIR="$BUILD/SealedLore.AppDir"
SRC="$BUILD/source"
DIST="$REPO/dist"

PYTHON_APPIMAGE="python3.12.14-cp312-cp312-manylinux_2_28_x86_64.AppImage"
PYTHON_URL="https://github.com/niess/python-appimage/releases/download/python3.12/$PYTHON_APPIMAGE"
PYTHON_SHA256="cefdd1b6e08dfb6c977d4233a4177ed3ee55a526991a122c8d92263f5901544f"
DEBIAN_POOL="https://deb.debian.org/debian/pool/main"
DEBIAN_LIBS=(
    # path in the pool                                              sha256
    "x/xcb-util-cursor/libxcb-cursor0_0.1.1-4_amd64.deb             bab731cd0143303f77461dd0a03ad20807bd3d767d5d6af6a11ff99c3b7aeac3"
    "x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1_amd64.deb                f323194cb04cd4e5ae064fafec39db6dcf8a431cbd65a0bc53fa6c359862d8ff"
    "x/xcb-util-image/libxcb-image0_0.4.0-1+b3_amd64.deb            36a381bb18c9f349a53457c66b3d1825631f3e50c4f6c12326cf05257302172d"
    "x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1+b2_amd64.deb        aed1436db9a3e63b10d00c4ed16248b5c82b5dd2963a83a761f406af65eb4b49"
    "x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1+b1_amd64.deb be4b38a63e65c84e2f1322f044d05a9baa677e0f3dc68b742a0a109a3ff40ae9"
    "x/xcb-util/libxcb-util1_0.4.0-1+b1_amd64.deb                   4c48af51fb2ac1be0490067e7450aeda27bf6c6c395165de02199eee4835336f"
    # Qt calls only xkbcommon's 0.5-era API, all present in 1.0.3; bundling the
    # pair keeps xkbcommon-x11 matched to the xkbcommon it was built with.
    "libx/libxkbcommon/libxkbcommon0_1.0.3-2_amd64.deb              d74d0b9f0a6641b44c279644c7ac627fa7a9b92350b7c6ff37da94352885bcfc"
    "libx/libxkbcommon/libxkbcommon-x11-0_1.0.3-2_amd64.deb         c786f80d1a5405e96167ebbebdd7d100b356c0a3ae0f87fb6f65f05f2e723f72"
)

# The oldest glibc the AppImage runs on; anything inside needing more fails the build.
GLIBC_FLOOR="2.34"

log() { printf '\n==> %s\n' "$*"; }

log "Source: the commit"
if ! git -C "$REPO" diff --quiet HEAD --; then
    echo "uncommitted changes to tracked files: the build is of a commit, so commit" >&2
    echo "them (or stash them) first" >&2
    exit 1
fi
COMMIT="$(git -C "$REPO" rev-parse HEAD)"
SOURCE_DATE_EPOCH="$(git -C "$REPO" log -1 --format=%ct HEAD)"
export SOURCE_DATE_EPOCH
rm -rf "$SRC" "$APPDIR" "$BUILD/wheel"
mkdir -p "$SRC" "$CACHE" "$DIST"
git -C "$REPO" archive HEAD | tar -x -C "$SRC"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)"
OUTPUT="$DIST/SealedLore-$VERSION-x86_64.AppImage"
echo "$COMMIT, version $VERSION"

log "Python base"
fetch "$PYTHON_URL" "$CACHE/$PYTHON_APPIMAGE" "$PYTHON_SHA256"
chmod +x "$CACHE/$PYTHON_APPIMAGE"
# --appimage-extract needs no FUSE, so this works in containers too.
(cd "$BUILD" && rm -rf squashfs-root && "$CACHE/$PYTHON_APPIMAGE" --appimage-extract >/dev/null)
mv "$BUILD/squashfs-root" "$APPDIR"
# Its own launcher, desktop entry and icon give way to SealedLore's.
rm -f "$APPDIR"/AppRun "$APPDIR"/*.desktop "$APPDIR"/*.png "$APPDIR"/.DirIcon
rm -rf "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" "$APPDIR/usr/share/icons"
PYTHON="$APPDIR/opt/python3.12/bin/python3.12"

log "SealedLore and its packages"
# No .pyc is written while building (they are compiled once, at the end), and
# nothing of this machine's Python or pip settings applies.
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0
unset PYTHONHOME PYTHONPATH
BIN="$APPDIR/opt/python3.12/bin"
base_bin="$(ls "$BIN")"
PIP=("$PYTHON" -s -m pip install --quiet --isolated --disable-pip-version-check
     --no-warn-script-location --no-compile --no-deps --only-binary=:all:
     --cache-dir "$CACHE/pip")
# Every package, by hash; pip refuses anything the lock doesn't name.
"${PIP[@]}" --require-hashes -r "$SRC/packaging/appimage/requirements.lock"
# SealedLore itself, as a wheel built with the pinned backend. --no-deps: the
# project names PySide6 (the whole thing); Essentials is what it actually
# uses, installed above.
fetch_uv
"$UV" build --quiet --wheel --python "$PYTHON" --out-dir "$BUILD/wheel" \
    --build-constraints "$SRC/packaging/appimage/build-requirements.lock" --require-hashes "$SRC"
"${PIP[@]}" "$BUILD"/wheel/sealedlore-"$VERSION"-*.whl
"$PYTHON" -s -c "import sealedlore.gui.app, PySide6.QtWidgets, tiktoken, numpy, httpx, pydantic"
SITE="$("$PYTHON" -s -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
# pip writes the build folder's path into the console scripts it adds (their
# first line) and into direct_url.json, and each one's hash into the package's
# RECORD. AppRun runs `python -m`, so the scripts go; direct_url.json only
# says where the wheel was installed from. Their RECORD lines go with them:
# left in, the scripts' hashes still differed from one folder to another.
for script in "$BIN"/*; do
    name="$(basename "$script")"
    grep -qxF "$name" <<<"$base_bin" && continue
    rm -f "$script"
    sed -i "\\|^\\.\\./\\.\\./\\.\\./bin/$name,|d" "$SITE"/*.dist-info/RECORD
done
find "$SITE" -path '*.dist-info/direct_url.json' -delete
sed -i '/\/direct_url\.json,/d' "$SITE"/*.dist-info/RECORD

log "Trimming what the app never loads"
QT="$SITE/PySide6"
# Of the add-ons, only video playback (gui/video_view.py): QtMultimedia, its
# FFmpeg plugin, and FFmpeg's libraries by the name they are loaded by (the
# wheel carries each three times over, as .so, .so.N and .so.N.x.y, since a
# wheel can't hold links). Everything else the add-ons put in is deleted, by
# the add-ons' own list of their files (less those Essentials installs too),
# and a file kept that isn't there
# stops the build. The add-ons' dist-info stays, for the notices.
"$PYTHON" -s - "$SITE" <<'ADDONS'
import csv
import re
import sys
from pathlib import Path

site = Path(sys.argv[1])
record = next(site.glob("pyside6_addons-*.dist-info")) / "RECORD"
# Files both wheels install (PySide6/__init__.py among them) are Essentials'.
essentials = {
    row[0]
    for row in csv.reader(
        (next(site.glob("pyside6_essentials-*.dist-info")) / "RECORD").read_text().splitlines()
    )
}
KEEP = [
    "PySide6/QtMultimedia.abi3.so",
    "PySide6/Qt/lib/libQt6Multimedia.so.6",
    "PySide6/Qt/plugins/multimedia/libffmpegmediaplugin.so",
    r"PySide6/Qt/lib/libQt6FFmpegStub-[a-z-]+\.so\.\d+",
    r"PySide6/Qt/lib/lib(avcodec|avformat|avutil|swresample|swscale)\.so\.\d+",
]
keep = [re.compile(pattern.replace(".abi3", r"\.abi3") + "$") for pattern in KEEP]
kept, removed = set(), 0
for row in csv.reader(record.read_text().splitlines()):
    name = row[0]
    if ".dist-info/" in name or name in essentials:
        continue
    if any(pattern.fullmatch(name) for pattern in keep):
        kept.add(name)
        continue
    path = site / name
    if path.is_file():
        path.unlink()
        removed += 1
wanted = {"libQt6Multimedia", "libffmpegmediaplugin", "QtMultimedia.abi3", "libavcodec", "libavformat",
          "libavutil", "libswresample", "libswscale"}
found = {Path(name).name.split(".so")[0] for name in kept}
if not wanted <= found:
    sys.exit(f"the add-ons lack what video needs: {sorted(wanted - found)}")
print(f"kept {len(kept)} of the add-ons' files, removed {removed}")
ADDONS
find "$QT" -type d -empty -delete
# Qt's developer tools and headers: designer, linguist, the QML tooling.
rm -rf "$QT"/{include,typesystems,glue,scripts,examples,doc} \
       "$QT"/{assistant,designer,linguist,lrelease,lupdate,qmlformat,qmllint,qmlls,balsam,balsamui} \
       "$QT"/Qt/libexec "$QT"/Qt/plugins/designer "$QT"/Qt/plugins/qmltooling
"$PYTHON" -s -m pip uninstall --quiet --yes pip setuptools wheel 2>/dev/null || true
# Parts of the Python base the app never imports, with the libraries only they
# use: readline and gdbm are GPL, crypt's libxcrypt is LGPL, and Tk is the
# toolkit the app doesn't use. Fewer licences to carry, and ~8 MB less.
STDLIB="$APPDIR/opt/python3.12/lib/python3.12"
rm -f "$STDLIB"/lib-dynload/{readline,_gdbm,_dbm,_crypt,_curses,_curses_panel,_tkinter}.cpython-*.so
rm -f "$APPDIR"/usr/lib/{libreadline,libgdbm,libgdbm_compat,libcrypt,libncursesw,libpanelw,libtinfo,libtcl8.6,libtk8.6,libXft,libXrender}.so*
rm -rf "$APPDIR/usr/share/tcltk" "$STDLIB"/{tkinter,idlelib,turtledemo,turtle.py}
# The base's usr/bin links to pip, gone now.
find "$APPDIR/usr/bin" -xtype l -delete
find "$APPDIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
# Every Python file, the standard library and the packages with SealedLore's
# own: the AppImage is mounted read-only, so what isn't compiled here is
# compiled again at every launch and never kept (with only SealedLore's
# compiled, that was more than half the time the imports took).
# Hash-checked rather than dated, named without the build folder (-s), and in
# this one process: marshal's output can depend on what the process compiled
# before, so parallel workers (-j) would make it vary. compileall takes each
# folder's files in sorted order.
"$PYTHON" -s -m compileall -q --invalidation-mode checked-hash -s "$APPDIR" "$STDLIB" >/dev/null
# None left out (a file that doesn't compile stops the build above), and none
# without its source: Python runs the compiled file, so one with no source
# beside it to be checked against is code nobody can read.
uncompiled="$("$PYTHON" -s - "$APPDIR" <<'CHECK'
import sys
from importlib.util import cache_from_source
from pathlib import Path

root = Path(sys.argv[1])
sources = {path for path in root.rglob("*.py") if path.is_file()}
compiled = {path for path in root.rglob("*.pyc")}
expected = {Path(cache_from_source(str(path))) for path in sources}
for path in sorted(expected - compiled):
    print(f"not compiled: {path.relative_to(root)}")
for path in sorted(compiled - expected):
    print(f"compiled, with no source: {path.relative_to(root)}")
CHECK
)"
if [ -n "$uncompiled" ]; then
    echo "$uncompiled" >&2
    exit 1
fi

log "X11 helper libraries (Debian 11)"
# Their own folder, the only one AppRun puts on LD_LIBRARY_PATH. usr/lib holds
# the Python base's libraries (an old liblzma, libtinfo, OpenSSL 1.1), which
# Python finds through its RUNPATH; exported, they would override the system's
# copies for everything Qt loads, libsystemd included.
X11LIB="$APPDIR/usr/lib/x11"
LICENCES="$APPDIR/usr/share/licenses/sealedlore"
mkdir -p "$X11LIB" "$CACHE/debs"
for entry in "${DEBIAN_LIBS[@]}"; do
    read -r path sum <<<"$entry"
    deb="$CACHE/debs/$(basename "$path")"
    fetch "$DEBIAN_POOL/$path" "$deb" "$sum"
    rm -rf "$CACHE/debs/x" && dpkg-deb -x "$deb" "$CACHE/debs/x"
    cp -a "$CACHE"/debs/x/usr/lib/x86_64-linux-gnu/*.so.* "$X11LIB/"
    # Their MIT/X11 licences ask for the notice to go with every copy.
    pkg="$(basename "$path" | cut -d_ -f1)"
    install -D -m 644 "$CACHE/debs/x/usr/share/doc/$pkg/copyright" "$LICENCES/debian/$pkg/copyright"
done
rm -rf "$CACHE/debs/x"

log "Tokenizer data"
mkdir -p "$APPDIR/usr/share/sealedlore/tiktoken"
TIKTOKEN_CACHE_DIR="$APPDIR/usr/share/sealedlore/tiktoken" "$PYTHON" -s -c \
    "from sealedlore.engine.tokens import DEFAULT_ENCODING; import tiktoken; tiktoken.get_encoding(DEFAULT_ENCODING)"

log "Licences and third-party notices"
install -m 644 "$SRC/LICENSE" "$LICENCES/LICENSE"
mkdir -p "$LICENCES/texts"
install -m 644 "$SRC"/packaging/appimage/licenses/*.txt "$LICENCES/texts/"
"$PYTHON" -s "$SRC/packaging/appimage/notices.py" \
    "$SRC/packaging/appimage/THIRD-PARTY-NOTICES.md" "$LICENCES/THIRD-PARTY-NOTICES.md"

log "Sample stories and the plot format guide"
mkdir -p "$APPDIR/usr/share/sealedlore/samples"
cp "$SRC"/SampleStories/*.md "$APPDIR/usr/share/sealedlore/samples/"

log "Launcher, desktop entry, icon"
install -m 755 "$SRC/packaging/appimage/AppRun" "$APPDIR/AppRun"
cp "$SRC/packaging/sealedlore.svg" "$APPDIR/sealedlore.svg"
ln -sf sealedlore.svg "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$SRC/packaging/sealedlore.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/sealedlore.svg"
# The repo's entry, minus its template comments, with the AppImage's own
# launcher and an icon. Integration tools rewrite Exec to the AppImage's path.
grep -v '^#' "$SRC/sealedlore.desktop" \
    | sed 's|^Exec=.*|Exec=sealedlore|' > "$APPDIR/sealedlore.desktop"
# What AppImage managers (Gear Lever, AppImageLauncher, Shelly) read as the
# version: the key in the embedded desktop entry.
echo "X-AppImage-Version=$VERSION" >> "$APPDIR/sealedlore.desktop"

log "Checking the result"
# Every ELF inside must run on the glibc floor.
floor="$(find "$APPDIR" -type f \( -name '*.so*' -o -perm -u+x \) -exec sh -c \
    'file -b "$1" | grep -q ELF && objdump -T "$1" 2>/dev/null' _ {} \; \
    | grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)"
echo "highest glibc symbol needed: $floor"
if [ "$(printf '%s\n%s\n' "$floor" "$GLIBC_FLOOR" | sort -V | tail -1)" != "$GLIBC_FLOOR" ]; then
    echo "something inside needs glibc $floor, above the $GLIBC_FLOOR floor" >&2
    exit 1
fi
# The xcb plugin's libraries must all resolve from the AppImage or the core X
# libraries every X11 desktop has.
missing="$(LD_LIBRARY_PATH="$X11LIB" ldd "$QT/Qt/plugins/platforms/libqxcb.so" | grep 'not found' || true)"
if [ -n "$missing" ]; then
    echo "the xcb plugin can't resolve: $missing" >&2
    exit 1
fi

# What was trimmed must not be missed: the app still imports, and nothing left
# in the Python base needs a library that went.
"$PYTHON" -s -c "import sealedlore.gui.app, ssl, hashlib, sqlite3, lzma, bz2, ctypes, dbm"
# Video: QtMultimedia loads, and its FFmpeg plugin resolves (from the
# AppImage, or libraries every desktop with sound has: libpulse, X11).
"$PYTHON" -s -c "import PySide6.QtMultimedia"
missing="$(LD_LIBRARY_PATH="$X11LIB" ldd "$QT/Qt/plugins/multimedia/libffmpegmediaplugin.so" \
    | grep 'not found' || true)"
if [ -n "$missing" ]; then
    echo "the FFmpeg plugin can't resolve: $missing" >&2
    exit 1
fi
missing="$(find "$APPDIR/usr/lib" "$STDLIB/lib-dynload" -maxdepth 1 -name '*.so*' -type f \
    -exec ldd {} \; 2>/dev/null | grep 'not found' || true)"
if [ -n "$missing" ]; then
    echo "the Python base can't resolve: $missing" >&2
    exit 1
fi

# Nothing inside may name the folder it was built in: that is the builder's
# machine showing through, and a rebuild elsewhere would differ.
leaks="$(grep -rlF "$REPO" "$APPDIR" || true)"
if [ -n "$leaks" ]; then
    echo "files carry the build folder's path ($REPO):" >&2
    echo "$leaks" >&2
    exit 1
fi

log "Packing"
TOOL="$CACHE/appimagetool-$APPIMAGETOOL_VERSION-x86_64.AppImage"
RUNTIME="$CACHE/runtime-$RUNTIME_VERSION-x86_64"
fetch "$APPIMAGETOOL_URL" "$TOOL" "$APPIMAGETOOL_SHA256"
fetch "$RUNTIME_URL" "$RUNTIME" "$RUNTIME_SHA256"
chmod +x "$TOOL"
# Every file dated to the commit (mksquashfs also reads SOURCE_DATE_EPOCH, and
# refuses its own time options beside it), owned by root, without extended
# attributes.
find "$APPDIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
rm -f "$OUTPUT" "$OUTPUT.sha256"
ARCH=x86_64 VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream \
    --runtime-file "$RUNTIME" --mksquashfs-opt -all-root --mksquashfs-opt -no-xattrs \
    "$APPDIR" "$OUTPUT" >/dev/null
(cd "$DIST" && sha256sum "$(basename "$OUTPUT")" >"$(basename "$OUTPUT").sha256")
echo
echo "Built $OUTPUT ($(du -h "$OUTPUT" | cut -f1)) from $COMMIT"
cat "$OUTPUT.sha256"
