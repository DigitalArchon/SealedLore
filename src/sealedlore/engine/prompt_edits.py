"""The author's edits to the prompt texts: which text is in force, and why. Pure.

Two layers over the defaults (engine/prompt_texts.py): edits for every story,
kept in the config, and edits for one story, kept on it. A story's edit wins.
An edit to a key the program no longer has is kept (a newer version may know
it again) but never sent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Literal

from sealedlore.engine.prompt_texts import TEXTS, PromptTexts
from sealedlore.models.prompt_edit import PromptEdit

Source = Literal["story", "app", "default"]


def default_sha(key: str) -> str:
    return hashlib.sha256(TEXTS[key].default.encode("utf-8")).hexdigest()[:16]


def texts_in_force(
    app: Mapping[str, PromptEdit], story: Mapping[str, PromptEdit] | None = None
) -> PromptTexts:
    overrides = {key: edit.text for key, edit in app.items() if key in TEXTS}
    overrides.update({key: edit.text for key, edit in (story or {}).items() if key in TEXTS})
    return PromptTexts(overrides)


def source_of(
    key: str, app: Mapping[str, PromptEdit], story: Mapping[str, PromptEdit] | None = None
) -> Source:
    if story and key in story:
        return "story"
    if key in app:
        return "app"
    return "default"


def set_text(edits: dict[str, PromptEdit], key: str, text: str) -> None:
    """Record `text` for `key`. Writing the default back is a reset: an edit
    that says what the default says would stop following the default when
    it changes."""
    if text == TEXTS[key].default:
        edits.pop(key, None)
    else:
        edits[key] = PromptEdit(text=text, default_sha=default_sha(key))


def reset(edits: dict[str, PromptEdit], keys: Iterable[str] | None = None) -> None:
    """Back to the defaults: the keys given, or every edit."""
    for key in list(edits) if keys is None else list(keys):
        edits.pop(key, None)


def default_changed(key: str, edit: PromptEdit) -> bool:
    """Whether the program's default has changed since this edit was made."""
    return key in TEXTS and bool(edit.default_sha) and edit.default_sha != default_sha(key)


def unknown_keys(edits: Mapping[str, PromptEdit]) -> list[str]:
    """Edits to texts the program doesn't have (removed, or from a newer version)."""
    return sorted(key for key in edits if key not in TEXTS)


def missing_slots(key: str, text: str) -> list[str]:
    """Placeholders the default uses that `text` leaves out. Each carries
    something (a name, a list, the story's person): without it the model
    never gets it. Allowed, but the author should know."""
    default = TEXTS[key].default
    return [
        slot for slot in TEXTS[key].slots if f"{{{slot}}}" in default and f"{{{slot}}}" not in text
    ]


def copied(
    source: Mapping[str, PromptEdit], keys: Iterable[str] | None = None
) -> dict[str, PromptEdit]:
    """Another story's edits (or some of them), as new records to keep."""
    chosen = source if keys is None else {key: source[key] for key in keys if key in source}
    return {key: edit.model_copy() for key, edit in chosen.items()}
