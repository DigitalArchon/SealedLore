"""What the Cast page says about the cast's room in the prompt: how the cards
fit (engine/cast_fit.py), which are condensed, being condensed, or couldn't
be, in words. The window builds a `CastRoom` from the session; the page
only shows it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.cast_fit import CastFit
from sealedlore.models.character import Character
from sealedlore.models.config import Config


@dataclass(frozen=True)
class CastRoom:
    fit: CastFit
    # Every cast card's name, by id.
    names: Mapping[str, str]
    # How the room is set, in words ("15% of the 60,000-token budget").
    room: str
    # Cards being condensed now, and cards whose condensing failed (why).
    writing: frozenset[str] = frozenset()
    failures: Mapping[str, str] = field(default_factory=dict)
    # Cards with a condensed text from before their last edit.
    stale: frozenset[str] = frozenset()


def room_words(config: Config, budget: int) -> str:
    if config.cast_limit == "tokens":
        return f"a fixed {config.cast_token_cap:,} tokens"
    return f"{round(config.cast_share * 100)}% of the {budget:,}-token budget"


def _names(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def banner_text(room: CastRoom) -> str:
    """The Cast page's line about the room; "" when the cast fits it."""
    fit = room.fit
    if not fit.over:
        return ""

    def named(ids) -> str:
        return _names([room.names[i] for i in ids if i in room.names])

    parts = [
        f"The cast's cards come to about {fit.whole_tokens:,} tokens, more than their "
        f"room of {fit.cap:,} ({room.room})."
    ]
    condensed = fit.ids("condensed")
    if condensed:
        parts.append(f"Sent condensed: {named(condensed)}.")
    writing = [i for i in fit.waiting if i in room.writing]
    if writing:
        parts.append(f"Being condensed now, and sent whole until then: {named(writing)}.")
    failed = [i for i in fit.waiting if i in room.failures]
    if failed:
        parts.append(f"Couldn't be condensed, so sent whole: {named(failed)} (the card says why).")
    waiting = [i for i in fit.waiting if i not in room.writing and i not in room.failures]
    if waiting:
        parts.append(f"To be condensed, and sent whole until then: {named(waiting)}.")
    short = fit.ids("short")
    if short:
        parts.append(
            f"Even condensed the cast doesn't fit, so these go as their name, summary and "
            f"voice alone: {named(short)}. Give the cast more room, or make some of them "
            "supporting characters."
        )
    return " ".join(parts)


def supporting_note(room: CastRoom | None, character: Character) -> str:
    """A supporting card's condensed text: it rides the turns that name
    them, and a long description goes condensed."""
    if room is None:
        return ""
    from sealedlore.engine.characters import SUPPORTING_WHOLE_WORDS, supporting_form

    cid = character.id
    form = supporting_form(character)
    if cid in room.writing:
        return "Being written now. Until then the story's turns get their summary and voice."
    if cid in room.failures:
        return f"Couldn't be written ({room.failures[cid]}). Write it again to try once more."
    if cid in room.stale:
        return (
            "Out of date: the card changed since it was written, so it isn't sent. "
            "Write it again, or edit it to keep it as it is."
        )
    if form == "condensed":
        return (
            "Sent in place of the description and voice on the turns that name them or "
            "find them here."
        )
    if form == "whole" and character.condensed:
        return (
            f"Not needed: the description is short enough (up to {SUPPORTING_WHOLE_WORDS} "
            "words) to go whole."
        )
    if form == "short" and character.full_description:
        return "To be written: the description is too long to go whole on the turns that name them."
    return ""


def card_note(room: CastRoom | None, character: Character) -> str:
    """What the card's condensed text is doing, in a line."""
    if room is None:
        return ""
    cid = character.id
    form = room.fit.forms.get(cid)
    if character.keep_whole:
        return "This card is always sent whole."
    if cid in room.fit.waiting:
        if cid in room.writing:
            return "Being written now. The whole card is sent until it is."
        if cid in room.failures:
            return f"Couldn't be written ({room.failures[cid]}). The whole card is sent meanwhile."
        return "To be written: the cast is over its room. The whole card is sent until then."
    if cid in room.stale:
        return (
            "Out of date: the card changed since it was written, so it isn't sent. "
            "Write it again, or edit it to keep it as it is."
        )
    if form == "condensed":
        return "Sent in place of the description and voice while the cast is over its room."
    if form == "short":
        return "Even condensed, the cast doesn't fit its room: this card goes as its summary alone."
    if character.condensed:
        return "Not needed now: the cast's cards fit their room whole."
    return ""


def cast_room(session) -> CastRoom | None:  # noqa: ANN001 - a StorySession
    """The room as the session has it now; None in a chat."""
    if session is None or session.story.chat:
        return None
    from sealedlore.engine.cast_fit import has_condensed

    fit = session.cast_fit()
    everyone = [*session.cast, *session.supporting]
    names = {c.id: c.name or "(unnamed)" for c in everyone}
    stale = frozenset(c.id for c in everyone if c.condensed and not has_condensed(c))
    return CastRoom(
        fit=fit,
        names=names,
        room=room_words(session.config, session.story.defaults.context_token_budget),
        writing=frozenset(session.condensing_ids()),
        failures=dict(session.condense_failures()),
        stale=stale,
    )
