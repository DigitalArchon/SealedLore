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


# --- a card's own lorebook, edited in place -------------------------------------------------
#
# Your characters keep a card's lorebook as it came (TavernFields.character_book):
# the spec's shape (entries in a list), or SillyTavern's own (entries keyed
# by number) that some cards carry. The editor reads and writes each field
# under whichever name the entry already uses, so an edit changes only what
# was edited and the book goes out again in its own shape.

# A field as the editor names it: the spec's name, then SillyTavern's.
_ALIASES = {
    "keys": ("keys", "key"),
    "secondary_keys": ("secondary_keys", "keysecondary"),
    "insertion_order": ("insertion_order", "order"),
    "case_sensitive": ("case_sensitive", "caseSensitive"),
}
# What the editor shows in fields of its own; the rest of an entry is edited
# as JSON (`entry_rest`).
EDITED = (
    "comment",
    "name",
    "keys",
    "key",
    "secondary_keys",
    "keysecondary",
    "content",
    "constant",
    "enabled",
    "disable",
    "selective",
    "use_regex",
    "case_sensitive",
    "caseSensitive",
)
BOOK_EDITED = ("name", "description", "entries")


def _name_in(raw: Mapping[str, Any], field_name: str) -> str:
    names = _ALIASES.get(field_name, (field_name,))
    return next((name for name in names if name in raw), names[0])


def entry_get(raw: Mapping[str, Any], field_name: str, default: Any = None) -> Any:
    return raw.get(_name_in(raw, field_name), default)


def entry_set(raw: dict[str, Any], field_name: str, value: Any) -> None:
    raw[_name_in(raw, field_name)] = value


def entry_title(raw: Mapping[str, Any]) -> str:
    return str(raw.get("comment") or raw.get("name") or "").strip()


def set_entry_title(raw: dict[str, Any], title: str) -> None:
    """The title is SillyTavern's memo (`comment`); a spec entry's `name`
    follows it where it has one."""
    raw["comment"] = title
    if "name" in raw:
        raw["name"] = title


def entry_keys(raw: Mapping[str, Any], field_name: str = "keys") -> list[str]:
    return _keys(entry_get(raw, field_name))


def entry_enabled(raw: Mapping[str, Any]) -> bool:
    if "enabled" in raw:
        return bool(raw["enabled"])
    return not raw.get("disable", False)


def set_entry_enabled(raw: dict[str, Any], enabled: bool) -> None:
    if "disable" in raw and "enabled" not in raw:
        raw["disable"] = not enabled
    else:
        raw["enabled"] = enabled


def book_entries(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The book's own entry objects, in its order, to change in place."""
    return entries_of(book)  # type: ignore[return-value]


def new_book(name: str = "") -> dict[str, Any]:
    """An empty lorebook in the spec's shape, for a card that had none."""
    return character_book_from_lore([], name=name)


def add_entry(book: dict[str, Any]) -> dict[str, Any]:
    """A new, empty entry at the end of the book, in the book's own shape."""
    entries = book.get("entries")
    if isinstance(entries, Mapping):
        found = [entry for entry in entries.values() if isinstance(entry, Mapping)]
        uid = max([*(_int(entry.get("uid"), -1) for entry in found), -1]) + 1
        raw = _entry(uid, LoreEntry(title="", content=""))
        entries[str(uid)] = raw
        return raw
    if not isinstance(entries, list):
        entries = book["entries"] = []
    ids = [_int(entry.get("id"), -1) for entry in entries if isinstance(entry, Mapping)]
    raw = character_book_from_lore([LoreEntry(title="", content="")])["entries"][0]
    raw["id"] = max([*ids, -1]) + 1
    entries.append(raw)
    return raw


def remove_entry(book: dict[str, Any], raw: Mapping[str, Any]) -> None:
    entries = book.get("entries")
    if isinstance(entries, Mapping):
        for key, entry in list(entries.items()):
            if entry is raw:
                del entries[key]
    elif isinstance(entries, list):
        book["entries"] = [entry for entry in entries if entry is not raw]


def copy_entry(book: dict[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Another entry like `raw`, under a number of its own, after the others."""
    new = add_entry(book)
    number = {key: new[key] for key in ("uid", "id", "displayIndex") if key in new}
    new.clear()
    new.update(json.loads(json.dumps(raw)))
    new.update(number)
    return new


def entry_rest(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Everything on an entry the editor has no field for (its order, its
    place in the prompt, SillyTavern's own settings), to edit as JSON."""
    return {key: value for key, value in raw.items() if key not in EDITED}


def set_entry_rest(raw: dict[str, Any], rest: Mapping[str, Any]) -> None:
    for key in [key for key in raw if key not in EDITED]:
        del raw[key]
    raw.update({key: value for key, value in rest.items() if key not in EDITED})


def book_rest(book: Mapping[str, Any]) -> dict[str, Any]:
    """The book's own settings beyond its name and description (how deep
    SillyTavern scans, its budget, recursion, other apps' settings)."""
    return {key: value for key, value in book.items() if key not in BOOK_EDITED}


def set_book_rest(book: dict[str, Any], rest: Mapping[str, Any]) -> None:
    for key in [key for key in book if key not in BOOK_EDITED]:
        del book[key]
    book.update({key: value for key, value in rest.items() if key not in BOOK_EDITED})


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
