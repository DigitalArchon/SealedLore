# Shared by build.sh and lock.sh: the download helper and the pinned tools.
# Every download is HTTPS, from a fixed release (never a "continuous" or
# "latest" one), and checked against the sum here before it is used.

HERE="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
REPO="$(cd "$HERE/../.." && pwd)"
BUILD="$REPO/build/appimage"
CACHE="$BUILD/cache"

UV_VERSION="0.13.5"
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256="f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f"

APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
# The small launcher at the front of every AppImage. appimagetool downloads
# the newest one unless it is handed a file, so it is pinned here too.
RUNTIME_VERSION="20251108"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_VERSION/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

# Tor, for phone access (gui/tor_service.py): the official Tor Expert Bundle,
# of which build.sh takes tor and the three libraries it was built against.
# archive.torproject.org keeps every release (dist.torproject.org only the
# newest). Checked by hand once against its signature: the Tor Browser
# Developers key, EF6E 286D DA85 EA2A 4BA7 DE68 4E2C 6E87 9329 8290. Bump it
# for each release: the network refuses a tor once it is obsolete.
TOR_BUNDLE_VERSION="15.0.25"
TOR_VERSION="0.4.9.14"
TOR_URL="https://archive.torproject.org/tor-package-archive/torbrowser/$TOR_BUNDLE_VERSION/tor-expert-bundle-linux-x86_64-$TOR_BUNDLE_VERSION.tar.gz"
TOR_SHA256="19cf18726eecb362e0f21d27afa9ea9efe5da928bfdfbb1ceb5fe54519e87f4a"
# The bundle's tor is built with --enable-gpl (the onion service's
# proof-of-work defence), which makes that program GPL-3: THIRD-PARTY-NOTICES.md
# names its exact source on the Tor Project's archive, and the tag of Tor
# Browser's build system that built it (tor-browser-build tbb-15.0.25-build2).

fetch() {  # fetch <url> <file> <sha256>
    local url="$1" file="$2" sum="$3"
    mkdir -p "$(dirname "$file")"
    if [ ! -f "$file" ]; then
        curl --proto '=https' --tlsv1.2 -fL --retry 3 -o "$file.part" "$url"
        mv "$file.part" "$file"
    fi
    if ! echo "$sum  $file" | sha256sum -c --quiet -; then
        echo "checksum mismatch for $file (from $url)" >&2
        echo "the upstream file changed; check it and update the pinned sum" >&2
        rm -f "$file"
        exit 1
    fi
}

fetch_uv() {  # sets UV
    local archive="$CACHE/uv-$UV_VERSION.tar.gz"
    fetch "$UV_URL" "$archive" "$UV_SHA256"
    UV="$CACHE/uv-$UV_VERSION/uv"
    if [ ! -x "$UV" ]; then
        mkdir -p "$CACHE/uv-$UV_VERSION"
        tar -xzf "$archive" -C "$CACHE/uv-$UV_VERSION" --strip-components=1
    fi
    # Nothing from this machine's uv or pip settings: no other index, no
    # other cache, no Python downloads.
    export UV_NO_CONFIG=1 UV_CACHE_DIR="$CACHE/uv" UV_PYTHON_DOWNLOADS=never
}
