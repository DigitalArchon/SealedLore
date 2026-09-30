"""Turn orchestration: assemble, stream, record, persist.

This is the seam the GUI will sit on in Phase 2 — it has no GUI imports and no
knowledge of Qt, so the same code path serves the CLI and the window. It owns
the story bundle in memory and writes it back after each turn.
"""

from __future__ import annotations

import json
import random
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sealedlore.engine.archival import (
    SUMMARY_CUT_OFF,
    PathSplit,
    build_summary_messages,
    chapter_number,
    chapter_numbers,
    characters_in,
    mark_stale,
    plan_chunk,
    render_chunk,
    split_path,
    swap_covered,
    turns_in,
)
from sealedlore.engine.authoring import (
    EVIDENCE_REPLIES,
    REVIEW_MAX_TOKENS,
    REVIEW_TEMPERATURE,
    apply_change,
    build_review_messages,
    ordered_for_apply,
    parse_review,
    render_exchange,
    render_prompt_catalogue,
    render_whole_prompt,
    reply_nodes,
    review_prompt_keys,
    review_system,
    settings_view,
)
from sealedlore.engine.branches import (
    Branch,
    MapLane,
    branch_starts,
    branches,
    map_columns,
    next_branch_name,
    unique_branch_name,
)
from sealedlore.engine.catalog import ModelInfo, chat_models
from sealedlore.engine.characters import (
    build_extraction_messages,
    card_from_suggestion,
    known_names,
    mentioned,
    parse_suggestions,
)
from sealedlore.engine.chronicle import (
    chronicle_at,
)
from sealedlore.engine.competence import build_inference_messages, parse_tiers
from sealedlore.engine.dice import explain, roll_for
from sealedlore.engine.director import (
    render_story_time_block,
)
from sealedlore.engine.ledger import build_ledger_messages, check_ledger, ledger_on, normalised
from sealedlore.engine.lore_pick import (
    JEV_THRESHOLD,
    PICK_MAX_TOKENS,
    build_pick_messages,
    choose_picked,
    jev_batches,
    jev_state,
    parse_pick,
    pick_candidates,
    scene_line,
)
from sealedlore.engine.pricing import estimate_cost, parse_price
from sealedlore.engine.prompt import (
    AssembledPrompt,
    AssemblyOptions,
    TurnRequest,
    assemble_chat,
    assemble_prompt,
    assemble_question,
)
from sealedlore.engine.prompt_edits import texts_in_force
from sealedlore.engine.prompt_texts import PromptTexts
from sealedlore.engine.retrieval import (
    LoreLayout,
    RetrievalReport,
    content_hash,
    cosine_scores,
    embedding_text,
    keyword_window,
    lore_layout,
    query_text,
    select_lore,
)
from sealedlore.engine.routing import (
    HostPrice,
    RouteCheck,
    check_route,
    describe_sent,
    role_in_use,
    routable,
    route_body,
)
from sealedlore.engine.scene_state import (
    SCENE_LOG_KEPT,
    closed_entry,
    describe_changes,
    log_on_path,
    move_into_cast,
    move_out_of_cast,
    rename_in_scene,
    same_person,
    scene_at,
    snapshot_index,
)
from sealedlore.engine.scene_update import (
    READ_MAX_TOKENS,
    SEED_CONTEXT_NODES,
    Resolved,
    SceneDelta,
    SceneProposal,
    build_read_messages,
    build_scene_messages,
    cut_to,
    parse_delta,
    parse_scene,
    strict_perspective,
)
from sealedlore.engine.session_archival import LEDGER_MAX_TOKENS, ArchivalRuntime
from sealedlore.engine.session_images import ImageRuntime
from sealedlore.engine.session_merge import MergeRuntime, _close
from sealedlore.engine.session_plot import PlotRuntime, SessionNotice
from sealedlore.engine.session_private import PrivateRuntime
from sealedlore.engine.session_reasoning import ReasoningRuntime
from sealedlore.engine.tokens import TokenEstimator, updated_correction_factor
from sealedlore.engine.validators import locate_quote, mentions
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.aside import Aside
from sealedlore.models.authoring import ReviewRecord, SettingsChange, SettingsReview
from sealedlore.models.character import Character, CompetenceTier
from sealedlore.models.config import Config, ModelPrice
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import (
    DIRECTOR_SPEAKER_ID,
    NARRATOR_SPEAKER_ID,
    AgencyMode,
    Difficulty,
    Node,
    NodeMeta,
    NpcScope,
    ResponseStyle,
    Usage,
)
from sealedlore.models.private import PrivateSpan
from sealedlore.models.route import ROUTE_ROLES
from sealedlore.models.scene import SceneLogEntry, SceneSource, SceneState
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import CharacterSuggestion
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    parse_usage,
)
from sealedlore.providers.decisions import DecisionsClient, build_decisions_payload
from sealedlore.providers.embeddings import EmbeddingBackend
from sealedlore.providers.tee import is_tee, move_refusal
from sealedlore.providers.wire import should_use_cache_control
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures, PictureStore
from sealedlore.storage.repository import (
    StoryBundle,
    append_api_log,
    load_story_bundle,
    save_config,
    save_story_bundle,
)
from sealedlore.tree import (
    active_path,
    index_nodes,
    latest_leaf,
    link_child,
    move_continuation,
    path_to,
    subtree_ids,
    takes_of,
)

# The model list the review checks suggestions against; refetched after this.
MODELS_MAX_AGE = timedelta(hours=1)
# How many of the endpoint's other models the reviewer is shown, so a model
# suggestion is an id that exists. The full list runs to hundreds.
REVIEW_ALTERNATIVES = 20

# Listed prices move; refetch after this long.
PRICE_MAX_AGE = timedelta(days=7)


def _stale(price: ModelPrice) -> bool:
    if not price.fetched_at:
        return True
    try:
        fetched = datetime.fromisoformat(price.fetched_at)
    except ValueError:
        return True
    return datetime.now(UTC) - fetched > PRICE_MAX_AGE


# How many chunks one turn may archive before giving up and sending a
# shortened window instead. Each round is a paid summariser call, so a budget
# set far too low must not quietly spend its way down to it. Catching up from
# a long story or a lowered budget can legitimately take several chunks at
# once — the cap is against runaway spending, not against a deep catch-up.
MAX_AUTO_ARCHIVE_ROUNDS = 8


@dataclass(frozen=True)
class PassageDone:
    """The storyteller's passage is in and saved; what follows is background.

    The scene read and the plot's read come after it. The window lets the
    author write their next turn from here (and queues it if they send before
    the reads finish), since nothing left in the job is theirs to wait for.
    """


@dataclass(frozen=True)
class TurnShape:
    """How the author wants the next passage shaped, right now.

    What the composer's per-turn controls hold: the length, the outcome mode,
    who may be voiced, and the dice settings. `regenerate` takes one so a new
    take follows the controls as they stand rather than the ones the first
    take was sent with.
    """

    response_style: ResponseStyle | None = None
    length_hint: str | None = None
    agency_mode: AgencyMode | None = None
    npc_scope: NpcScope | None = None
    npc_scope_ids: tuple[str, ...] = ()
    difficulty: Difficulty | None = None
    dice_domain: str | None = None


@dataclass(frozen=True)
class Deletion:
    """What deleting a message takes with it, for the confirmation and the report."""

    node_ids: tuple[str, ...]
    # How many of those are on the active path; the rest are other takes and
    # branches below the deleted message.
    on_path: int
    summaries: tuple[Summary, ...]
    asides: tuple[Aside, ...]

    @property
    def other_branches(self) -> int:
        return len(self.node_ids) - self.on_path


@dataclass(frozen=True)
class ReviewSizes:
    """Token estimates for what a settings review can be sent (see review_sizes)."""

    settings: int
    system_prompt: int
    # The per-turn instructions, always sent with the system prompt.
    tail: int
    whole_prompt: int
    # One per reply, newest first.
    exchanges: tuple[int, ...]
    # How many of the newest replies the whole prompt already carries.
    in_prompt: int
    # Passages the author marked as evidence, by node id, newest first, with
    # their sizes; sent whether or not they are among the newest replies.
    marked: tuple[tuple[str, int], ...] = ()
    # The storyteller's prompt texts the review may change (always sent), and
    # the side calls' (sent when the author asks).
    prompt_texts: int = 0
    side_texts: int = 0

    def total(self, replies: int, whole_prompt: bool, side_texts: bool = False) -> int:
        chosen = self.exchanges[:replies]
        skip = self.in_prompt if whole_prompt else 0
        prompt = self.whole_prompt if whole_prompt else self.system_prompt + self.tail
        marked = sum(size for _, size in self.marked)
        texts = self.prompt_texts + (self.side_texts if side_texts else 0)
        return self.settings + prompt + sum(chosen[skip:]) + marked + texts


@dataclass(frozen=True)
class ReviewSnapshot:
    """The settings as they were before a review's changes were applied, so
    they can be put back. Story, cast, supporting and lore: everything a
    change can touch, deep-copied."""

    story: Story
    cast: list[Character]
    supporting: list[Character]
    lore: list[LoreEntry]
    record_id: str | None
    # A move or a supporting rename rewrites the scene on every node, and a
    # model change becomes the provider's default: both come back too.
    scenes: dict[str, SceneState | None] = field(default_factory=dict)
    provider_model: str | None = None


@dataclass
class GenerationResult:
    node: Node | None
    text: str
    reasoning: str | None
    usage: Usage
    finish_reason: str | None
    stopped_early: bool


def cut_off_notice(max_tokens: int | None, completed: StreamCompleted) -> str:
    """What to tell the author when a passage stopped at the token limit.

    Live (GLM 5.3): 1,246 of a 1,500-token limit went on reasoning and the
    passage ended mid-sentence, saved as if it were whole.
    """
    raw = completed.raw_usage or {}
    thinking = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or raw.get(
        "reasoning_tokens"
    )
    spent = f"; {thinking:,} of them went on the model's reasoning" if thinking else ""
    return (
        f"The passage was cut off at the length limit ({max_tokens or 0:,} tokens{spent}). "
        "Raise Max tokens in the story's generation settings, then Regenerate."
    )


@dataclass
class _LedgerJob:
    """A ledger update in flight beside a chapter's summary call."""

    request: ChatRequest
    previous: str | None
    thread: threading.Thread | None = None
    reply: tuple[str, StreamCompleted] | None = None
    error: str | None = None


@dataclass
class _LorePick:
    """The lore model's pick for the passage after `node`, made beside the reads."""

    node: Node
    entries: tuple[LoreEntry, ...]
    prose: str
    scene: str
    vectors: dict[str, tuple[float, ...]]
    model: str
    # Its route (`route_for`), read before the thread starts.
    route: dict[str, Any] = field(default_factory=dict)
    candidates: list[LoreEntry] = field(default_factory=list)
    request: ChatRequest | None = None
    reply: tuple[str, StreamCompleted] | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None


# The pick runs beside the reads (~1.5s on DeepSeek V4.1 Flash, p90 3.6s); this
# only bounds a hung call, after which the next turn falls back to similarity.
LORE_PICK_WAIT_SECONDS = 60


def _weak_watch(
    method: Callable[[ChatRequest, StreamCompleted], None],
) -> Callable[[ChatRequest, StreamCompleted], None]:
    ref = weakref.WeakMethod(method)

    def watch(request: ChatRequest, completed: StreamCompleted) -> None:
        found = ref()
        if found is not None:
            found(request, completed)

    return watch


class StorySession(
    PlotRuntime, MergeRuntime, ArchivalRuntime, ImageRuntime, PrivateRuntime, ReasoningRuntime
):
    def __init__(
        self,
        bundle: StoryBundle,
        config: Config,
        provider: ChatProvider,
        *,
        root: Path | None = None,
        estimator: TokenEstimator | None = None,
        learn_corrections: bool = True,
        embeddings: EmbeddingBackend | None = None,
    ) -> None:
        self.bundle = bundle
        self.config = config
        # What each routed call's bill said about its route, latest per route
        # (`route_checks`); `host_price` is the window's lookup of a host's
        # listed price, None where it doesn't know it.
        self._route_lock = threading.Lock()
        self._route_checks: dict[tuple[str, str], tuple[str, RouteCheck]] = {}
        self.host_price: Callable[[str, str], HostPrice | None] | None = None
        self._init_reasoning()
        self.provider = provider
        # Optional by design: with no embeddings endpoint, retrieval falls back
        # to keywords silently (§7).
        self.embeddings = embeddings
        self.root = root
        self.estimator = estimator or TokenEstimator(factors=config.token_correction_factors)
        # Usage reported by a stand-in provider says nothing about a real
        # tokenizer, so callers driving a mock turn this off rather than
        # teaching the estimator from invented numbers.
        self.learn_corrections = learn_corrections
        self.last_prompt: AssembledPrompt | None = None
        self.last_result: GenerationResult | None = None
        self.last_retrieval: RetrievalReport | None = None
        self._decisions_client: DecisionsClient | None = None
        # Spend outside a turn or a summary — character scans — waiting for
        # the window to add it to the session's running cost.
        self.unreported_usage: list[Usage] = []
        # A memory-only chat's API log, which is never written (`memory_only`).
        self.memory_log: list[dict[str, Any]] = []
        # A memory-only chat's pictures (`pictures`); a story's are on disk.
        self._memory_pictures: MemoryPictures | None = None
        # Prices fetched for a memory-only chat, never written to the config.
        self._session_prices: dict[str, ModelPrice] = {}
        # Why the last archival's ledger update was refused, if it was.
        self.ledger_problem: str | None = None
        # Models whose price couldn't be found this session: don't ask again.
        self._price_misses: set[str] = set()
        # The endpoint's model list, and when it was fetched (see endpoint_models).
        self._models: dict[str, ModelInfo] | None = None
        self._models_at: datetime | None = None
        # The settings before the last review was applied, until something
        # else changes them (see apply_review / undo_review).
        self.review_undo: ReviewSnapshot | None = None
        # Passages the author marked as evidence for the next review.
        self.review_marks: list[str] = []
        # A branch named before its first message exists (the review's
        # "Branch before message N"): the message the next turn follows, and
        # the name that turn carries.
        self.pending_branch: tuple[str | None, str] | None = None
        # Dice (§4.2). A seeded Random in tests; the system's everywhere else.
        self.rng = random.Random()
        # A memory-only private scene the app closed in the middle of is gone.
        self.recover_private_spans()
        self._sync_scene()

    @classmethod
    def load(
        cls,
        story_id: str,
        config: Config,
        provider: ChatProvider,
        *,
        root: Path | None = None,
        estimator: TokenEstimator | None = None,
        learn_corrections: bool = True,
        embeddings: EmbeddingBackend | None = None,
    ) -> StorySession:
        bundle = load_story_bundle(story_id, root=root)
        session = cls(
            bundle,
            config,
            provider,
            root=root,
            estimator=estimator,
            learn_corrections=learn_corrections,
            embeddings=embeddings,
        )
        session.clean_stored_images()
        return session

    # --- state -------------------------------------------------------------

    @property
    def story(self) -> Story:
        return self.bundle.story

    @property
    def cast(self) -> list[Character]:
        return self.bundle.cast

    @property
    def nodes(self) -> list[Node]:
        return self.bundle.nodes

    def full_path(self) -> list[Node]:
        """Every message on the active path, private scenes included: what the
        author sees. Nothing that goes to a model reads this."""
        return active_path(self.nodes, self.story.active_leaf_id)

    def path(self) -> list[Node]:
        """The active path as models may see it: a private scene's messages
        left out (its approved summary stands in for them). Every model-facing
        read goes through here or `_path_to`, which is the leak rule."""
        return self.model_nodes(self.full_path())

    @staticmethod
    def model_nodes(nodes: Sequence[Node], include_span: str | None = None) -> list[Node]:
        """`nodes` without private messages, except those of `include_span`."""
        return [
            node
            for node in nodes
            if node.meta.private_span is None or node.meta.private_span == include_span
        ]

    def _path_to(self, node_id: str) -> list[Node]:
        return self.model_nodes(path_to(self.nodes, node_id))

    # --- what the plot has brought into the story -------------------------------

    def character_by_name(self, name: str) -> Character | None:
        lowered = name.strip().lower()
        for character in self.cast:
            if character.name.lower() == lowered:
                return character
            if any(alias.lower() == lowered for alias in character.aliases):
                return character
        return None

    @property
    def model(self) -> str:
        provider_config = self.config.active_provider()
        return self.story.defaults.main_model or (provider_config.model if provider_config else "")

    @property
    def summarization_model(self) -> str:
        """§5.2: the story's own, else the one set for every story, else the
        story's main model. A TEE chat's summaries are its own model's: the
        conversation never leaves it."""
        if self.story.chat and is_tee(self.model):
            return self.model
        return (
            self.story.defaults.summarization_model or self.config.summarization_model or self.model
        )

    @property
    def scene_model(self) -> str:
        """The model that reads the scene after each passage: small and fast is right."""
        return self.config.scene_model or self.summarization_model

    @property
    def provider(self) -> ChatProvider:
        return self._provider

    @provider.setter
    def provider(self, provider: ChatProvider) -> None:
        """Every provider the session is given reports its routed replies, and
        replies that reasoned unasked, here (held weakly: a provider outlives
        the session that set it)."""
        self._provider = provider
        provider.route_watch = _weak_watch(self._watch_route)
        provider.reasoning_watch = _weak_watch(self._watch_reasoning)

    def _watch_route(self, request: ChatRequest, completed: StreamCompleted) -> None:
        """On whichever thread the reply came back: record what its bill says."""
        sent = request.extra_body.get("provider") or {}
        preferred = None
        order = sent.get("order") or []
        if order and self.host_price is not None:
            preferred = self.host_price(request.model, str(order[0]))
        check = check_route(request.model, sent, completed.usage, preferred)
        key = (request.model, json.dumps(sent, sort_keys=True))
        with self._route_lock:
            self._route_checks[key] = (utc_now_iso(), check)

    def route_checks(self) -> list[tuple[str, RouteCheck, list[str]]]:
        """(when, the latest check, the roles on that route) for each route
        used: what the status bar shows."""
        with self._route_lock:
            found = list(self._route_checks.items())
        roles_by_key: dict[tuple[str, str], list[str]] = {}
        for role in ROUTE_ROLES:
            _role, model = self._role_in_use(role)
            body = self.route_for(role)
            if body:
                key = (model, json.dumps(body["provider"], sort_keys=True))
                roles_by_key.setdefault(key, []).append(role)
        return [(at, check, roles_by_key.get(key, [])) for key, (at, check) in found]

    def role_model(self, role: str) -> str:
        """The model a role's calls go to, its fallbacks followed."""
        return self._role_in_use(role)[1]

    def route_role(self, role: str) -> str:
        """The role whose route a role's calls take, its fallbacks followed."""
        return self._role_in_use(role)[0]

    def route_text(self, role: str) -> str | None:
        """The role's route as it is sent ("Fastest first word · FP8+"), or
        None when its calls go on NanoGPT's own routing or can't be routed."""
        body = self.route_for(role)
        return describe_sent(body["provider"]) if body else None

    def _role_in_use(self, role: str) -> tuple[str, str]:
        """(the role whose model a call uses, that model): a blank role takes
        its fallback's model, and with it that role's route."""
        own = {
            "summarisation": self.story.defaults.summarization_model
            or self.config.summarization_model,
            "scene": self.config.scene_model,
            "plot": self.config.plot_model,
            "lore": self.config.lore_model,
            "authoring": self.config.authoring_model,
            "image_prompt": self.config.image_prompt_model,
        }
        role = role_in_use(role, own)
        if role == "summarisation" and self.story.chat and is_tee(self.model):
            role = "story"  # a TEE chat's summaries are its own model's
        return role, (self.model if role == "story" else own[role] or self.model)

    def route_for(self, role: str, model: str | None = None) -> dict[str, Any]:
        """The request fields that send `role`'s call to the host its route
        asks for (engine/routing.py): none on another endpoint, for a TEE or
        encrypted model, or in a private scene (its calls are the private
        model's). A chat's own model has its own route. `model`: the one the
        call is actually sent to, when a caller chose it (the picture dialog's
        prompt writer)."""
        provider = self.config.active_provider()
        if provider is None or self.open_span is not None:
            return {}
        role, in_use = self._role_in_use(role)
        model = model or in_use
        if not routable(provider.base_url, model):
            return {}
        if role == "story" and self.story.chat:
            route = self.story.defaults.main_route
        else:
            route = self.config.model_routes.get(role)
        return route_body(route, model)

    @property
    def summaries(self) -> list[Summary]:
        return self.bundle.summaries

    @property
    def texts(self) -> PromptTexts:
        """The prompt texts in force for this story: the defaults, the
        author's edits for every story over them, this story's over those."""
        return texts_in_force(self.config.prompt_edits, self.story.prompt_edits)

    def assembly_options(self) -> AssemblyOptions:
        return AssemblyOptions(
            token_budget=self.story.defaults.context_token_budget,
            min_cacheable_tokens=self.config.min_cacheable_tokens,
            cache_exchanges_outside_prefix=self.config.cache_exchanges_outside_prefix,
            cast_token_cap=self.config.cast_token_cap,
            texts=self.texts,
        )

    def uses_cache_control(self) -> bool:
        return self._cache_control_for(self.model)

    def _cache_control_for(self, model: str) -> bool:
        return should_use_cache_control(
            model,
            mode=self.config.cache_control_mode,
            patterns=self.config.anthropic_model_patterns,
        )

    # --- assembly ----------------------------------------------------------

    def _refuse_in_chat(self, what: str) -> None:
        """For what only a story has (engine/chat.py)."""
        if self.story.chat:
            raise ValueError(f"{what} isn't part of a simple chat.")

    @property
    def tee_chat(self) -> bool:
        """A chat on a TEE model: attested, its replies' signatures checked,
        and every call it makes on that model."""
        return self.story.chat and is_tee(self.model)

    def check_chat_model(self, model: str) -> None:
        """A TEE chat moves only to another TEE model: it must never hand the
        conversation to one that can't be attested, and an end-to-end
        encrypted one never to one whose gateway reads it."""
        reason = move_refusal(self.model, model) if self.story.chat else None
        if reason:
            raise ValueError(reason)

    def set_keep_full(self, node_id: str, keep: bool) -> None:
        """Keep a chat message word for word, never summarised (or stop).

        Once its part is summarised, the message is carried after that part's
        summary in the cached block, so a change there costs one cache miss."""
        node = index_nodes(self.nodes).get(node_id)
        if node is None:
            raise KeyError(f"unknown node id: {node_id!r}")
        node.meta.keep_full = keep
        self.save()

    def kept_tokens(self) -> int:
        """What the kept messages on this path cost, summarised or not."""
        return sum(
            self.estimator.estimate(node.content, self.model)
            for node in self.path()
            if node.meta.keep_full
        )

    def split(self, nodes: Sequence[Node] | None = None) -> PathSplit:
        """Which summaries stand in for which nodes on this path (§5.2)."""
        return split_path(self.bundle.summaries, self.path() if nodes is None else nodes)

    def assemble(
        self,
        turn: TurnRequest,
        *,
        history_nodes: Sequence[Node] | None = None,
        lore: Sequence[LoreEntry] | None = None,
    ) -> AssembledPrompt:
        # Only the summaries that match this path are sent, and only the nodes
        # they don't already cover. On a branch that forks below the summarised
        # line nothing matches, so the prose goes verbatim instead.
        split = self.split(history_nodes)
        if self.story.chat:
            prompt = assemble_chat(
                story=self.story,
                history_nodes=split.verbatim,
                tail=turn.user_text,
                summaries=split.summaries,
                nodes_by_id=index_nodes(self.nodes),
                estimator=self.estimator,
                options=self.assembly_options(),
            )
            self.last_prompt = prompt
            return prompt
        nodes = self.path() if history_nodes is None else list(history_nodes)
        if lore is None:
            # Assembling must never block on the network — the inspector calls
            # this on the GUI thread — so an unqualified assemble previews the
            # offline selection rather than embedding a query.
            lore = self.retrieve(turn, nodes, allow_network=False).entries
        plot = self.story.plot
        if plot is not None and not turn.story_time:
            turn = replace(
                turn,
                story_time=render_story_time_block(
                    plot, chronicle_at(nodes, plot), texts=self.texts
                ),
            )
        prompt = assemble_prompt(
            story=self.story,
            cast=self.visible_cast(),
            history_nodes=split.verbatim,
            turn=turn,
            summaries=split.summaries,
            lore=lore,
            standing_lore=self.lore_layout().standing,
            supporting=self.supporting_for(turn.user_text, nodes),
            on_file=self.visible_supporting(),
            scene_log=log_on_path(self.story.scene_log, nodes),
            estimator=self.estimator,
            options=self.assembly_options(),
        )
        self.last_prompt = prompt
        return prompt

    # --- lore retrieval (§7) ------------------------------------------------

    def retrieve(
        self,
        turn: TurnRequest,
        history: Sequence[Node],
        *,
        allow_network: bool = True,
    ) -> RetrievalReport:
        """Choose this turn's lore, semantically when possible.

        A missing or failing embeddings endpoint is not an error: §7 says fall
        back to keywords silently and show an indicator, so the report carries
        the reason and the caller decides how loudly to say it.
        """
        layout = self.lore_layout()
        query = query_text(turn.user_text, history, turns=self.config.lore_query_turns)
        if layout.mode == "whole":
            # Nothing to choose: every entry is standing in the system block.
            return RetrievalReport(mode="whole", standing=layout.standing, query_text=query)
        report = self._select(layout.selectable, turn, history, query, allow_network=allow_network)
        return replace(report, standing=layout.standing)

    def lore_layout(self) -> LoreLayout:
        """Whether the lorebook goes whole into the system block or is selected from."""
        return lore_layout(
            self.visible_lore(),
            budget=self.story.defaults.context_token_budget,
            share=self.config.lore_whole_share,
            count_tokens=self._count_lore_tokens,
        )

    def _select(
        self,
        entries: Sequence[LoreEntry],
        turn: TurnRequest,
        history: Sequence[Node],
        query: str,
        *,
        allow_network: bool,
    ) -> RetrievalReport:
        """This turn's entries by the author's chosen method; similarity when it can't."""
        selector = self.config.lore_selector
        note: str | None = None
        if selector == "picker":
            last = history[-1] if history and history[-1].kind == "assistant" else None
            if last is not None and last.meta.lore_picks is not None:
                return choose_picked(
                    entries,
                    last.meta.lore_picks,
                    turn_text=turn.user_text,
                    token_cap=self.config.lore_token_cap,
                    count_tokens=self._count_lore_tokens,
                    query=query,
                )
            note = "no picks for the last passage"
        elif selector == "jev":
            if not allow_network:
                note = "preview: Jev not asked"
            else:
                try:
                    return self._jev_select(entries, turn, history, query)
                except ProviderError as exc:
                    note = f"Jev failed: {exc}"
        keywords_over, reach = keyword_window(entries, turn.user_text, history)
        report = self._similarity(entries, query, keywords_over, allow_network=allow_network)
        report = replace(report, keyword_reach=reach)
        return replace(report, selector_note=note) if note else report

    def _jev_select(
        self, entries: Sequence[LoreEntry], turn: TurnRequest, history: Sequence[Node], query: str
    ) -> RetrievalReport:
        """Jev scores every entry against the last passages and the turn (lore_pick)."""
        client = self._decisions()
        if client is None:
            raise ProviderError("no chat endpoint to ask")
        model = self.config.lore_decision_model
        state = jev_state(
            [node.content for node in history],
            turn.user_text,
            scene_line(self.story.scene.location, self._present_names()),
        )
        probabilities: dict[str, float] = {}
        state_tokens = self._count_lore_tokens(state)
        for batch in jev_batches(
            entries, self._count_lore_tokens, state_tokens=state_tokens, texts=self.texts
        ):
            payload = build_decisions_payload(model, state, batch)
            log_ref = self._log("lore_decision_request", {"payload": payload})
            decided = client.decide(model, state, batch)
            self._log(
                "lore_decision_response",
                {"request_log_ref": log_ref, "usage": decided.raw_usage},
            )
            self.unreported_usage.append(self.priced(parse_usage(decided.raw_usage), model))
            probabilities.update(decided.probabilities)
        picked = sorted(
            (i for i, p in probabilities.items() if p >= JEV_THRESHOLD),
            key=lambda i: -probabilities[i],
        )
        return choose_picked(
            entries,
            picked,
            turn_text=turn.user_text,
            token_cap=self.config.lore_token_cap,
            count_tokens=self._count_lore_tokens,
            scores=probabilities,
            query=query,
            selector="jev",
        )

    def _start_lore_pick(self) -> _LorePick | None:
        """After a passage, ask the lore model what the next one will need.

        Only for the "picker" method on a lorebook too big to send whole. The
        call goes out on a detached client while the scene and plot reads run,
        and `_finish_lore_pick` applies it; a provider without `detached` (the
        scripted mock) is asked there, in turn.
        """
        if self.config.lore_selector != "picker" or self.story.chat:
            return None
        result = self.last_result
        if result is None or result.node is None or result.stopped_early:
            return None
        node = result.node
        layout = self.lore_layout()
        if layout.mode != "select" or not layout.selectable or not node.content.strip():
            return None
        path = self._path_to(node.id)
        vectors: dict[str, tuple[float, ...]] = {}
        if self.embeddings is not None:
            try:
                vectors = self.ensure_lore_vectors()
            except ProviderError:
                vectors = {}
        pick = _LorePick(
            node=node,
            entries=layout.selectable,
            prose="\n\n".join(n.content for n in path[-3:]),
            scene=scene_line(self.story.scene.location, self._present_names()),
            vectors=vectors,
            model=self.config.lore_model or self.scene_model,
            route=self.route_for("lore"),
        )
        detached = getattr(self.provider, "detached", None)
        if callable(detached):
            client = detached()
            pick.thread = threading.Thread(
                target=self._run_lore_pick, args=(pick, client, True), name="lore-pick", daemon=True
            )
            pick.thread.start()
        return pick

    def _run_lore_pick(self, pick: _LorePick, client: ChatProvider, close: bool) -> None:
        try:
            scores: dict[str, float] | None = None
            if pick.vectors and self.embeddings is not None:
                query = self.embeddings.embed([pick.node.content], as_query=True).vectors
                ids = [e.id for e in pick.entries if e.id in pick.vectors]
                if query and ids:
                    found = cosine_scores(query[0], [pick.vectors[i] for i in ids])
                    scores = dict(zip(ids, found, strict=True))
            pick.candidates = pick_candidates(pick.entries, prose=pick.prose, scores=scores)
            if pick.candidates:
                pick.request = ChatRequest(
                    model=pick.model,
                    extra_body=pick.route,
                    messages=build_pick_messages(
                        pick.node.content, pick.scene, pick.candidates, texts=self.texts
                    ),
                    params=self.side_params(pick.model, max_tokens=PICK_MAX_TOKENS),
                )
                pick.reply = client.complete(pick.request)
        except BaseException as exc:  # noqa: BLE001 - reported on the worker thread
            pick.error = exc
        finally:
            if close:
                _close(client)

    def _finish_lore_pick(self, pick: _LorePick | None) -> Iterator[SessionNotice]:
        if pick is None:
            return
        if pick.thread is None:
            self._run_lore_pick(pick, self.provider, False)
        else:
            pick.thread.join(LORE_PICK_WAIT_SECONDS)
            if pick.thread.is_alive():
                # The call is out and will be paid for; its reply arrives to
                # nobody. The request at least is on record.
                if pick.request is not None:
                    self._log(
                        "lore_pick_request",
                        {
                            "node_id": pick.node.id,
                            "payload": self.provider.build_payload(pick.request),
                            "timed_out": True,
                        },
                    )
                yield SessionNotice(
                    "The lore pick is taking too long; the next turn uses similarity."
                )
                return
        if pick.request is not None:
            log_ref = self._log(
                "lore_pick_request",
                {"node_id": pick.node.id, "payload": self.provider.build_payload(pick.request)},
            )
            if pick.reply is not None:
                text, completed = pick.reply
                self._log(
                    "lore_pick_response",
                    {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, pick.model))
        try:
            if pick.error is not None:
                raise pick.error
            picks = parse_pick(pick.reply[0], pick.candidates) if pick.reply else []
        except (ProviderError, ValueError) as exc:
            yield SessionNotice(
                f"Lore pick failed on {pick.model} ({exc}); the next turn uses similarity. "
                "The lore model is set under Settings → Lore."
            )
            return
        pick.node.meta.lore_picks = picks
        self.save()

    def _decisions(self) -> DecisionsClient | None:
        """Jev rides on the chat endpoint and key; built once."""
        provider = self.config.active_provider()
        if provider is None:
            return None
        if self._decisions_client is None:
            self._decisions_client = DecisionsClient(provider.base_url, provider.api_key)
        return self._decisions_client

    def _present_names(self) -> list[str]:
        scene = self.story.scene
        names = [c.name for c in self.cast if c.id in scene.present_character_ids]
        return names + list(scene.present_others)

    def _similarity(
        self,
        entries: Sequence[LoreEntry],
        query: str,
        keyword_text: str,
        *,
        allow_network: bool,
    ) -> RetrievalReport:
        """Embeddings over the recent prose, keywords over the author's turn
        (or the last few exchanges when it names nothing: `keyword_window`)."""

        def keywords_only(reason: str | None) -> RetrievalReport:
            return select_lore(
                entries,
                query=query,
                keyword_text=keyword_text,
                k=self.config.lore_retrieval_k,
                threshold=self.config.lore_similarity_threshold,
                token_cap=self.config.lore_token_cap,
                count_tokens=self._count_lore_tokens,
                used_embeddings=False,
                fallback_reason=reason,
            )

        if not entries:
            return keywords_only(None)
        if self.embeddings is None:
            return keywords_only(None if self.config.embedding_provider is None else "no endpoint")
        if not allow_network:
            return keywords_only("preview: not embedded")
        if not query.strip():
            return keywords_only(None)

        try:
            vectors = self.ensure_lore_vectors()
            result = self.embeddings.embed([query], as_query=True)
        except ProviderError as exc:
            return keywords_only(str(exc))
        if not result.vectors:
            return keywords_only("endpoint returned no vector")

        scored = [(entry_id, vector) for entry_id, vector in vectors.items()]
        try:
            similarities = cosine_scores(result.vectors[0], [vector for _, vector in scored])
        except ValueError as exc:
            # A model change mid-story can leave cached vectors of another
            # width; drop them and let the next turn re-embed.
            self.bundle.embeddings.clear()
            self.save()
            return keywords_only(str(exc))

        scores = {
            entry_id: score for (entry_id, _), score in zip(scored, similarities, strict=True)
        }
        return select_lore(
            entries,
            query=query,
            keyword_text=keyword_text,
            scores=scores,
            k=self.config.lore_retrieval_k,
            threshold=self.config.lore_similarity_threshold,
            token_cap=self.config.lore_token_cap,
            count_tokens=self._count_lore_tokens,
            used_embeddings=True,
        )

    def ensure_lore_vectors(self) -> dict[str, tuple[float, ...]]:
        """Embed any entry whose text or model has changed; return id → vector.

        Vectors are cached by content hash *and* model, so editing one entry
        re-embeds one entry, and switching models re-embeds everything exactly
        once (§7).
        """
        settings = self.config.embeddings()
        model = settings.model if settings else ""
        wanted = settings.dimensions if settings else 0

        cache = {
            (row.content_hash, row.model): row
            for row in self.bundle.embeddings
            if not wanted or row.dimensions == wanted
        }

        pending: list[LoreEntry] = []
        hashes: dict[str, str] = {}
        # A held-back entry reaches no model, the embeddings endpoint's
        # included; it is embedded on the first turn after it is named.
        held = self.held_back_ids()
        for entry in self.bundle.lore:
            if not entry.enabled or entry.id in held:
                continue
            digest = content_hash(embedding_text(entry))
            hashes[entry.id] = digest
            if (digest, model) not in cache:
                pending.append(entry)

        if pending and self.embeddings is not None:
            result = self.embeddings.embed([embedding_text(entry) for entry in pending])
            for entry, vector in zip(pending, result.vectors, strict=True):
                row = EmbeddingCacheEntry(
                    # Keyed by the model *we asked for*, not the name the
                    # endpoint echoes back: a proxy that renames the model
                    # would otherwise miss the cache on every single turn.
                    content_hash=hashes[entry.id],
                    model=model,
                    dimensions=len(vector),
                    vector=list(vector),
                )
                self.bundle.embeddings.append(row)
                cache[(row.content_hash, row.model)] = row
                entry.embedding_hash = row.content_hash
            self._prune_embeddings(set(hashes.values()))
            self.save()

        vectors: dict[str, tuple[float, ...]] = {}
        for entry_id, digest in hashes.items():
            row = cache.get((digest, model))
            if row is not None:
                vectors[entry_id] = tuple(row.vector)
        return vectors

    def _prune_embeddings(self, live_hashes: set[str]) -> None:
        """Drop vectors for text no entry holds any more, so the file can shrink."""
        self.bundle.embeddings = [
            row for row in self.bundle.embeddings if row.content_hash in live_hashes
        ]

    def _count_lore_tokens(self, text: str) -> int:
        return self.estimator.estimate(text, self.model)

    def _retrieve_for_turn(
        self, turn: TurnRequest, history: Sequence[Node]
    ) -> tuple[LoreEntry, ...]:
        """Retrieve for a turn about to be sent, and keep the report for the UI."""
        if self.story.chat:  # a simple chat has no lore
            self.last_retrieval = None
            return ()
        report = self.retrieve(turn, history)
        self.last_retrieval = report
        return report.entries

    # --- turns -------------------------------------------------------------

    def _character(self, character_id: str | None) -> Character | None:
        return next((c for c in self.cast if c.id == character_id), None)

    def _reshape(self, turn: TurnRequest, shape: TurnShape, user_node: Node) -> TurnRequest:
        """Apply the author's current per-turn choices to a turn being retaken.

        The choices are written back onto the author's node: they are the
        author's intent for that turn now, and the next take should reproduce
        this one rather than the shape the first take happened to use.
        """
        turn = replace(
            turn,
            response_style=shape.response_style,
            length_hint=shape.length_hint,
            agency_mode=shape.agency_mode or turn.agency_mode,
            npc_scope=shape.npc_scope or turn.npc_scope,
            npc_scope_ids=shape.npc_scope_ids,
            difficulty=shape.difficulty or turn.difficulty,
            dice_domain=shape.dice_domain if shape.dice_domain is not None else turn.dice_domain,
        )
        # A roll belongs to dice mode. Leaving one on a turn the author has
        # moved to Contested would hand the model an outcome nothing asked for;
        # moving *to* dice with no roll needs one, and that isn't fishing
        # because there was no roll to dislike.
        if turn.agency_mode != "dice":
            turn = replace(turn, roll=None, roll_explanation=None)
        elif turn.roll is None:
            turn = self._rolled(turn)
        user_node.meta.response_style = turn.response_style
        user_node.meta.length_hint = turn.length_hint
        user_node.meta.agency_mode = turn.agency_mode
        user_node.meta.npc_scope = turn.npc_scope
        user_node.meta.npc_scope_ids = list(turn.npc_scope_ids)
        user_node.meta.difficulty = turn.difficulty
        user_node.meta.dice_domain = turn.dice_domain
        user_node.meta.roll = turn.roll
        return turn

    def _rolled(self, turn: TurnRequest) -> TurnRequest:
        """Roll for this turn if it's the author's character acting in dice mode.

        Only an in-character turn is an attempt. Direction and Narration are
        the author's fact, not something the dice get a say in.
        """
        if turn.agency_mode != "dice" or turn.roll is not None:
            return turn
        held = self._character(turn.controlled_character_id)
        if held is None or turn.speaker_id != held.id:
            return turn
        roll = roll_for(held, turn.dice_domain, turn.difficulty, self.rng)
        return replace(turn, roll=roll, roll_explanation=explain(roll, held.name))

    def send(self, turn: TurnRequest) -> Iterator[StreamEvent | SessionNotice | PassageDone]:
        """Append the author's turn, then stream the response into a new node."""
        self._tee_guard()
        turn = self._rolled(turn)
        history = self.path()
        user_node = Node(
            kind="user",
            speaker_id=turn.speaker_id,
            content=turn.user_text,
            ooc=turn.ooc,
            meta=NodeMeta(
                agency_mode=turn.agency_mode,
                npc_scope=turn.npc_scope,
                npc_scope_ids=list(turn.npc_scope_ids),
                controlled_character_id=turn.controlled_character_id,
                response_style=turn.response_style,
                length_hint=turn.length_hint,
                roll=turn.roll,
                dice_domain=turn.dice_domain,
                difficulty=turn.difficulty if turn.agency_mode == "dice" else None,
            ),
        )
        if self.in_private:
            # The scene's own flow: the private model, nothing else called.
            if self.private_provider is None:
                raise ValueError(
                    "the private scene has no model to send to (Settings → Private); "
                    "end the scene to go on"
                )
            # Marked before it is attached: a turn that can't be sent must never
            # stand in the tree as a public message.
            user_node.meta.private_span = self.open_span.id
            full = self.full_path()
            self._attach(user_node, parent=full[-1] if full else None)
            yield from self._private_turn(turn, user_node)
            return
        pending, self.pending_branch = self.pending_branch, None
        if pending is not None and pending[0] == (history[-1].id if history else None):
            user_node.meta.branch_name = pending[1]
        if self.story.chat and not history:
            # A chat's first message usually sets it up: kept in full unless
            # the author says otherwise.
            user_node.meta.keep_full = True
        self._attach(user_node, parent=history[-1] if history else None)

        turn = yield from self.direct_turn(turn, user_node, history)
        yield from self.archive_if_due(turn, history)
        lore = self._retrieve_for_turn(turn, history)
        prompt = self.assemble(turn, history_nodes=history, lore=lore)
        yield from self._generate(prompt, turn, parent=user_node)
        # Only reached when the stream ran to its end: a Stop closes this
        # generator at the yield above, and a stopped passage isn't read.
        yield PassageDone()
        pick = self._start_lore_pick()
        yield from self.read_after_turn()
        yield from self._finish_lore_pick(pick)
        # Off this thread entirely: adopted when archival is next due.
        self.prepare_archival()
        self.start_background_merge()

    def begin(self, held_character_id: str | None) -> Iterator[StreamEvent | SessionNotice]:
        """Start an unplayed story from its setup, as the character the author picked.

        A "generate" opening goes in as a Director turn — exactly what the
        author would otherwise type by hand — so the lock applies to it and
        Regenerate gives another opening. An "as_written" opening is the first
        passage itself and costs no call. With no opening, the author writes
        first.
        """
        self._refuse_in_chat("Starting from an opening")
        if self.nodes:
            raise ValueError("this story has already begun")
        story = self.story
        self.set_aside_unchosen(held_character_id)
        self.sync_unstarted_scene()
        story.held_character_id = held_character_id
        if held_character_id and held_character_id not in story.scene.present_character_ids:
            # Holding someone the roster puts elsewhere tells the model the
            # author's own character can't see the scene.
            story.scene.present_character_ids.append(held_character_id)

        opening = story.setup.opening_text.strip()
        if not opening:
            self.save()
            return
        if story.setup.opening_mode == "as_written":
            node = Node(
                kind="assistant",
                speaker_id=NARRATOR_SPEAKER_ID,
                content=opening,
                meta=NodeMeta(controlled_character_id=held_character_id),
            )
            self._attach(node, parent=None)
            self.save()
            # Read like any passage, or nobody the opening places is ever on
            # the roster. No call wrote it, so there is no result to keep.
            self.last_result = GenerationResult(
                node=node,
                text=opening,
                reasoning=None,
                usage=Usage(),
                finish_reason=None,
                stopped_early=False,
            )
            try:
                yield from self.read_after_turn()
            finally:
                self.last_result = None
            return
        yield from self.send(
            TurnRequest(
                speaker_id=DIRECTOR_SPEAKER_ID,
                user_text=opening,
                controlled_character_id=held_character_id,
                npc_scope=story.defaults.npc_scope,
                agency_mode=story.defaults.agency_mode,
            )
        )

    def sync_unstarted_scene(self) -> None:
        """While a story hasn't begun, its live scene is Setup's starting scene.

        `begin` starts from the live scene, and Setup wrote only the starting
        one, so a starting scene set in Setup before the first Start was
        never used: the story began with no location and nobody present.
        An empty starting scene (none was ever set) leaves the live one alone.
        """
        start = self.story.setup.starting_scene
        if self.nodes or start == SceneState():
            return
        self.story.scene = start.model_copy(deep=True)

    def pin_at_start(self, present: Sequence[str], absent: Sequence[str]) -> None:
        """Who the author has placed at the start: here, elsewhere; the rest
        are the opening's to place (`prompt.not_placed_at_start`). Kept on the
        setup, so a Restart places them the same way."""
        setup = self.story.setup
        pinned = list(dict.fromkeys(present))
        setup.starting_scene.present_character_ids = pinned
        setup.absent_at_start = [i for i in dict.fromkeys(absent) if i not in pinned]
        if not self.nodes:
            self.story.scene.present_character_ids = list(pinned)
        self.save()

    def set_aside_unchosen(self, held_character_id: str | None) -> list[Character]:
        """Play one of `setup.choose_one_of`; the others never enter the story.

        They leave the cast (and every roster) for `setup.set_aside`, which
        the scenario conversion puts back, so a restart offers the choice
        again. Directing, with nobody picked, keeps everyone. The GUI calls
        this before `begin` so the composer never lists them.
        """
        setup = self.story.setup
        if held_character_id not in setup.choose_one_of:
            return []
        unchosen = [
            character
            for character in self.cast
            if character.id in setup.choose_one_of and character.id != held_character_id
        ]
        if not unchosen:
            return []
        ids = {character.id for character in unchosen}
        self.bundle.cast[:] = [character for character in self.cast if character.id not in ids]
        setup.set_aside.extend(unchosen)
        setup.absent_at_start = [i for i in setup.absent_at_start if i not in ids]
        for scene in (self.story.scene, setup.starting_scene):
            scene.present_character_ids = [i for i in scene.present_character_ids if i not in ids]
            scene.offstage_but_nearby = [
                entry for entry in scene.offstage_but_nearby if entry.character_id not in ids
            ]
        self.save()
        return unchosen

    def switch_to(self, node_id: str) -> Node:
        """Make another take (or another branch) the active path.

        Lands at the end of that branch: the newest child at each step down.
        """
        leaf = latest_leaf(self.nodes, node_id)
        self.story.active_leaf_id = leaf.id
        self.last_result = None
        self.pending_branch = None
        self._sync_scene()
        self.save()
        return leaf

    def takes(self, node_id: str) -> list[Node]:
        """The takes of this passage, in order (just itself if it has none)."""
        return takes_of(self.nodes, node_id, {node.id for node in self.full_path()})

    def take_position(self, node_id: str) -> tuple[int, int]:
        """1-based position and count, for ‹ 2 / 3 ›."""
        return self.take_positions()(node_id)

    def take_positions(self) -> Callable[[str], tuple[int, int]]:
        """`take_position` for the transcript, which asks it of every passage:
        the path and the index are made once, not once a passage (building
        500 messages walked the whole tree 460 times)."""
        index = index_nodes(self.nodes)
        active = {node.id for node in self.full_path()}

        def position(node_id: str) -> tuple[int, int]:
            ids = [take.id for take in takes_of(self.nodes, node_id, active, index)]
            return ids.index(node_id) + 1, len(ids)

        return position

    def switch_take(self, node_id: str, step: int) -> list[Summary]:
        """Show the take `step` along from this one, in its place: the story
        after the passage stays, and follows the take now showing.

        The author's rule (Sept 2026): a take is another version of one
        passage, never a branch. Switching one used to switch lines, taking
        along whatever had been written after it. Returns the chapters that
        went stale (they summarised the other take).
        """
        group = self.takes(node_id)
        span = self.open_span
        if span is not None and group[0].meta.private_span != span.id:
            raise ValueError(
                "Switching takes before the private scene waits until it ends: it would "
                "change the story the scene's model is reading."
            )
        ids = [take.id for take in group]
        target = ids.index(node_id) + step
        if not 0 <= target < len(ids) or step == 0:
            return []
        stale = self._swap_take(group[target - step], group[target])
        self.last_result = None
        self.save()
        return stale

    def _swap_take(self, old: Node, new: Node) -> list[Summary]:
        """`new` stands where `old` stood: what followed `old` follows `new`."""
        if self.story.active_leaf_id == old.id:
            self.story.active_leaf_id = new.id
        move_continuation(self.nodes, old, new)
        stale = swap_covered(self.bundle.summaries, old.id, new.id)
        dropped = {summary.id for summary in self.drop_stale_parts()}
        self._sync_scene()
        self.story.updated_at = utc_now_iso()
        return [summary for summary in stale if summary.id not in dropped]

    def branch_from(self, node_id: str, name: str | None = None) -> None:
        """Continue the story from `node_id`; everything after it stays.

        Not on the message menu any more: a branch is made by rewriting a
        message (`rewrite_from`). The review's "Branch before message N" still
        comes here, with a name the next turn will carry as its branch's.
        """
        index_nodes(self.nodes)[node_id]  # KeyError for an unknown id
        self.story.active_leaf_id = node_id
        self.last_result = None
        self.pending_branch = (node_id, name) if name else None
        self._sync_scene()
        self.save()

    # --- branches: lines of the story, named (engine/branches.py) -------------

    def _off_branch_ids(self) -> set[str]:
        """A discarded private scene's messages, kept on disk as a side line
        no model is sent: never a branch."""
        gone = {span.id for span in self.story.private_spans if span.status == "discarded"}
        return {node.id for node in self.nodes if node.meta.private_span in gone}

    def branch_list(self) -> list[Branch]:
        return branches(
            self.nodes,
            [node.id for node in self.full_path()],
            main_name=self.story.main_branch_name,
            excluded=self._off_branch_ids(),
        )

    def current_branch(self) -> Branch | None:
        return next((branch for branch in self.branch_list() if branch.is_current), None)

    def branch_start_names(self) -> dict[str, str]:
        """Each branch's first message and its name, for "starts here"."""
        return branch_starts(
            self.nodes, self._off_branch_ids(), [node.id for node in self.full_path()]
        )

    def story_map(self) -> list[MapLane]:
        """The story map's lanes: each branch, its line and its chapters."""
        listed = self.branch_list()
        lanes: list[MapLane] = []
        for (column, parent), branch in zip(map_columns(listed), listed, strict=True):
            path = path_to(self.nodes, branch.end_id)
            position = {node.id: index for index, node in enumerate(path, start=1)}
            chapters = tuple(
                (position[summary.covered_node_ids[0]], position[summary.covered_node_ids[-1]])
                for summary in self.split(self.model_nodes(path)).summaries
                if summary.covered_node_ids[0] in position
                and summary.covered_node_ids[-1] in position
            )
            lanes.append(
                MapLane(
                    branch=branch,
                    column=column,
                    parent_column=parent,
                    node_ids=tuple(node.id for node in path),
                    chapters=chapters,
                )
            )
        return lanes

    def switch_branch(self, start_id: str | None) -> Node:
        """Go to the end of a branch (None: the first line)."""
        self._refuse_in_private("Switching branches")
        branch = next(b for b in self.branch_list() if b.start_id == start_id)
        self.story.active_leaf_id = branch.end_id
        self.last_result = None
        self.pending_branch = None
        self._sync_scene()
        self.save()
        return index_nodes(self.nodes)[branch.end_id]

    def rename_branch(self, start_id: str | None, name: str) -> str:
        """Rename a branch; returns the name given, numbered if it was taken."""
        name = " ".join(name.split())
        if not name:
            raise ValueError("a branch needs a name")
        name = unique_branch_name(name, self.branch_list(), keep=start_id)
        if start_id is None:
            self.story.main_branch_name = name
        else:
            index = index_nodes(self.nodes)
            start = index[start_id]
            old = start.meta.branch_name
            # Its first passage's takes carry its name too (`takes_of`): left
            # with the old one they would be read as a branch of their own.
            if start.parent_id in index:
                siblings = index[start.parent_id].children
            else:
                siblings = [node.id for node in self.nodes if node.parent_id is None]
            for sibling_id in siblings:
                sibling = index.get(sibling_id)
                if sibling is not None and old and sibling.meta.branch_name == old:
                    sibling.meta.branch_name = name
            start.meta.branch_name = name
        self.save()
        return name

    def delete_branch(self, start_id: str) -> Deletion:
        """Delete a branch: its own messages, and any branch made from them.

        The first line can't be deleted. Deleting the branch being read goes
        to the end of the one it split from first.
        """
        self._refuse_in_private("Deleting a branch")
        found = {b.start_id: b for b in self.branch_list()}
        branch = found.get(start_id)
        if branch is None or branch.is_main:
            raise ValueError("the first line of the story can't be deleted")
        if branch.is_current:
            self.switch_branch(branch.parent_start_id)
        return self.delete_from(start_id)

    def rewrite_from(
        self, node_id: str, text: str, *, name: str | None = None
    ) -> Iterator[StreamEvent | SessionNotice | PassageDone]:
        """Rewrite an earlier message as the start of a new branch.

        The author's rule (Sept 2026): editing an earlier message forks (Edit
        corrects in place). The message's rewrite stands beside it with a
        name; the old line is kept, and keeps its own. An author turn is then
        answered, as if sent; a passage is the author's prose, and waits.
        """
        self._refuse_in_private("Rewriting from an earlier message")
        index = index_nodes(self.nodes)
        original = index[node_id]
        if original.meta.private_span:
            raise ValueError("a private scene's messages can't start a branch")
        listed = self.branch_list()
        name = " ".join((name or "").split())
        name = unique_branch_name(name, listed) if name else next_branch_name(listed)
        parent = index.get(original.parent_id) if original.parent_id else None
        if original.kind == "user":
            # The rewritten turn goes out as a turn sent there would, with the
            # settings the original was sent with.
            self.story.active_leaf_id = parent.id if parent else None
            if parent is None:
                start = original.meta.scene or self.story.setup.starting_scene
                self.story.scene = start.model_copy(deep=True)
            else:
                self._sync_scene()
            self.pending_branch = (parent.id if parent else None, name)
            meta = original.meta
            yield from self.send(
                TurnRequest(
                    speaker_id=original.speaker_id,
                    user_text=text,
                    controlled_character_id=meta.controlled_character_id,
                    ooc=original.ooc,
                    npc_scope=meta.npc_scope or self.story.defaults.npc_scope,
                    npc_scope_ids=tuple(meta.npc_scope_ids),
                    agency_mode=meta.agency_mode or self.story.defaults.agency_mode,
                    response_style=meta.response_style,
                    length_hint=meta.length_hint,
                    dice_domain=meta.dice_domain,
                    difficulty=meta.difficulty or "standard",
                )
            )
            return
        node = Node(
            kind="assistant",
            speaker_id=original.speaker_id,
            content=text,
            meta=NodeMeta(
                controlled_character_id=original.meta.controlled_character_id,
                branch_name=name,
            ),
        )
        node.edited_at = utc_now_iso()
        if parent is None:
            node.meta.scene = (original.meta.scene or self.story.scene).model_copy(deep=True)
        self._attach(node, parent=parent)
        self.last_result = None
        self.pending_branch = None
        self._sync_scene()
        self.save()

    def regenerate(
        self,
        node_id: str | None = None,
        *,
        reroll: bool = False,
        shape: TurnShape | None = None,
    ) -> Iterator[StreamEvent | SessionNotice]:
        """Another take on the latest author turn, leaving any existing one intact.

        On a passage the story went on from, the new take stands in its place
        and the story after it stays (the author's rule, Sept 2026: a take is
        never a branch); no reads run, since they read where the story ends.

        Also serves as retry: when the leaf is an author turn with no response
        yet (the previous attempt failed, or was stopped before any text
        arrived), this produces its first take rather than a sibling.

        `shape` re-shapes the turn — the length, the outcome mode, who may be
        voiced — as the author has them set now. Without it a new take reuses
        what the turn was sent with, which is right for "give me another one"
        and wrong for "give me a longer one": asking for a longer passage and
        pressing Regenerate used to change nothing at all. The new shape is
        written back onto the author's turn, so it is what the next take
        reproduces.

        A dice roll is kept: another take rewrites the same outcome, so
        Regenerate can't be used to fish for a success. `reroll=True` is the
        deliberate exception, rolling again against the same odds.
        """
        self._tee_guard()
        # Any message on the tree can be retaken, not only the newest: the
        # result is a sibling of it, and the path follows the new take.
        path = path_to(self.nodes, node_id) if node_id else self.full_path()
        if not path:
            raise ValueError("regenerate needs a turn to work from")

        if path[-1].kind == "user":
            user_node, previous, history = path[-1], None, path[:-1]
        else:
            if len(path) < 2 or path[-2].kind != "user":
                raise ValueError("the assistant node's parent is not an author turn")
            user_node, previous, history = path[-2], path[-1], path[:-2]
        span_id = user_node.meta.private_span
        if span_id is not None and (self.open_span is None or self.open_span.id != span_id):
            raise ValueError("that private scene has ended; its messages can't be retaken")
        if span_id is None and self.in_private:
            raise ValueError("leave the private scene before retaking a passage from outside it")
        # Outside a private scene, the story's own messages only.
        history = self.model_nodes(history, include_span=span_id)

        turn = TurnRequest(
            speaker_id=user_node.speaker_id,
            user_text=user_node.content,
            controlled_character_id=user_node.meta.controlled_character_id,
            ooc=user_node.ooc,
            npc_scope=user_node.meta.npc_scope or self.story.defaults.npc_scope,
            npc_scope_ids=tuple(user_node.meta.npc_scope_ids),
            agency_mode=user_node.meta.agency_mode or self.story.defaults.agency_mode,
            roll=user_node.meta.roll,
            # Without these a regenerated take would silently revert to the
            # story default and come back at the wrong length.
            response_style=user_node.meta.response_style,
            length_hint=user_node.meta.length_hint,
            dice_domain=user_node.meta.dice_domain,
            difficulty=user_node.meta.difficulty or "standard",
        )
        if shape is not None:
            turn = self._reshape(turn, shape, user_node)
        if reroll:
            turn = self._rolled(replace(turn, roll=None))
            user_node.meta.roll = turn.roll
        elif turn.roll is not None:
            held = self._character(turn.controlled_character_id)
            turn = replace(
                turn, roll_explanation=explain(turn.roll, held.name if held else "The character")
            )
        # The scene as this turn found it, not as the take being replaced left
        # it: that take's read moved the roster on past the author's turn.
        self._sync_scene([*history, user_node])
        carrier = previous if previous is not None and previous.children else None
        if carrier is not None:
            yield from self._retake_in_place(turn, user_node, carrier, history, span_id)
            return
        # The same instruction as the take being replaced: the director is
        # asked once per author turn, as the dice are rolled once.
        if span_id is not None:
            yield from self._private_turn(turn, user_node, history=history)
            return
        turn = self._directed(turn, user_node.meta.direction, [*history, user_node])
        yield from self.archive_if_due(turn, history)
        lore = self._retrieve_for_turn(turn, history)
        prompt = self.assemble(turn, history_nodes=history, lore=lore)
        yield from self._generate(prompt, turn, parent=user_node)
        taken = index_nodes(self.nodes).get(self.story.active_leaf_id or "")
        if previous is not None and taken is not None and taken.parent_id == user_node.id:
            # A take of a branch's first passage is a take of that branch's.
            taken.meta.branch_name = previous.meta.branch_name
        yield PassageDone()
        pick = self._start_lore_pick()
        yield from self.read_after_turn()
        yield from self._finish_lore_pick(pick)
        # Off this thread entirely: adopted when archival is next due.
        self.prepare_archival()
        self.start_background_merge()

    def _retake_in_place(
        self,
        turn: TurnRequest,
        user_node: Node,
        carrier: Node,
        history: list[Node],
        span_id: str | None,
    ) -> Iterator[StreamEvent | SessionNotice | PassageDone]:
        """A new take of a passage the story went on from, swapped in.

        The swap happens even when the take is stopped partway (a stopped
        passage is kept, as any other); with no text at all, nothing changes.
        """
        leaf = self.story.active_leaf_id
        try:
            if span_id is not None:
                yield from self._private_turn(turn, user_node, history=history)
            else:
                turn = self._directed(turn, user_node.meta.direction, [*history, user_node])
                lore = self._retrieve_for_turn(turn, history)
                prompt = self.assemble(turn, history_nodes=history, lore=lore)
                yield from self._generate(prompt, turn, parent=user_node)
        finally:
            index = index_nodes(self.nodes)
            new = index.get(self.story.active_leaf_id or "")
            self.story.active_leaf_id = leaf
            if new is not None and new.id != leaf and new.parent_id == user_node.id:
                new.meta.branch_name = carrier.meta.branch_name
                self._swap_take(carrier, new)
            else:
                self._sync_scene()
            self.save()
        if span_id is None:
            yield PassageDone()  # a private turn yields its own

    def _generate(
        self,
        prompt: AssembledPrompt,
        turn: TurnRequest,
        *,
        parent: Node,
        provider: ChatProvider | None = None,
        model: str | None = None,
        span: PrivateSpan | None = None,
    ) -> Iterator[StreamEvent | SessionNotice]:
        provider = provider or self.provider
        model = model or self.model
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("story", model),
            messages=prompt.messages,
            params=self.story_params(model),
            use_cache_control=self._cache_control_for(model),
            cache_ttl=self.config.cache_ttl,
        )
        payload = provider.build_payload(request)
        log_ref = self._log_turn("request", {"node_id": parent.id, "payload": payload}, span)

        text: list[str] = []
        reasoning: list[str] = []
        completed: StreamCompleted | None = None
        try:
            for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    text.append(event.text)
                elif isinstance(event, ReasoningDelta):
                    reasoning.append(event.text)
                elif isinstance(event, StreamCompleted):
                    completed = event
                yield event
        finally:
            self._finalize(
                prompt=prompt,
                turn=turn,
                parent=parent,
                text="".join(text),
                reasoning="".join(reasoning) or None,
                completed=completed,
                log_ref=log_ref,
                model=model,
                span=span,
            )
        # A TEE reply's signature is fetched under this (session_private).
        self._last_response_id = completed.response_id if completed is not None else None
        # An end-to-end encrypted reply: opened with the attested enclave's key.
        self._last_sealed = completed is not None and completed.sealed
        if self.story.chat and span is None and self.last_result is not None:
            # A TEE chat's reply: signed by the enclave that was attested?
            self._check_tee(self.last_result.node)
        if completed is not None and completed.finish_reason == "length":
            yield SessionNotice(cut_off_notice(request.params.max_tokens, completed))
        # A model caught reasoning though asked for nothing: said at once, and
        # kept under the passage, so the slow start reads as the model's.
        written = self.last_result.node if self.last_result is not None else None
        for note in self.reasoning_notices(written):
            yield SessionNotice(note)

    def _finalize(
        self,
        *,
        prompt: AssembledPrompt,
        turn: TurnRequest,
        parent: Node,
        text: str,
        reasoning: str | None,
        completed: StreamCompleted | None,
        log_ref: str,
        model: str | None = None,
        span: PrivateSpan | None = None,
    ) -> None:
        model = model or prompt.model or self.model
        usage = self.priced(completed.usage if completed else Usage(), model)

        # A request that failed or was stopped before any text arrived has
        # nothing to record. Attaching an empty node would leave a dead turn in
        # the tree and in the transcript; leaving the author's turn as the leaf
        # instead means regenerate() can simply retry it.
        node: Node | None = None
        if text or reasoning:
            node = Node(
                kind="assistant",
                speaker_id="__narrator__",
                content=text,
                meta=NodeMeta(
                    model=model,
                    usage=usage,
                    reasoning=reasoning,
                    private_span=span.id if span is not None else None,
                    agency_mode=turn.agency_mode,
                    roll=turn.roll,
                    npc_scope=turn.npc_scope,
                    npc_scope_ids=list(turn.npc_scope_ids),
                    controlled_character_id=turn.controlled_character_id,
                    request_log_ref=log_ref,
                ),
            )
            self._attach(node, parent=parent)

        self._log_turn(
            "response",
            {
                "request_log_ref": log_ref,
                "node_id": node.id if node else None,
                "text": text,
                "reasoning": reasoning,
                "finish_reason": completed.finish_reason if completed else None,
                "usage": completed.raw_usage if completed else {},
                "stopped_early": completed is None,
            },
            span,
        )

        self._update_correction_factor(prompt, usage)
        self.last_result = GenerationResult(
            node=node,
            text=text,
            reasoning=reasoning,
            usage=usage,
            finish_reason=completed.finish_reason if completed else None,
            stopped_early=completed is None,
        )
        self.save()

    # --- archival (§5.2, §5.3) ---------------------------------------------

    def next_chunk(
        self, nodes: Sequence[Node] | None = None, *, allow_partial: bool = False
    ) -> list[Node]:
        """The run of nodes archival would take next, or empty if none can be.

        Automatic archival waits for a whole chunk; `allow_partial` is for the
        author asking for one deliberately (see `plan_chunk`).
        """
        return plan_chunk(
            self.split(nodes).verbatim,
            turns=self.config.archive_chunk_turns,
            allow_partial=allow_partial,
        )

    def archive_target_tokens(self) -> int:
        """The prompt size archival works down to once it has started.

        Archival is triggered at the budget but must not stop there. Stopping
        the moment the prompt fits leaves only the last chunk's savings as
        headroom — a few hundred tokens, three or four turns — and every one of
        those crossings costs a summariser call *and* a full cache write,
        because a new chapter rewrites the summary block. Working down to a
        fraction of the budget spends the same number of summaries over the
        story's life and buys tens of quiet turns with each one.
        """
        budget = self.story.defaults.context_token_budget
        return int(budget * self.config.archive_target_ratio)

    def archive_if_due(self, turn: TurnRequest, history: Sequence[Node]) -> Iterator[SessionNotice]:
        """Archive whole chunks down to the target, or until it's clear it can't.

        A summariser failure must never cost the author their turn: the prompt
        still assembles, the oldest nodes simply fall out of the window as they
        did before Phase 4, and the notice says so.
        """
        if not self.config.auto_archive:
            return
        prompt = self.assemble(turn, history_nodes=history)
        if not (prompt.budget.over_budget or prompt.needs_archival):
            return

        # Chapters written in the background go in first; the summary block is
        # changing anyway, so a part merged in the background goes in with them.
        yield from self.adopt_archival(history)
        part = self.adopt_merge()
        if part is not None:
            yield SessionNotice(f"Merged {part.chapters} old chapters into one part.")
        prompt = self.assemble(turn, history_nodes=history)
        if not (prompt.budget.over_budget or prompt.needs_archival):
            return
        if self.can_archive_in_background():
            # Never wait for a summariser: this turn's oldest prose falls out of
            # the window, and the chapters go in when they are written.
            if self._archive_job is not None or self.prepare_archival(force=True):
                yield SessionNotice(
                    "Archiving the oldest turns in the background; until it's done they "
                    "are left out of the prompt."
                )
            else:
                yield SessionNotice(self._cannot_archive_notice(history))
            return
        yield from self._archive_loop(turn, history, prompt)

    def archive_down(self) -> Iterator[SessionNotice]:
        """Archive now, down to the target: the author's button when the prompt
        is over budget and automatic archival is off. Asked for by hand, so a
        short last chapter is the author's call (§5.2)."""
        history = self.path()
        turn = self._next_turn()
        yield from self.adopt_archival(history)
        prompt = self.assemble(turn, history_nodes=history)
        yield from self._archive_loop(turn, history, prompt, allow_partial=True)

    def context_overflow(self) -> AssembledPrompt | None:
        """The next turn's prompt if it is over the budget, else None.

        For the window's banner, on the GUI thread: pure assembly, never a
        call, and `last_prompt` is left as the last turn sent it.
        """
        prompt = self._measure(self.path())
        return prompt if prompt.budget.over_budget or prompt.needs_archival else None

    def _archive_loop(
        self,
        turn: TurnRequest,
        history: Sequence[Node],
        prompt: AssembledPrompt,
        *,
        allow_partial: bool = False,
    ) -> Iterator[SessionNotice]:
        """Whole chunks, one call each, until the prompt is down to the target."""
        budget = self.story.defaults.context_token_budget
        target = self.archive_target_tokens()
        archived: list[Node] = []
        rounds = 0
        # `full_total`, not the reported total: trimming caps the total at the
        # budget, so archiving what is already being dropped would look like it
        # changed nothing and the loop would stop with the window still full.
        while prompt.full_total > target:
            if rounds >= MAX_AUTO_ARCHIVE_ROUNDS:
                yield SessionNotice(
                    "Stopping after several chapters — the story is still over the budget. "
                    "The oldest turns will be left out of this prompt; archive again or "
                    "raise the budget."
                )
                break
            chunk = self.next_chunk(history, allow_partial=allow_partial)
            if not chunk:
                if not archived:
                    yield SessionNotice(self._cannot_archive_notice(history))
                break
            try:
                summary = self.archive(chunk)
            except ProviderError as exc:
                yield SessionNotice(
                    f"Could not summarise the oldest turns ({exc}). Sending a shortened "
                    "window instead; nothing has been lost from the story."
                )
                break
            if self.ledger_problem:
                yield SessionNotice(
                    f"The story ledger wasn't updated ({self.ledger_problem}); it stands as it "
                    "was, and this chapter's summary holds what it would have added."
                )
            archived.extend(chunk)
            rounds += 1
            prompt = self.assemble(turn, history_nodes=history)
            numbers = chapter_numbers(self.split(history).summaries)
            yield SessionNotice(
                f"Archived {turns_in(chunk)} turns into chapter "
                f"{numbers.get(summary.id) or chapter_number(self.bundle.summaries, summary)} — "
                f"prompt now ~{prompt.full_total:,} of {budget:,} tokens."
            )
        # One scan over everything archived this turn, not one per chapter:
        # each is a paid call, and a deep catch-up takes several chunks.
        if archived:
            yield from self.suggest_characters_after_archive(archived)

    def _cannot_archive_notice(self, history: Sequence[Node]) -> str:
        """Why the budget can't be met, and which lever to reach for.

        The bad case is worth naming separately: once the budget is small
        enough that no verbatim prose fits at all, the model is writing from
        the summaries alone and the scene will read like it.
        """
        verbatim = self.split(history).verbatim
        prompt = self.last_prompt
        starved = (
            prompt is not None and bool(verbatim) and len(prompt.excluded_node_ids) >= len(verbatim)
        )
        if starved:
            return (
                "None of the recent prose fits in the context budget, so this turn is being "
                "written from the chapter summaries alone. Raise the budget — the scene will "
                "read as though it has forgotten itself otherwise."
            )
        return (
            f"Over the context budget, but there aren't yet {self.config.archive_chunk_turns} "
            "older turns to make a chapter from, so the oldest messages will be left out of "
            "this prompt instead. Raise the budget, lower turns-per-chapter, or archive a "
            "short chapter by hand."
        )

    def archive(self, chunk: Sequence[Node]) -> Summary:
        """Summarise `chunk` and record it as the next chapter.

        The summariser runs with default generation params rather than the
        story's: prose settings (and reasoning in particular) are tuned for
        writing, and would be paid for here to no purpose.
        """
        self._refuse_in_private("Archiving")
        if not chunk:
            raise ValueError("nothing to archive")

        previous = self.split(None).summaries
        # Out alongside the summary call, so the ledger costs no extra wait.
        ledger = self._start_ledger(
            chunk,
            ledger_on(previous),
            earlier="" if ledger_on(previous) else "\n\n".join(s.content for s in previous),
        )
        messages = build_summary_messages(
            chunk,
            self.visible_cast(),
            previous=previous[-1] if previous else None,
            target_words=self.config.summary_target_words,
            chat=self.story.chat,
            texts=self.texts,
        )
        request = ChatRequest(
            model=self.summarization_model,
            extra_body=self.route_for("summarisation"),
            messages=messages,
            params=self.side_params(self.summarization_model),
        )
        log_ref = self._log(
            "summary_request",
            {
                "node_ids": [node.id for node in chunk],
                "payload": self.provider.build_payload(request),
            },
        )

        text, completed = self.provider.complete(request)
        text = text.strip()
        self._log(
            "summary_response",
            {
                "request_log_ref": log_ref,
                "text": text,
                "usage": completed.raw_usage,
            },
        )
        if not text:
            raise ProviderError("the summariser returned no text")
        if completed.finish_reason == "length":
            raise ProviderError(SUMMARY_CUT_OFF)

        summary = Summary(
            covered_node_ids=[node.id for node in chunk],
            content=text,
            model=self.summarization_model,
            usage=self.priced(completed.usage, self.summarization_model),
            ledger=self._finish_ledger(ledger),
        )
        self.bundle.summaries.append(summary)
        self.save()
        return summary

    def _start_ledger(
        self, chunk: Sequence[Node], previous: str | None, *, earlier: str = ""
    ) -> _LedgerJob | None:
        """The ledger update for `chunk`, sent on its own thread when it can be."""
        if not self.config.story_ledger or self.story.chat:
            return None
        prose = render_chunk(chunk, self.visible_cast(), texts=self.texts)
        request = ChatRequest(
            model=self.summarization_model,
            extra_body=self.route_for("summarisation"),
            messages=build_ledger_messages(
                previous,
                prose,
                names=[c.name for c in characters_in(prose, self.visible_cast())],
                earlier=earlier,
                texts=self.texts,
            ),
            params=self.side_params(self.summarization_model, max_tokens=LEDGER_MAX_TOKENS),
        )
        job = _LedgerJob(request=request, previous=previous)
        make_sibling = getattr(self.provider, "sibling", None)
        if callable(make_sibling):
            side = make_sibling()

            def call() -> None:
                try:
                    job.reply = side.complete(request)
                except Exception as exc:  # noqa: BLE001 - reported by _finish_ledger
                    job.error = str(exc) or type(exc).__name__

            job.thread = threading.Thread(target=call, name="ledger", daemon=True)
            job.thread.start()
        return job

    def _finish_ledger(self, job: _LedgerJob | None) -> str | None:
        """The new ledger, or the previous one if the update can't be used.

        Never fails the archival: the chapter stands either way, and the
        ledger's facts from this chunk are in its summary meanwhile.
        """
        self.ledger_problem = None
        if job is None:
            return None
        if job.thread is not None:
            job.thread.join()
        else:
            try:
                job.reply = self.provider.complete(job.request)
            except ProviderError as exc:
                job.error = str(exc)
        log_ref = self._log("ledger_request", {"payload": self.provider.build_payload(job.request)})
        if job.reply is None:
            self._log("ledger_response", {"request_log_ref": log_ref, "error": job.error})
            self.ledger_problem = job.error or "no reply"
            return job.previous
        text, completed = job.reply
        self._log(
            "ledger_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, job.request.model))
        problem = check_ledger(text, job.previous, finish_reason=completed.finish_reason)
        if problem is not None:
            self._log("ledger_refused", {"request_log_ref": log_ref, "reason": problem})
            self.ledger_problem = problem
            return job.previous
        return normalised(text)

    def rebuild_summary(self, summary_id: str, *, force: bool = False) -> Summary:
        """Re-summarise the same run of nodes, keeping the summary's identity.

        The id and covered range are preserved so the path chain still matches;
        only the text changes. A hand-edited summary needs `force`, which is
        the confirmation §5.2 asks for.
        """
        self._refuse_in_private("Rebuilding a summary")
        summary = self.summary_by_id(summary_id)
        if summary.hand_edited and not force:
            raise ValueError("this summary was edited by hand; rebuilding would overwrite it")
        if summary.merged_from:
            return self.rebuild_part(summary)

        index = {node.id: node for node in self.nodes}
        missing = [node_id for node_id in summary.covered_node_ids if node_id not in index]
        if missing or not summary.covered_node_ids:
            raise ValueError("the nodes this summary covers are no longer in the story")
        chunk = [index[node_id] for node_id in summary.covered_node_ids]

        position = self.bundle.summaries.index(summary)
        previous = self.bundle.summaries[position - 1] if position > 0 else None
        messages = build_summary_messages(
            chunk,
            self.visible_cast(),
            previous=previous,
            target_words=self.config.summary_target_words,
            chat=self.story.chat,
            texts=self.texts,
        )
        request = ChatRequest(
            model=self.summarization_model,
            extra_body=self.route_for("summarisation"),
            messages=messages,
            params=self.side_params(self.summarization_model),
        )
        log_ref = self._log(
            "summary_request",
            {
                "summary_id": summary.id,
                "node_ids": list(summary.covered_node_ids),
                "payload": self.provider.build_payload(request),
            },
        )
        text, completed = self.provider.complete(request)
        text = text.strip()
        self._log(
            "summary_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        if not text:
            raise ProviderError("the summariser returned no text")
        if completed.finish_reason == "length":
            raise ProviderError(SUMMARY_CUT_OFF)

        summary.content = text
        summary.model = self.summarization_model
        summary.usage = self.priced(completed.usage, self.summarization_model)
        summary.hand_edited = False
        summary.stale = False
        summary.edited_at = utc_now_iso()
        self.save()
        return summary

    def edit_summary(self, summary_id: str, content: str) -> Summary:
        """The author's own words win: hand-edited, and no longer stale (§5.2)."""
        summary = self.summary_by_id(summary_id)
        summary.content = content.strip()
        summary.hand_edited = True
        summary.stale = False
        summary.edited_at = utc_now_iso()
        self.save()
        return summary

    def summary_by_id(self, summary_id: str) -> Summary:
        summary = next((s for s in self.bundle.summaries if s.id == summary_id), None)
        if summary is None:
            raise KeyError(f"unknown summary id: {summary_id!r}")
        return summary

    def edit_node(self, node_id: str, content: str, *, ooc: str | None = None) -> list[Summary]:
        """Edit any message, archived or not, and report what went stale (§5.3).

        The edit is never blocked and summaries are never rebuilt here — the
        caller shows the banner and offers the rebuild.
        """
        node = next((candidate for candidate in self.nodes if candidate.id == node_id), None)
        if node is None:
            raise KeyError(f"unknown node id: {node_id!r}")

        node.content = content
        if ooc is not None:
            node.ooc = ooc.strip() or None
        node.edited_at = utc_now_iso()
        if node.kind == "assistant":
            # What was found in the old text may no longer be in it.
            self.prune_shortcuts(node)

        went_stale = mark_stale(self.bundle.summaries, node_id)
        # A part is merged again, not rebuilt: its chapters stand in meanwhile.
        dropped = {summary.id for summary in self.drop_stale_parts()}
        self.story.updated_at = utc_now_iso()
        self.save()
        return [summary for summary in went_stale if summary.id not in dropped]

    def deletion_for(self, node_id: str) -> Deletion:
        """Everything `delete_from(node_id)` would remove, without removing it."""
        ids = subtree_ids(self.nodes, node_id)
        doomed = set(ids)
        on_path = sum(1 for node in self.path() if node.id in doomed)
        # A summary covers one exact run of nodes; lose any of them and it can
        # never match a path again, so it goes too.
        summaries = tuple(s for s in self.summaries if doomed.intersection(s.covered_node_ids))
        asides = tuple(a for a in self.bundle.asides if a.anchor_node_id in doomed)
        return Deletion(node_ids=tuple(ids), on_path=on_path, summaries=summaries, asides=asides)

    def delete_from(self, node_id: str) -> Deletion:
        """Delete a message and everything after it, on every branch below it.

        The story steps back to the message's parent. Deleting a reply leaves
        its author turn as the leaf, so Regenerate can take another run at it.
        The API log keeps every request regardless; `nodes.json.bak` holds the
        tree as it was one save ago.
        """
        deletion = self.deletion_for(node_id)
        doomed = set(deletion.node_ids)
        node = index_nodes(self.nodes)[node_id]
        if node.parent_id is not None:
            parent = index_nodes(self.nodes)[node.parent_id]
            parent.children = [child for child in parent.children if child != node_id]

        self.bundle.nodes[:] = [n for n in self.nodes if n.id not in doomed]
        gone_summaries = {summary.id for summary in deletion.summaries}
        self.bundle.summaries[:] = [s for s in self.summaries if s.id not in gone_summaries]
        gone_asides = {aside.id for aside in deletion.asides}
        self.bundle.asides[:] = [a for a in self.bundle.asides if a.id not in gone_asides]

        self.story.scene_log = [
            entry for entry in self.story.scene_log if entry.closed_at_node_id not in doomed
        ]

        if self.story.active_leaf_id in doomed:
            self.story.active_leaf_id = node.parent_id
        self.story.updated_at = utc_now_iso()
        self.last_result = None
        # Scene state lives on the nodes, so stepping back steps the roster back.
        self._sync_scene()
        self.save()
        return deletion

    # --- cost (§8.3) --------------------------------------------------------

    def price_for(self, model: str) -> ModelPrice | None:
        """The model's listed prices, fetched once and cached in config for a week."""
        cached = self.config.model_prices.get(model) or self._session_prices.get(model)
        if cached is not None and not _stale(cached):
            return cached
        if model in self._price_misses or self.in_private:
            # In a private scene the main endpoint isn't called, not even for a price.
            return cached
        try:
            listing = self.provider.fetch_model_prices()
        except ProviderError:
            self._price_misses.add(model)
            return cached
        entry = listing.get(model)
        price = parse_price(entry) if entry else None
        if price is None:
            self._price_misses.add(model)
            return cached
        price.fetched_at = utc_now_iso()
        if self.memory_only:
            # Kept for this session only: an entry under the chat's model
            # would otherwise reach config.json with the window's next save.
            self._session_prices[model] = price
            return price
        self.config.model_prices[model] = price
        self._save_config()
        return price

    def priced(self, usage: Usage, model: str) -> Usage:
        """The usage with a cost: as reported, or estimated from listed prices."""
        if (
            usage.cost
            or usage.cost_reported
            or not (usage.prompt_tokens or usage.completion_tokens)
            or not model
        ):
            return usage
        price = self.price_for(model)
        if price is None:
            return usage
        return usage.model_copy(
            update={
                "cost": estimate_cost(usage, price, model, cache_ttl=self.config.cache_ttl),
                "cost_estimated": True,
            }
        )

    # --- supporting characters ---------------------------------------------

    @property
    def supporting(self) -> list[Character]:
        return self.bundle.supporting.characters

    @property
    def character_suggestions(self) -> list[CharacterSuggestion]:
        return self.bundle.supporting.suggestions

    def supporting_for(self, text: str, history: Sequence[Node]) -> list[Character]:
        """Supporting cards for this turn's tail — no network, ever.

        Those named in this turn or the recent ones, and everyone the scene
        says is here: someone standing in the room belongs in the prompt
        whether or not the last ten passages happened to say their name.
        """
        if not self.visible_supporting():
            return []
        query = query_text(text, history, turns=self.config.supporting_recall_turns)
        cards = mentioned(self.visible_supporting(), query, limit=self.config.supporting_limit)
        here = [
            card
            for card in self.visible_supporting()
            if card not in cards
            and any(same_person(name, card) for name in self.story.scene.present_others)
        ]
        return cards + here

    def scan_for_characters(self, nodes: Sequence[Node]) -> list[CharacterSuggestion]:
        """Ask the model who in `nodes` is new; keep them as suggestions.

        Raises ProviderError or ValueError on failure. Callers on the turn path
        use `suggest_characters_after_archive`, which never does.
        """
        self._refuse_in_private("Scanning for characters")
        prose = render_chunk(nodes, self.cast, texts=self.texts)
        if not prose.strip():
            return []
        supporting = self.bundle.supporting
        on_file = [c.name for c in (*self.cast, *supporting.characters)]
        on_file += [suggestion.name for suggestion in supporting.suggestions]
        request = ChatRequest(
            model=self.summarization_model,
            extra_body=self.route_for("summarisation"),
            messages=build_extraction_messages(
                prose, on_file=on_file, declined=supporting.dismissed, texts=self.texts
            ),
            params=self.side_params(self.summarization_model),
        )
        log_ref = self._log(
            "characters_request",
            {
                "node_ids": [node.id for node in nodes],
                "payload": self.provider.build_payload(request),
            },
        )
        text, completed = self.provider.complete(request)
        self._log(
            "characters_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, self.summarization_model))

        known = known_names(
            (*self.cast, *supporting.characters),
            [*supporting.dismissed, *(s.name for s in supporting.suggestions)],
        )
        found = parse_suggestions(text, known=known)
        supporting.suggestions.extend(found)
        self.save()
        return found

    def scene_age(self) -> int:
        """Passages written since the scene was last set, by the author or a read.

        The count the Scene panel warns on. A scene never set is as old as the
        story: those are exactly the long games that drifted furthest.
        """
        path = self.path()
        index = snapshot_index(path)
        return len(path) if index is None else len(path) - index - 1

    def suggest_scene(self, nodes: int = 8) -> SceneProposal:
        """Read the current scene back out of the recent prose.

        One call on the scene model, at the author's request. Raises
        ProviderError or ValueError on failure — nothing is applied here, the
        author accepts or edits the proposal.
        """
        self._refuse_in_private("Reading the scene")
        recent = list(self.split().verbatim)[-nodes:]
        prose = render_chunk(recent, self.visible_cast(), texts=self.texts)
        if not prose.strip():
            raise ValueError("there is no story yet to read the scene from")
        request = ChatRequest(
            model=self.scene_model,
            extra_body=self.route_for("scene"),
            messages=build_scene_messages(
                prose,
                cast=self.visible_cast(),
                supporting=self.visible_supporting(),
                texts=self.texts,
            ),
            params=self.side_params(self.scene_model, max_tokens=READ_MAX_TOKENS),
        )
        log_ref = self._log(
            "scene_request",
            {
                "node_ids": [node.id for node in recent],
                "payload": self.provider.build_payload(request),
            },
        )
        text, completed = self.provider.complete(request)
        self._log(
            "scene_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, self.scene_model))
        return parse_scene(text, cast=self.visible_cast(), supporting=self.visible_supporting())

    def set_scene(self, scene: SceneState, *, source: SceneSource = "author") -> None:
        """Record a scene, as a snapshot on the current leaf.

        The leaf may be a passage or an author turn still waiting for one;
        either way the scene from here on is this one, until the next read.
        An author's scene replaces a read's on the same passage, which is
        what "author edits win" means: the next read starts from it.
        """
        # In a private scene the card is frozen: an edit would be stamped on
        # the last public passage, saved even in memory mode, and shape every
        # later prompt to the story's model.
        self._refuse_in_private("Editing the scene")
        scene.source = source
        if source == "author":
            scene.changes = []
        path = self.path()
        scene.set_at_node_id = path[-1].id if path else None
        self.story.scene = scene
        if path:
            path[-1].meta.scene = scene.model_copy(deep=True)
        elif not self.nodes:
            # Not begun: this is where it will begin (`sync_unstarted_scene`),
            # and whoever is ticked here is pinned present.
            setup = self.story.setup
            setup.starting_scene = scene.model_copy(deep=True)
            setup.absent_at_start = [
                i for i in setup.absent_at_start if i not in scene.present_character_ids
            ]
        self.save()

    def _sync_scene(self, path: Sequence[Node] | None = None) -> None:
        """Point the live scene at the one standing at the end of `path`.

        `story.scene` is a copy for the panels and the prompt; the snapshots
        on the nodes are the record. Every move of the leaf calls this.
        """
        nodes = self.path() if path is None else path
        self.story.scene = scene_at(
            nodes, self.story.scene, cast_ids={character.id for character in self.cast}
        )

    def hold(self, character_id: str | None) -> bool:
        """Take up a character. Returns whether the scene moved to them.

        Holding someone who is elsewhere cuts the scene to them rather than
        pulling them into it: the author's own character is never teleported.
        That needs a roster that knows who is elsewhere, which one the reads
        are keeping does. A hand-kept roster may just be behind — John can
        have walked in fifty passages ago with nobody ticking him in — so
        there, as before, taking them up puts them in the scene.
        """
        self._refuse_in_private("Changing who you hold")
        story = self.story
        story.held_character_id = character_id or None
        character = self._character(character_id)
        if character is None or character.id in story.scene.present_character_ids:
            self.save()
            return False
        path = self.path()
        if not path or not story.scene.tracked:
            scene = story.scene.model_copy(deep=True)
            scene.present_character_ids.append(character.id)
            self.set_scene(scene)
            return False
        cast_by_id = {c.id: c for c in self.cast}
        self._close_scene(
            story.scene, gist=story.scene.situation, node_id=path[-1].id, cast_by_id=cast_by_id
        )
        self.set_scene(cut_to(story.scene, character, cast=self.cast))
        return True

    def _close_scene(
        self,
        scene: SceneState,
        *,
        gist: str | None,
        node_id: str,
        cast_by_id: dict[str, Character],
    ) -> None:
        if not (scene.present_character_ids or scene.present_others or scene.location):
            return
        self.story.scene_log.append(closed_entry(scene, cast_by_id, gist=gist, node_id=node_id))
        del self.story.scene_log[:-SCENE_LOG_KEPT]

    # --- the scene read, after each passage -----------------------------------

    def update_scene_after_turn(self) -> Iterator[SessionNotice]:
        """Read what the latest passage did to the scene, and follow it.

        Never costs the author their turn: a failed read leaves the roster as
        it was, runs the older pattern check in its place, and says so.
        """
        if self.config.scene_reads != "every_turn":
            return
        result = self.last_result
        if result is None or result.node is None or result.stopped_early:
            return
        node = result.node
        if not node.content.strip():
            return
        path = self._path_to(node.id)
        before = scene_at(
            path[:-1], self.story.scene, cast_ids={c.id for c in self.visible_cast(pending=True)}
        )
        author = path[-2] if len(path) > 1 and path[-2].kind == "user" else None
        opening = not any(earlier_node.kind == "assistant" for earlier_node in path[:-1])
        if opening and before.tracked:
            # Nothing was under way before the first passage: whoever it finds
            # there was there from the start, which is what a seeding read asks.
            before = before.model_copy(update={"tracked": False})
        earlier = ""
        if not before.tracked:
            # The first read of a card that has only ever kept the cast: it
            # needs to see who is already in the room, not just who came in.
            prior = path[:-2] if author is not None else path[:-1]
            earlier = render_chunk(
                list(self.split(prior).verbatim)[-SEED_CONTEXT_NODES:],
                self.visible_cast(pending=True),
                texts=self.texts,
            )

        yield SessionNotice(f"Reading the scene with {self.scene_model}…")
        try:
            delta = self.read_scene(before, node=node, author=author, earlier=earlier)
        except (ProviderError, ValueError) as exc:
            yield SessionNotice(
                f"Couldn't read the scene from this passage ({exc}). The roster is as it "
                "was; set it in the Scene panel if the story has moved on."
            )
            return

        if opening:
            self._keep_pinned(delta)
        self._apply_read(node, before, delta)
        words = describe_changes(node.meta.scene.changes if node.meta.scene else [])
        # No banner: a shortcut the read stands behind is named here, once, and
        # kept on the passage for the settings review.
        shortcuts = "; ".join(
            f"{shortcut.name} {shortcut.kind.replace('_', ' ')}: “{shortcut.quote}”"
            for shortcut in node.meta.scene_shortcuts
        )
        words = "; ".join(
            part for part in (words, f"shortcut — {shortcuts}" if shortcuts else "") if part
        )
        yield SessionNotice(f"Scene: {words}" if words else "Scene: no change")

    def _keep_pinned(self, delta: SceneDelta) -> None:
        """The opening's read keeps whoever the author pinned present.

        A seeding read replaces the roster with who it found, and an opening
        that happens not to mention someone the author put there would lose
        them. Only a departure the read reports takes them out.
        """
        if delta.already_here is None:
            return
        cast_by_id = {character.id: character for character in self.visible_cast(pending=True)}
        found = {person.character_id for person in delta.already_here}
        gone = {person.character_id for person in delta.left}
        for character_id in self.story.setup.starting_scene.present_character_ids:
            character = cast_by_id.get(character_id)
            if character is not None and character_id not in found | gone:
                delta.already_here.append(Resolved(name=character.name, character_id=character_id))

    def read_scene(
        self, before: SceneState, *, node: Node, author: Node | None, earlier: str = ""
    ) -> SceneDelta:
        """One call on the scene model. Raises ProviderError or ValueError."""
        request = ChatRequest(
            model=self.scene_model,
            extra_body=self.route_for("scene"),
            messages=build_read_messages(
                scene=before,
                cast=self.visible_cast(pending=True),
                supporting=self.visible_supporting(),
                held=self._character(node.meta.controlled_character_id),
                author_turn=render_chunk(
                    [author], self.visible_cast(pending=True), texts=self.texts
                )
                if author is not None
                else "",
                passage=node.content,
                earlier=earlier,
                perspective=self.story.style.perspective,
                texts=self.texts,
            ),
            params=self.side_params(self.scene_model, max_tokens=READ_MAX_TOKENS),
        )
        log_ref = self._log(
            "scene_request", {"node_id": node.id, "payload": self.provider.build_payload(request)}
        )
        text, completed = self.provider.complete(request)
        self._log(
            "scene_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, self.scene_model))
        return parse_delta(
            text, cast=self.visible_cast(pending=True), supporting=self.visible_supporting()
        )

    def _apply_read(self, node: Node, before: SceneState, delta: SceneDelta) -> None:
        cast_by_id = {character.id: character for character in self.visible_cast(pending=True)}
        author = index_nodes(self.nodes).get(node.parent_id or "")
        delta.unnamed_arrivals(
            (node.content, author.content if author is not None else ""),
            people=[*self.visible_cast(pending=True), *self.visible_supporting()],
        )
        after = delta.applied_to(
            before,
            cast=self.visible_cast(pending=True),
            held_id=node.meta.controlled_character_id,
            node_id=node.id,
        )
        node.meta.scene = after
        node.meta.scene_read = True
        # A read that can't point at the sentence it accuses doesn't get to.
        node.meta.scene_shortcuts = [
            shortcut
            for shortcut in delta.credible_shortcuts(
                before,
                cast=self.visible_cast(pending=True),
                passage=node.content,
                previous=self._previous_passage(node),
                introduced=self.being_introduced(),
                strict=strict_perspective(
                    self.story.style.perspective, node.meta.controlled_character_id
                ),
            )
            if locate_quote(node.content, shortcut.quote) is not None
        ]
        if delta.closed:
            self._close_scene(before, gist=delta.gist, node_id=node.id, cast_by_id=cast_by_id)
        self.story.scene = after.model_copy(deep=True)
        self.save()

    def _previous_passage(self, node: Node) -> str:
        """The storyteller's passage before `node` on its path, or ""."""
        path = self._path_to(node.id)
        earlier = [n for n in path[:-1] if n.kind == "assistant"]
        return earlier[-1].content if earlier else ""

    def scene_changed_here(self) -> bool:
        """Whether the leaf carries a scene the read set, which Undo can take back."""
        path = self.path()
        return (
            bool(path) and path[-1].meta.scene is not None and path[-1].meta.scene.source == "story"
        )

    def undo_scene_update(self) -> bool:
        """Take back what the read did at the leaf; the scene before it stands again."""
        self._refuse_in_private("Undoing a scene change")
        if not self.scene_changed_here():
            return False
        leaf = self.path()[-1]
        leaf.meta.scene = None
        self.story.scene_log = [
            entry for entry in self.story.scene_log if entry.closed_at_node_id != leaf.id
        ]
        self._sync_scene()
        self.save()
        return True

    def scene_log_on_path(self) -> list[SceneLogEntry]:
        return log_on_path(self.story.scene_log, self.path())

    # --- the plot's clock and facts ------------------------------------------

    def suggest_characters_after_archive(self, chunk: Sequence[Node]) -> Iterator[SessionNotice]:
        """Look for new characters in prose that was just archived; never fails the turn."""
        self._refuse_in_private("Scanning for characters")
        if not self.config.suggest_characters or self.story.chat:
            return
        try:
            found = self.scan_for_characters(chunk)
        except (ProviderError, ValueError) as exc:
            yield SessionNotice(f"Couldn't check the archived turns for new characters ({exc}).")
            return
        if found:
            names = ", ".join(suggestion.name for suggestion in found)
            yield SessionNotice(f"New characters to consider, in the Cast tab: {names}.")

    def scan_recent(self, nodes: int = 20) -> Iterator[SessionNotice]:
        """The on-demand scan: the most recent verbatim prose."""
        self._refuse_in_private("Scanning for characters")
        recent = list(self.split().verbatim)[-nodes:]
        found = self.scan_for_characters(recent)
        if found:
            names = ", ".join(suggestion.name for suggestion in found)
            yield SessionNotice(f"Found {len(found)} new character(s): {names}.")
        else:
            yield SessionNotice("No new named characters in the recent story.")

    def _suggestion(self, suggestion_id: str) -> CharacterSuggestion:
        for suggestion in self.character_suggestions:
            if suggestion.id == suggestion_id:
                return suggestion
        raise KeyError(f"unknown suggestion id: {suggestion_id!r}")

    def accept_suggestion(self, suggestion_id: str, *, to_cast: bool = False) -> Character:
        """Turn a suggestion into a card: supporting by default, or straight into the cast.

        Into the cast costs a cache miss on the next turn (the system block
        changes) and puts them under the roster, so it is the author's choice.
        """
        suggestion = self._suggestion(suggestion_id)
        card = card_from_suggestion(suggestion)
        (self.cast if to_cast else self.supporting).append(card)
        self.character_suggestions.remove(suggestion)
        self.save()
        return card

    def dismiss_suggestion(self, suggestion_id: str) -> None:
        suggestion = self._suggestion(suggestion_id)
        self.character_suggestions.remove(suggestion)
        dismissed = self.bundle.supporting.dismissed
        for name in (suggestion.name, *suggestion.aliases):
            if name.lower() not in dismissed:
                dismissed.append(name.lower())
        self.save()

    def promote(self, character_id: str) -> Character:
        """Supporting → cast: on the roster, holdable, in the cached system block."""
        character = next(c for c in self.supporting if c.id == character_id)
        self.supporting.remove(character)
        self.cast.append(character)
        for scene in self._all_scenes():
            move_into_cast(scene, character)
        self.save()
        return character

    def demote(self, character_id: str) -> Character:
        """Cast → supporting: off the roster and out of the system block."""
        if character_id == self.story.held_character_id:
            raise ValueError("the author is holding this character")
        character = next(c for c in self.cast if c.id == character_id)
        self.cast.remove(character)
        # Off the roster as cast, but still in the room if they were: by name.
        for scene in self._all_scenes():
            move_out_of_cast(scene, character)
        self.supporting.append(character)
        self.save()
        return character

    def _all_scenes(self) -> Iterator[SceneState]:
        yield self.story.scene
        for node in self.nodes:
            if node.meta.scene is not None:
                yield node.meta.scene

    # --- competence (§2.3, §4.1) -------------------------------------------

    def infer_competence(self, character_id: str) -> dict[str, CompetenceTier]:
        """Draft tiers for a character from their sheet. Returned, never applied.

        Raises ProviderError or ValueError; the caller shows the draft for the
        author to accept or discard.
        """
        character = next(c for c in (*self.cast, *self.supporting) if c.id == character_id)
        request = ChatRequest(
            model=self.summarization_model,
            extra_body=self.route_for("summarisation"),
            messages=build_inference_messages(character, self.story.world_bible),
            params=self.side_params(self.summarization_model),
        )
        log_ref = self._log(
            "competence_request",
            {"character_id": character_id, "payload": self.provider.build_payload(request)},
        )
        text, completed = self.provider.complete(request)
        self._log(
            "competence_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, self.summarization_model))
        return parse_tiers(text)

    # --- settings review ------------------------------------------------------

    @property
    def authoring_model(self) -> str:
        return self.config.authoring_model or self.model

    def endpoint_models(self) -> dict[str, ModelInfo] | None:
        """The endpoint's chat models, fetched at most hourly; None if unavailable.

        Network: call off the GUI thread. A failure isn't an error — the review
        just goes without the facts, and can't offer model changes.
        """
        now = datetime.now(UTC)
        if self._models is not None and self._models_at and now - self._models_at < MODELS_MAX_AGE:
            return self._models
        try:
            listing = self.provider.fetch_model_prices()
        except ProviderError:
            return self._models
        models = chat_models(listing)
        if not models:
            return self._models
        self._models, self._models_at = models, now
        return models

    def cached_context_length(self, model: str) -> int | None:
        """A model's context length from the list already fetched; never the network."""
        info = self._models.get(model) if self._models else None
        return info.context_length if info else None

    def cached_output_limit(self, model: str) -> int | None:
        """A model's output limit from the list already fetched; never the network."""
        info = self._models.get(model) if self._models else None
        return info.max_output_tokens if info else None

    def cached_price(self, model: str) -> ModelPrice | None:
        """A model's listed price from config, however old; never the network.

        For the request dialog's estimate on the GUI thread: a stale price is a
        fair estimate, and no price is simply no estimate.
        """
        return self.config.model_prices.get(model)

    def system_prompt_text(self) -> str:
        """The system block exactly as the storyteller receives it.

        Leaves the inspector's kept prompt alone: a review used to replace it
        with this placeholder turn.
        """
        return next((m.text for m in self._next_turn_prompt().messages if m.role == "system"), "")

    @staticmethod
    def _tail_text(prompt: AssembledPrompt) -> str:
        """The per-turn part of a prompt: everything after the story, less the
        placeholder turn itself."""
        return "\n\n".join(
            section.text
            for section in prompt.sections
            if section.name.startswith("tail.") and section.name != "tail.author_turn"
        )

    def mark_for_review(self, node_id: str) -> bool:
        """Toggle a passage as evidence for the next review; True if now marked."""
        if node_id in self.review_marks:
            self.review_marks.remove(node_id)
            return False
        self.review_marks.append(node_id)
        return True

    def _marked_replies(self, path: Sequence[Node]) -> list[Node]:
        """The marked passages that are on this path, oldest first."""
        marks = set(self.review_marks)
        return [node for node in reply_nodes(path) if node.id in marks]

    def _review_material(
        self, replies: int | None, whole_prompt: bool
    ) -> tuple[list[str], str, int, str]:
        """What the review is sent from the story: exchanges, the whole prompt,
        how many of the newest replies that prompt already carries, and the
        per-turn instructions.

        `replies` None is every reply in the story. Passages the author marked
        are sent whether or not they are among the newest. With the whole
        prompt, the replies already in it aren't sent twice.
        """
        path = self.path()
        replies_on_path = reply_nodes(path)
        if replies is None:
            chosen = replies_on_path
        else:
            chosen = replies_on_path[-replies:] if replies > 0 else []
        chosen_ids = {node.id for node in chosen}
        chosen = [n for n in replies_on_path if n.id in chosen_ids or n.id in self.review_marks]
        prompt = self._next_turn_prompt()
        text, in_prompt = "", 0
        if whole_prompt:
            text = render_whole_prompt(prompt.messages)
            in_prompt = self._replies_carried(prompt, path)
        skip = {node.id for node in replies_on_path[len(replies_on_path) - in_prompt :]}
        positions = {node.id: number for number, node in enumerate(path, start=1)}
        evidence = [
            render_exchange(path, node, self.cast, position=positions[node.id], texts=self.texts)
            for node in chosen
            if node.id not in skip
        ]
        return evidence, text, in_prompt, self._tail_text(prompt)

    def _replies_carried(self, prompt: AssembledPrompt, path: Sequence[Node]) -> int:
        """How many of the newest replies `prompt` carries word for word."""
        carried = {node.id for node in self.split(path).verbatim} - set(prompt.excluded_node_ids)
        count = 0
        for node in reversed(reply_nodes(path)):
            if node.id not in carried:
                break
            count += 1
        return count

    def _next_turn_prompt(self) -> AssembledPrompt:
        """The prompt the storyteller would get next, with a placeholder turn.

        Offline, like the inspector's: assembling never embeds. The inspector's
        kept prompt is put back afterwards.
        """
        held = self.story.held_character_id
        kept = self.last_prompt
        try:
            return self.assemble(
                TurnRequest(
                    speaker_id=held or DIRECTOR_SPEAKER_ID,
                    user_text="(The author's next turn goes here.)",
                    controlled_character_id=held,
                    agency_mode=self.story.defaults.agency_mode,
                    npc_scope=self.story.defaults.npc_scope,
                )
            )
        finally:
            self.last_prompt = kept

    def _alternatives(self, models: Mapping[str, ModelInfo] | None) -> list[ModelInfo]:
        """A few of the endpoint's other models for the reviewer to choose from:
        the current model's own family first, then the rest by id."""
        if not models:
            return []
        current = self.model
        family = current.split("/", 1)[0] if "/" in current else ""
        others = [info for model_id, info in models.items() if model_id != current]
        others.sort(key=lambda info: (not (family and info.id.startswith(family + "/")), info.id))
        return others[:REVIEW_ALTERNATIVES]

    def review_sizes(self) -> ReviewSizes:
        """Token estimates for each thing a review can be sent, for the dialog.

        On the GUI thread: nothing here touches the network.
        """
        estimate = lambda text: self.estimator.estimate(text, self.model)  # noqa: E731
        path = self.path()
        whole = self._next_turn_prompt()
        replies = reply_nodes(path)
        in_prompt = self._replies_carried(whole, path)
        facts = self._models.get(self.model) if self._models else None
        settings = json.dumps(
            settings_view(
                self.bundle,
                model=self.model,
                facts=facts,
                config=self.config,
                alternatives=self._alternatives(self._models),
                hidden_ids=self.hidden_ids(),
                path=path,
                cast_fits=whole.include_cast_full_descriptions,
            ),
            ensure_ascii=False,
        )
        positions = {node.id: number for number, node in enumerate(path, start=1)}
        sizes = {
            node.id: estimate(
                render_exchange(
                    path, node, self.cast, position=positions[node.id], texts=self.texts
                )
            )
            for node in replies
        }
        system = next((m.text for m in whole.messages if m.role == "system"), "")
        shown = system + "\n" + self._tail_text(whole)
        storyteller = self._prompt_catalogue(review_prompt_keys(), shown)
        everything = self._prompt_catalogue(review_prompt_keys(side=True), shown)
        return ReviewSizes(
            prompt_texts=estimate(storyteller),
            side_texts=estimate(everything) - estimate(storyteller),
            settings=estimate(review_system(texts=self.texts)) + estimate(settings),
            system_prompt=estimate(
                next((m.text for m in whole.messages if m.role == "system"), "")
            ),
            tail=estimate(self._tail_text(whole)),
            whole_prompt=estimate(render_whole_prompt(whole.messages)),
            exchanges=tuple(sizes[node.id] for node in reversed(replies)),
            in_prompt=in_prompt,
            marked=tuple(
                (node.id, sizes[node.id]) for node in reversed(self._marked_replies(path))
            ),
        )

    def review_settings(
        self,
        complaint: str,
        *,
        replies: int | None = EVIDENCE_REPLIES,
        whole_prompt: bool = False,
        model: str | None = None,
        side_texts: bool = False,
    ) -> SettingsReview:
        """Proposed changes to the settings that answer the author's complaint.

        Returned, never applied. The reviewer sees the settings, the actual
        system block and the per-turn instructions — many complaints trace to
        a line already in them — or, with `whole_prompt`, everything the
        storyteller receives; and as many exchanges from the story as asked
        (`replies` None for all of them), plus any the author marked.
        The review is kept in `bundle.reviews`.
        Raises ProviderError, or ValueError — including when the request would
        not fit the reviewer's context, or the reviewer ran out of room.
        """
        self._refuse_in_chat("A settings review")
        self._refuse_in_private("A settings review")
        if not complaint.strip():
            raise ValueError("say what you'd like to change first")
        model = model or self.authoring_model
        path = self.path()
        models = self.endpoint_models()
        evidence, whole, in_prompt, tail = self._review_material(replies, whole_prompt)
        system_prompt = self.system_prompt_text()
        prompt_keys = review_prompt_keys(side=side_texts)
        catalogue = self._prompt_catalogue(prompt_keys, whole or f"{system_prompt}\n{tail}")
        messages = build_review_messages(
            self.bundle,
            complaint=complaint,
            system_prompt=system_prompt,
            evidence=evidence,
            messages_so_far=len(path),
            model=self.model,
            facts=models.get(self.model) if models else None,
            whole_prompt=whole,
            in_prompt=in_prompt,
            tail=tail,
            config=self.config,
            alternatives=self._alternatives(models),
            hidden_ids=self.hidden_ids(),
            path=self.path(),
            cast_fits=self._next_turn_prompt().include_cast_full_descriptions,
            texts=self.texts,
            prompt_catalogue=catalogue,
        )
        reviewer = models.get(model) if models else None
        # As much room as the reviewer can write in: the ceiling costs nothing
        # unless used, and a cut-off review is a paid failure.
        answer_tokens = (reviewer.max_output_tokens if reviewer else None) or REVIEW_MAX_TOKENS
        limit = reviewer.context_length if reviewer else None
        if limit:
            size = sum(self.estimator.estimate(m.text, model) for m in messages)
            if size + min(answer_tokens, REVIEW_MAX_TOKENS) > limit:
                raise ValueError(
                    f"that is about {size:,} tokens, and {model} takes {limit:,} including "
                    f"room for its answer. Send fewer exchanges, leave out the whole prompt, "
                    "or pick a model with a longer context"
                )
            answer_tokens = min(answer_tokens, max(limit - size, 1))
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("authoring", model),
            messages=messages,
            params=self.side_params(
                model, max_tokens=answer_tokens, temperature=REVIEW_TEMPERATURE
            ),
        )
        log_ref = self._log(
            "review_request",
            {"complaint": complaint, "payload": self.provider.build_payload(request)},
        )
        text, completed = self.provider.complete(request)
        self._log(
            "review_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, model))
        if completed.finish_reason == "length":
            raise ValueError(
                f"the reviewer ran out of room at {answer_tokens:,} tokens before finishing "
                "its answer. Try a model with a larger output limit, or a narrower complaint"
            )
        review = parse_review(
            text,
            self.bundle,
            models=models,
            current_model=self.model,
            prompt_keys=prompt_keys,
            texts=self.texts,
            config=self.config,
        )
        self.bundle.reviews.append(ReviewRecord(model=model, complaint=complaint, review=review))
        self.save()
        return review

    def _prompt_catalogue(self, keys: Sequence[str], shown: str) -> str:
        """The prompt texts a review may change, as it is sent them."""
        return render_prompt_catalogue(
            keys,
            self.texts,
            story_edits=self.story.prompt_edits,
            app_edits=self.config.prompt_edits,
            shown=shown,
        )

    @property
    def last_review(self) -> ReviewRecord | None:
        return self.bundle.reviews[-1] if self.bundle.reviews else None

    def apply_review(
        self, changes: Sequence[SettingsChange], *, sent_director_turn: bool = False
    ) -> dict[str, str | None]:
        """Apply the changes the author ticked. Returns, by change id, why each
        one that couldn't be applied wasn't (None for the ones that were).

        Each was checked when the review arrived, but only against the whole
        set: leaving one out can strand another, so each is applied on its
        own, renames last. The settings as they were are kept for
        `undo_review` (in memory; every review is also in reviews.json).
        """
        record = self.last_review
        provider = self.config.active_provider()
        self.review_undo = ReviewSnapshot(
            story=self.story.model_copy(deep=True),
            cast=[c.model_copy(deep=True) for c in self.cast],
            supporting=[c.model_copy(deep=True) for c in self.supporting],
            lore=[e.model_copy(deep=True) for e in self.bundle.lore],
            record_id=record.id if record else None,
            scenes={
                node.id: node.meta.scene.model_copy(deep=True) if node.meta.scene else None
                for node in self.nodes
            },
            provider_model=provider.model if provider is not None else None,
        )
        results: dict[str, str | None] = {}
        for change in ordered_for_apply(changes):
            if change.kind == "generation":
                # The author's settings for every story: suggested, and made
                # by hand in Settings → Generation, never applied here.
                results[change.id] = "a suggestion, made in Settings → Generation"
                continue
            try:
                apply_change(self.bundle, change)
                results[change.id] = None
            except ValueError as exc:
                results[change.id] = str(exc)
        if record is not None:
            record.applied_ids = [i for i, problem in results.items() if problem is None]
            if sent_director_turn:
                record.applied_ids.append("director")
            record.undone = False
        self.story.updated_at = utc_now_iso()
        self.save()
        return results

    def forget_review_undo(self) -> None:
        """The settings have moved on by hand; the snapshot no longer describes them."""
        self.review_undo = None

    def undo_review(self) -> bool:
        """Put the settings back as they were before the last applied review."""
        snapshot = self.review_undo
        if snapshot is None:
            return False
        live = self.story
        restored = snapshot.story
        # Everything a change can touch; the playthrough (leaf, scene log,
        # held character) stays as it is now.
        live.title = restored.title
        live.world_bible = restored.world_bible
        live.setup = restored.setup
        live.defaults = restored.defaults
        live.style = restored.style
        live.prompt_edits = restored.prompt_edits
        self.bundle.cast[:] = snapshot.cast
        self.bundle.supporting.characters[:] = snapshot.supporting
        self.bundle.lore[:] = snapshot.lore
        for node in self.nodes:
            if node.id in snapshot.scenes:
                node.meta.scene = snapshot.scenes[node.id]
        provider = self.config.active_provider()
        if (
            provider is not None
            and snapshot.provider_model is not None
            and provider.model != snapshot.provider_model
        ):
            # A model change had become the provider's default as well.
            provider.model = snapshot.provider_model
            save_config(self.config, root=self.root)
        for record in self.bundle.reviews:
            if record.id == snapshot.record_id:
                record.undone = True
        self.review_undo = None
        self._sync_scene()
        self.story.updated_at = utc_now_iso()
        self.save()
        return True

    def rename_on_rosters(self, character: Character, old_name: str) -> None:
        """A card renamed by hand keeps its place on every roster it was on by name."""
        if old_name.strip() == character.name.strip():
            return
        before = Character(id=character.id, name=old_name, aliases=list(character.aliases))
        for scene in self._all_scenes():
            rename_in_scene(scene, before, character.name)
        self.save()

    def first_appearance(self, character: Character) -> Node | None:
        """The first message on the active path that names the character, or None."""
        return next((node for node in self.path() if mentions(node.content, character)), None)

    # --- asides: questions out of character ---------------------------------

    def asides_at(self, anchor_node_id: str | None) -> list[Aside]:
        return [aside for aside in self.bundle.asides if aside.anchor_node_id == anchor_node_id]

    def delete_aside(self, aside_id: str) -> None:
        self.bundle.asides[:] = [a for a in self.bundle.asides if a.id != aside_id]
        self.save()

    @staticmethod
    def _question_as_turn(question: str) -> TurnRequest:
        # Only for choosing lore: retrieval keys off the text and recent history.
        return TurnRequest(speaker_id=DIRECTOR_SPEAKER_ID, user_text=question)

    def assemble_question(
        self,
        question: str,
        *,
        lore: Sequence[LoreEntry] | None = None,
        history: Sequence[Node] | None = None,
        options: AssemblyOptions | None = None,
    ) -> AssembledPrompt:
        """The prompt for an aside at the current leaf. Like `assemble`, never embeds.

        `history` and `options` are a private scene's (its messages, its budget
        and rules) when the aside is asked in one."""
        history = self.path() if history is None else list(history)
        # Earlier asides from a private scene go only to that scene's model.
        span_id = self.open_span.id if self.open_span is not None else None
        anchor = history[-1].id if history else None
        if lore is None:
            lore = self.retrieve(
                self._question_as_turn(question), history, allow_network=False
            ).entries
        split = self.split(history)
        prompt = assemble_question(
            story=self.story,
            cast=self.visible_cast(),
            history_nodes=split.verbatim,
            question=question,
            earlier=[
                (a.question, a.answer)
                for a in self.asides_at(anchor)
                if a.answer and a.private_span in (None, span_id)
            ],
            summaries=split.summaries,
            lore=lore,
            standing_lore=self.lore_layout().standing,
            supporting=self.supporting_for(question, history),
            on_file=self.visible_supporting(),
            scene_log=log_on_path(self.story.scene_log, history),
            estimator=self.estimator,
            options=options or self.assembly_options(),
        )
        self.last_prompt = prompt
        return prompt

    def ask(self, question: str) -> Iterator[StreamEvent]:
        """Ask the model something about the story, out of character.

        The answer is saved as an aside anchored at the current leaf. It is
        never part of the story: no node, no archival, no validation, and the
        story's own prompts never include it.
        """
        self._tee_guard()
        self._refuse_in_chat("An out-of-character question")
        # Asked in a private scene, it goes to the private model like the scene.
        span = self.open_span
        provider = self.private_provider if span is not None else self.provider
        if provider is None:
            raise ValueError("the private scene has no model to ask")
        model = span.model if span is not None else self.model
        history = self.private_history(span) if span is not None else self.path()
        anchor = history[-1].id if history else None
        lore = self.retrieve(
            self._question_as_turn(question), history, allow_network=span is None
        ).entries
        prompt = self.assemble_question(
            question,
            lore=lore,
            history=history,
            options=self.private_options() if span is not None else None,
        )
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("story"),
            messages=prompt.messages,
            params=self.story_params(model),
            use_cache_control=self._cache_control_for(model),
            cache_ttl=self.config.cache_ttl,
        )
        log_ref = self._log_turn(
            "request",
            {"aside": True, "node_id": anchor, "payload": provider.build_payload(request)},
            span,
        )

        text: list[str] = []
        completed: StreamCompleted | None = None
        try:
            for event in provider.stream(request):
                if isinstance(event, TextDelta):
                    text.append(event.text)
                elif isinstance(event, StreamCompleted):
                    completed = event
                yield event
        finally:
            answer = "".join(text).strip()
            usage = self.priced(completed.usage if completed else Usage(), model)
            if answer:
                self.bundle.asides.append(
                    Aside(
                        anchor_node_id=anchor,
                        question=question,
                        answer=answer,
                        model=model,
                        usage=usage,
                        request_log_ref=log_ref,
                        private_span=span.id if span is not None else None,
                    )
                )
            self._log_turn(
                "response",
                {
                    "aside": True,
                    "request_log_ref": log_ref,
                    "text": answer,
                    "finish_reason": completed.finish_reason if completed else None,
                    "usage": completed.raw_usage if completed else {},
                    "stopped_early": completed is None,
                },
                span,
            )
            self._update_correction_factor(prompt, usage)
            self.save()

    def prune_shortcuts(self, node: Node) -> None:
        """Drop the scene read's shortcuts whose sentence an edit has removed."""
        node.meta.scene_shortcuts = [
            shortcut
            for shortcut in node.meta.scene_shortcuts
            if locate_quote(node.content, shortcut.quote) is not None
        ]

    def _attach(self, node: Node, *, parent: Node | None) -> None:
        if parent is None and node.meta.scene is None:
            # Every path starts from a scene, so no branch ever falls back
            # to another branch's.
            node.meta.scene = self.story.scene.model_copy(deep=True)
        if parent is not None:
            link_child(parent, node)
        self.nodes.append(node)
        self.story.active_leaf_id = node.id
        self.story.updated_at = utc_now_iso()

    def _update_correction_factor(self, prompt: AssembledPrompt, usage: Usage) -> None:
        if not self.learn_corrections:
            return
        model = prompt.model or self.model
        if not model or usage.prompt_tokens <= 0 or prompt.budget.total <= 0:
            return
        current = self.estimator.factor_for(model)
        rolled = updated_correction_factor(current, prompt.budget.total, usage.prompt_tokens)
        if abs(rolled - current) < 1e-6:
            return
        self.estimator.factors[model] = rolled
        if self.memory_only:
            # The estimator learns for this process; the config doesn't: an
            # entry keyed by the chat's model would reach config.json with the
            # window's next save.
            return
        self.config.token_correction_factors[model] = rolled
        self._save_config()

    @property
    def pictures(self) -> PictureStore:
        """Where this story's pictures are kept: its folder, or for a chat
        kept in memory only, memory (`storage.picture_store`)."""
        if not self.memory_only:
            return DiskPictures(self.story.id, self.root)
        if self._memory_pictures is None:
            self._memory_pictures = MemoryPictures(self._log)
        return self._memory_pictures

    def restore_memory(self, files: dict[str, bytes], log: list[dict[str, Any]]) -> None:
        """A memory-only chat brought back from its backup, in memory: its
        pictures and its log, as they were exported."""
        self._memory_pictures = MemoryPictures(self._log, files)
        self.memory_log.extend(log)

    @property
    def memory_only(self) -> bool:
        """A chat kept in memory only: nothing about it reaches the disk, not
        the story, not its log, not what it teaches the config (the author: "a
        full incognito conversation")."""
        return self.story.chat and self.story.chat_keep == "memory"

    def _log(self, kind: str, body: dict[str, Any]) -> str:
        entry_id = new_id()
        entry = {"id": entry_id, "kind": kind, "at": utc_now_iso(), **body}
        if self.memory_only:
            self.memory_log.append(entry)
        else:
            append_api_log(self.story.id, entry, root=self.root)
        return entry_id

    def _save_config(self) -> None:
        if not self.memory_only:
            save_config(self.config, root=self.root)

    def save(self) -> None:
        # A memory-only private scene never reaches the disk (session_private);
        # nor does a memory-only chat, at all.
        if self.memory_only:
            return
        save_story_bundle(self.bundle_to_save(), root=self.root)

    def close(self, *, wait_seconds: float = 5.0) -> None:
        """Before the story is left (another opened, the window closed).

        Chapters and merges written in the background are paid for whether
        or not a turn ever adopts them; without this they were abandoned on
        their daemon threads and the API log, the story's cost record, never
        saw them. A job still out gets `wait_seconds` to finish (on the GUI
        thread, so switching stories passes 0: a chapter takes ~40s, and the
        wait mostly froze the window), then whatever it has is logged as not
        adopted.
        """
        job = self._archive_job
        if job is not None:
            job.done.wait(wait_seconds)
            self._archive_job = None
            reason = "not adopted: the story was closed"
            for chapter in job.chapters:
                self._log_dropped("summary", chapter.summary, reason, node_ids=chapter.node_ids)
                if chapter.ledger is not None:
                    self._log_dropped("ledger", chapter.ledger, reason)
            if job.scan is not None:
                self._log_dropped("characters", job.scan, reason)
        merge = self._merge_job
        if merge is not None:
            merge.done.wait(wait_seconds)
            self._merge_job = None
            log_ref = self._log(
                "merge_request",
                {
                    "summary_ids": merge.run_ids,
                    "payload": self.provider.build_payload(merge.request),
                },
            )
            if merge.reply is None:
                self._log(
                    "merge_response",
                    {
                        "request_log_ref": log_ref,
                        "error": merge.error or "the story was closed before the reply",
                    },
                )
            else:
                text, completed = merge.reply
                self._log(
                    "merge_response",
                    {
                        "request_log_ref": log_ref,
                        "text": text,
                        "usage": completed.raw_usage,
                        "dropped": "not adopted: the story was closed",
                    },
                )
        if self._memory_pictures is not None:
            # A memory-only chat's pictures go with it.
            self._memory_pictures.close()
