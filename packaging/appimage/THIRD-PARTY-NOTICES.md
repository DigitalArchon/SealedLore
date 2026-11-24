# Third-party software in the SealedLore AppImage

SealedLore itself is licensed under the GNU Affero General Public License,
version 3 or later (`LICENSE`, beside this file). Its source is at
<https://github.com/DigitalArchon/SealedLore>, tagged for each release.

The AppImage also contains the software listed below. Each part keeps its own
licence, and nothing in SealedLore's licence changes those terms. Paths are
inside the AppImage: run it with `--appimage-extract` to see them. A path
starting `texts/` is in `usr/share/licenses/sealedlore/texts/`, which holds
the licence texts the components don't carry themselves; one starting `tor/`
is in `usr/share/licenses/sealedlore/tor/`, the Debian copyright files of tor
and the libraries it loads.

## Qt and Qt for Python

PySide6-Essentials 6.11.3 and shiboken6 6.11.3, which contain Qt 6.11.3, are
in `opt/python3.12/lib/python3.12/site-packages/PySide6` and `shiboken6`,
unmodified. They are used under the GNU Lesser General Public License,
version 3 (`texts/LGPL-3.0.txt`, which adds to `texts/GPL-3.0.txt`).

- **Source:** Qt 6.11.3 at <https://download.qt.io/archive/qt/6.11/6.11.3/single/>
  and Qt for Python 6.11.3 at
  <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.3-src/>
  (also tag `v6.11.3` of <https://code.qt.io/cgit/pyside/pyside-setup.git>).
OpenSSL is Ubuntu's 3.0.2, with the security fixes Ubuntu has backported to
it (`3.0.2-0ubuntu1.30`); `tor --version` names it 3.0.2.

- **Replacing them:** extract the AppImage, put your own build of these
  libraries in place of the bundled ones, and run `AppRun` from the extracted
  folder (or repack it with appimagetool).
- **Qt's own third-party code** (image formats, fonts, text shaping and so
  on) is listed, with its licences, at
  <https://doc.qt.io/qt-6.11/licenses-used-in-qt.html>.

Of PySide6-Addons 6.11.3, only Qt Multimedia is kept (`QtMultimedia.abi3.so`,
`Qt/lib/libQt6Multimedia.so.6`, `Qt/plugins/multimedia/libffmpegmediaplugin.so`),
for playing videos, under the same licence and from the same sources as above.

## FFmpeg

Qt Multimedia plays video with FFmpeg 7.1, as built and shipped by The Qt
Company in PySide6-Addons 6.11.3: `libavcodec.so.61`, `libavformat.so.61`,
`libavutil.so.59`, `libswresample.so.5` and `libswscale.so.8`, with Qt's
`libQt6FFmpegStub-*` loaders, in `Qt/lib` beside the Qt libraries. FFmpeg is
licensed under the GNU Lesser General Public License, version 2.1 or later
(`texts/LGPL-2.1.txt`); this build uses no GPL parts.

- **Source:** FFmpeg 7.1 at <https://ffmpeg.org/releases/> (and
  <https://git.ffmpeg.org/ffmpeg.git>); how Qt configures it is in Qt
  Multimedia's source, above.
- **Replacing it:** as with Qt, extract the AppImage and put your own build
  of these libraries in their place.

## The Python runtime

CPython 3.12.15, relocatable build from python-appimage
(<https://github.com/niess/python-appimage>, `manylinux_2_28`), in
`opt/python3.12`. Its licence, the Python Software Foundation License, and
the notices of the code it includes are in
`opt/python3.12/lib/python3.12/LICENSE.txt`.

The runtime brings these libraries, in `usr/lib`:

| Library | Version | Licence | Licence text |
|---|---|---|---|
| OpenSSL (libssl, libcrypto) | 1.1.1k | OpenSSL License and original SSLeay License | `texts/OpenSSL-1.1.1.txt` |
| bzip2 (libbz2) | 1.0.6 | bzip2 License (BSD-style) | `texts/bzip2-1.0.6.txt` |
| libffi | 3.1 | MIT | `texts/libffi-3.1.txt` |
| XZ Utils (liblzma) | 5.2.4 | Public domain | none needed |
| SQLite (libsqlite3) | 3.53.4 | Public domain | none needed |

This product includes software developed by the OpenSSL Project for use in
the OpenSSL Toolkit (<http://www.openssl.org/>).

This product includes cryptographic software written by Eric Young
(eay@cryptsoft.com).

`opt/_internal/certs.pem` is Mozilla's CA certificate list, under the Mozilla
Public License 2.0 (the same text as certifi's, listed below).

## X11 helper libraries

Qt's X11 support needs these, and many systems lack them. They are taken
unmodified from Debian 11 and placed in `usr/lib/x11`. Each package's Debian
copyright file, with its licence (MIT/X11 style), is in
`usr/share/licenses/sealedlore/debian/<package>/copyright`. Source:
<https://sources.debian.org/>.

| Debian package | Version |
|---|---|
| libxcb-cursor0 | 0.1.1-4 |
| libxcb-icccm4 | 0.4.1-1.1 |
| libxcb-image0 | 0.4.0-1+b3 |
| libxcb-keysyms1 | 0.4.0-1+b2 |
| libxcb-render-util0 | 0.3.9-1+b1 |
| libxcb-util1 | 0.4.0-1+b1 |
| libxkbcommon0 | 1.0.3-2 |
| libxkbcommon-x11-0 | 1.0.3-2 |
| libxcb-xkb1 | 1.14-3 |

## Tor

Phone access runs tor 0.4.9.17, taken unmodified from the Tor Project's own
package for Ubuntu 22.04 (`tor` 0.4.9.17-1~jammy+1, from
<https://deb.torproject.org/torproject.org>, signed by the Tor Project's
archive key A3C4 F0F9 79CA A22C DBA8 F512 EE8C BC9E 886D DD89), with the
libraries it loads, taken unmodified from Ubuntu 22.04's packages, in
`usr/lib/tor`. Tor is a program of its own, which SealedLore starts only while
phone access is on.

- **Tor** is under the 3-clause BSD license, with the licences of the code it
  includes, in `tor/tor/copyright`. **This build is made with
  `--enable-gpl`**, for the onion service's proof-of-work defence, so the
  `tor` program as a whole is covered by the GNU General Public License,
  version 3 (`texts/GPL-3.0.txt`), as `tor --version` says.
- **Source:** tor 0.4.9.17 is
  <https://archive.torproject.org/tor-package-archive/tor-0.4.9.17.tar.gz>
  (git tag `tor-0.4.9.17` of <https://gitlab.torproject.org/tpo/core/tor>;
  sha256 `ff372c41ee7013557fe5b7acd96c909950b27a7959c59fcb87c02591682fabe2`,
  the same file as the Debian source's `tor_0.4.9.17.orig.tar.gz`). The
  package is built from it by the Debian source package
  `tor_0.4.9.17-1~jammy+1`, whose packaging is tag `debian-tor-0.4.9.17-1` of
  <https://gitlab.torproject.org/tpo/core/debian/tor>. Its two other files
  are on <https://deb.torproject.org/torproject.org/pool/main/t/tor/> while
  0.4.9.17 is Tor's current version, and attached for good to SealedLore's
  release v3.1.0b13
  (<https://github.com/DigitalArchon/SealedLore/releases/tag/v3.1.0b13>)
  under names GitHub keeps as they are:
  `tor_0.4.9.17-1~jammy+1.dsc` as `tor_0.4.9.17-1.jammy.1.dsc` (sha256
  `68a0a524b1fa657612690e6c551df5dcf8a4e908319612dd055f204d404bb2f2`) and
  `tor_0.4.9.17-1~jammy+1.diff.gz` as `tor_0.4.9.17-1.jammy.1.diff.gz`
  (sha256 `e0a065df902ec2a22f6120f4aacf81a4f0e9c6728a7d490e788e2fca56038078`),
  beside the package itself, `tor_0.4.9.17-1.jammy.1_amd64.deb`.
- **The libraries**, each with its Debian copyright file (its licence) in
  `tor/<package>/copyright`; the licence texts those files name from
  `/usr/share/common-licenses` are in `texts/` (`Apache-2.0.txt`,
  `GPL-2.0.txt`, `GPL-3.0.txt`, `LGPL-2.0.txt`, `LGPL-2.1.txt`, `CC0-1.0.txt`,
  `Artistic-1.0.txt`, `GPL-1.0.txt`). Their source is Ubuntu's source package
  of the same name and version, at
  <https://launchpad.net/ubuntu/+source/SOURCE/VERSION> (and in
  `deb-src` of the Ubuntu 22.04 archive).

| Library | Ubuntu package (source) | Version | Licence |
|---|---|---|---|
| `libevent-2.1.so.7` | libevent-2.1-7 (libevent) | 2.1.12-stable-1ubuntu0.2 | 3-clause BSD |
| `libssl.so.3`, `libcrypto.so.3` | libssl3 (openssl) | 3.0.2-0ubuntu1.30 | Apache 2.0 |
| `libz.so.1` | zlib1g (zlib) | 1:1.2.11.dfsg-2ubuntu9.2 | zlib |
| `liblzma.so.5` | liblzma5 (xz-utils) | 5.2.5-2ubuntu1.2 | public domain |
| `libzstd.so.1` | libzstd1 (libzstd) | 1.4.8+dfsg-3build1 | BSD or GPL-2 |
| `libcap.so.2` | libcap2 (libcap2) | 1:2.44-1ubuntu0.22.04.3 | BSD or GPL-2 |
| `libseccomp.so.2` | libseccomp2 (libseccomp) | 2.5.3-2ubuntu3~22.04.1 | LGPL 2.1 |
| `libsystemd.so.0` | libsystemd0 (systemd) | 249.11-0ubuntu3.23 | LGPL 2.1 or later |
| `liblz4.so.1` | liblz4-1 (lz4) | 1.9.3-2build2 | 2-clause BSD |
| `libgcrypt.so.20` | libgcrypt20 (libgcrypt20) | 1.9.4-3ubuntu3.3 | LGPL 2.1 or later |
| `libgpg-error.so.0` | libgpg-error0 (libgpg-error) | 1.43-3 | LGPL 2.1 or later |

OpenSSL is Ubuntu's 3.0.2, with the security fixes Ubuntu has backported to
it (`3.0.2-0ubuntu1.30`); `tor --version` names it 3.0.2.

- **Replacing them:** extract the AppImage and put your own tor and libraries
  in `usr/lib/tor`, or run the AppImage with `SEALEDLORE_TOR` set to the path
  of another tor.

## Python packages

Installed unmodified in `opt/python3.12/lib/python3.12/site-packages`, and
licence paths are relative to that folder. ONNX Runtime is there less its
model tools (its `transformers`, `quantization` and `tools` folders), which
SealedLore never loads. tokenizers names huggingface-hub as a requirement,
for a downloader SealedLore never calls, so it isn't included.

@PYTHON_PACKAGES@

## Tokenizer data

`usr/share/sealedlore/tiktoken` holds the `o200k_base` encoding file that
tiktoken (MIT, above) downloads from OpenAI. It is bundled so the first run
works offline.

## The AppImage runtime

The start of the AppImage file is the AppImage type 2 runtime, release
20251108 (<https://github.com/AppImage/type2-runtime/releases/tag/20251108>,
which has its source). It is under the MIT License
(`texts/AppImage-runtime.txt`) and contains, statically linked:

- libfuse, under the GNU Lesser General Public License, version 2.1
  (`texts/LGPL-2.1.txt`);
- squashfuse, under a BSD-style licence (`texts/squashfuse.txt`);
- Zstandard, under the BSD License (`texts/zstd.txt`).
