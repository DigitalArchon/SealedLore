"""Prompt-set files: the author's edited prompt texts, to share or to keep.

A set is the edits of one scope (models/prompt_edit.py `PromptSet`), exported
from File → Prompts (advanced)… and imported into a story or into every
story. Only `write_prompt_set` and `read_prompt_set` touch disk.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from sealedlore import __version__
from sealedlore.models.prompt_edit import (
    PROMPT_SET_FORMAT,
    PROMPT_SET_VERSION,
    PromptEdit,
    PromptSet,
)
from sealedlore.storage.atomic import atomic_write_json

PROMPT_SET_SUFFIX = ".sealedlore-prompts.json"
# Far past any real set (every text the program has, edited, is ~200 KB):
# a file this big is something else, and isn't read into memory to find out.
MAX_BYTES = 8 * 1024 * 1024


class PromptSetError(ValueError):
    """A file that isn't a prompt set this version can read, said in plain words."""


def prompt_set(edits: Mapping[str, PromptEdit]) -> PromptSet:
    """The edits as a set, copied: the set never shares records with a story."""
    return PromptSet(
        written_by=__version__,
        edits={key: edit.model_copy() for key, edit in sorted(edits.items())},
    )


def write_prompt_set(path: Path, edits: Mapping[str, PromptEdit]) -> None:
    atomic_write_json(Path(path), prompt_set(edits).model_dump())


def read_prompt_set(path: Path) -> PromptSet:
    name = Path(path).name
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            raise PromptSetError(f"{name} is too large to be a prompt set.")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptSetError(f"Couldn't read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise PromptSetError(f"{name} isn't a text file.") from exc
    except json.JSONDecodeError as exc:
        raise PromptSetError(f"{name} isn't valid JSON (line {exc.lineno}).") from exc
    if not isinstance(data, dict) or data.get("format") != PROMPT_SET_FORMAT:
        raise PromptSetError(f"{name} isn't a SealedLore prompt set.")
    version = data.get("version")
    if not isinstance(version, int) or version > PROMPT_SET_VERSION:
        raise PromptSetError(
            f"{name} is prompt set version {version}; this SealedLore reads up to "
            f"version {PROMPT_SET_VERSION}."
        )
    try:
        return PromptSet.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        raise PromptSetError(f"{name} has invalid content — {problems}") from exc
