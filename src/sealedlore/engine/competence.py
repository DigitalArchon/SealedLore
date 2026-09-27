"""Suggest competence tiers from a character's description (§2.3).

Tiers are optional and the author's ruling. This only drafts them: the author
sees the suggestion and accepts or edits it, and nothing is ever required.
"""

from __future__ import annotations

from typing import get_args

from sealedlore.engine.jsonreply import extract_json
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character, CompetenceTier

TIERS: tuple[str, ...] = get_args(CompetenceTier)

INFER_SYSTEM = f"""\
You help an author describe what a story character can and cannot do. From the
character sheet, propose three to six skill domains that matter for this
character in this story, each with one tier from: {", ".join(TIERS)}.

Use short lowercase domain names ("combat", "piloting", "diplomacy",
"tracking"). Judge only from what the sheet and the world say; include a
weakness where the sheet implies one, since what a character is bad at matters
as much as what they are good at.

Reply with a single JSON object mapping domain to tier, and nothing else."""

# The world only needs to supply vocabulary (is there a Force? magic?); the
# whole bible would cost more than the suggestion is worth.
WORLD_EXCERPT_CHARS = 1500


def build_inference_messages(character: Character, world: str | None) -> list[PromptMessage]:
    sheet = [f"Name: {character.name}"]
    for label, value in (
        ("Summary", character.summary),
        ("Description", character.full_description),
        ("Competence notes", character.competence.notes),
        ("Voice", character.voice_notes),
    ):
        if value and value.strip():
            sheet.append(f"{label}: {value.strip()}")
    body = []
    if world and world.strip():
        body.append("The world, in brief:\n\n" + world.strip()[:WORLD_EXCERPT_CHARS])
    body.append("The character sheet:\n\n" + "\n".join(sheet))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=INFER_SYSTEM),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def parse_tiers(text: str) -> dict[str, CompetenceTier]:
    """Domain → tier from the reply; entries with an unknown tier are dropped."""
    data = extract_json(text, dict, "set of skills")
    tiers: dict[str, CompetenceTier] = {}
    for domain, tier in data.items():
        name = str(domain).strip().lower()
        value = str(tier).strip().lower()
        if name and value in TIERS:
            tiers[name] = value  # type: ignore[assignment]
    if not tiers:
        raise ValueError("no usable skills in the reply")
    return tiers
