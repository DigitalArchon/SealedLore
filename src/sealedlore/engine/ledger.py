"""The story ledger: point-form facts that outlast the chapter summaries. Pure.

Summaries lose what is quiet. Measured on a real 216-node story (48
questions written from the raw prose, 33 of them still true at the end, three
judges agreeing): six chapters kept 0.80 of the facts, but only 0.50-0.62 of
who knows what and 0.67 of how people stand with each other. A ledger updated
from the raw prose of each archived chunk lifted the chapters to 0.87, those
two to 0.75 and 0.83-0.92; a half-length merge of the chapters with the ledger
beside it (2.7k tokens) kept 0.80, as much as the chapters alone (2.5k).

One snapshot per chapter (`Summary.ledger`, the ledger as of that chapter's
end), so it follows branches and merges the way the summaries do and changes
only when archival does. The update goes out beside the chapter's own summary
call. The model writes the lines; code decides whether the reply is a ledger at
all (`check_ledger`): live, GLM 5.3 reasoned its way to a truncated reply and
three lines replaced thirty-eight.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.summary import Summary

HEADINGS = (
    "People and bonds",
    "What people know",
    "Promises, plans and debts",
    "Where things stand",
)

# Asked for 40; Sonnet 4.6 settled at 43-50 over nine chapters. Past this it
# is not a ledger any more but a second summary.
MAX_LINES = 80
# A reply that keeps less than this share of the lines it was given has lost
# the ledger, not tidied it. Small ledgers are exempt: the story's first
# chapters legitimately rewrite most of what little there is.
MIN_KEPT_SHARE = 0.6
SMALL_LEDGER = 12


def build_ledger_messages(
    previous: str | None,
    prose: str,
    *,
    names: Sequence[str] = (),
    earlier: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """`earlier` is the chapters' own record, for a story whose first ledger
    comes after it already has chapters: without it, everything they hold
    never reaches the ledger (live: three chapters of a 216-node story)."""
    body: list[str] = []
    if names:
        body.append(texts.fill("ledger.names", names=", ".join(names)))
    if earlier.strip() and not (previous and previous.strip()):
        body.append(texts["ledger.earlier"] + "\n\n" + earlier.strip())
    body.append(
        "THE LEDGER AS IT STANDS:\n\n"
        + (previous.strip() if previous and previous.strip() else "(empty: the story's start)")
    )
    body.append("THE NEWEST STRETCH OF THE STORY:\n\n" + prose)
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["ledger.system"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip().startswith("- ")]


def check_ledger(text: str, previous: str | None, *, finish_reason: str | None) -> str | None:
    """Why `text` can't be the new ledger, or None if it can.

    Only the shape is judged here — every heading, bullet lines, a sane
    length, and not most of the previous ledger gone at once. What the lines
    say is the model's to judge.
    """
    if finish_reason == "length":
        return "the reply was cut off"
    headings = [m.group(1).strip() for m in re.finditer(r"(?m)^##\s+(.+)$", text)]
    missing = [h for h in HEADINGS if not any(h.lower() == found.lower() for found in headings)]
    if missing:
        return "it is missing " + ", ".join(missing)
    lines = _lines(text)
    if not lines:
        return "it has no entries"
    if len(lines) > MAX_LINES:
        return f"it ran to {len(lines)} lines"
    before = len(_lines(previous or ""))
    if before >= SMALL_LEDGER and len(lines) < MIN_KEPT_SHARE * before:
        return f"it kept {len(lines)} of {before} lines"
    return None


def normalised(text: str) -> str:
    """The ledger as stored: the four sections, bullet lines only, in order."""
    sections: dict[str, list[str]] = {heading: [] for heading in HEADINGS}
    current: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+)$", line.strip())
        if heading:
            current = next(
                (h for h in HEADINGS if h.lower() == heading.group(1).strip().lower()), None
            )
        elif current is not None and line.strip().startswith("- "):
            sections[current].append(line.strip())
    return "\n".join(
        f"## {heading}\n" + "\n".join(lines) for heading, lines in sections.items() if lines
    )


def ledger_on(summaries: Sequence[Summary]) -> str | None:
    """The ledger as of the last summary that has one."""
    for summary in reversed(summaries):
        if summary.ledger:
            return summary.ledger
    return None


def render_ledger_block(summaries: Sequence[Summary], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    ledger = ledger_on(summaries)
    if not ledger:
        return ""
    return f"{texts['storyteller.ledger']}\n\n{ledger.strip()}"
