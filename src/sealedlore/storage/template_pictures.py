"""Pictures kept for a template: a sample or plot file, or a scenario,
named by its title ("Doomsville").

The author (Nov 3 2026): a story given many pictures should be able to
start new stories with them, and the default template should be able to
take them, staying as it is but with the extra pictures easy to take or
leave. So they are kept in the data folder, never in the template: every
character's pictures, labels and sheet rows and columns, by the
character's name. When a story starts from that template again, the Start
dialog offers to add them.

A picture the new story's card already has (the template's own, the same
bytes) keeps its place and takes the kept labels; the rest are added in
the order they were kept. The template's own pictures the kept story no
longer had stay at the end: the template stays as it is. One file a
picture, named by its contents, so a picture kept twice is kept once.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.storage.atomic import atomic_write_json
from sealedlore.storage.image_meta import picture_kind
from sealedlore.storage.images import REFS_DIR, write_bytes_atomic
from sealedlore.storage.paths import data_home

FORMAT = "sealedlore-template-pictures"
VERSION = 1
FOLDER = "templates"
PICTURES = "pictures"
LIST = "pictures.json"


def template_of(story) -> str:
    """The template a story was made from; an older story's own title."""
    return (story.setup.template or story.title or "").strip()


def _slug(template: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", template.lower()).strip("-")[:60] or "template"
    digest = hashlib.sha256(template.strip().lower().encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def _folder(template: str, root: Path | None) -> Path:
    return (root or data_home()) / FOLDER / _slug(template)


@dataclass
class Kept:
    template: str
    kept_from: str
    kept_at: str
    # By the character's name: their pictures in order, each (sha256, caption,
    # look, expression, framing), and their sheet's own rows and columns.
    cards: dict[str, dict] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return sum(len(card["pictures"]) for card in self.cards.values())


def read_kept(template: str, root: Path | None = None) -> Kept | None:
    path = _folder(template, root) / LIST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    cards = data.get("cards")
    return Kept(
        template=str(data.get("template") or template),
        kept_from=str(data.get("kept_from") or ""),
        kept_at=str(data.get("kept_at") or ""),
        cards=cards if isinstance(cards, dict) else {},
    )


def keep_pictures(
    template: str,
    kept_from: str,
    characters: Sequence[Character],
    read: Callable[[str], bytes | None],
    root: Path | None = None,
) -> Kept:
    """Keep every picture of `characters` (`read` gives a picture file's
    bytes), with its labels and their sheets' rows and columns, for new
    stories from `template`; what was kept for it before is replaced."""
    folder = _folder(template, root)
    (folder / PICTURES).mkdir(parents=True, exist_ok=True)
    cards: dict[str, dict] = {}
    used: set[str] = set()
    for character in characters:
        pictures = []
        for ref in character.reference_images:
            data = read(ref.file)
            suffix = picture_kind(data) if data else None
            if not data or suffix is None:
                continue
            digest = hashlib.sha256(data).hexdigest()
            name = f"{digest}{suffix}"
            target = folder / PICTURES / name
            if not target.is_file():
                write_bytes_atomic(target, data)
            used.add(name)
            pictures.append(
                {
                    "sha256": digest,
                    "file": f"{PICTURES}/{name}",
                    "caption": ref.caption,
                    "look": ref.look,
                    "expression": ref.expression,
                    "framing": ref.framing,
                    "style": ref.style,
                }
            )
        cards[character.name] = {
            "pictures": pictures,
            "sheet_looks": list(character.sheet_looks),
            "sheet_expressions": list(character.sheet_expressions),
        }
    for stale in (folder / PICTURES).iterdir():
        if stale.name not in used:
            stale.unlink(missing_ok=True)
    kept = Kept(template=template, kept_from=kept_from, kept_at=utc_now_iso(), cards=cards)
    atomic_write_json(
        folder / LIST,
        {
            "format": FORMAT,
            "version": VERSION,
            "template": kept.template,
            "kept_from": kept.kept_from,
            "kept_at": kept.kept_at,
            "cards": cards,
        },
    )
    return kept


def forget_kept(template: str, root: Path | None = None) -> bool:
    """Forget what was kept for `template`; whether there was anything."""
    folder = _folder(template, root)
    if not (folder / LIST).is_file():
        return False
    for path in sorted(folder.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    folder.rmdir()
    return True


def apply_kept(
    kept: Kept,
    characters: Sequence[Character],
    read: Callable[[str], bytes | None],
    write: Callable[[str, bytes], None],
    root: Path | None = None,
) -> int:
    """Put what was kept on the characters of the same names: `read` and
    `write` are the new story's picture files. How many pictures were added
    (the template's own are matched, not added again)."""
    folder = _folder(kept.template, root)
    added = 0
    for character in characters:
        card = kept.cards.get(character.name)
        if not isinstance(card, dict):
            continue
        own: dict[str, ImageRef] = {}
        for ref in character.reference_images:
            data = read(ref.file)
            if data:
                own.setdefault(hashlib.sha256(data).hexdigest(), ref)
        pictures: list[ImageRef] = []
        for row in card.get("pictures") or []:
            if not isinstance(row, dict):
                continue
            ref = own.pop(str(row.get("sha256")), None)
            if ref is None:
                path = folder / str(row.get("file") or "")
                if folder not in path.resolve().parents or not path.is_file():
                    continue
                data = path.read_bytes()
                suffix = picture_kind(data)
                if suffix is None:
                    continue
                ref_id = new_id()
                ref = ImageRef(id=ref_id, file=f"{REFS_DIR}/{ref_id}{suffix}")
                write(ref.file, data)
                added += 1
            ref.caption = str(row.get("caption") or "")
            ref.look = str(row.get("look") or "")
            ref.expression = str(row.get("expression") or "")
            ref.framing = str(row.get("framing") or "")
            ref.style = str(row.get("style") or "")
            pictures.append(ref)
        # The template's own that weren't kept stay, after: it is as it was.
        pictures += [ref for ref in character.reference_images if ref in own.values()]
        character.reference_images = pictures
        character.sheet_looks = [str(x) for x in card.get("sheet_looks") or []]
        character.sheet_expressions = [str(x) for x in card.get("sheet_expressions") or []]
    return added
