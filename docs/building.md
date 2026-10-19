# Building and checking the AppImage

The AppImage is a single file that runs on most Linux machines with nothing to
install (see [the manual](manual.md#linux-the-appimage) for running it). To build
one from a checkout:

```bash
packaging/appimage/build.sh     # -> dist/SealedLore-<version>-x86_64.AppImage (+ .sha256)
```

It builds the current commit, not uncommitted changes (it stops if there are
any). It needs `git`, `curl`, `tar`, `dpkg-deb`, `sha256sum`, `file`, `objdump`
(binutils) and `ldd`.

The build downloads, over HTTPS, a relocatable Python (python-appimage), the
pinned packages from PyPI, eight small libraries from Debian 11's archive,
the Tor Expert Bundle from the Tor Project's archive (checked against pinned
checksums), and `appimagetool`. They are cached in `build/appimage/cache`.

## Tor, for phone access

Phone access runs tor, and the AppImage brings its own: the `tor` program
and the three libraries it was built against (libevent and OpenSSL), taken
unmodified from the official Tor Expert Bundle into `usr/lib/tor`. The
bundle's version and checksum are pinned in `packaging/appimage/tools.sh`
(`TOR_BUNDLE_VERSION`, `TOR_URL`, `TOR_SHA256`). The build runs the bundled
tor with the environment the app gives it and stops if it isn't the pinned
version, or would load a library from anywhere but its own folder.

That build of tor is made with `--enable-gpl` (for the onion service's
proof-of-work defence), so the `tor` program is covered by the GPL, version 3,
and its source goes with every release. `packaging/appimage/tor_sources.sh`
puts the source tarballs of tor, libevent and OpenSSL, as their projects
publish them and checked against the sums in `tools.sh` (`TOR_SOURCES`), in
`dist/`; the release workflow attaches them beside the AppImage.

Tor is updated with each release, since the network turns away a tor that has
become obsolete: change the versions and sums in `tools.sh` (the source
versions are the ones Tor Browser's build system,
[tor-browser-build](https://gitlab.torproject.org/tpo/applications/tor-browser-build),
uses for that bundle, in `projects/tor`, `projects/libevent` and
`projects/openssl`), and the versions `packaging/appimage/THIRD-PARTY-NOTICES.md`
names. `tests/test_packaging_tor.py` checks that they agree, and
`tools/smoke/appimage_tor.sh` checks a built AppImage's tor publishes an onion
address (it needs the network).

Running from source, phone access uses a `tor` on `PATH` (for example
`sudo apt install tor`); only the program is used, never a tor service the
system runs. `SEALEDLORE_TOR` names another one.

To change a package version, edit `packaging/appimage/requirements.in` and
run `packaging/appimage/lock.sh` (it uses the same pinned `uv` as the build).

## Checking an AppImage is what it claims to be

The build is reproducible: the same commit gives the same file, byte for
byte, whoever builds it and wherever. Every download it uses is pinned by
checksum, every Python package by hash (`packaging/appimage/requirements.lock`),
and nothing of the building machine gets in (timestamps are the commit's,
no user names, paths or permissions). So a downloaded AppImage can be
checked without trusting whoever built it:

```bash
git clone https://github.com/DigitalArchon/SealedLore && cd SealedLore
git checkout <the release's tag or commit>
packaging/appimage/build.sh
sha256sum dist/SealedLore-*-x86_64.AppImage /path/to/the/downloaded.AppImage
```

The two sums match only if the download holds exactly that commit's code.
Checking the published `.sha256` alone (`sha256sum -c`) catches a damaged
download, but not a replaced one if the sum came from the same place.

Releases are built by GitHub Actions from the tag ([`.github/workflows/release.yml`](../.github/workflows/release.yml))
and carry a signed build attestation, which says that workflow made that file
from that commit. Checking it takes the GitHub CLI and no rebuild:

```bash
gh attestation verify SealedLore-<version>-x86_64.AppImage --repo DigitalArchon/SealedLore
```

## Checking the compiled Python

The AppImage is mounted read-only, so Python can't keep what it compiles:
whatever isn't compiled when the image is built is compiled again at every
launch. The build therefore compiles every Python file, the standard library
and the packages with SealedLore's own, and ships each compiled file
(`__pycache__/*.pyc`) beside its source.

Python runs the compiled file, not the source. Before it does, it checks that
the source is the one the compiled file was made from; it does not check that
the compiled file is what that source compiles to. Someone reading the `.py`
files inside an AppImage is reading what runs only if the compiled files are
honest. To check that they are:

```bash
git clone https://github.com/DigitalArchon/SealedLore && cd SealedLore
packaging/appimage/verify_bytecode.sh /path/to/the/downloaded.AppImage
```

It unpacks the image into a temporary folder, compiles every source file
again the way the build does, and compares each result with the file that
was shipped, byte for byte. It fails on any compiled file that differs, and
on any with no source beside it. Of the image it runs the unpacker and
Python, never the app, and it needs no network.

The image's own Python does the compiling, so this takes that Python's word
for what source compiles to. To take nobody's, give it a Python of the same
version that you built or trust (compiled files differ between versions, so
it must match to the patch number):

```bash
PYTHON=/path/to/python3.12 packaging/appimage/verify_bytecode.sh /path/to/the/downloaded.AppImage
```

This covers the Python code only. The image also holds libraries in machine
code (Python itself, Qt, parts of numpy, pydantic and others), taken from
their publishers and pinned by checksum; for those, and for the image as a
whole, rebuild the release and compare checksums as above.

Releases up to 1.0.0b2 shipped only SealedLore's own code compiled. The script
checks those too, and says how many files have no compiled one.
