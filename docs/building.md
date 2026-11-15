# Building and checking the AppImage

The AppImage is a single file that runs on most Linux machines with nothing to
install (see [the manual](manual.md#linux-the-appimage) for running it). To build
one from a checkout:

```bash
packaging/appimage/build.sh         # -> dist/SealedLore-<version>-x86_64.AppImage (+ .sha256, .zsync)
packaging/appimage/build.sh check   # build it twice and compare (below)
```

The AppImage carries update information
(`gh-releases-zsync|DigitalArchon|SealedLore|latest|SealedLore-*-x86_64.AppImage.zsync`),
and a release attaches the `.zsync` beside it, so AppImage updaters fetch only
the blocks that changed. The `.zsync` is made by appimagetool's own
`zsyncmake` from the AppImage dated to the commit, so it is reproducible too.

It builds the current commit, not uncommitted changes (it stops if there are
any): each file as committed, read from git's own store, so no setting of
yours (`core.autocrlf`, a filter, `tar.umask`) changes what goes in. It needs
`git`, `curl`, `tar`, `dpkg-deb`, `sha256sum`, `file`, `objdump` (binutils)
and `ldd`. `DIST=folder` puts the AppImage somewhere other than `dist/`;
`BUILD_DIR` and `CACHE_DIR` move its working folder (`build/appimage`) and its
downloads.

The build downloads, over HTTPS, a relocatable Python (python-appimage), the
pinned packages from PyPI, nine small libraries from Debian 11's archive,
tor from the Tor Project's own Ubuntu 22.04 repository and its libraries
from Ubuntu's archive (through Launchpad), the tokenizer's data
from OpenAI's public file store (each checked against a pinned checksum), and
`appimagetool`. They are cached in `build/appimage/cache`. Nothing of your
uv or pip settings is used, the `UV_` variables in your environment
included.

To change a package version, edit `packaging/appimage/requirements.in` and
run `packaging/appimage/lock.sh` (it uses the same pinned `uv` as the build).
A relock keeps every version the lock already pins, so only what you added
or floored moves; `UPGRADE=1 packaging/appimage/lock.sh` re-resolves
everything to the newest versions. A version with an advisory moves only
when `requirements.in` floors it past the fix (as it does pyasn1, dnspython
and cryptography) or the lock is re-resolved.

## Tor, for phone access

Phone access runs tor, and the AppImage brings its own: the `tor` program of
the Tor Project's own package for Ubuntu 22.04 (from
<https://deb.torproject.org/torproject.org>) and the Ubuntu 22.04 packages of
exactly the libraries it loads (libevent, OpenSSL, zlib, xz, zstd, libcap,
libseccomp, libsystemd, lz4, libgcrypt and libgpg-error), taken unmodified into
`usr/lib/tor`. Each package is pinned by its sha256 in
`packaging/appimage/tools.sh` (`TOR_DEB`, `TOR_SHA256`, `TOR_LIBRARY_DEBS`),
and `packaging/appimage/check-tor-debs.sh` checks the pins against the
repositories' indexes, signed by the Tor Project's archive key (`TOR_ARCHIVE_KEY`)
and Ubuntu's. The build runs the bundled tor with the environment the app gives
it and stops if it isn't the pinned version, isn't the `--enable-gpl` build or
lacks the onion service's proof-of-work defence, would load a library from
anywhere but its own folder (glibc's aside), or loads one the app doesn't list
(`BUNDLED_LIBRARIES` in `phone/torrc.py`). Everything in it runs on glibc 2.34,
the AppImage's floor.

That build of tor is made with `--enable-gpl` (for the onion service's
proof-of-work defence), so the `tor` program is covered by the GPL, version 3.
`packaging/appimage/THIRD-PARTY-NOTICES.md`, inside the AppImage too, says
where its source is: the Tor Project's own tarball of that version, and the
Debian source package that built it; and each library's Ubuntu source package.

Tor is updated with each release, to carry its fixes: change the package and
its sum in `tools.sh` (and any library whose package moved), run
`check-tor-debs.sh`, and change the versions and source `THIRD-PARTY-NOTICES.md`
names. `tests/test_packaging_tor.py` checks that they agree, and
`tools/smoke/appimage_tor.sh` checks a built AppImage's tor publishes an onion
address (it needs the network).

The Tor Project's repository keeps only the current tor, so the same package
is also an asset of the SealedLore release that first carried it
(`TOR_FALLBACK_URL`: for tor 0.4.9.17, v3.1.0b13's
`tor_0.4.9.17-1.jammy.1_amd64.deb`, named without the `~` and `+` GitHub
doesn't keep). A build tries deb.torproject.org first and that asset when it
fails or no longer has the file; either way the file must have the pinned
sum, or the build stops. So any tag can be rebuilt. A tor bump points
`TOR_FALLBACK_URL` at the release it comes with, and that release uploads the
package and the Debian source's `.dsc` and `.diff.gz` under the names
`THIRD-PARTY-NOTICES.md` gives (the source tarball stays on
archive.torproject.org).

A new release of Tor doesn't by itself call for a new release of SealedLore. By
[Tor's specification](https://spec.torproject.org/tor-spec/subprotocol-versioning.html),
a client the network no longer recommends only warns. It stops connecting if the
network comes to require a protocol it doesn't have, which isn't tied to Tor's
releases. What does call for one is a Tor security fix that affects clients or
onion services. When tor says its version is out of date, the app says so too
(the status bar's phone sign, and File → Phone devices…).

Running from source, phone access uses a `tor` on `PATH` (for example
`sudo apt install tor`); only the program is used, never a tor service the
system runs. `SEALEDLORE_TOR` names another one.

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

To check that the build is reproducible on your own machine, without a
download to compare with:

```bash
packaging/appimage/build.sh check
```

It builds the commit twice, from scratch, each in a temporary folder of its
own: the first with the usual cache, the second with none of its package
caches (the Python packages are downloaded again and SealedLore's own package
built anew), only the pinned downloads, each checked by its checksum as it is
used. If the two AppImages are the same, byte for byte, it says so and puts
the AppImage in `dist/`; if not, it keeps both in `dist/check-a` and
`dist/check-b` for comparing (with `diffoscope`, say) and fails.

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
