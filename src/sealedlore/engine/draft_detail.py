"""How much a premise draft sets up: the world, the people, and each person.

The author (Nov 6 2026): "regarding both generating plots and generating the
rest from a premise, I want to add a level of world and character
detail/quantity as well. So the user can decide if they just want a handful
of characters and a basic world based on their premise, or if they want it
really outlined in detail, with lots of characters, each with lots of info
about them."

Three choices of three levels each. The middle level is the draft as it was:
the sizes the drafting prompt itself asks for (`authoring.draft`). Another
level is said in the request, after the premise, as sizes that replace the
prompt's (`authoring.draft.detail`), so the system prompt is the same
whatever is chosen, and a draft left at the middle levels is sent as it
always was, byte for byte.

The plot writer has its own say over how many people a plot brings in and how
fully they are written (engine/plot_draft.py, `PlotWishes`); a plot written
for a story drafted in depth writes its newcomers in depth too
(gui/generate_dialog.py).

Pure.
"""

from __future__ import annotations

from dataclasses import dataclass

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts

STANDARD = "standard"
# Each choice's levels, the least first; the words for each level but the
# standard one are `authoring.draft.<choice>.<level>`.
WORLD_LEVELS = ("basic", STANDARD, "detailed")
PEOPLE_LEVELS = ("handful", STANDARD, "lots")
DEPTH_LEVELS = ("sketch", STANDARD, "deep")
LEVELS = {"world": WORLD_LEVELS, "people": PEOPLE_LEVELS, "depth": DEPTH_LEVELS}


@dataclass(frozen=True)
class DraftDetail:
    """How long the world and its lore are, how many people, and how much
    about each of them."""

    world: str = STANDARD
    people: str = STANDARD
    depth: str = STANDARD

    def __post_init__(self) -> None:
        for choice, levels in LEVELS.items():
            value = getattr(self, choice)
            if value not in levels:
                raise ValueError(f"“{value}” isn't one of {', '.join(levels)}")

    def chosen(self) -> dict[str, str]:
        """The choices away from the standard level, in the request's order."""
        return {
            choice: getattr(self, choice) for choice in LEVELS if getattr(self, choice) != STANDARD
        }


STANDARD_DETAIL = DraftDetail()


def render_detail(detail: DraftDetail, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The sizes the author chose, for the draft's request, a line each;
    nothing when every choice is the standard one."""
    lines = [
        texts[f"authoring.draft.{choice}.{level}"] for choice, level in detail.chosen().items()
    ]
    if not lines:
        return ""
    return texts.fill("authoring.draft.detail", sizes="\n".join(f"- {line}" for line in lines))
