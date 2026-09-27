# Shared by build.sh and lock.sh: the download helper and the pinned tools.
# Every download is HTTPS, from a fixed release (never a "continuous" or
# "latest" one), and checked against the sum here before it is used.

HERE="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
REPO="$(cd "$HERE/../.." && pwd)"
BUILD="$REPO/build/appimage"
CACHE="$BUILD/cache"

UV_VERSION="0.12.18"
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256="89eadd7c76fc063887959510d5ba0ab1264dfd5f1143b925ddb73021a40acf16"

APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
# The small launcher at the front of every AppImage. appimagetool downloads
# the newest one unless it is handed a file, so it is pinned here too.
RUNTIME_VERSION="20251108"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_VERSION/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

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
