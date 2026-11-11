"""Cards and lorebooks by link (providers/card_links.py): each site's way to
fetch them, read with the same readers as a file (storage/tavern_cards.py
`tavern_from_file`), and every refusal in words. A scripted transport stands
in for the sites: nothing here reaches the network."""

from __future__ import annotations

import io
import json
import threading
import zipfile

import httpx
import pytest

from sealedlore.models.lore import LoreEntry
from sealedlore.providers import card_links
from sealedlore.providers.card_links import (
    BOT_CHECK,
    WEB_PAGE,
    LinkError,
    LinkLarge,
    LinkRefused,
    LinkStopped,
    fetch_link,
    read_link,
)
from sealedlore.storage.png_text import text_chunks
from sealedlore.storage.tavern_cards import TavernCard, tavern_from_file
from sealedlore.storage.tavern_lore import book_from_lore, lore_from_book
from tests.test_tavern import ELENA, tiny_png, v2

CHUB_META = {
    "node": {
        "id": 2017730,
        "name": "Elena",
        "fullPath": "someone/elena-1234",
        "topics": ["survival"],
        "max_res_url": "https://avatars.charhub.io/avatars/someone/elena-1234/chara_card_v2.png",
        "avatar_url": "https://avatars.charhub.io/avatars/someone/elena-1234/avatar.webp",
        "definition": {
            "name": "Elena",
            # Chub's names: its "personality" is a card's description, its
            # "description" the creator's notes.
            "personality": ELENA["description"],
            "tavern_personality": ELENA["personality"],
            "description": "A slow-burn survival story.",
            "scenario": ELENA["scenario"],
            "first_message": ELENA["first_mes"],
            "example_dialogs": ELENA["mes_example"],
            "system_prompt": "",
            "post_history_instructions": "",
            "alternate_greetings": ["The door is barred."],
            "embedded_lorebook": ELENA["character_book"],
            "extensions": {},
        },
    }
}


class Sites:
    """A scripted web: path (with its query) → response, and what was asked."""

    def __init__(self, routes: dict[str, httpx.Response | Exception]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        address = f"{request.url.scheme}://{request.url.host}{raw}"
        self.asked.append(address)
        answer = self.routes.get(address)
        if answer is None:
            return httpx.Response(404, json={"error": "Not Found"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def fetched_card(link: str, sites: Sites):
    result = fetch_link(link, transport=sites.transport())
    try:
        return tavern_from_file(result.file, result.name, result.picture, lorebook=result.lorebook)
    finally:
        result.close()


def charx(prefix: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    fields = {**ELENA, "assets": [{"type": "icon", "uri": "embeded://assets/main.png"}]}
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("card.json", json.dumps({"spec": "chara_card_v3", "data": fields}))
        archive.writestr("assets/main.png", tiny_png())
    return prefix + buffer.getvalue()


# --- reading a link -------------------------------------------------------------------


ELENA_PAGE = ("chub_character", "someone", "elena-1234")


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://chub.ai/characters/someone/elena-1234", ELENA_PAGE),
        ("https://www.chub.ai/characters/someone/elena-1234?tab=x", ELENA_PAGE),
        ("https://venus.chub.ai/characters/someone/elena-1234", ELENA_PAGE),
        ("https://characterhub.org/characters/someone/elena-1234", ELENA_PAGE),
        ("https://chub.ai/someone/elena-1234", ELENA_PAGE),
        ("https://chub.ai/lorebooks/someone/the-ridge", ("chub_lorebook", "someone", "the-ridge")),
        ("https://realm.risuai.net/character/7adb0ed8-d818-55c8", ("risu", "", "")),
        ("https://files.catbox.moe/abc123.png", ("file", "", "")),
        ("https://avatars.charhub.io/avatars/someone/elena-1234/card.png", ("file", "", "")),
        ("http://localhost:8000/card.json", ("file", "", "")),
    ],
)
def test_a_link_is_read_as_its_site_s(link: str, expected: tuple[str, str, str]):
    read = read_link(link)
    assert (read.kind, read.creator, read.slug) == expected


@pytest.mark.parametrize(
    ("link", "said"),
    [
        ("chub.ai/characters/a/b", "isn't a web link"),
        ("ftp://example.com/card.png", "isn't a web link"),
        ("http://example.com/card.png", "Only https links"),
        ("https://chub.ai/characters/someone", "doesn't name a character or a lorebook"),
        ("https://janitorai.com/characters/1234abcd_character-elena", "JanitorAI"),
        ("https://jannyai.com/characters/1234abcd_character-elena", "JanitorAI"),
    ],
)
def test_a_link_that_can_t_be_fetched_says_why_before_anything_is_sent(link: str, said: str):
    sites = Sites({})
    with pytest.raises(LinkError, match=said):
        fetch_link(link, transport=sites.transport())
    assert sites.asked == []


# --- the sites ------------------------------------------------------------------------


def test_a_chub_character_is_built_from_its_fields_with_its_picture():
    sites = Sites(
        {
            "https://api.chub.ai/api/characters/someone/elena-1234?full=true": httpx.Response(
                200, json=CHUB_META
            ),
            CHUB_META["node"]["max_res_url"]: httpx.Response(
                200, content=tiny_png(), headers={"content-type": "image/png"}
            ),
        }
    )
    card = fetched_card("https://chub.ai/characters/someone/elena-1234", sites)
    assert isinstance(card, TavernCard) and card.name == "Elena"
    assert card.text("description") == ELENA["description"]
    assert card.text("personality") == ELENA["personality"]
    assert card.text("creator_notes") == "A slow-burn survival story."
    assert card.greetings[0] == ELENA["first_mes"] and len(card.greetings) == 2
    assert card.book is not None
    assert card.picture is not None and text_chunks(card.picture) == {}


def test_a_chub_character_whose_picture_won_t_come_comes_without_it():
    sites = Sites(
        {
            "https://api.chub.ai/api/characters/someone/elena-1234?full=true": httpx.Response(
                200, json=CHUB_META
            ),
        }
    )
    card = fetched_card("https://chub.ai/characters/someone/elena-1234", sites)
    assert card.name == "Elena" and card.picture is None
    assert len(sites.asked) == 3  # the card, then each picture Chub names


def test_a_chub_lorebook_is_chub_s_own_export():
    book = book_from_lore([LoreEntry(title="The ridge", content="Cold.", keywords=["ridge"])])
    export = (
        "https://api.chub.ai/api/v4/projects/318055/repository/files/"
        "raw%252Fsillytavern_raw.json/raw"
    )
    sites = Sites(
        {
            "https://api.chub.ai/api/lorebooks/someone/the-ridge": httpx.Response(
                200, json={"node": {"id": 318055}}
            ),
            export: httpx.Response(200, json=book),
        }
    )
    found = fetched_card("https://chub.ai/lorebooks/someone/the-ridge", sites)
    assert [entry.title for entry in lore_from_book(found, str).entries] == ["The ridge"]
    # Sent as Chub asks for it, encoded twice, not encoded a third time.
    assert sites.asked[-1] == export


RISU_PNG = "https://realm.risuai.net/api/v1/download/png-v3/7adb0ed8-d818"
RISU_CHARX = "https://realm.risuai.net/api/v1/download/charx-v3/7adb0ed8-d818"
NOT_IN_THIS_FORMAT = httpx.Response(
    403,
    json={
        "error": "Forbidden",
        "message": "This card is not allowed to be downloaded in this format.",
    },
)


def png_card_bytes() -> bytes:
    import base64

    from sealedlore.storage.png_text import with_text

    return with_text(tiny_png(), {"chara": base64.b64encode(json.dumps(v2()).encode()).decode()})


def test_an_older_risurealm_card_is_its_png_card():
    sites = Sites({RISU_PNG: httpx.Response(200, content=png_card_bytes())})
    card = fetched_card("https://realm.risuai.net/character/7adb0ed8-d818", sites)
    assert card.name == "Elena" and card.picture
    assert sites.asked == [RISU_PNG]  # never the archive


def test_a_newer_risurealm_card_is_its_charx_even_with_a_picture_before_the_zip():
    sites = Sites(
        {
            RISU_PNG: NOT_IN_THIS_FORMAT,
            RISU_CHARX: httpx.Response(200, content=charx(prefix=b"\xff\xd8\xff\xe0 a picture")),
        }
    )
    card = fetched_card("https://realm.risuai.net/character/7adb0ed8-d818", sites)
    assert card.name == "Elena" and card.spec == "chara_card_v3" and card.picture
    assert sites.asked == [RISU_PNG, RISU_CHARX]


def test_risurealm_s_own_refusal_is_passed_on():
    sites = Sites({RISU_PNG: NOT_IN_THIS_FORMAT, RISU_CHARX: NOT_IN_THIS_FORMAT})
    with pytest.raises(LinkError, match="RisuRealm refused \\(HTTP 403\\): This card is not"):
        fetch_link("https://realm.risuai.net/character/7adb0ed8-d818", transport=sites.transport())


def test_a_large_card_file_is_asked_about_before_it_is_read(monkeypatch):
    monkeypatch.setattr(card_links, "ASK_BYTES", 1000)
    big = charx() + b"\0" * 2000  # a zip's directory is found from its end: still a CHARX
    sites = Sites({RISU_PNG: NOT_IN_THIS_FORMAT, RISU_CHARX: httpx.Response(200, content=big)})
    link = "https://realm.risuai.net/character/7adb0ed8-d818"
    with pytest.raises(LinkLarge) as raised:
        fetch_link(link, transport=sites.transport())
    assert raised.value.size == len(big)
    result = fetch_link(link, large_ok=True, transport=sites.transport())
    try:
        assert tavern_from_file(result.file, result.name).name == "Elena"
    finally:
        result.close()
    # No size said: asked once it runs past.
    plain = "https://example.com/card.charx"
    streamed = Sites({plain: httpx.Response(200, content=iter([big[:900], big[900:]]))})
    with pytest.raises(LinkLarge) as raised:
        fetch_link(plain, transport=streamed.transport())
    assert raised.value.size is None


def test_chub_s_pictures_are_never_asked_about(monkeypatch):
    monkeypatch.setattr(card_links, "ASK_BYTES", 10)
    sites = Sites(
        {
            "https://api.chub.ai/api/characters/someone/elena-1234?full=true": httpx.Response(
                200, json=CHUB_META
            ),
            CHUB_META["node"]["max_res_url"]: httpx.Response(200, content=tiny_png()),
        }
    )
    card = fetched_card("https://chub.ai/characters/someone/elena-1234", sites)
    assert card.name == "Elena" and card.picture


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        (b"", "png"),
        (json.dumps(v2()).encode(), "card"),
        (json.dumps(book_from_lore([LoreEntry(title="A", content="B")])).encode(), "lorebook"),
    ],
)
def test_a_link_straight_to_a_file_is_read_by_what_it_is(content: bytes, kind: str):
    import base64

    from sealedlore.storage.png_text import with_text

    if kind == "png":
        content = with_text(
            tiny_png(), {"chara": base64.b64encode(json.dumps(v2()).encode()).decode()}
        )
    link = "https://files.catbox.moe/abc123"  # no suffix: the bytes say what it is
    sites = Sites({link: httpx.Response(200, content=content)})
    found = fetched_card(link, sites)
    if kind == "lorebook":
        assert not isinstance(found, TavernCard)
    else:
        assert isinstance(found, TavernCard) and found.name == "Elena"


# --- refusals --------------------------------------------------------------------------


def test_a_web_page_is_not_a_card():
    link = "https://example.com/elena"
    sites = Sites(
        {
            link: httpx.Response(
                200, text="<html><body>Elena</body></html>", headers={"content-type": "text/html"}
            )
        }
    )
    with pytest.raises(LinkError, match=WEB_PAGE[:30]):
        fetch_link(link, transport=sites.transport())


def test_a_bot_check_is_named_for_what_it_is():
    link = "https://example.com/card.png"
    page = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
    sites = Sites({link: httpx.Response(403, text=page, headers={"content-type": "text/html"})})
    with pytest.raises(LinkError, match=BOT_CHECK.format(host="example.com")[:40]):
        fetch_link(link, transport=sites.transport())


def test_nothing_there_and_a_busy_site_are_said_plainly():
    with pytest.raises(LinkError, match="Chub has nothing at that address"):
        fetch_link("https://chub.ai/characters/a/b", transport=Sites({}).transport())
    link = "https://example.com/card.png"
    sites = Sites({link: httpx.Response(429)})
    with pytest.raises(LinkError, match="fewer requests"):
        fetch_link(link, transport=sites.transport())
    sites = Sites({link: httpx.ReadTimeout("slow")})
    with pytest.raises(LinkError, match="didn't answer in time"):
        fetch_link(link, transport=sites.transport())


def test_a_refusal_is_read_only_as_far_as_its_words():
    """A refusal's body was read whole: a 404 streaming 300 MB was held in
    memory, all of it. Only its start is read now, a piece at a time."""
    link = "https://example.com/card.png"
    sent: list[int] = []

    def endless():
        message = json.dumps({"message": "No such card here"}).encode()
        sent.append(len(message))
        yield message
        for _ in range(64):  # 64 MB, were it all read
            sent.append(1024 * 1024)
            yield b" " * (1024 * 1024)

    sites = Sites({link: httpx.Response(410, content=endless())})
    with pytest.raises(LinkRefused, match="No such card here") as refused:
        fetch_link(link, transport=sites.transport())
    assert refused.value.status == 410
    assert sum(sent) < 2 * 1024 * 1024, "read no further than its words"


def test_too_large_is_refused_by_what_it_says_and_by_what_comes(monkeypatch):
    link = "https://example.com/card.png"
    declared = httpx.Response(200, content=b"x", headers={"content-length": str(2**40)})
    with pytest.raises(LinkError, match="too large"):
        fetch_link(link, transport=Sites({link: declared}).transport())
    monkeypatch.setattr(card_links, "MAX_ARCHIVE_BYTES", 1000)
    streamed = httpx.Response(200, content=iter([b"x" * 600, b"x" * 600]))
    with pytest.raises(LinkError, match="too large"):
        fetch_link(link, transport=Sites({link: streamed}).transport())


def test_a_large_download_is_kept_out_of_memory_and_read_whole(monkeypatch):
    monkeypatch.setattr(card_links, "SPOOL_BYTES", 64)
    link = "https://example.com/elena.charx"
    sites = Sites({link: httpx.Response(200, content=charx())})
    result = fetch_link(link, transport=sites.transport())
    try:
        assert result.file._rolled  # on disk past SPOOL_BYTES, unnamed
        card = tavern_from_file(result.file, result.name)
    finally:
        result.close()
    assert card.name == "Elena"


def test_a_redirect_off_https_is_refused():
    link = "https://example.com/card.png"
    sites = Sites({link: httpx.Response(302, headers={"location": "http://example.org/card.png"})})
    with pytest.raises(LinkError, match="insecure"):
        fetch_link(link, transport=sites.transport())


def test_stopping_ends_it_before_anything_more_is_read():
    stop = threading.Event()
    stop.set()
    sites = Sites({})
    with pytest.raises(LinkStopped):
        fetch_link("https://example.com/card.png", stop=stop, transport=sites.transport())
    assert sites.asked == []
