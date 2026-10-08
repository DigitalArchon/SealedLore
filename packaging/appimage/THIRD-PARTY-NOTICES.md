# Third-party software in the SealedLore AppImage

SealedLore itself is licensed under the GNU Affero General Public License,
version 3 or later (`LICENSE`, beside this file). Its source is at
<https://github.com/DigitalArchon/SealedLore>, tagged for each release.

The AppImage also contains the software listed below. Each part keeps its own
licence, and nothing in SealedLore's licence changes those terms. Paths are
inside the AppImage: run it with `--appimage-extract` to see them. A path
starting `texts/` is in `usr/share/licenses/sealedlore/texts/`, which holds
the licence texts the components don't carry themselves.

## Qt and Qt for Python

PySide6-Essentials 6.11.2 and shiboken6 6.11.2, which contain Qt 6.11.2, are
in `opt/python3.12/lib/python3.12/site-packages/PySide6` and `shiboken6`,
unmodified. They are used under the GNU Lesser General Public License,
version 3 (`texts/LGPL-3.0.txt`, which adds to `texts/GPL-3.0.txt`).

- **Source:** Qt 6.11.2 at <https://download.qt.io/archive/qt/6.11/6.11.2/single/>
  and Qt for Python 6.11.2 at
  <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/>
  (also tag `v6.11.2` of <https://code.qt.io/cgit/pyside/pyside-setup.git>).
- **Replacing them:** extract the AppImage, put your own build of these
  libraries in place of the bundled ones, and run `AppRun` from the extracted
  folder (or repack it with appimagetool).
- **Qt's own third-party code** (image formats, fonts, text shaping and so
  on) is listed, with its licences, at
  <https://doc.qt.io/qt-6.11/licenses-used-in-qt.html>.

Of PySide6-Addons 6.11.2, only Qt Multimedia is kept (`QtMultimedia.abi3.so`,
`Qt/lib/libQt6Multimedia.so.6`, `Qt/plugins/multimedia/libffmpegmediaplugin.so`),
for playing videos, under the same licence and from the same sources as above.

## FFmpeg

Qt Multimedia plays video with FFmpeg 7.1, as built and shipped by The Qt
Company in PySide6-Addons 6.11.2: `libavcodec.so.61`, `libavformat.so.61`,
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

## Python packages

Installed unmodified in `opt/python3.12/lib/python3.12/site-packages`, and
licence paths are relative to that folder.

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
