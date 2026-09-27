"""Choosing lore with a model, for a lorebook too big to send whole. Pure.

Measured on the long test story with its lorebook grown to 100 entries (80 turns,
then 16 turns live on Sonnet 4.6, three blind judges).
Similarity and keywords, as sent today, left 10-11 lore contradictions in 32
passages: its keywords fire on some 39 entries a turn, and the token cap then
keeps them alphabetically. A model choosing from candidates left 0-8, and was
ranked first two to five times as often.

What made the difference beside the model was the author's own turn: a name
the author writes is the strongest sign an entry is needed. Jev alone, without
the turn's keyword hits, came last live even though it scored best offline;
with them it tied the chat-model picker. So both methods here add them.

Two ways to ask, the author's choice in Settings (`Config.lore_selector`):
- the picker: a chat model (`Config.lore_model`) picks after each passage,
  in the background, from candidates (keyword hits over the recent prose and
  the entries most similar to the passage). The picks ride on the passage's
  node, so Regenerate and every branch reuse them.
- Jev: one Noul question per entry just before the turn, over the last three
  passages and the turn itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.retrieval import (
    Injection,
    RetrievalReport,
    keyword_matches,
    render_entry,
)
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.lore import LoreEntry

# Candidates the picker sees beyond the keyword hits: the entries most similar
# to the passage. Twelve kept recall of what the judges called relevant near
# the keyword scan's while keeping the list to a few thousand tokens.
PICK_TOP_K = 12
# Per candidate, enough to know what the entry is about.
PICK_EXCERPT_CHARS = 400
PICK_MAX_TOKENS = 2000

# Jev's probabilities bunch between 0.3 and 0.8; 0.6 was the best cut on the
# test set (recall 0.63-0.71 against three judges, ~18 entries).
JEV_THRESHOLD = 0.6
# A /decisions request carries at most 64k tokens, state and questions
# together; batches stay well under it.
JEV_BATCH_TOKENS = 40_000
JEV_PASSAGES = 3


def scene_line(location: str | None, present: Sequence[str]) -> str:
    return f"SCENE: {location or 'unknown'}; present: {', '.join(present) or 'unknown'}"


def pick_candidates(
    entries: Sequence[LoreEntry],
    *,
    prose: str,
    scores: Mapping[str, float] | None = None,
    top_k: int = PICK_TOP_K,
) -> list[LoreEntry]:
    """The most similar entries first, then every keyword hit in the prose."""
    ranked = sorted(
        (entry for entry in entries if scores and entry.id in scores),
        key=lambda entry: -scores[entry.id],  # type: ignore[index]
    )[:top_k]
    chosen = {entry.id: entry for entry in ranked}
    for entry in entries:
        if entry.id not in chosen and keyword_matches(prose, entry):
            chosen[entry.id] = entry
    return list(chosen.values())


def build_pick_messages(
    passage: str,
    scene: str,
    candidates: Sequence[LoreEntry],
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    listing = "\n\n".join(
        f"[{number}] {entry.title}\n{entry.content.strip()[:PICK_EXCERPT_CHARS]}"
        for number, entry in enumerate(candidates, 1)
    )
    user = f"THE LAST PASSAGE:\n{passage}\n\n{scene}\n\nCANDIDATES:\n{listing}"
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["lore.picker"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def parse_pick(text: str, candidates: Sequence[LoreEntry]) -> list[str]:
    """The picked entries' ids, in the order given; numbers off the list are ignored."""
    data = extract_json(text, dict, "list of picks")
    picked: list[str] = []
    for number in data.get("pick") or []:
        try:
            index = int(number) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(candidates) and candidates[index].id not in picked:
            picked.append(candidates[index].id)
    return picked


def jev_state(passages: Sequence[str], author_turn: str, scene: str) -> str:
    story = "\n\n".join(text.strip() for text in passages[-JEV_PASSAGES:] if text.strip())
    parts = [f"THE STORY SO FAR (most recent last):\n\n{story}"]
    if author_turn.strip():
        parts.append(f"THE AUTHOR'S NEW TURN:\n{author_turn.strip()}")
    parts.append(scene)
    return "\n\n".join(parts)


def jev_batches(
    entries: Sequence[LoreEntry],
    count_tokens: Callable[[str], int],
    *,
    state_tokens: int,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[dict[str, str]]:
    """Questions keyed by entry id, split so no request passes the endpoint's limit."""
    room = max(JEV_BATCH_TOKENS - state_tokens, 1)
    batches: list[dict[str, str]] = [{}]
    used = 0
    for entry in entries:
        text = texts.fill("lore.jev", title=entry.title, content=entry.content.strip())
        tokens = count_tokens(text)
        if batches[-1] and used + tokens > room:
            batches.append({})
            used = 0
        batches[-1][entry.id] = text
        used += tokens
    return [batch for batch in batches if batch]


def choose_picked(
    entries: Sequence[LoreEntry],
    picked: Sequence[str],
    *,
    turn_text: str,
    token_cap: int,
    count_tokens: Callable[[str], int],
    scores: Mapping[str, float] | None = None,
    query: str = "",
    selector: str = "picker",
) -> RetrievalReport:
    """The picks, then the entries the author's own turn names, within the cap.

    Picks go first, in the model's order (or by probability, for Jev), so the
    cap drops keyword hits before anything a model chose.
    """
    by_id = {entry.id: entry for entry in entries}
    ordered: list[tuple[LoreEntry, str]] = [(by_id[i], "picked") for i in picked if i in by_id]
    seen = {entry.id for entry, _ in ordered}
    for entry in entries:
        if entry.id not in seen and turn_text.strip() and keyword_matches(turn_text, entry):
            ordered.append((entry, "keyword"))
            seen.add(entry.id)
    injected: list[Injection] = []
    dropped: list[Injection] = []
    used = 0
    for entry, reason in ordered:
        injection = Injection(
            entry=entry,
            reason=reason,  # type: ignore[arg-type]
            tokens=count_tokens(render_entry(entry)),
            score=(scores or {}).get(entry.id) if reason == "picked" else None,
        )
        if used + injection.tokens > token_cap:
            dropped.append(injection)
            continue
        injected.append(injection)
        used += injection.tokens
    return RetrievalReport(
        injected=tuple(injected),
        dropped=tuple(dropped),
        query_text=query,
        tokens=used,
        selector=selector,
    )
