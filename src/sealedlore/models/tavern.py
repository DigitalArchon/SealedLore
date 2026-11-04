"""What a SillyTavern character card holds that a story's card has no place
for (storage/tavern_cards.py): its greetings, its scenario and example
messages, its own instructions, who made it, its tags and its lorebook.

Kept on the character (`Character.tavern`) so a card read in goes out again
as it came, and for a chat with them. A story's storyteller is never sent
them: a story has its own world, opening and rules. Text keeps its
`{{char}}` and `{{user}}` as the card wrote them in the author's own
characters (storage/collection.py); a story's copy has the names in.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TavernFields(BaseModel):
    scenario: str = ""
    # The greeting a chat opens with, and the others it may open with.
    first_mes: str = ""
    alternate_greetings: list[str] = Field(default_factory=list)
    # SillyTavern's group chats only: kept, never used here.
    group_only_greetings: list[str] = Field(default_factory=list)
    # Example messages, `<START>` between the exchanges, as SillyTavern writes them.
    mes_example: str = ""
    # The card's own instructions: its main prompt, and what goes after the
    # chat's history (a chat's tail, engine/chat.py).
    system_prompt: str = ""
    post_history_instructions: str = ""
    creator_notes: str = ""
    creator_notes_multilingual: dict[str, Any] = Field(default_factory=dict)
    creator: str = ""
    character_version: str = ""
    tags: list[str] = Field(default_factory=list)
    source: list[str] = Field(default_factory=list)
    # Seconds since 1970, as the V3 spec has them; None when the card had none.
    creation_date: int | None = None
    modification_date: int | None = None
    # The card's own lorebook, as SillyTavern writes one (V2/V3
    # `character_book`): made lore entries when the character joins a story.
    character_book: dict[str, Any] | None = None
    # Other apps' settings on the card (`extensions`), as they came:
    # SillyTavern's talkativeness and favourite mark, Chub's, RisuAI's.
    extensions: dict[str, Any] = Field(default_factory=dict)

    @property
    def greetings(self) -> list[str]:
        """The first message, then the alternate greetings, the empty left out."""
        return [text for text in (self.first_mes, *self.alternate_greetings) if text.strip()]

    @property
    def book_entries(self) -> int:
        entries = (self.character_book or {}).get("entries")
        return len(entries) if isinstance(entries, list | dict) else 0
