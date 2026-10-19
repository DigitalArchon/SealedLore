"""The phone page itself (phone/static/, phone/page.py): served as written,
and held to what keeps it safe on a phone that isn't ours.

- No inline script or style and no inline event handlers: the CSP allows
  'self' only, and a page that needed them would be one that could be fed
  them.
- Story text goes in as text: no innerHTML or anything like it.
- Nothing loaded from anywhere else, and nothing kept on the phone (no
  localStorage, sessionStorage or IndexedDB): the bookmark is the only thing
  the phone holds, and Tor Browser forgets the rest.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from importlib import resources

import pytest

from sealedlore.phone.api import Api
from sealedlore.phone.http import RequestParser
from sealedlore.phone.page import FILES, SOURCE, page_files
from sealedlore.phone.pairing import Pairing

STATIC = resources.files("sealedlore.phone") / "static"
ONION = "a" * 56 + ".onion"


def text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_every_file_is_served_with_its_type_and_the_version_written_in():
    files = page_files("9.9.9")
    assert set(files) == set(FILES)
    index, content_type = files["/index.html"]
    assert content_type == "text/html"
    assert b"SealedLore 9.9.9" in index and SOURCE.encode() in index
    assert b"@VERSION@" not in index and b"@SOURCE@" not in index
    assert files["/app.js"][1] == "application/javascript"


def test_the_page_needs_no_login_and_comes_with_the_security_headers():
    class NoBackend:
        def open_story(self):
            return None

    api = Api(NoBackend(), pairing=Pairing(), onion=lambda: ONION, static=page_files("1"))
    raw = f"GET / HTTP/1.1\r\nHost: {ONION}\r\n\r\n".encode()
    (request,) = RequestParser().feed(raw)
    response = api.handle(request)
    assert response.status == 200 and b"<!doctype html>" in response.body
    (request,) = RequestParser().feed(raw.replace(b"GET /", b"GET /app.js"))
    assert api.handle(request).status == 200


def test_the_html_has_no_inline_script_style_or_handlers():
    html = text("index.html")
    for script in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/' in script, script
    assert re.search(r"<script\b[^>]*>\s*[^<\s]", html) is None  # nothing between the tags
    assert "<style" not in html
    assert re.search(r"\sstyle\s*=", html) is None
    assert re.search(r"\son[a-z]+\s*=", html) is None


def test_nothing_is_loaded_from_anywhere_else():
    html = text("index.html")
    loads = re.findall(r'(?:src|href)="([^"]+)"', html)
    # The one address out is the source code link, a page the author may
    # open, never something this page loads.
    assert [url for url in loads if not url.startswith("/")] == ["@SOURCE@"]
    assert re.search(r"https?://", text("app.js")) is None
    css = text("app.css")
    assert "@import" not in css and re.search(r"url\(", css) is None


def test_story_text_never_goes_in_as_html_and_nothing_is_kept_on_the_phone():
    script = text("app.js")
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        'setAttribute("style"',
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
    ):
        assert banned not in script, banned


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_the_script_parses():
    result = subprocess.run(
        ["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
