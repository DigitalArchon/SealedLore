#!/usr/bin/env bash
# Check the tor the AppImage brings against the repositories' signed indexes.
#
#   packaging/appimage/check-tor-debs.sh
#
# For the pins in tools.sh: Tor's archive key, fetched over https from
# deb.torproject.org, must have the fingerprint TOR_ARCHIVE_KEY; the jammy
# InRelease must be signed by it (gpgv), and the Packages index it signs must
# list TOR_DEB with TOR_SHA256. Ubuntu's jammy, jammy-updates and
# jammy-security InRelease must be signed by Ubuntu's archive key (the
# ubuntu-keyring package's, or UBUNTU_KEYRING), and each of TOR_LIBRARY_DEBS
# must be in one of their Packages indexes with its sha256. It also says where
# a newer version of a library is out (a reason to bump it). Network: those
# hosts alone, over https. Run it when bumping tor or its libraries; the build
# itself checks every file against its sha256.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=packaging/appimage/tools.sh
source "$HERE/tools.sh"
UBUNTU_KEY="F6ECB3762474EDA9D21B7022871920D1991BC93C"
UBUNTU_KEYRING="${UBUNTU_KEYRING:-/usr/share/keyrings/ubuntu-archive-keyring.gpg}"
TOR_REPO="https://deb.torproject.org/torproject.org"
UBUNTU="https://archive.ubuntu.com/ubuntu"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

get() {  # get <url> <file>
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$2" "$1"
}

fingerprints() {  # the primary keys' fingerprints in a key file
    gpg --show-keys --with-colons "$1" 2>/dev/null | awk -F: '$1=="pub"{p=1} $1=="fpr"&&p{print $10; p=0}'
}

signed() {  # signed <keyring> <InRelease>: gpgv's verdict, or a failure
    gpgv --keyring "$1" "$2" 2>&1 | grep -q '^gpgv: Good signature' || {
        echo "FAIL: $2 isn't signed by the key it must be" >&2
        exit 1
    }
}

index() {  # index <InRelease> <path> <url> <file>: the index, its sum the one signed
    local want
    want="$(awk -v p="$2" '/^SHA256:/{f=1;next} /^[^ ]/{f=0} f&&$3==p{print $1}' "$1")"
    get "$3" "$4"
    if [ -z "$want" ] || [ "$(sha256sum < "$4" | cut -d' ' -f1)" != "$want" ]; then
        echo "FAIL: $4 isn't the index $1 signs" >&2
        exit 1
    fi
}

echo "Tor's archive key"
get "$TOR_REPO/$TOR_ARCHIVE_KEY.asc" "$WORK/tor.asc"
if ! fingerprints "$WORK/tor.asc" | grep -qx "$TOR_ARCHIVE_KEY"; then
    echo "FAIL: the key at $TOR_REPO isn't $TOR_ARCHIVE_KEY" >&2
    exit 1
fi
gpg --dearmor < "$WORK/tor.asc" > "$WORK/tor.gpg"
get "$TOR_REPO/dists/jammy/InRelease" "$WORK/tor.InRelease"
signed "$WORK/tor.gpg" "$WORK/tor.InRelease"
index "$WORK/tor.InRelease" main/binary-amd64/Packages \
    "$TOR_REPO/dists/jammy/main/binary-amd64/Packages" "$WORK/tor.Packages"

echo "Ubuntu's archive key"
if ! gpg --show-keys --with-colons "$UBUNTU_KEYRING" 2>/dev/null | grep -q ":$UBUNTU_KEY:"; then
    echo "FAIL: $UBUNTU_KEYRING doesn't hold Ubuntu's archive key $UBUNTU_KEY" >&2
    exit 1
fi
for suite in jammy jammy-updates jammy-security; do
    get "$UBUNTU/dists/$suite/InRelease" "$WORK/$suite.InRelease"
    signed "$UBUNTU_KEYRING" "$WORK/$suite.InRelease"
    index "$WORK/$suite.InRelease" main/binary-amd64/Packages.xz \
        "$UBUNTU/dists/$suite/main/binary-amd64/Packages.xz" "$WORK/$suite.Packages.xz"
    xz -dc "$WORK/$suite.Packages.xz" > "$WORK/$suite.Packages"
done

printf '%s\n' "$TOR_DEB $TOR_SHA256" > "$WORK/tor.pins"
printf '%s\n' "${TOR_LIBRARY_DEBS[@]}" > "$WORK/libs.pins"
python3 -I - "$WORK" <<'PY'
import subprocess
import sys
from pathlib import Path

work = Path(sys.argv[1])


def stanzas(path):
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ": " in line and not line.startswith(" "):
                key, value = line.split(": ", 1)
                fields[key] = value
        if fields.get("Package"):
            yield fields


def newer(a, b):
    return subprocess.run(["dpkg", "--compare-versions", a, "gt", b]).returncode == 0


failed = False
for pins, indexes in (
    ("tor.pins", ["tor.Packages"]),
    ("libs.pins", ["jammy.Packages", "jammy-updates.Packages", "jammy-security.Packages"]),
):
    listed = [s for name in indexes for s in stanzas(work / name)]
    for line in (work / pins).read_text().split("\n"):
        if not line.strip():
            continue
        file, sum_ = line.split()
        found = [s for s in listed if s.get("Filename", "").endswith("/" + file)]
        if not found or all(s.get("SHA256") != sum_ for s in found):
            print(f"FAIL: {file} isn't in the signed indexes with sha256 {sum_}")
            failed = True
            continue
        stanza = found[0]
        latest = max(
            (s for s in listed if s["Package"] == stanza["Package"]),
            key=lambda s: s["Version"],
            default=stanza,
        )
        for s in listed:
            if s["Package"] == stanza["Package"] and newer(s["Version"], latest["Version"]):
                latest = s
        note = "" if latest["Version"] == stanza["Version"] else f" (newer: {latest['Version']})"
        print(f"ok: {stanza['Package']} {stanza['Version']}{note}")
sys.exit(1 if failed else 0)
PY
echo "Every pin is in an index signed by its repository's key."
