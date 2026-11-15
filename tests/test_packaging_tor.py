"""The tor the AppImage brings for phone access: the pins in tools.sh, the
notices that name its versions and say where its source is (it is a GPL-3
build), and where build.sh puts it and AppRun points. A bump of tor touches
all of them; these keep them in step. The build itself runs the bundled tor
as the app would (build.sh)."""

from __future__ import annotations

import re
from pathlib import Path

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
