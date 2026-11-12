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

from collections.abc import Sequence
from dataclasses import replace

from sealedlore.engine.prompt import AssembledPrompt, PromptSection
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.tokens import TokenEstimator
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.node import Node

SECTION_SO_FAR = "continue.so_far"
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
    passage: str,
    request: str,
    estimator: TokenEstimator | None = None,
    *,
    sections: tuple[str, str] = (SECTION_SO_FAR, SECTION_CONTINUE),
) -> AssembledPrompt:
    """The turn's request carried on: the passage so far as the storyteller's
    own message after the author's turn, then `request`. Everything before
    them is the turn's request as it was assembled, breakpoints and all.
    `sections` names the two for the Prompt page (Change this passage… asks
    the same way, engine/revision.py)."""
    estimator = estimator or TokenEstimator()
    so_far = PromptSection(sections[0], passage, estimator.estimate(passage, prompt.model))
    ask = PromptSection(sections[1], request, estimator.estimate(request, prompt.model))
    added = so_far.tokens + ask.tokens
    by_section = dict(prompt.budget.by_section)
    by_section["tail"] = by_section.get("tail", 0) + added
    return replace(
        prompt,
        messages=(
            *prompt.messages,
            PromptMessage(role="assistant", parts=(ContentPart(text=passage),)),
            PromptMessage(role="user", parts=(ContentPart(text=request),)),
        ),
        sections=(*prompt.sections, so_far, ask),
        budget=replace(prompt.budget, total=prompt.budget.total + added, by_section=by_section),
    )
