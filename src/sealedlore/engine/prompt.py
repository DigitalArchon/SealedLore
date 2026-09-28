"""Prompt assembly.

A pure function of story state: no network, no GUI, no clock. Sections are
ordered strictly from most stable to most volatile, so that everything a cache
prefix depends on is settled before anything that changes every turn:

    1  SYSTEM BLOCK      engine rules, style, world, cast   <- breakpoint 1
    2  ARCHIVED SUMMARY  chapter summaries                  <- breakpoint 2
    3  VERBATIM HISTORY  recent turns of the active path    <- breakpoint 3
    4  VOLATILE TAIL     lore, scene, directives, the turn

Retrieved lore deliberately sits in the tail rather than up with the world
material: it changes every turn, and placing it earlier would invalidate the
cache across the whole history. Standing lore (a small lorebook whole, or the
`always_on` entries) is the opposite case: the same every turn, so it goes in
the system block after the world (`retrieval.lore_layout`).

The tail is part of the final *user* message, never a system message, so the
cached prefix stays intact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from sealedlore.engine.budget import BudgetReport, HistoryItem, select_history_items
from sealedlore.engine.characters import render_supporting_block
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.response_style import for_turn
from sealedlore.engine.rules import (
    render_author_turn,
    render_cast_block,
    render_history_node,
    render_lore_block,
    render_perspective_block,
    render_question,
    render_reminder,
    render_scene_block,
    render_scene_log_block,
    render_standing_lore,
    render_style_block,
    render_summaries_block,
    render_turn_directives,
    render_world_bible,
    render_world_block,
    voicing,
)
from sealedlore.engine.tokens import TokenEstimator
from sealedlore.messages import ContentPart, PromptMessage, Role
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import AgencyMode, Difficulty, Node, NpcScope, ResponseStyle, Roll
from sealedlore.models.scene import SceneLogEntry
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary

SECTION_ENGINE_RULES = "system.engine_rules"
SECTION_STYLE = "system.style"
SECTION_PERSPECTIVE = "system.perspective"
SECTION_WORLD_ACTIVITY = "system.world_activity"
SECTION_WORLD = "system.world_bible"
SECTION_STANDING_LORE = "system.lore"
SECTION_CAST = "system.cast"
SECTION_SUMMARIES = "summaries"
SECTION_HISTORY = "history"
SECTION_LORE = "tail.lore"
SECTION_SUPPORTING = "tail.supporting"
SECTION_SCENE_LOG = "tail.scene_log"
SECTION_STORY_TIME = "tail.story_time"
SECTION_SCENE = "tail.scene"
SECTION_DIRECTION = "tail.direction"
SECTION_DIRECTIVES = "tail.directives"
SECTION_AUTHOR_TURN = "tail.author_turn"
SECTION_QUESTION = "tail.question"
SECTION_REMINDER = "tail.reminder"

BREAKPOINT_SYSTEM = "system"
BREAKPOINT_SUMMARIES = "summaries"
BREAKPOINT_HISTORY = "history"

# Anthropic's limit.
MAX_CACHE_BREAKPOINTS = 4


@dataclass(frozen=True)
class TurnRequest:
    """Everything turn-specific. None of this may reach the system block."""

    speaker_id: str
    user_text: str
    controlled_character_id: str | None = None
    ooc: str | None = None
    npc_scope: NpcScope = "all"
    npc_scope_ids: tuple[str, ...] = ()
    agency_mode: AgencyMode = "contested"
    roll: Roll | None = None
    roll_explanation: str | None = None
    # Per-turn length: a preset, or the author's own wording (which wins).
    # None for both means the story's standing default.
    response_style: ResponseStyle | None = None
    length_hint: str | None = None
    # Dice mode: the skill the author picked (None = general) and how hard.
    # The roll itself is made by StorySession.send, never by the assembler.
    dice_domain: str | None = None
    difficulty: Difficulty = "standard"
    # A plot's clock and known events, and the director's instruction for
    # this passage (engine/director.py), both rendered by the session. The
    # plot itself never reaches the prompt.
    story_time: str = ""
    direction: str = ""
    # Whether the direction starts an event, which the REMEMBER line repeats.
    direction_begins: bool = False


@dataclass(frozen=True)
class AssemblyOptions:
    token_budget: int = 40_000
    min_cacheable_tokens: int = 1024
    cache_exchanges_outside_prefix: int = 1
    cast_token_cap: int = 8_000
    enable_cache_breakpoints: bool = True
    # A private scene's rules in place of the usual four sections, as
    # (section name, text); None for the story's own (engine/private_prompt.py).
    rules_override: tuple[tuple[str, str], ...] | None = None
    # The texts in force: the defaults, with the author's edits over them
    # (engine/prompt_texts.py).
    texts: PromptTexts = DEFAULT_TEXTS


@dataclass(frozen=True)
class PromptSection:
    name: str
    text: str
    tokens: int


@dataclass(frozen=True)
class CacheBreakpoint:
    label: str
    message_index: int
    part_index: int
    segment_tokens: int
    prefix_tokens: int
    # The section this breakpoint falls at the end of, so the inspector can
    # show the marker against a section without re-deriving it from the text.
    section_name: str = ""


@dataclass(frozen=True)
class AssembledPrompt:
    messages: tuple[PromptMessage, ...]
    sections: tuple[PromptSection, ...]
    breakpoints: tuple[CacheBreakpoint, ...]
    budget: BudgetReport
    model: str | None = None
    excluded_node_ids: tuple[str, ...] = ()
    # What the dropped nodes would have cost. Trimming caps `budget.total` at
    # the budget, which hides how far over the story really is; archival plans
    # against `full_total` so freeing space can't simply pull older prose back
    # into the window and leave the prompt just as full.
    excluded_tokens: int = 0
    include_cast_full_descriptions: bool = True

    @property
    def needs_archival(self) -> bool:
        """Oldest turns had to be dropped to fit — Phase 4 should archive them."""
        return bool(self.excluded_node_ids)

    @property
    def full_total(self) -> int:
        """What this prompt would cost with the whole verbatim path in it."""
        return self.budget.total + self.excluded_tokens

    def section(self, name: str) -> PromptSection | None:
        return next((section for section in self.sections if section.name == name), None)


@dataclass
class _DraftMessage:
    role: Role
    texts: list[str] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens)


def story_rules_sections(
    story: Story, texts: PromptTexts = DEFAULT_TEXTS
) -> tuple[tuple[str, str], ...]:
    """The storytelling rules at the top of the system block: the part a
    private scene may replace (engine/private_prompt.py)."""
    return (
        (SECTION_ENGINE_RULES, texts["storyteller.rules"]),
        (SECTION_STYLE, render_style_block(story.style, texts)),
        (SECTION_PERSPECTIVE, render_perspective_block(story.style.perspective, texts)),
        (SECTION_WORLD_ACTIVITY, render_world_block(story.defaults.world_activity, texts)),
    )


def not_placed_at_start(
    story: Story,
    cast_by_id: Mapping[str, Character],
    history_nodes: Sequence[Node],
    summaries: Sequence[Summary] = (),
) -> list[str]:
    """The cast the opening places, while nothing has been written yet.

    Everyone the author pinned neither present (the scene's roster) nor
    elsewhere (`StorySetup.absent_at_start`, or a note saying where they
    are). From the first passage on, the scene read has placed them and the
    roster says where everyone is, so this is empty.
    """
    if summaries or any(node.kind == "assistant" for node in history_nodes):
        return []
    scene = story.scene
    placed = {
        *scene.present_character_ids,
        *story.setup.absent_at_start,
        *(entry.character_id for entry in scene.offstage_but_nearby),
    }
    return [character_id for character_id in cast_by_id if character_id not in placed]


def assemble_prompt(
    *,
    story: Story,
    cast: Sequence[Character],
    history_nodes: Sequence[Node],
    turn: TurnRequest,
    summaries: Sequence[Summary] = (),
    lore: Sequence[LoreEntry] = (),
    standing_lore: Sequence[LoreEntry] = (),
    supporting: Sequence[Character] = (),
    on_file: Sequence[Character] = (),
    scene_log: Sequence[SceneLogEntry] = (),
    estimator: TokenEstimator | None = None,
    options: AssemblyOptions | None = None,
) -> AssembledPrompt:
    """Assemble the request for one turn.

    `supporting` is the cards whose full text rides this turn; `on_file` is
    every supporting card, which the scene names when it has kept track of
    who is present. `scene_log` is the ended scenes this path passed through.

    `history_nodes` is the active path *excluding* the turn being composed —
    that text arrives in `turn` and is rendered into the volatile tail.
    """
    cast_by_id = {character.id: character for character in cast}
    texts = (options or AssemblyOptions()).texts

    controlled = (
        cast_by_id.get(turn.controlled_character_id)
        if turn.controlled_character_id is not None
        else None
    )
    length = for_turn(story.style, turn.response_style, turn.length_hint, texts)

    tail_specs = [
        (SECTION_LORE, render_lore_block(lore, texts)),
        # In the tail, never the system block: which cards apply changes with
        # every turn, and a new card must not cost a cache miss.
        (SECTION_SUPPORTING, render_supporting_block(supporting, texts)),
        (SECTION_SCENE_LOG, render_scene_log_block(scene_log, texts)),
        (SECTION_STORY_TIME, turn.story_time),
        (
            SECTION_SCENE,
            render_scene_block(
                story.scene,
                cast_by_id,
                turn.controlled_character_id,
                story.style.perspective,
                supporting=on_file,
                texts=texts,
                not_placed=not_placed_at_start(story, cast_by_id, history_nodes, summaries),
            ),
        ),
        # After the scene it has to happen in, and close to the end: the
        # instruction the passage exists to carry out.
        (SECTION_DIRECTION, turn.direction),
        (
            SECTION_DIRECTIVES,
            render_turn_directives(
                controlled_character=controlled,
                npc_scope=turn.npc_scope,
                npc_scope_ids=turn.npc_scope_ids,
                cast_by_id=cast_by_id,
                agency_mode=turn.agency_mode,
                roll=turn.roll,
                roll_explanation=turn.roll_explanation,
                length=length,
                texts=texts,
            ),
        ),
        (
            SECTION_AUTHOR_TURN,
            render_author_turn(
                speaker_id=turn.speaker_id,
                speaker=cast_by_id.get(turn.speaker_id),
                text=turn.user_text,
                ooc=turn.ooc,
                texts=texts,
            ),
        ),
        (
            SECTION_REMINDER,
            render_reminder(
                controlled,
                length,
                story.style.perspective,
                turn.roll,
                author_only_names=[
                    character.name
                    for character_id, character in cast_by_id.items()
                    if voicing(character, character_id, turn.controlled_character_id) == "author"
                ],
                private=story.scene.privacy == "private",
                direction_begins=turn.direction_begins,
                person=story.style.person,
                tense=story.style.tense,
                texts=texts,
            ),
        ),
    ]
    return _assemble(
        story=story,
        cast=cast,
        history_nodes=history_nodes,
        tail_specs=tail_specs,
        summaries=summaries,
        standing_lore=standing_lore,
        estimator=estimator,
        options=options,
    )


def assemble_question(
    *,
    story: Story,
    cast: Sequence[Character],
    history_nodes: Sequence[Node],
    question: str,
    earlier: Sequence[tuple[str, str]] = (),
    summaries: Sequence[Summary] = (),
    lore: Sequence[LoreEntry] = (),
    standing_lore: Sequence[LoreEntry] = (),
    supporting: Sequence[Character] = (),
    on_file: Sequence[Character] = (),
    scene_log: Sequence[SceneLogEntry] = (),
    estimator: TokenEstimator | None = None,
    options: AssemblyOptions | None = None,
    rendered: str | None = None,
) -> AssembledPrompt:
    """Assemble an out-of-character question about the story (an aside).

    `rendered` replaces the author's question and its rules with a request
    of the program's own (the plot asking how a skipped-over event went),
    on the same cached prefix.

    Everything ahead of the tail is exactly what a story turn sends, so the
    question rides on the same cached prefix. `history_nodes` is the whole
    active path, since nothing is being composed; `earlier` is the question
    and answer pairs already asked at this point, so a follow-up has context.
    """
    cast_by_id = {character.id: character for character in cast}
    texts = (options or AssemblyOptions()).texts
    tail_specs = [
        (SECTION_LORE, render_lore_block(lore, texts)),
        # In the tail, never the system block: which cards apply changes with
        # every turn, and a new card must not cost a cache miss.
        (SECTION_SUPPORTING, render_supporting_block(supporting, texts)),
        (SECTION_SCENE_LOG, render_scene_log_block(scene_log, texts)),
        (
            SECTION_SCENE,
            render_scene_block(
                story.scene,
                cast_by_id,
                story.held_character_id,
                story.style.perspective,
                supporting=on_file,
                texts=texts,
                not_placed=not_placed_at_start(story, cast_by_id, history_nodes, summaries),
            ),
        ),
        (SECTION_QUESTION, rendered or render_question(question, earlier, texts)),
    ]
    return _assemble(
        story=story,
        cast=cast,
        history_nodes=history_nodes,
        tail_specs=tail_specs,
        summaries=summaries,
        standing_lore=standing_lore,
        estimator=estimator,
        options=options,
    )


def assemble_chat(
    *,
    story: Story,
    history_nodes: Sequence[Node],
    tail: str,
    summaries: Sequence[Summary] = (),
    nodes_by_id: Mapping[str, Node] | None = None,
    estimator: TokenEstimator | None = None,
    options: AssemblyOptions | None = None,
    with_chat_tail: bool = True,
) -> AssembledPrompt:
    """A simple chat's request (engine/chat.py): the author's system prompt,
    the summaries with the kept messages, the conversation as written, and
    `tail` (the user's message, or the picture writer's request) last, then
    the chat's own tail text if it has one (not for the picture writer)."""
    from sealedlore.engine.chat import (
        SECTION_CHAT_PROMPT,
        SECTION_CHAT_TAIL,
        render_chat_node,
        render_chat_summaries,
    )

    texts = (options or AssemblyOptions()).texts
    tail_specs = [(SECTION_AUTHOR_TURN, tail.strip())]
    if with_chat_tail:
        tail_specs.append((SECTION_CHAT_TAIL, story.chat_tail.strip()))

    return _assemble(
        story=story,
        cast=(),
        history_nodes=history_nodes,
        tail_specs=tail_specs,
        summaries=summaries,
        standing_lore=(),
        estimator=estimator,
        options=options,
        system_parts=[(SECTION_CHAT_PROMPT, story.chat_prompt.strip())],
        summaries_text=render_chat_summaries(summaries, nodes_by_id or {}, texts),
        render_node=lambda node: render_chat_node(node, texts),
    )


def _assemble(
    *,
    story: Story,
    cast: Sequence[Character],
    history_nodes: Sequence[Node],
    tail_specs: Sequence[tuple[str, str]],
    summaries: Sequence[Summary],
    standing_lore: Sequence[LoreEntry],
    estimator: TokenEstimator | None,
    options: AssemblyOptions | None,
    system_parts: Sequence[tuple[str, str]] | None = None,
    summaries_text: str | None = None,
    render_node: Callable[[Node], str] | None = None,
) -> AssembledPrompt:
    """The request, in the cached order. A simple chat (`assemble_chat`) gives
    its own `system_parts` in place of the rules, world, lore and cast, its
    own `summaries_text`, and `render_node` for plain history."""
    estimator = estimator or TokenEstimator()
    options = options or AssemblyOptions()
    model = story.defaults.main_model
    cast_by_id = {character.id: character for character in cast}

    def count(text: str) -> int:
        return estimator.estimate(text, model)

    sections: list[PromptSection] = []

    def add_section(name: str, text: str) -> int:
        tokens = count(text)
        sections.append(PromptSection(name=name, text=text, tokens=tokens))
        return tokens

    # --- 1. system block: stable for the whole story ------------------------
    system = _DraftMessage(role="system")
    system_section_names: list[str] = []

    def add_system_part(name: str, text: str) -> None:
        if not text:
            return
        system.texts.append(text)
        system.tokens.append(add_section(name, text))
        system_section_names.append(name)

    include_full_descriptions = True
    if system_parts is not None:
        for name, text in system_parts:
            add_system_part(name, text)
    else:
        rules = (
            options.rules_override
            if options.rules_override is not None
            else story_rules_sections(story, options.texts)
        )
        for name, text in rules:
            add_system_part(name, text)
        add_system_part(SECTION_WORLD, render_world_bible(story.world_bible))
        add_system_part(SECTION_STANDING_LORE, render_standing_lore(standing_lore, options.texts))

        # Full descriptions are in or out based on the cast alone, never on how
        # full the budget happens to be this turn: a system block that changes
        # shape mid-story would throw away the cache prefix every time it flipped.
        cast_text = render_cast_block(cast, include_full_descriptions=True, texts=options.texts)
        include_full_descriptions = count(cast_text) <= options.cast_token_cap
        if not include_full_descriptions:
            cast_text = render_cast_block(
                cast, include_full_descriptions=False, texts=options.texts
            )
        add_system_part(SECTION_CAST, cast_text)

    system_tokens = system.total_tokens

    # --- 2. archived summaries: change only when a chunk is archived --------
    if summaries_text is None:
        summaries_text = render_summaries_block(summaries, options.texts)
    summaries_tokens = 0
    if summaries_text:
        system.texts.append(summaries_text)
        summaries_tokens = add_section(SECTION_SUMMARIES, summaries_text)
        system.tokens.append(summaries_tokens)

    # --- 4. volatile tail (measured now, appended last) ---------------------
    tail_parts = [(name, text) for name, text in tail_specs if text]
    tail_tokens = sum(count(text) for _, text in tail_parts)

    # --- 3. verbatim history: whatever is left of the budget ----------------
    def rendered(node: Node) -> str:
        return (
            render_node(node)
            if render_node is not None
            else render_history_node(node, cast_by_id, options.texts)
        )

    items = [
        HistoryItem(node=node, text=text, tokens=count(text))
        for node, text in ((node, rendered(node)) for node in history_nodes)
        if text
    ]
    available = options.token_budget - (system_tokens + summaries_tokens + tail_tokens)
    kept, dropped = select_history_items(items, available)

    history_messages = _merge_history(kept, options.texts)
    history_tokens = sum(message.total_tokens for message in history_messages)
    if history_messages:
        history_text = "\n\n".join("\n\n".join(message.texts) for message in history_messages)
        sections.append(
            PromptSection(name=SECTION_HISTORY, text=history_text, tokens=history_tokens)
        )

    # The tail is appended to the final user message rather than sent as its
    # own system message, so the cached prefix ahead of it stays byte-identical.
    if history_messages and history_messages[-1].role == "user":
        tail_message = history_messages[-1]
    else:
        tail_message = _DraftMessage(role="user")
        history_messages.append(tail_message)
    tail_message_index = history_messages.index(tail_message)

    for name, text in tail_parts:
        tail_message.texts.append(text)
        tail_message.tokens.append(add_section(name, text))

    drafts = [system, *history_messages]
    breakpoints = _place_breakpoints(
        options=options,
        system_part_count=len(system.texts),
        has_summaries=bool(summaries_text),
        system_section_name=system_section_names[-1] if system_section_names else "",
        system_tokens=system_tokens,
        summaries_tokens=summaries_tokens,
        history_messages=history_messages,
        tail_message_index=tail_message_index,
    )

    if not system.texts:
        # A chat with no system prompt and nothing summarised yet: no system
        # message at all (some endpoints refuse an empty one).
        drafts = drafts[1:]
        breakpoints = [replace(bp, message_index=bp.message_index - 1) for bp in breakpoints]
    total = system_tokens + summaries_tokens + history_tokens + tail_tokens
    by_section = {
        "system": system_tokens,
        "summaries": summaries_tokens,
        "history": history_tokens,
        "tail": tail_tokens,
    }

    return AssembledPrompt(
        messages=tuple(_as_prompt(drafts, breakpoints)),
        sections=tuple(sections),
        breakpoints=tuple(breakpoints),
        budget=BudgetReport(budget=options.token_budget, total=total, by_section=by_section),
        model=model,
        excluded_node_ids=tuple(item.node.id for item in dropped),
        excluded_tokens=sum(item.tokens for item in dropped),
        include_cast_full_descriptions=include_full_descriptions,
    )


def _merge_history(
    items: Sequence[HistoryItem], texts: PromptTexts = DEFAULT_TEXTS
) -> list[_DraftMessage]:
    """Nodes to messages, merging runs of the same role.

    Anthropic requires strictly alternating user/assistant messages and a
    leading user message, and consecutive author turns (a speaker switch, a
    director aside) would otherwise break that.
    """
    messages: list[_DraftMessage] = []
    for item in items:
        role: Role = "assistant" if item.node.kind == "assistant" else "user"
        if messages and messages[-1].role == role:
            messages[-1].texts.append(item.text)
            messages[-1].tokens.append(item.tokens)
        else:
            messages.append(_DraftMessage(role=role, texts=[item.text], tokens=[item.tokens]))

    if messages and messages[0].role == "assistant":
        messages.insert(
            0, _DraftMessage(role="user", texts=[texts["storyteller.opening_turn"]], tokens=[0])
        )
    return messages


def _place_breakpoints(
    *,
    options: AssemblyOptions,
    system_part_count: int,
    has_summaries: bool,
    system_section_name: str,
    system_tokens: int,
    summaries_tokens: int,
    history_messages: Sequence[_DraftMessage],
    tail_message_index: int,
) -> list[CacheBreakpoint]:
    """Choose cache breakpoints, skipping any segment too short to be cached.

    Candidates are contiguous and ordered, so a skipped candidate's tokens roll
    forward into the next one rather than being lost.
    """
    if not options.enable_cache_breakpoints:
        return []

    candidates: list[tuple[str, int, int, int, str]] = []

    if system_part_count:
        system_last_part = system_part_count - (1 if has_summaries else 0) - 1
        if system_last_part >= 0 and system_tokens:
            candidates.append(
                (BREAKPOINT_SYSTEM, 0, system_last_part, system_tokens, system_section_name)
            )
        if has_summaries:
            candidates.append(
                (
                    BREAKPOINT_SUMMARIES,
                    0,
                    system_part_count - 1,
                    summaries_tokens,
                    SECTION_SUMMARIES,
                )
            )

    history_index = _history_breakpoint_index(
        exchanges_outside=options.cache_exchanges_outside_prefix,
        tail_message_index=tail_message_index,
    )
    if history_index is not None:
        segment = sum(message.total_tokens for message in history_messages[: history_index + 1])
        target = history_messages[history_index]
        # +1 for the system message that precedes the history in `drafts`.
        candidates.append(
            (
                BREAKPOINT_HISTORY,
                history_index + 1,
                len(target.texts) - 1,
                segment,
                SECTION_HISTORY,
            )
        )

    placed: list[CacheBreakpoint] = []
    pending = 0
    prefix = 0
    for label, message_index, part_index, segment_tokens, section_name in candidates:
        pending += segment_tokens
        prefix += segment_tokens
        if pending < options.min_cacheable_tokens:
            continue
        if len(placed) >= MAX_CACHE_BREAKPOINTS:
            break
        placed.append(
            CacheBreakpoint(
                label=label,
                message_index=message_index,
                part_index=part_index,
                segment_tokens=pending,
                prefix_tokens=prefix,
                section_name=section_name,
            )
        )
        pending = 0
    return placed


def _history_breakpoint_index(*, exchanges_outside: int, tail_message_index: int) -> int | None:
    """Index of the history message that carries breakpoint 3.

    Counted back from the tail-bearing message, which is never a candidate: it
    is rebuilt every turn and so could never be hit. The trailing
    `exchanges_outside` user+assistant pairs before it also stay outside the
    cached prefix, so that regenerating a recent take does not discard a cache
    write we just paid for.
    """
    index = tail_message_index - 1 - 2 * max(exchanges_outside, 0)
    return index if index >= 0 else None


def _as_prompt(
    drafts: Sequence[_DraftMessage], breakpoints: Sequence[CacheBreakpoint] = ()
) -> list[PromptMessage]:
    marked = {(bp.message_index, bp.part_index) for bp in breakpoints}
    messages: list[PromptMessage] = []
    for message_index, draft in enumerate(drafts):
        parts = tuple(
            ContentPart(text=text, cache_breakpoint=(message_index, part_index) in marked)
            for part_index, text in enumerate(draft.texts)
        )
        messages.append(PromptMessage(role=draft.role, parts=parts))
    return messages
