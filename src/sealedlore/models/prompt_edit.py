"""An author's edit to one of the program's prompt texts (engine/prompt_texts.py)."""

from __future__ import annotations

from pydantic import BaseModel


class PromptEdit(BaseModel):
    """The text the author wrote in place of a default, and a fingerprint of
    the default it replaced. When the program's default later changes, the
    edit still stands, but the editor can say so rather than let an old
    wording silently hide a measured improvement."""

    text: str
    default_sha: str = ""
