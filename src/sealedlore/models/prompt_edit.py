"""An author's edit to one of the program's prompt texts (engine/prompt_texts.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PROMPT_SET_FORMAT = "sealedlore-prompts"
PROMPT_SET_VERSION = 1


class PromptEdit(BaseModel):
    """The text the author wrote in place of a default, and a fingerprint of
    the default it replaced. When the program's default later changes, the
    edit still stands, but the editor can say so rather than let an old
    wording silently hide a measured improvement."""

    text: str
    default_sha: str = ""


class PromptSet(BaseModel):
    """A set of edited prompt texts in a file of its own
    (storage/prompt_sets.py), to share or to keep: exported from the prompt
    editor, imported into a story or into every story. Each edit keeps its
    fingerprint of the default it replaced, so a set written for an older
    version still says which of its texts the program has changed since."""

    format: Literal["sealedlore-prompts"] = PROMPT_SET_FORMAT
    version: int = PROMPT_SET_VERSION
    # The program version that wrote it, for the reader's information.
    written_by: str = ""
    edits: dict[str, PromptEdit] = Field(default_factory=dict)
