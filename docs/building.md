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
the Tor Expert Bundle from the Tor Project's archive, the tokenizer's data
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

Phone access runs tor, and the AppImage brings its own: the `tor` program
and the three libraries it was built against (libevent and OpenSSL), taken
unmodified from the official Tor Expert Bundle into `usr/lib/tor`. The
bundle's version and checksum are pinned in `packaging/appimage/tools.sh`
(`TOR_BUNDLE_VERSION`, `TOR_URL`, `TOR_SHA256`). The build runs the bundled
tor with the environment the app gives it and stops if it isn't the pinned
version, or would load a library from anywhere but its own folder.

That build of tor is made with `--enable-gpl` (for the onion service's
proof-of-work defence), so the `tor` program is covered by the GPL, version 3.
`packaging/appimage/THIRD-PARTY-NOTICES.md`, inside the AppImage too, says
where its source is: the Tor Project's own tarball of that version, and the
tag of Tor Browser's build system that built the bundle from it.

Tor is updated with each release, to carry its fixes: change the versions and
sum in `tools.sh`, and the versions, source and build-system tag that
`THIRD-PARTY-NOTICES.md` names (the tag's `projects/tor`, `projects/libevent` and
`projects/openssl` in
[tor-browser-build](https://gitlab.torproject.org/tpo/applications/tor-browser-build)
give the bundle's versions). `tests/test_packaging_tor.py` checks that they
agree, and `tools/smoke/appimage_tor.sh` checks a built AppImage's tor
publishes an onion address (it needs the network).

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
