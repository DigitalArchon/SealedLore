"""The tor the AppImage brings for phone access: the pins in tools.sh, the
notices that name its versions, where build.sh puts it and AppRun points, and
the release that carries its source (GPL-3). A bump of tor touches all of
them; these keep them in step. The build itself runs the bundled tor as the
app would (build.sh)."""

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


def _sources() -> list[tuple[str, str]]:
    text = (APPIMAGE / "tools.sh").read_text()
    block = text.split("TOR_SOURCES=(", 1)[1].split("\n)", 1)[0]
    rows = [line.strip().strip('"') for line in block.splitlines()]
    pairs = [tuple(row.split()) for row in rows if row and not row.startswith("#")]
    version = _value("TOR_VERSION")
    return [(url.replace("$TOR_VERSION", version), sum_) for url, sum_ in pairs]


def test_the_bundle_and_its_sources_are_pinned_over_https():
    bundle, tor = _value("TOR_BUNDLE_VERSION"), _value("TOR_VERSION")
    url = _value("TOR_URL").replace("$TOR_BUNDLE_VERSION", bundle)
    assert url == (
        "https://archive.torproject.org/tor-package-archive/torbrowser/"
        f"{bundle}/tor-expert-bundle-linux-x86_64-{bundle}.tar.gz"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", _value("TOR_SHA256"))
    sources = _sources()
    names = [url.rsplit("/", 1)[1] for url, _ in sources]
    assert names[0] == f"tor-{tor}.tar.gz"
    assert [name.split("-")[0] for name in names] == ["tor", "libevent", "openssl"]
    for url, sum_ in sources:
        assert url.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", sum_)


def test_the_notices_name_what_is_bundled():
    notices = (APPIMAGE / "THIRD-PARTY-NOTICES.md").read_text()
    section = notices.split("## Tor\n", 1)[1].split("\n## ", 1)[0]
    assert f"Tor Expert Bundle {_value('TOR_BUNDLE_VERSION')}" in section
    assert f"`tor` {_value('TOR_VERSION')}" in section
    assert "--enable-gpl" in section and "texts/GPL-3.0.txt" in section
    for url, _ in _sources():
        assert f"`{url.rsplit('/', 1)[1]}`" in section
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


def test_a_release_carries_tor_s_source():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "packaging/appimage/tor_sources.sh" in workflow
    for pattern in ("dist/tor-*.tar.gz", "dist/libevent-*.tar.gz", "dist/openssl-*.tar.gz"):
        assert workflow.count(pattern) == 2, pattern  # in the artifact, and on the release
