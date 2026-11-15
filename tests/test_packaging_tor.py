"""The tor the AppImage brings for phone access: the pins in tools.sh, the
notices that name its versions and say where its source is (it is a GPL-3
build), and where build.sh puts it and AppRun points. A bump of tor touches
all of them; these keep them in step. The build itself runs the bundled tor
as the app would (build.sh)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sealedlore.phone.torrc import BUNDLED_LIBRARIES

ROOT = Path(__file__).resolve().parents[1]
APPIMAGE = ROOT / "packaging/appimage"


def _value(name: str) -> str:
    match = re.search(
        rf'^{name}="([^"]*)"', (APPIMAGE / "tools.sh").read_text(encoding="utf-8"), re.M
    )
    assert match, f"tools.sh sets no {name}"
    return match.group(1)


def _library_debs() -> list[tuple[str, str]]:
    tools = (APPIMAGE / "tools.sh").read_text(encoding="utf-8")
    block = re.search(r"^TOR_LIBRARY_DEBS=\(\n(.*?)^\)", tools, re.M | re.S)
    assert block, "tools.sh sets no TOR_LIBRARY_DEBS"
    return [tuple(line.strip().strip('"').split()) for line in block.group(1).splitlines()]


def test_tor_and_its_libraries_are_pinned_over_https():
    """tor from the Tor Project's own repository, its libraries from Launchpad's file
    store (which keeps every version), each by its sha256, the archive key by its
    fingerprint."""
    tor, deb = _value("TOR_VERSION"), _value("TOR_DEB")
    assert re.fullmatch(rf"tor_{re.escape(tor)}-1~jammy\+\d+_amd64\.deb", deb), deb
    assert _value("TOR_URL").replace("$TOR_DEB", deb) == (
        f"https://deb.torproject.org/torproject.org/pool/main/t/tor/{deb}"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", _value("TOR_SHA256"))
    assert _value("TOR_ARCHIVE_KEY") == "A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89"
    assert _value("UBUNTU_FILES") == "https://launchpad.net/ubuntu/+archive/primary/+files"
    debs = _library_debs()
    assert len(debs) == 11
    for file, sum_ in debs:
        assert re.fullmatch(r"[a-z0-9.+-]+_[A-Za-z0-9.+~:-]+_amd64\.deb", file), file
        assert re.fullmatch(r"[0-9a-f]{64}", sum_), file


def test_the_notices_name_what_is_bundled_and_where_its_source_is():
    notices = (APPIMAGE / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    section = notices.split("## Tor\n", 1)[1].split("\n## ", 1)[0]
    tor, deb = _value("TOR_VERSION"), _value("TOR_DEB")
    package = deb.split("_")[1]
    assert f"tor {tor}" in section and f"`tor` {package}" in section
    assert "--enable-gpl" in section and "texts/GPL-3.0.txt" in section
    # The GPL-3 build's source: Tor's tarball, the Debian source package, its packaging.
    assert f"<https://archive.torproject.org/tor-package-archive/tor-{tor}.tar.gz>" in section
    assert f"`tor_{package}.dsc`" in section and f"`tor_{tor}.orig.tar.gz`" in section
    # Those that vanish from deb.torproject.org are attached to the release, named so.
    asset = package.replace("~", ".").replace("+", ".")
    for name in (f"`tor_{asset}.dsc`", f"`tor_{asset}.diff.gz`", f"`tor_{asset}_amd64.deb`"):
        assert name in section, name
    assert f"`debian-tor-{tor}-1`" in section
    assert "A3C4 F0F9 79CA A22C DBA8 F512 EE8C BC9E 886D DD89" in section
    for library in BUNDLED_LIBRARIES:
        assert f"`{library}`" in section, library
    for file, _sum in _library_debs():
        name, version = file.split("_")[:2]
        assert f"| {name} (" in section, name
        assert version in section, file
    for text in re.findall(r"`([A-Za-z0-9.-]+\.txt)`", section):
        assert (APPIMAGE / "licenses" / text).exists(), text
    assert (APPIMAGE / "licenses/GPL-3.0.txt").exists()


def test_apprun_points_at_the_tor_build_sh_puts_in_place():
    build = (APPIMAGE / "build.sh").read_text(encoding="utf-8")
    assert 'TORDIR="$APPDIR/usr/lib/tor"' in build
    assert 'install -m 755 "$CACHE/tor-x/usr/bin/tor" "$TORDIR/tor"' in build
    # The libraries are the ones the app checks for beside tor, taken from it, and
    # the build stops on one tor loads that isn't among them, on no PoW, on no GPL.
    assert "from sealedlore.phone.torrc import BUNDLED_LIBRARIES" in build
    assert "if loaded != set(BUNDLED_LIBRARIES):" in build
    assert '"pow: yes" not in modules.splitlines()' in build
    assert '"GNU General Public License" not in said' in build
    apprun = (APPIMAGE / "AppRun").read_text(encoding="utf-8")
    assert 'export SEALEDLORE_TOR="${SEALEDLORE_TOR:-$HERE/usr/lib/tor/tor}"' in apprun


def test_the_checker_reads_the_pins_it_checks():
    check = (APPIMAGE / "check-tor-debs.sh").read_text(encoding="utf-8")
    for name in ("TOR_ARCHIVE_KEY", "TOR_DEB", "TOR_SHA256", "TOR_LIBRARY_DEBS"):
        assert f"${name}" in check or f"${{{name}" in check, name
    assert "gpgv --keyring" in check


FAKE_CURL = """#!/bin/sh
# curl as fetch_any calls it: the URL is the last argument, -o names the file.
out=""; prev=""
for arg in "$@"; do [ "$prev" = "-o" ] && out="$arg"; prev="$arg"; url="$arg"; done
echo "$url" >> "$FAKE_LOG"
case "$url" in
  *gone*) exit 22 ;;
  *wrong*) printf 'not the package' > "$out" ;;
  *) printf 'the tor package' > "$out" ;;
esac
"""


def _fetch_any(tmp_path: Path, *urls: str) -> tuple[subprocess.CompletedProcess, Path, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    log = tmp_path / "asked.log"
    log.write_text("")
    target = tmp_path / "cache" / "debs" / "tor.deb"
    target.unlink(missing_ok=True)
    pinned = hashlib.sha256(b"the tor package").hexdigest()
    done = subprocess.run(
        [
            "bash",
            "-c",
            'source "$0"; fetch_any "$@"',
            str(APPIMAGE / "tools.sh"),
            str(target),
            pinned,
            *urls,
        ],
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, target, log.read_text().split()


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="bash")
def test_tor_comes_from_its_fallback_when_the_primary_is_gone_its_sum_still_held(tmp_path):
    """deb.torproject.org keeps only the current tor: once it has gone, the copy on
    SealedLore's own release is fetched, and only a file with the pinned sum is used."""
    done, target, asked = _fetch_any(
        tmp_path, "https://primary/gone.deb", "https://release/tor.deb"
    )
    assert done.returncode == 0, done.stderr
    assert target.read_bytes() == b"the tor package"
    assert asked == ["https://primary/gone.deb", "https://release/tor.deb"]
    assert "couldn't be fetched; trying the next address" in done.stderr
    # A primary that gives another file: refused, and the fallback's taken.
    done, target, asked = _fetch_any(
        tmp_path, "https://primary/wrong.deb", "https://release/tor.deb"
    )
    assert done.returncode == 0 and target.read_bytes() == b"the tor package"
    assert "isn't the one pinned" in done.stderr
    # Neither has it: the build stops, and nothing is left to be taken for it.
    done, target, _asked = _fetch_any(
        tmp_path, "https://primary/gone.deb", "https://release/wrong.deb"
    )
    assert done.returncode == 1 and not target.exists()
    assert "no address gave tor.deb with its pinned sum" in done.stderr
    # The primary has it: the fallback is never asked; and a cached copy asks nobody.
    done, target, asked = _fetch_any(tmp_path, "https://primary/tor.deb", "https://release/tor.deb")
    assert done.returncode == 0 and asked == ["https://primary/tor.deb"]


def test_the_fallback_is_this_projects_own_release_asset_named_as_github_keeps_it():
    fallback = _value("TOR_FALLBACK_URL")
    deb = _value("TOR_DEB")
    assert fallback.startswith("https://github.com/DigitalArchon/SealedLore/releases/download/v")
    asset = fallback.rsplit("/", 1)[1]
    assert asset == deb.replace("~", ".").replace("+", "."), asset
    assert "~" not in asset and "+" not in asset
    build = (APPIMAGE / "build.sh").read_text(encoding="utf-8")
    assert 'fetch_any "$CACHE/debs/$TOR_DEB" "$TOR_SHA256" "$TOR_URL" "$TOR_FALLBACK_URL"' in build
