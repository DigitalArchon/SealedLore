"""Put tiktoken's vocabulary where tiktoken looks for it, for the test suite.

The tests never use the network (tests/conftest.py refuses it), so where
tiktoken has nothing cached they count tokens approximately. CI runs this
first, so the suite counts as the app does: the same file the AppImage
carries, from the address and with the sha256 that packaging/appimage/build.sh
pins, saved under the name tiktoken caches it by (the sha1 of its address).

    python tools/fetch_tokenizer.py DIR     # then TIKTOKEN_CACHE_DIR=DIR
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import httpx

BUILD = Path(__file__).resolve().parents[1] / "packaging" / "appimage" / "build.sh"


def pinned() -> tuple[str, str]:
    text = BUILD.read_text(encoding="utf-8")
    url = re.search(r'^TIKTOKEN_URL="(https://[^"]+)"$', text, re.MULTILINE)
    sha = re.search(r'^TIKTOKEN_SHA256="([0-9a-f]{64})"$', text, re.MULTILINE)
    if not url or not sha:
        raise SystemExit(f"no pinned tokenizer in {BUILD}")
    return url.group(1), sha.group(1)


def main(folder: str) -> None:
    url, sha = pinned()
    target = Path(folder) / hashlib.sha1(url.encode()).hexdigest()
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
        print(f"already there: {target}")
        return
    # No redirects: the pinned address serves the file itself.
    reply = httpx.get(url, follow_redirects=False, timeout=60)
    reply.raise_for_status()
    got = hashlib.sha256(reply.content).hexdigest()
    if got != sha:
        raise SystemExit(f"{url}: sha256 {got}, pinned {sha}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(reply.content)
    print(f"{target} ({len(reply.content)} bytes, sha256 {sha})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
