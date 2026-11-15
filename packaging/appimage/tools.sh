# Shared by build.sh and lock.sh: the download helper and the pinned tools.
# Every download is HTTPS, from a fixed release (never a "continuous" or
# "latest" one), and checked against the sum here before it is used.

HERE="${HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
REPO="$(cd "$HERE/../.." && pwd)"
# Where the build works and keeps its downloads: build/appimage and its cache
# unless BUILD_DIR or CACHE_DIR say otherwise (build.sh check builds twice,
# each in a folder of its own).
BUILD="${BUILD_DIR:-$REPO/build/appimage}"
CACHE="${CACHE_DIR:-$BUILD/cache}"

# Nothing from this machine's uv settings. UV_NO_CONFIG (fetch_uv) skips only
# uv's config files: uv reads UV_INDEX_URL, UV_DEFAULT_INDEX, UV_FIND_LINKS,
# UV_INSECURE_HOST and the rest from the environment as well, so every UV_
# variable goes before this file sets its own (which are never exported).
# (DA Assistant's lock script, 9b1d6b8.)
unset "${!UV_@}"

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

# Tor, for phone access (gui/tor_service.py): the Tor Project's own package of
# tor for Ubuntu 22.04 (jammy), from deb.torproject.org, and the Ubuntu 22.04
# packages of exactly the libraries it loads (BUNDLED_LIBRARIES in
# phone/torrc.py), which build.sh puts beside it. Checked by hand
# (packaging/appimage/check-tor-debs.sh, which redoes it): the repository's
# InRelease signed by Tor's archive key, A3C4 F0F9 79CA A22C DBA8 F512 EE8C
# BC9E 886D DD89 (deb.torproject.org archive signing key, fetched over https
# from deb.torproject.org), Ubuntu's InRelease files by Ubuntu's archive key
# (Ubuntu Archive Automatic Signing Key (2018), F6EC B376 2474 EDA9 D21B 7022
# 8719 20D1 991B C93C), and each .deb's sum here the one its Packages index
# gives. Ubuntu's packages come from Launchpad's file store, which keeps every
# version (archive.ubuntu.com drops a superseded one); deb.torproject.org keeps
# only the current tor, so a build from an older tag needs the .deb in the
# cache. Built with --enable-gpl (the onion service's proof-of-work defence),
# so that tor is GPL-3: THIRD-PARTY-NOTICES.md names its source. Bump it for
# each release, to carry tor's fixes (docs/building.md says when one calls for
# a release of its own).
TOR_VERSION="0.4.9.17"
TOR_DEB="tor_0.4.9.17-1~jammy+1_amd64.deb"
TOR_URL="https://deb.torproject.org/torproject.org/pool/main/t/tor/$TOR_DEB"
TOR_SHA256="249f3beef647186a7127f1828c7dec9aaf93de900baca8e01c0fb99bb47c2a58"
# deb.torproject.org keeps only the current tor: the same file, under a name
# GitHub keeps as it is (no "~" or "+"), is an asset of the SealedLore release
# that first carried it, tried when deb.torproject.org fails or no longer has it,
# so any tag can be rebuilt. Its sum is the same pin. A tor bump points this at
# the release it comes with, and that release uploads the file (docs/building.md).
TOR_FALLBACK_URL="https://github.com/DigitalArchon/SealedLore/releases/download/v3.1.0b13/tor_0.4.9.17-1.jammy.1_amd64.deb"
TOR_ARCHIVE_KEY="A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89"
UBUNTU_FILES="https://launchpad.net/ubuntu/+archive/primary/+files"
# The Ubuntu 22.04 packages of tor's libraries: file and sha256 (jammy, and
# jammy-updates where it has a newer one, as of Nov 15 2026).
TOR_LIBRARY_DEBS=(
    "zlib1g_1.2.11.dfsg-2ubuntu9.2_amd64.deb 9dc17e51a1be2d9ed63b7b84ef0e4e29c5abe6f1bc62cb03e7181483cce8a2f2"
    "libevent-2.1-7_2.1.12-stable-1ubuntu0.2_amd64.deb 7e73ca523ba62a774c54efc53974178f4f6e94b5726d201f2ebec1a7d684bc89"
    "libssl3_3.0.2-0ubuntu1.30_amd64.deb d1b4636e32ac6d3b95d583ac05ac548b1aa248414bae1c51b14f54be0f8fd7bc"
    "libsystemd0_249.11-0ubuntu3.23_amd64.deb ff02a85ba73f850cf7958d2867b34947c67a1072e2b2d1c108a8c6e23715624d"
    "liblzma5_5.2.5-2ubuntu1.2_amd64.deb a88fca7506963c443c42a578db6c35424336ae28abcdaa60c89f06d30aeb625b"
    "libzstd1_1.4.8+dfsg-3build1_amd64.deb ae7db00ce8b093e50c994518b90203544e063b4bc574836a048bb142b950b2c9"
    "libseccomp2_2.5.3-2ubuntu3~22.04.1_amd64.deb 25c63fd46d24f518f362c47c429b6918182875b0c58f7e2742aaa2d1e545bbac"
    "libcap2_2.44-1ubuntu0.22.04.3_amd64.deb c497efb7fa3cd19a7ff297d95c7c5d678063c80c9fc230c9d034a8ee02ca8446"
    "libgcrypt20_1.9.4-3ubuntu3.3_amd64.deb 8f16329a0aabf4900271a3687890721bbef307e60983acae5b92eeff12e799b3"
    "liblz4-1_1.9.3-2build2_amd64.deb ac9b54d0feb840345060c74fb687675c5e1eb2b195effafae38c5f9991041e98"
    "libgpg-error0_1.43-3_amd64.deb 1fbacdf9bd1e431cee874a697b339f6f925182bc79bba5a112b53669b33265c5"
)

fetch_any() {  # fetch_any <file> <sha256> <url>...: from the first address that has it
    local file="$1" sum="$2" url
    shift 2
    mkdir -p "$(dirname "$file")"
    if [ -f "$file" ] && echo "$sum  $file" | sha256sum -c --quiet - 2>/dev/null; then
        return 0
    fi
    rm -f "$file"
    for url in "$@"; do
        if curl --proto '=https' --tlsv1.2 -fL --retry 3 -o "$file.part" "$url"; then
            mv "$file.part" "$file"
            if echo "$sum  $file" | sha256sum -c --quiet - 2>/dev/null; then
                return 0
            fi
            echo "the file from $url isn't the one pinned; trying the next address" >&2
            rm -f "$file"
        else
            rm -f "$file.part"
            echo "$url couldn't be fetched; trying the next address" >&2
        fi
    done
    echo "no address gave $(basename "$file") with its pinned sum $sum" >&2
    exit 1
}

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
    # other cache, no Python downloads (the environment's UV_ variables went
    # as this file was read).
    export UV_NO_CONFIG=1 UV_CACHE_DIR="$CACHE/uv" UV_PYTHON_DOWNLOADS=never
}
