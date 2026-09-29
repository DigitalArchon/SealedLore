#!/usr/bin/env bash
# Check that the compiled Python in an AppImage is what its source compiles to.
#
#   packaging/appimage/verify_bytecode.sh APPIMAGE
#   packaging/appimage/verify_bytecode.sh EXTRACTED_FOLDER
#
# Python runs a module's compiled file (__pycache__/*.pyc), not the .py beside
# it, and only checks that the .py is the one the .pyc was made from: not that
# the .pyc is what that .py compiles to. So reading the source inside an
# AppImage says what runs only if the compiled files are honest. This compiles
# every .py again, as the build does, and compares the result with what was
# shipped, byte for byte.
#
# It fails on a compiled file that differs, and on one with no source beside
# it to be checked against. A .py with no compiled file is only reported:
# Python compiles it from the source at each launch (releases up to 1.0.0b2
# shipped SealedLore's own code compiled and nothing else).
#
# The image's own Python does the compiling, which takes its word for what
# source compiles to. To take nobody's word, give another build of the same
# version: PYTHON=/path/to/python3.12 (it must match to the patch number).
#
# Offline. Of the image it runs the unpacker and Python, never the app. This
# checks the Python code only: for everything else in the image, rebuild the
# release and compare checksums (docs/building.md).
set -euo pipefail

if [ $# -ne 1 ]; then
    sed -n '2,5p' "$0" >&2
    exit 2
fi
TARGET="$(readlink -f "$1")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/squashfs-root"
if [ -d "$TARGET" ]; then
    # A copy: the check compiles in place.
    cp -a "$TARGET" "$ROOT"
else
    (cd "$WORK" && "$TARGET" --appimage-extract >/dev/null)
fi

# What was shipped, before any Python runs: started without the build's
# settings, Python writes compiled files of its own for whatever it imports.
sums() { (cd "$ROOT" && find . -name '*.pyc' -type f -print0 | sort -z | xargs -0 -r sha256sum); }
sums >"$WORK/shipped"
# The build's settings (build.sh), and nothing of this machine's.
python() { env -i PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 PYTHONNOUSERSITE=1 "$@"; }

STDLIB="$(find "$ROOT/opt" -maxdepth 3 -type d -path '*/lib/python3.*' | head -1)"
OWN="$(find "$ROOT/opt" -maxdepth 3 -type f -path '*/bin/python3.*' ! -name '*-config' | head -1)"
if [ -z "$STDLIB" ] || [ -z "$OWN" ]; then
    echo "no Python found under opt/ in $1" >&2
    exit 2
fi
PYTHON="${PYTHON:-$OWN}"
version() { python "$1" -s -c 'import sys; print(sys.version.split()[0])'; }
if [ "$(version "$PYTHON")" != "$(version "$OWN")" ]; then
    echo "$PYTHON is Python $(version "$PYTHON"); the image's is $(version "$OWN")." >&2
    echo "Compiled files differ between versions, so the comparison would mean nothing." >&2
    exit 2
fi

# Compiled files with no source beside them, and sources with none.
python "$PYTHON" -s - "$ROOT" >"$WORK/pairs" <<'CHECK'
import sys
from importlib.util import cache_from_source
from pathlib import Path

root = Path(sys.argv[1])
sources = {path for path in root.rglob("*.py") if path.is_file()}
compiled = {path for path in root.rglob("*.pyc") if path.is_file()}
expected = {Path(cache_from_source(str(path))) for path in sources}
for path in sorted(compiled - expected):
    print(f"orphan ./{path.relative_to(root)}")
print(f"uncompiled {len(expected - compiled)}")
print(f"sources {len(sources)}")
CHECK

# As the build compiles them (build.sh): hash-checked, named without the
# folder, one process, a fixed hash seed.
find "$ROOT" -name '__pycache__' -type d -prune -exec rm -rf {} +
if ! python "$PYTHON" -s -m compileall -q \
    --invalidation-mode checked-hash -s "$ROOT" "$STDLIB" >"$WORK/compile.log" 2>&1; then
    echo "some source files don't compile:" >&2
    head -20 "$WORK/compile.log" >&2
    exit 1
fi
sums >"$WORK/rebuilt"

# Only what was shipped is compared: an image with less compiled than this
# makes is reported above, not failed.
# (Whole lines, so a name with a space in it is still one name; a sum is 64
# characters and two spaces.)
grep -Fxv -f "$WORK/rebuilt" "$WORK/shipped" | cut -c67- >"$WORK/differing" || true
grep '^orphan ' "$WORK/pairs" | cut -d' ' -f2- >"$WORK/orphans" || true

shipped="$(wc -l <"$WORK/shipped")"
sources="$(sed -n 's/^sources //p' "$WORK/pairs")"
uncompiled="$(sed -n 's/^uncompiled //p' "$WORK/pairs")"
echo "Python $(version "$PYTHON"): $sources source files, $shipped compiled files shipped."
if [ "$uncompiled" -gt 0 ]; then
    echo "$uncompiled source files have no compiled file: Python compiles those at each launch."
fi
failed=0
if [ -s "$WORK/orphans" ]; then
    echo "Compiled files with no source beside them:" >&2
    sed 's/^/  /' "$WORK/orphans" >&2
    failed=1
fi
if [ -s "$WORK/differing" ]; then
    echo "Compiled files that are not what their source compiles to:" >&2
    sed 's/^/  /' "$WORK/differing" >&2
    failed=1
fi
if [ "$failed" -ne 0 ]; then
    echo "FAILED" >&2
    exit 1
fi
echo "OK: every compiled file is what its source compiles to."
