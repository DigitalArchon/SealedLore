"""Continue: more of the latest passage, written into the same message.

Pure: which passage can be carried on, the request that asks for it, and how
the new text joins the old. `StorySession.continue_passage` makes the call.

The request is the passage's own turn as Regenerate sends it, so it rides the
cached prefix the take was written on, then the passage so far as the
storyteller's own message and a short request to carry it on, with the turn's
REMEMBER line again (recency is the point, §3.3). Asking for it in the user's
message rather than by prefilling the model's reply works on every endpoint:
most OpenAI-compatible hosts take a trailing assistant message as a finished
one, and Anthropic refuses a prefill once thinking is on.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import replace

from sealedlore.engine.prompt import (
    MAX_CACHE_BREAKPOINTS,
    AssembledPrompt,
    CacheBreakpoint,
    PromptSection,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.tokens import TokenEstimator
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.node import Node

SECTION_SO_FAR = "continue.so_far"
BREAKPOINT_SO_FAR = "so_far"
SECTION_CONTINUE = "continue.request"

NOT_CONTINUABLE = (
    "Continue carries on the latest passage written in answer to a turn: "
    "this one has none to carry on."
)

# A passage that ends on one of these (after any closing quotes or
# brackets) ended its sentence; the next goes in a paragraph of its own.
_SENTENCE_ENDS = ".!?…"
_CLOSERS = "\"'”’»)]*_"
# Text that leans on the word before it: no space ahead of it.
_LEANS_BACK = ",.;:!?…)]}”’»-—–"


def continuable(path: Sequence[Node]) -> bool:
    """Whether the end of `path` is a passage Continue can carry on: the
    storyteller's, answering an author turn. An opening written as given
    answers none, and a private scene's summary is the author's approved text."""
    return (
        len(path) > 1
        and path[-1].kind == "assistant"
        and path[-2].kind == "user"
        and path[-1].meta.private_summary_of is None
        and bool(path[-1].content.strip())
    )


def joiner(before: str, start: str) -> str:
    """What goes between a passage and the first text carrying it on.

    Nothing when the new text brings its own space or leans on the word
    before it ("," "…" a closing quote); a new paragraph after a finished
    sentence, unless someone is still speaking; else a space. A passage cut off inside a word
    ("the corrid") can't be told from one cut off after it, so a model that
    picks up with the word's end gets a space before it: rare, and Edit
    mends it.
    """
    if not start or start[0].isspace() or not before or before[-1].isspace():
        return ""
    if start[0] in _LEANS_BACK:
        return ""
    ending = before.rstrip(_CLOSERS)
    if ending and ending[-1] in _SENTENCE_ENDS and not _inside_speech(before):
        return "\n\n"
    return " "


def _inside_speech(text: str) -> bool:
    """Whether the last paragraph of `text` stops inside an open quotation:
    the speech goes on, so the next words belong in the same paragraph."""
    paragraph = text.rsplit("\n", 1)[-1]
    return paragraph.count('"') % 2 == 1 or paragraph.count("“") > paragraph.count("”")


def carried_start(before: str, start: str, whole: bool | None = None) -> str:
    """The first text of a continuation as it goes onto the passage.

    After a reply that came back whole (`NodeMeta.finished`) the new text is
    a paragraph of its own, unless it visibly stops mid-sentence (on a word
    or a comma, outside a list item or heading: `_reads_finished`). A chat's
    reply often ends on an action ("*she smiles*"), an emoji, a list or a
    colon before one, which `joiner` reads as a sentence still going, and
    the new text ran on after it. A continuation that leans on the word
    before it ("…", a dash) stays on its line. After a reply stopped or cut
    off, or one of unknown ending, as `joiner` says.
    """
    if whole and before and not before[-1].isspace() and _reads_finished(before):
        body = start.lstrip()
        if not body:
            return start
        if not start[0].isspace() and body[0] in _LEANS_BACK:
            return body
        return "\n\n" + body
    return joiner(before, start) + start


# A line that is a list item, heading, quote or table row: a block of its
# own, finished whatever it ends on.
_BLOCK_LINE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\|)")


def _reads_finished(before: str) -> bool:
    """Whether a whole reply's end reads as finished, not as a sentence
    the model stopped partway through."""
    if not (before[-1].isalnum() or before[-1] == ","):
        return True
    return bool(_BLOCK_LINE.match(before.rsplit("\n", 1)[-1]))


def text_digest(text: str) -> str:
    """What `NodeMeta.continued_sha` holds: the passage's text, trailing
    space aside, as its continuation offsets were taken in."""
    return hashlib.sha256(text.rstrip().encode("utf-8")).hexdigest()


def pieces(text: str, starts: Sequence[int], digest: str | None) -> list[str]:
    """The passage (`text`, trailing space aside) cut where each of its
    continuations began, when those offsets were taken in this very text;
    else whole. The pieces joined are the text exactly."""
    kept = text.rstrip()
    if not starts or digest != text_digest(kept):
        return [kept]
    cuts = [0, *starts, len(kept)]
    if any(a >= b for a, b in zip(cuts, cuts[1:], strict=False)):
        return [kept]
    return [kept[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]


# How many of the passage's last words the request quotes, to say where it stops.
ENDING_WORDS = 8


def ending_of(passage: str, words: int = ENDING_WORDS) -> str:
    """The passage's last few words, as the request quotes them."""
    found = passage.split()
    tail = " ".join(found[-words:])
    return f"…{tail}" if len(found) > words else tail


# Shorter than this, a passage repeated at the start of its continuation
# could be a coincidence; nothing that short is carried on in practice.
MIN_REPEAT = 20


def repeat_length(before: str, added: str) -> int:
    """How much of `added` is `before` written out again from its first word
    (spacing aside), or 0 when it doesn't begin that way.

    Live, asked to carry a passage on, Sonnet 4.6 sometimes wrote it out
    again from the start, word for word, and went on from where it stopped
    (5 of 24 takes before the request quoted the passage's ending, 1 of 12
    after): the new text is what follows the repeat.
    """
    kept, start = before.strip(), len(added) - len(added.lstrip())
    if len(kept) < MIN_REPEAT:
        return 0
    i, j = 0, start
    while i < len(kept):
        if j >= len(added):
            return 0
        if kept[i].isspace() or added[j].isspace():
            if not (kept[i].isspace() and added[j].isspace()):
                return 0
            while i < len(kept) and kept[i].isspace():
                i += 1
            while j < len(added) and added[j].isspace():
                j += 1
            continue
        if kept[i] != added[j]:
            return 0
        i, j = i + 1, j + 1
    return j


def continue_request(
    texts: PromptTexts = DEFAULT_TEXTS, *, passage: str, reminder: str = "", chat: bool = False
) -> str:
    """The message after the passage so far. A story's carries the turn's
    REMEMBER line again; a chat has none."""
    ending = ending_of(passage)
    if chat:
        return texts.fill("chat.continue", ending=ending)
    parts = ["# CONTINUE THE PASSAGE", texts.fill("turn.continue", ending=ending)]
    if reminder:
        parts.append(reminder)
    return "\n\n".join(parts)


def continued(
    prompt: AssembledPrompt,
    passage: str | Sequence[str],
    request: str,
    estimator: TokenEstimator | None = None,
    *,
    sections: tuple[str, str] = (SECTION_SO_FAR, SECTION_CONTINUE),
    cache_from: int | None = None,
) -> AssembledPrompt:
    """The turn's request carried on: the passage so far as the storyteller's
    own message after the author's turn, then `request`. Everything before
    them is the turn's request as it was assembled, breakpoints and all.
    `sections` names the two for the Prompt page (Change this passage… asks
    the same way, engine/revision.py).

    `passage` may come in pieces (`pieces`: the passage and each
    continuation of it), sent as parts glued with nothing between, so the
    text is the same either way. With `cache_from` (Continue) the passage's
    last piece carries a cache point, when one is left and the stretch
    since the last is at least that many tokens: the turn's own request put
    its last one two exchanges back (the tail is rebuilt each turn), so every
    Continue sent those exchanges, the turn and the whole passage so far
    afresh, more of it each time. With the point, the next Continue reads
    all but what this one adds."""
    estimator = estimator or TokenEstimator()
    texts = [passage] if isinstance(passage, str) else list(passage)
    whole = "".join(texts)
    so_far = PromptSection(sections[0], whole, estimator.estimate(whole, prompt.model))
    ask = PromptSection(sections[1], request, estimator.estimate(request, prompt.model))
    added = so_far.tokens + ask.tokens
    by_section = dict(prompt.budget.by_section)
    by_section["tail"] = by_section.get("tail", 0) + added
    breakpoints = prompt.breakpoints
    marked = False
    if cache_from is not None and len(breakpoints) < MAX_CACHE_BREAKPOINTS:
        before = breakpoints[-1].prefix_tokens if breakpoints else 0
        prefix = prompt.budget.total + so_far.tokens
        if prefix - before >= cache_from:
            marked = True
            breakpoints = (
                *breakpoints,
                CacheBreakpoint(
                    label=BREAKPOINT_SO_FAR,
                    message_index=len(prompt.messages),
                    part_index=len(texts) - 1,
                    segment_tokens=prefix - before,
                    prefix_tokens=prefix,
                    section_name=sections[0],
                ),
            )
    parts = tuple(
        ContentPart(
            text=text,
            glued=index > 0,
            cache_breakpoint=marked and index == len(texts) - 1,
        )
        for index, text in enumerate(texts)
    )
    return replace(
        prompt,
        messages=(
            *prompt.messages,
            PromptMessage(role="assistant", parts=parts),
            PromptMessage(role="user", parts=(ContentPart(text=request),)),
        ),
        sections=(*prompt.sections, so_far, ask),
        breakpoints=breakpoints,
        budget=replace(prompt.budget, total=prompt.budget.total + added, by_section=by_section),
    )
