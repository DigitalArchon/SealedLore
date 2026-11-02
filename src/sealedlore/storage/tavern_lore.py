"""SillyTavern lorebooks: read into lore entries, and lore written as one.

SillyTavern keeps a lorebook ("World Info") as a JSON file of entries keyed
by number; a character card carries its own in another shape (`character_book`,
entries in a list). Both are read here. A lorebook is written in the first
shape, which SillyTavern imports as it is; a card's own in the second.

SealedLore's lore is keywords, content, always-on and enabled. What a
SillyTavern entry does beyond that (second keywords with their logic, a
chance below 100%, timing, groups, where in the prompt it goes, regular
expressions) has nothing to become, so it is left out and counted, and the
import says what went. Conversions are pure; only `read_lorebook` and
`write_lorebook` touch disk.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sealedlore.models.lore import LoreEntry
from sealedlore.storage.atomic import atomic_write_json

# Far past any lorebook (a big one is a megabyte or two): something else.
MAX_BYTES = 32 * 1024 * 1024
# A key written /like this/i is a regular expression in SillyTavern.
_REGEX_KEY = re.compile(r"^/.+/[a-z]*$", re.S)


class LorebookError(ValueError):
    """A file that isn't a lorebook this version can read, in plain words."""


@dataclass
class LoreImport:
    entries: list[LoreEntry] = field(default_factory=list)
    # What SillyTavern did with them that SealedLore doesn't, counted by kind.
    left_out: Counter[str] = field(default_factory=Counter)

    def notes(self) -> list[str]:
        """The left-out features in words, one line each."""
        return [
            f"{_LEFT_OUT[kind]} ({count} {'entry' if count == 1 else 'entries'})"
            for kind, count in sorted(self.left_out.items(), key=lambda item: _ORDER[item[0]])
        ]


_LEFT_OUT = {
    "secondary": "second keywords and their logic: the first keywords alone decide",
    "regex": "keywords written as regular expressions",
    "probability": "a chance below 100%: the entry is sent whenever it applies",
    "timing": "sticky, cooldown and delay",
    "group": "inclusion groups",
    "placement": "where in the prompt it went: SealedLore places lore itself",
    "recursion": "recursion settings",
    "filter": "character filters",
    "empty": "entries with no content, not imported",
}
_ORDER = {kind: index for index, kind in enumerate(_LEFT_OUT)}


def _keys(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(key).strip() for key in value if str(key).strip()]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def entries_of(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A lorebook's entries, in its own order, whichever shape it has."""
    entries = book.get("entries")
    if isinstance(entries, Mapping):
        found = [entry for entry in entries.values() if isinstance(entry, Mapping)]
        return sorted(found, key=lambda entry: _int(entry.get("displayIndex", entry.get("uid")), 0))
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, Mapping)]
    return []


def looks_like_lorebook(data: Any) -> bool:
    """A SillyTavern lorebook (either shape), not a character card."""
    if not isinstance(data, Mapping) or "spec" in data or "format" in data:
        return False
    entries = data.get("entries")
    return isinstance(entries, Mapping | list) and all(
        isinstance(entry, Mapping) and "content" in entry
        for entry in (entries.values() if isinstance(entries, Mapping) else entries)
    )


def lore_from_book(
    book: Mapping[str, Any], fill: Callable[[str], str] = lambda text: text
) -> LoreImport:
    """A lorebook's entries as lore. `fill` puts names in for {{char}} and
    {{user}} (storage/tavern_cards.py `macro_filler`)."""
    result = LoreImport()
    for number, raw in enumerate(entries_of(book), start=1):
        content = fill(str(raw.get("content") or "")).strip()
        if not content:
            result.left_out["empty"] += 1
            continue
        primary = _keys(raw.get("key", raw.get("keys")))
        regex = bool(raw.get("use_regex")) or any(_REGEX_KEY.match(key) for key in primary)
        keywords = [] if raw.get("use_regex") else [k for k in primary if not _REGEX_KEY.match(k)]
        if regex:
            result.left_out["regex"] += 1
        secondary = _keys(raw.get("keysecondary", raw.get("secondary_keys")))
        if secondary and raw.get("selective", True):
            result.left_out["secondary"] += 1
        for kind in _features(raw):
            result.left_out[kind] += 1
        title = str(raw.get("comment") or raw.get("name") or "").strip()
        title = title or (keywords[0] if keywords else f"Entry {number}")
        disabled = raw.get("disable", not raw.get("enabled", True))
        result.entries.append(
            LoreEntry(
                title=fill(title),
                content=content,
                keywords=list(dict.fromkeys(fill(key) for key in keywords)),
                always_on=bool(raw.get("constant")),
                enabled=not disabled,
            )
        )
    return result


def _features(raw: Mapping[str, Any]) -> Iterable[str]:
    """The kinds of what a SillyTavern entry does that SealedLore doesn't."""
    extensions = raw.get("extensions") if isinstance(raw.get("extensions"), Mapping) else {}
    merged = {**extensions, **raw}
    probability = merged.get("probability")
    if (
        merged.get("useProbability", True)
        and probability not in (None, "")
        and (_int(probability, 100) < 100)
    ):
        yield "probability"
    if any(_int(merged.get(name), 0) for name in ("sticky", "cooldown", "delay")):
        yield "timing"
    if str(merged.get("group") or "").strip():
        yield "group"
    # Before or after the character's definitions is the card's part of the
    # prompt, as SealedLore's lore is; the Author's Note, a depth or an
    # outlet is somewhere SealedLore has nothing like.
    if merged.get("position") not in (None, "", 0, 1, "before_char", "after_char"):
        yield "placement"
    if any(
        merged.get(name)
        for name in (
            "excludeRecursion",
            "preventRecursion",
            "delayUntilRecursion",
            "exclude_recursion",
            "prevent_recursion",
            "delay_until_recursion",
        )
    ):
        yield "recursion"
    character_filter = merged.get("characterFilter") or merged.get("character_filter")
    if isinstance(character_filter, Mapping) and (
        character_filter.get("names") or character_filter.get("tags")
    ):
        yield "filter"


def _entry(uid: int, entry: LoreEntry) -> dict[str, Any]:
    """One entry as SillyTavern writes it: its own defaults for everything
    SealedLore doesn't set."""
    return {
        "uid": uid,
        "key": list(entry.keywords),
        "keysecondary": [],
        "comment": entry.title,
        "content": entry.content,
        "constant": entry.always_on,
        "vectorized": False,
        "selective": True,
        "selectiveLogic": 0,
        "addMemo": True,
        "order": 100,
        "position": 0,
        "disable": not entry.enabled,
        "ignoreBudget": False,
        "excludeRecursion": False,
        "preventRecursion": False,
        "delayUntilRecursion": False,
        "probability": 100,
        "useProbability": True,
        "depth": 4,
        "group": "",
        "groupOverride": False,
        "groupWeight": 100,
        "scanDepth": None,
        "caseSensitive": None,
        "matchWholeWords": None,
        "useGroupScoring": None,
        "automationId": "",
        "role": None,
        "sticky": 0,
        "cooldown": 0,
        "delay": 0,
        "displayIndex": uid,
    }


def book_from_lore(entries: Sequence[LoreEntry]) -> dict[str, Any]:
    """Lore as a SillyTavern lorebook file."""
    return {"entries": {str(uid): _entry(uid, entry) for uid, entry in enumerate(entries)}}


def character_book_from_lore(entries: Sequence[LoreEntry], name: str = "") -> dict[str, Any]:
    """Lore as a character card's own lorebook (`character_book`)."""
    return {
        "name": name,
        "description": "",
        "scan_depth": 4,
        "token_budget": 2048,
        "recursive_scanning": False,
        "extensions": {},
        "entries": [
            {
                "id": uid,
                "keys": list(entry.keywords),
                "secondary_keys": [],
                "comment": entry.title,
                "name": entry.title,
                "content": entry.content,
                "constant": entry.always_on,
                "selective": False,
                "insertion_order": 100,
                "enabled": entry.enabled,
                "position": "before_char",
                "use_regex": False,
                "extensions": {},
            }
            for uid, entry in enumerate(entries)
        ],
    }


def read_json(path: Path) -> Any:
    """A JSON file, refused in plain words when it can't be one."""
    name = Path(path).name
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            raise LorebookError(f"{name} is too large to read.")
        data = Path(path).read_bytes()
    except OSError as exc:
        raise LorebookError(f"Couldn't read {path}: {exc.strerror or exc}") from exc
    return json_of(data, name)


def json_of(data: bytes, name: str) -> Any:
    """JSON from a file's bytes (or a download's), refused in plain words."""
    if len(data) > MAX_BYTES:
        raise LorebookError(f"{name} is too large to read.")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise LorebookError(f"{name} isn't a text file.") from exc
    except json.JSONDecodeError as exc:
        raise LorebookError(f"{name} isn't valid JSON (line {exc.lineno}).") from exc


def read_lorebook(path: Path) -> Mapping[str, Any]:
    data = read_json(path)
    if not looks_like_lorebook(data):
        raise LorebookError(f"{Path(path).name} isn't a SillyTavern lorebook.")
    return data


def write_lorebook(path: Path, entries: Sequence[LoreEntry]) -> None:
    atomic_write_json(Path(path), book_from_lore(entries))
