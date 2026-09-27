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
pinned packages from PyPI, eight small libraries from Debian 11's archive
(checked against pinned checksums), and `appimagetool`. They are cached in
`build/appimage/cache`.

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
