"""A simple chat started from one of the author's own characters (Your
characters, storage/collection.py). Pure.

The author (Nov 4 2026): a chat with a character "basically just brings in
their info as the first message into a Simple Chat, including any system
message for the character", customisable first. So the card is the chat's
first message (kept in full, as a chat's first message is: engine/chat.py),
the card's own system prompt its system prompt (`chat.card.system` when it
has none), its post-history instructions the chat's tail, and a greeting of
the card's its first reply, written as the card has it. With no greeting the
first message is sent and the model answers it.
"""

from __future__ import annotations

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.node import CHAT_USER_ID, NARRATOR_SPEAKER_ID, Node
from sealedlore.models.tavern import TavernFields
from sealedlore.storage.tavern_cards import example_dialogue, macro_filler
from sealedlore.tree import link_child

# Who the author is when they leave it blank, as SillyTavern's persona is.
DEFAULT_USER = "User"


def _tavern(card: Character) -> TavernFields:
    return card.tavern or TavernFields()


def filled(card: Character, text: str, user: str) -> str:
    return macro_filler(card.name, user or DEFAULT_USER)(text)


def character_text(
    card: Character, *, user: str, about: str = "", texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """The first message: the card, with the names in."""
    user = user or DEFAULT_USER
    fill = macro_filler(card.name, user)
    tavern = _tavern(card)
    parts = [texts["chat.card.info"], f"# {card.name}"]
    if card.aliases:
        parts.append("Also called: " + ", ".join(fill(alias) for alias in card.aliases))
    for label, text in (
        ("", card.full_description or card.summary or ""),
        ("Personality: ", card.personality or ""),
        ("Voice: ", card.voice_notes or ""),
        ("Scenario: ", tavern.scenario),
    ):
        if text.strip():
            parts.append(label + fill(text).strip())
    examples = example_dialogue(fill(tavern.mes_example))
    if examples:
        parts.append(f"# How {card.name} talks\n\n{examples}")
    who = texts.fill("chat.card.user", user=user)
    if about.strip():
        who += " " + fill(about).strip()
    parts.append(who)
    return "\n\n".join(parts)


def system_prompt(card: Character, *, user: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The card's own system prompt, else `chat.card.system`, names in."""
    own = filled(card, _tavern(card).system_prompt, user).strip()
    return own or texts.fill("chat.card.system", name=card.name, user=user or DEFAULT_USER)


def tail(card: Character, *, user: str) -> str:
    """The card's post-history instructions: a chat's tail is the same thing."""
    return filled(card, _tavern(card).post_history_instructions, user).strip()


def greetings(card: Character, *, user: str) -> list[str]:
    return [filled(card, text, user).strip() for text in _tavern(card).greetings]


def opening(first: str, greeting: str | None) -> list[Node]:
    """The chat's first message and the character's greeting after it, as
    the card wrote it (no call makes it); none without a greeting, when the
    first message is sent and answered like any other."""
    if greeting is None:
        return []
    user = Node(kind="user", speaker_id=CHAT_USER_ID, content=first)
    user.meta.keep_full = True
    reply = Node(kind="assistant", speaker_id=NARRATOR_SPEAKER_ID, content=greeting)
    link_child(user, reply)
    return [user, reply]
