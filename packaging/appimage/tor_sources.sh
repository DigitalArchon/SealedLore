#!/usr/bin/env bash
# The source of the tor inside the AppImage, to go beside each release:
# dist/tor-<version>.tar.gz, and libevent's and OpenSSL's, the tarballs Tor
# Browser's build system builds the Tor Expert Bundle from, as their projects
# publish them, checked against the sums pinned in tools.sh.
#
#   packaging/appimage/tor_sources.sh
#
# Why: the bundle's tor is built with --enable-gpl (the onion service's
# proof-of-work defence), which makes that program GPL-3, and GPL-3 asks for
# its source to be offered beside it. The two libraries' go too, since GPL-3's
# Corresponding Source counts the shared libraries a program is made to
# require. The release workflow (.github/workflows/release.yml) attaches all
# three; THIRD-PARTY-NOTICES.md says where they are.
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/appimage/tools.sh
source "$HERE/tools.sh"
DIST="$REPO/dist"
mkdir -p "$DIST" "$CACHE/sources"
for entry in "${TOR_SOURCES[@]}"; do
    read -r url sum <<<"$entry"
    file="$CACHE/sources/$(basename "$url")"
    fetch "$url" "$file" "$sum"
    cp "$file" "$DIST/"
    echo "$DIST/$(basename "$url")"
done
