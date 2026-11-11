"""SillyTavern cards and lorebooks fetched by link, from the sites that share them.

A link is read as SillyTavern reads one (its content manager's importURL, as
of 1.19.0), for the sites whose way of handing a card out was checked here:

- Chub (chub.ai, characterhub.org): a character's fields come from Chub's API
  and its picture from Chub's own copy, the card built as SillyTavern builds
  it; a lorebook is Chub's own SillyTavern export of it.
- RisuRealm: what its own Download button gives. An older card is a PNG
  card (its picture with the card inside); a newer one is offered only as a
  CHARX, which carries the card's whole set of expression pictures (82 MB
  for one), so a large one is asked about first (`LinkLarge`). Its JSON
  export has no picture, so it isn't used.
- Anything else is downloaded as it is and must turn out to be a card (PNG,
  JSON or CHARX) or a lorebook (JSON): a link straight to the file.

JanitorAI gives no download of its own: SillyTavern goes through JannyAI's
copy, which answers programs with a bot check, so such a link is refused here
with the way round it (the card's file). Nothing of the author's is sent: a
plain GET each time, https only (http on this machine, as everywhere), and
nothing followed off https (`make_client`). What comes is kept in memory up
to `SPOOL_BYTES` and past that in an unnamed temporary file, to
`MAX_ARCHIVE_BYTES` (a CHARX, read in place) or `MAX_BYTES` (anything read
whole). A card's file over `ASK_BYTES` is fetched only once the author has
said yes (`large_ok`): it is nearly all pictures SealedLore doesn't keep.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import quote, unquote, urlparse

import httpx

from sealedlore.models.config import require_https
from sealedlore.providers.base import ProviderError
from sealedlore.providers.http import make_client

# As a card file chosen on disk (storage/tavern_cards.py): read whole, a PNG
# or JSON; read in place, a CHARX, which may carry a set of expression
# pictures (RisuRealm's run past a hundred megabytes).
MAX_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
SPOOL_BYTES = 8 * 1024 * 1024
# Past this a card's file is asked about before it is downloaded: a card is
# text and a picture, a few megabytes at most.
ASK_BYTES = 20 * 1024 * 1024
# Of a site's refusal, only its message is read: from the start of the body.
REFUSAL_BYTES = 20_000
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
USER_AGENT = "SealedLore"
CHUB_API = "https://api.chub.ai"
RISU = "https://realm.risuai.net"
CHUB_HOSTS = ("chub.ai", "characterhub.org")
RISU_HOST = "realm.risuai.net"
JANITOR_HOSTS = ("janitorai.com", "jannyai.com")

JANITOR = (
    "JanitorAI doesn't let programs download its characters (SillyTavern goes "
    "through JannyAI's copy, which turns programs away). If the character's page "
    "offers its card as a file, download it and import that (File → Import…)."
)
WEB_PAGE = (
    "That link is a web page, not a card's file. Open it in a browser and copy the "
    "card's download link instead, or download the card and import it (File → Import…)."
)
BOT_CHECK = (
    "{host} turns programs away with a bot check, so SealedLore can't fetch from it. "
    "Open the link in a browser, download the card, and import it (File → Import…)."
)
TOO_LARGE = "That file is too large to be a card ({size} MB at most)."
_RISU_ID = re.compile(r"[0-9A-Fa-f-]{8,64}")


class LinkError(Exception):
    """A link that can't be fetched, in words for the author."""


class LinkStopped(LinkError):
    """The author stopped waiting."""


class LinkRefused(LinkError):
    """The site said no (an HTTP error), in its words; `status` is its code."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class LinkLarge(LinkError):
    """The card's file is over ASK_BYTES: nothing was read yet. `size` in
    bytes when the site said it, else None (it ran past while coming)."""

    def __init__(self, size: int | None) -> None:
        super().__init__("That card's file is large.")
        self.size = size


@dataclass(frozen=True, eq=False)
class Fetched:
    """What a link brought: the file, rewound, as the card and lorebook
    readers take it (`tavern_from_file`), the name to call it by, and, for a
    card built from a site's own fields (Chub's), the picture that goes with
    it. `lorebook` when the site said that is what it is. Close it once
    read: past SPOOL_BYTES it is a temporary file."""

    file: BinaryIO
    name: str
    site: str
    picture: bytes | None = None
    lorebook: bool = False

    def close(self) -> None:
        self.file.close()


@dataclass(frozen=True)
class Link:
    """A link read: which site's way to fetch it, and what that needs."""

    kind: str  # "chub_character", "chub_lorebook", "risu" or "file"
    url: str
    host: str
    creator: str = ""
    slug: str = ""
    ident: str = ""


def _host_is(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def read_link(text: str) -> Link:
    """A pasted link, read as a site's or as a file's; LinkError when it is
    neither a link SealedLore may fetch nor one it can do anything with."""
    url = text.strip()
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        _ = parsed.port  # a port that isn't a number, or past 65535
    except ValueError:
        # Half typed or mangled ("https://[", a port of letters): said as
        # the rest are, never raised out of the dialog reading it as typed.
        raise LinkError(
            "That address can't be read: check it was pasted whole, starting with https://."
        ) from None
    if parsed.scheme not in ("http", "https") or not host:
        raise LinkError(
            "That isn't a web link: paste the address of the card's page or file, "
            "starting with https://."
        )
    try:
        require_https(url)
    except ValueError:
        raise LinkError(
            "Only https links are fetched (plain http only from this computer)."
        ) from None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if _host_is(host, JANITOR_HOSTS):
        raise LinkError(JANITOR)
    if _host_is(host, CHUB_HOSTS):
        # chub.ai/characters/creator/slug, the same on venus. or www., and
        # SillyTavern's shorthand chub.ai/creator/slug for a character.
        lowered = [part.lower() for part in parts]
        for kind in ("characters", "lorebooks"):
            if kind in lowered:
                at = lowered.index(kind)
                if len(parts) >= at + 3:
                    return Link(
                        "chub_character" if kind == "characters" else "chub_lorebook",
                        url,
                        host,
                        creator=parts[at + 1],
                        slug=parts[at + 2],
                    )
        if len(parts) == 2 and lowered[0] not in ("characters", "lorebooks"):
            return Link("chub_character", url, host, creator=parts[0], slug=parts[1])
        raise LinkError(
            "That Chub link doesn't name a character or a lorebook: copy the address "
            "of the character's or lorebook's own page."
        )
    if host == RISU_HOST and len(parts) >= 2 and parts[0] == "character":
        if _RISU_ID.fullmatch(parts[1]):
            return Link("risu", url, host, ident=parts[1])
    return Link("file", url, host)


def fetch_link(
    text: str,
    *,
    stop: threading.Event | None = None,
    large_ok: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> Fetched:
    """The card or lorebook a link leads to, read by its site's way. Blocks:
    run it off the GUI thread. `stop` set ends it at the next piece read
    (LinkStopped); a card's file over ASK_BYTES raises LinkLarge unless
    `large_ok`; `transport` is a test's."""
    link = read_link(text)
    kwargs: dict[str, Any] = {
        "timeout": TIMEOUT,
        "follow_redirects": True,
        "headers": {"User-Agent": USER_AGENT},
    }
    if transport is not None:
        kwargs["transport"] = transport
    with make_client(link.url, **kwargs) as client:
        get = _Getter(client, stop, large_ok)
        if link.kind == "chub_character":
            return _chub_character(get, link)
        if link.kind == "chub_lorebook":
            return _chub_lorebook(get, link)
        if link.kind == "risu":
            return _risu(get, link)
        return _file(get, link)


# --- the sites ---------------------------------------------------------------------------


def _chub_character(get: _Getter, link: Link) -> Fetched:
    path = f"{quote(link.creator)}/{quote(link.slug)}"
    meta = get.json(f"{CHUB_API}/api/characters/{path}?full=true", "Chub")
    node = meta.get("node") if isinstance(meta, dict) else None
    definition = node.get("definition") if isinstance(node, dict) else None
    if not isinstance(node, dict) or not isinstance(definition, dict):
        raise LinkError("Chub didn't send that character's card.")

    def text(key: str) -> str:
        value = definition.get(key)
        return value if isinstance(value, str) else ""

    greetings = definition.get("alternate_greetings")
    topics = node.get("topics")
    extensions = definition.get("extensions")
    # Chub's names for a card's fields, as SillyTavern maps them: Chub's
    # "personality" is the card's description, its "description" the
    # creator's notes.
    data: dict[str, Any] = {
        "name": text("name") or str(node.get("name") or link.slug),
        "description": text("personality"),
        "personality": text("tavern_personality"),
        "scenario": text("scenario"),
        "first_mes": text("first_message"),
        "mes_example": text("example_dialogs"),
        "creator_notes": text("description"),
        "system_prompt": text("system_prompt"),
        "post_history_instructions": text("post_history_instructions"),
        "alternate_greetings": [g for g in greetings if isinstance(g, str)]
        if isinstance(greetings, list)
        else [],
        "tags": [t for t in topics if isinstance(t, str)] if isinstance(topics, list) else [],
        "creator": link.creator,
        "character_version": "",
        "extensions": extensions if isinstance(extensions, dict) else {},
    }
    book = definition.get("embedded_lorebook")
    if isinstance(book, dict) and book.get("entries"):
        data["character_book"] = book
    card = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}
    picture = None
    for key in ("max_res_url", "avatar_url"):
        address = node.get(key)
        if isinstance(address, str) and address.startswith("https://"):
            try:
                picture, _type = get.bytes(address, "Chub")
            except LinkStopped:
                raise
            except LinkError:
                continue  # the card without its picture, as SillyTavern falls back
            break
    return Fetched(
        io.BytesIO(json.dumps(card, ensure_ascii=False).encode("utf-8")),
        f"{link.slug}.json",
        "Chub",
        picture=picture,
    )


def _chub_lorebook(get: _Getter, link: Link) -> Fetched:
    path = f"{quote(link.creator)}/{quote(link.slug)}"
    meta = get.json(f"{CHUB_API}/api/lorebooks/{path}", "Chub")
    node = meta.get("node") if isinstance(meta, dict) else None
    project = node.get("id") if isinstance(node, dict) else None
    if not isinstance(project, int) or isinstance(project, bool):
        raise LinkError("Chub didn't say where that lorebook is.")
    # Chub's SillyTavern export, its path encoded twice as Chub asks for it.
    file, _type = get.file(
        f"{CHUB_API}/api/v4/projects/{project}/repository/files/raw%252Fsillytavern_raw.json/raw",
        "Chub",
    )
    return Fetched(file, f"{link.slug}.json", "Chub", lorebook=True)


def _risu(get: _Getter, link: Link) -> Fetched:
    """The PNG card an older card is offered as; else the CHARX, the only way
    a newer one is (RisuRealm answers the other with 400 or 403)."""
    ident = quote(link.ident)
    try:
        file, _type = get.file(f"{RISU}/api/v1/download/png-v3/{ident}", "RisuRealm")
        return Fetched(file, f"{link.ident}.png", "RisuRealm")
    except LinkRefused as exc:
        if exc.status not in (400, 403):
            raise
    file, _type = get.file(
        f"{RISU}/api/v1/download/charx-v3/{ident}", "RisuRealm", limit=MAX_ARCHIVE_BYTES, ask=True
    )
    return Fetched(file, f"{link.ident}.charx", "RisuRealm")


def _file(get: _Getter, link: Link) -> Fetched:
    file, content_type = get.file(link.url, link.host, limit=MAX_ARCHIVE_BYTES, ask=True)
    head = file.read(512).lstrip().lower()
    file.seek(0)
    if content_type.lower().startswith("text/html") or head.startswith((b"<!doctype", b"<html")):
        file.close()
        raise LinkError(WEB_PAGE)
    parts = [unquote(part) for part in urlparse(link.url).path.split("/") if part]
    return Fetched(file, parts[-1] if parts else link.host, link.host)


# --- reading -----------------------------------------------------------------------------


class _Getter:
    def __init__(
        self, client: httpx.Client, stop: threading.Event | None, large_ok: bool = False
    ) -> None:
        self.client = client
        self.stop = stop
        self.large_ok = large_ok

    def json(self, url: str, site: str) -> Any:
        data, _type = self.bytes(url, site, accept="application/json")
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LinkError(f"{site} sent an answer that can't be read.") from None

    def bytes(self, url: str, site: str, accept: str = "*/*") -> tuple[bytes, str]:
        """The body, read whole, and its content type."""
        file, content_type = self.file(url, site, accept, limit=MAX_BYTES)
        with file:
            return file.read(), content_type

    def file(
        self,
        url: str,
        site: str,
        accept: str = "*/*",
        *,
        limit: int = MAX_BYTES,
        ask: bool = False,
    ) -> tuple[BinaryIO, str]:
        """The body, a piece at a time to `limit`, in a spooled file (memory,
        then an unnamed file past SPOOL_BYTES), rewound; and its content type.
        `ask`: a card's own file, over ASK_BYTES only when `large_ok`."""
        self._check_stop()
        host = urlparse(url).hostname or site
        too_large = TOO_LARGE.format(size=limit // (1024 * 1024))
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
        try:
            with self.client.stream("GET", url, headers={"Accept": accept}) as response:
                if response.status_code >= 400:
                    raise LinkRefused(_refusal(response, site), response.status_code)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > limit:
                    raise LinkError(too_large)
                asking = ask and not self.large_ok
                if asking and declared.isdigit() and int(declared) > ASK_BYTES:
                    raise LinkLarge(int(declared))
                size = 0
                for piece in response.iter_bytes():
                    self._check_stop()
                    size += len(piece)
                    if size > limit:
                        raise LinkError(too_large)
                    if asking and size > ASK_BYTES:
                        raise LinkLarge(None)
                    spool.write(piece)
                spool.seek(0)
                return spool, response.headers.get("content-type", "")
        except BaseException as exc:
            spool.close()
            if isinstance(exc, httpx.TimeoutException):
                self._check_stop()
                raise LinkError(f"{host} didn't answer in time.") from None
            if isinstance(exc, httpx.HTTPError):
                self._check_stop()
                raise LinkError(f"Couldn't reach {host}: {exc}") from None
            if isinstance(exc, ProviderError):  # a redirect off https (make_client's guard)
                raise LinkError(str(exc)) from None
            raise

    def _check_stop(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise LinkStopped("Stopped.")


def _refusal_body(response: httpx.Response) -> bytes:
    """The start of a refusal's body, all of it that `_refusal` reads: a piece
    at a time and no further, decoded. Read whole, a refusal could be as large
    as the site liked (a 404 of 300 MB was held in memory, all of it)."""
    kept = bytearray()
    try:
        for piece in response.iter_bytes():
            kept += piece[: REFUSAL_BYTES - len(kept)]
            if len(kept) >= REFUSAL_BYTES:
                break
    except httpx.HTTPError:
        pass  # the words as far as they came
    return bytes(kept)


def _refusal(response: httpx.Response, site: str) -> str:
    """A site's no, in words: its own message when it gives one."""
    status = response.status_code
    body = _refusal_body(response)
    if status in (403, 503) and b"just a moment" in body.lower():
        return BOT_CHECK.format(host=response.request.url.host)
    said = ""
    try:
        answer = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        answer = None
    if isinstance(answer, dict):
        for key in ("message", "error", "detail"):
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                said = " ".join(value.split())[:200]
                break
    if status == 404:
        return f"{site} has nothing at that address (it may have been removed or made private)."
    if status == 429:
        return f"{site} asks for fewer requests: try again in a minute."
    if status >= 500:
        return f"{site} had a problem (HTTP {status}); try again later."
    return f"{site} refused (HTTP {status}){': ' + said if said else '.'}"
