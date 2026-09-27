#!/usr/bin/env bash
# Pin everything the AppImage installs, by version and hash:
#   requirements.in        -> requirements.lock        (what ships)
#   build-requirements.in  -> build-requirements.lock  (the build backend)
#
# Uses the same pinned uv as build.sh (fetched into build/appimage/cache).
# Run after changing either .in file, and commit the .lock files with it: the
# build installs only what they name, and only if every hash matches.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/appimage/tools.sh
source "$HERE/tools.sh"
fetch_uv

cd "$HERE"
compile() {  # compile <in> <out>
    "$UV" pip compile --quiet "$1" -o "$2" \
        --python-version 3.12 --python-platform x86_64-manylinux_2_34 \
        --only-binary :all: --generate-hashes \
        --custom-compile-command "packaging/appimage/lock.sh"
}
compile requirements.in requirements.lock
# setuptools is only published as a wheel for every Python; the backend's own
# build needs nothing else.
compile build-requirements.in build-requirements.lock
echo "Locked: $(grep -c '==' requirements.lock) packages to ship, $(grep -c '==' build-requirements.lock) to build"
