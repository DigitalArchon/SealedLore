#!/usr/bin/env bash
# Pin everything the AppImage installs, by version and hash:
#   requirements.in        -> requirements.lock        (what ships)
#   build-requirements.in  -> build-requirements.lock  (the build backend)
#
# Uses the same pinned uv as build.sh (fetched into build/appimage/cache).
# Run after changing either .in file, and commit the .lock files with it: the
# build installs only what they name, and only if every hash matches.
#
#   packaging/appimage/lock.sh             lock what the .in files add or change
#   UPGRADE=1 packaging/appimage/lock.sh   re-resolve everything to today's newest
#
# uv keeps the versions a lock it writes over already pins, so a package added
# or floored in an .in file moves without everything else moving to whatever
# is newest today; and nothing moves otherwise, a version with an advisory
# included, until an .in file floors it or UPGRADE=1 re-resolves the lot.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/appimage/tools.sh
source "$HERE/tools.sh"
fetch_uv

upgrade=""
if [ "${UPGRADE:-0}" = 1 ]; then
    upgrade="--upgrade"
fi

# Resolved with the rest but never shipped: tokenizers needs huggingface-hub by
# its metadata, for Tokenizer.from_pretrained, which the app never calls
# (providers/local_embeddings.py reads the tokenizer.json it fetched and checked
# itself), and these are the hub and what only the hub needs (the lock's "via"
# lines said so when they were left out, Nov 24 2026: 27 MB, 12 of it hf-xet's).
# build.sh installs only what the lock names, and stops if a package it ships
# needs one that isn't there (but the hub, for tokenizers).
NOT_SHIPPED=(huggingface-hub hf-xet fsspec pyyaml tqdm click filelock)

cd "$HERE"
compile() {  # compile <in> <out> [uv option ...]
    local in="$1" out="$2"
    shift 2
    "$UV" pip compile --quiet "$in" -o "$out" \
        --python-version 3.12 --python-platform x86_64-manylinux_2_34 \
        --only-binary :all: --generate-hashes \
        --custom-compile-command "packaging/appimage/lock.sh" ${upgrade:+"$upgrade"} "$@"
}
left_out=()
for package in "${NOT_SHIPPED[@]}"; do
    left_out+=(--no-emit-package "$package")
done
compile requirements.in requirements.lock "${left_out[@]}"
# setuptools is only published as a wheel for every Python; the backend's own
# build needs nothing else.
compile build-requirements.in build-requirements.lock
echo "Locked: $(grep -c '==' requirements.lock) packages to ship, $(grep -c '==' build-requirements.lock) to build"
