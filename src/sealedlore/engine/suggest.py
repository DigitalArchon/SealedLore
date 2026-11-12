"""Suggest my turn: a few things the author's character might do next,
offered to the author outside the story.

Pure: the request's words and reading the reply.
`StorySession.suggest_turns` makes the call (engine/session_suggest.py).

The storyteller never writes the author's character inside the story, and
this changes nothing of that: the options are shown to the author, who may
take one into the box they write in, change it and send it as their own
turn, or take none. Nothing is stored in the story, and no later request
carries what was offered.
"""

from __future__ import annotations

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts

DEFAULT_COUNT = 3
MAX_COUNT = 6

NOBODY_HELD = "Suggestions are for the character you play: choose one under Playing first."


def how_many(count: int) -> int:
    """The number of options asked for, held to what a menu can show."""
    return max(1, min(int(count), MAX_COUNT))


def suggest_request(
    texts: PromptTexts = DEFAULT_TEXTS, *, held: str = "", count: int = DEFAULT_COUNT, chat: bool
) -> str:
    """What is asked, in place of a turn's tail (a story) or as the last
    message (a chat)."""
    number = str(how_many(count))
    if chat:
        return texts.fill("chat.suggest", count=number)
    return texts.fill("suggest.rules", held=held, count=number)


def parse_suggestions(text: str, count: int = DEFAULT_COUNT) -> list[str]:
    """The options in the reply, in its order, each once, `count` at most.

    The reply is asked for as a JSON array of strings. An object per option
    with its words under one key is read too (a model that labels its
    options); anything else is a ValueError in plain words.
    """
    try:
        data = extract_json(text, list, "list of suggestions")
    except ValueError as exc:
        raise ValueError(f"The suggestions couldn't be read: {exc}. Ask again.") from exc
    options: list[str] = []
    for item in data:
        if isinstance(item, dict):
            words = [value for value in item.values() if isinstance(value, str)]
            item = max(words, key=len) if words else None
        if not isinstance(item, str):
            continue
        option = item.strip()
        if option and option not in options:
            options.append(option)
    if not options:
        raise ValueError("The reply held no suggestions. Ask again.")
    return options[: how_many(count)]
