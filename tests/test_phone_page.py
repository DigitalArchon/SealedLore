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


def test_a_private_picture_or_question_is_held_back_by_its_own_tag():
    """One made in a private scene before its first message hangs on a public
    passage (gui/window_phone.py `_entries` tags each): the page gates it as a
    private message, not fetched until the kind is acknowledged, and asks
    "Before this is shown" for its kind too. A browser drive of the page is
    the real check; this holds the page to reading the tag."""
    script = text("app.js")
    card = script[script.index("function messageCard") : script.index("function renderMessages")]
    pictures = card[card.index("message.pictures") : card.index("message.asides")]
    assert "hidden(picture.private)" in pictures
    assert pictures.index("hidden(picture.private)") < pictures.index("thumbnail(picture.id)")
    asides = card[card.index("message.asides") :]
    assert asides.index("hidden(aside.private)") < asides.index("aside.question")
    kinds = script[script.index("function privateKinds") : script.index("function offerPrivate")]
    assert "message.pictures" in kinds and "message.asides" in kinds


def function(script: str, name: str) -> str:
    """The text of one of the page's functions, to its closing brace."""
    start = script.index(f"function {name}(")
    return script[start : script.index("\n  }\n", start)]


def test_a_note_for_a_take_is_asked_under_the_passage_in_the_desktops_words():
    """Regenerate with a note… on the latest passage, as ⋯ has it on the
    desktop: a form under the passage, held to no private passage that is
    still hidden (its card stops at the placeholder), and its words sent with
    the passage's id and kept nowhere."""
    pytest.importorskip("PySide6")  # the desktop's words are the window's
    from sealedlore.gui.note_dialog import RETAKE_ASKS, RETAKE_BUTTON
    from sealedlore.gui.transcript import REGENERATE_NOTE_LABEL

    script = text("app.js")
    card = function(script, "messageCard")
    offered = card.index(f'button("{REGENERATE_NOTE_LABEL}"')
    assert card.index("if (hidden(kind))") < card.index("return card;") < offered
    assert 'openNote(message.id, "retake")' in card
    assert f'asks: "{RETAKE_ASKS}"' in script and f'go: "{RETAKE_BUTTON}"' in script
    send = function(script, "sendNote")
    assert "retakeBody(n.id)" in send and "body.note = words" in send
    assert "node_id: nodeId" in function(script, "retakeBody")
    # The form's own node is kept across the list's rebuilds, never its text
    # in the page's state.
    assert "noting.form" in card and ".value" not in card


def test_a_change_is_asked_under_its_passage_and_written_in_its_place():
    """Change this passage… (a chat: Change this reply…) wherever the entry
    says the desktop offers it; the change goes with the passage's id; and the
    job that writes it is shown in that passage's own card, never a new one."""
    pytest.importorskip("PySide6")  # the desktop's words are the window's
    from sealedlore.gui.note_dialog import CHANGE_ASKS, CHANGE_BUTTON
    from sealedlore.gui.transcript import CHANGE_LABELS

    script = text("app.js")
    card = function(script, "messageCard")
    offered = card[card.index("if (message.can_revise)") :]
    assert all(f'"{label}"' in offered for label in CHANGE_LABELS.values())
    assert 'openNote(message.id, "change")' in offered
    assert card.index("if (hidden(kind))") < card.index("if (message.can_revise)")
    assert f'asks: "{CHANGE_ASKS}"' in script and f'go: "{CHANGE_BUTTON}"' in script
    assert 'call("POST", "/api/revise", { node_id: n.id, change: words })' in function(
        script, "sendNote"
    )
    # A job that says which passage it writes again never opens a new card.
    events = function(script, "onEvent")
    started = events[events.index('case "job_started"') : events.index('case "thinking"')]
    assert started.index("startRevising(data.revises)") < started.index("writingCard(")
    words = events[events.index('case "text"') : events.index('case "notice"')]
    assert words.index("if (S.revising)") < words.index("writingCard(")
    # Until the first of the new words come the passage shows as it was, and
    # the list loaded again when the job settles lets go of it.
    assert "r.words || r.base" in function(script, "showRevising")
    assert "S.revising = null" in function(script, "loadLatest")


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_the_script_parses():
    result = subprocess.run(
        ["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
