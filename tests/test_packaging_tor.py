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
    match = re.search(rf'^{name}="([^"]*)"', (APPIMAGE / "tools.sh").read_text(), re.M)
    assert match, f"tools.sh sets no {name}"
    return match.group(1)


def test_the_bundle_is_pinned_over_https():
    bundle = _value("TOR_BUNDLE_VERSION")
    url = _value("TOR_URL").replace("$TOR_BUNDLE_VERSION", bundle)
    assert url == (
        "https://archive.torproject.org/tor-package-archive/torbrowser/"
        f"{bundle}/tor-expert-bundle-linux-x86_64-{bundle}.tar.gz"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", _value("TOR_SHA256"))


def test_the_notices_name_what_is_bundled_and_where_its_source_is():
    notices = (APPIMAGE / "THIRD-PARTY-NOTICES.md").read_text()
    section = notices.split("## Tor\n", 1)[1].split("\n## ", 1)[0]
    bundle, tor = _value("TOR_BUNDLE_VERSION"), _value("TOR_VERSION")
    assert f"Tor Expert Bundle {bundle}" in section
    assert f"`tor` {tor}" in section
    assert "--enable-gpl" in section and "texts/GPL-3.0.txt" in section
    # The GPL-3 build's source, exactly, and the recipe that built the bundle.
    assert f"<https://archive.torproject.org/tor-package-archive/tor-{tor}.tar.gz>" in section
    assert f"`tbb-{bundle}-build" in section
    for library in BUNDLED_LIBRARIES:
        assert f"`{library}`" in section
    for text in ("tor/tor.txt", "tor/libevent.txt", "tor/openssl.txt"):
        assert text in section
    assert (APPIMAGE / "licenses/GPL-3.0.txt").exists()


def test_apprun_points_at_the_tor_build_sh_puts_in_place():
    build = (APPIMAGE / "build.sh").read_text()
    assert 'TORDIR="$APPDIR/usr/lib/tor"' in build
    assert 'install -m 755 "$CACHE/tor-x/tor/tor" "$TORDIR/tor"' in build
    # The libraries are the ones the app checks for beside tor, taken from it.
    assert "from sealedlore.phone.torrc import BUNDLED_LIBRARIES" in build
    apprun = (APPIMAGE / "AppRun").read_text()
    assert 'export SEALEDLORE_TOR="${SEALEDLORE_TOR:-$HERE/usr/lib/tor/tor}"' in apprun
