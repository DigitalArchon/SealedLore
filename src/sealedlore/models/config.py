"""App-level config (config.json): providers, API keys, app defaults. See §1, §5.1, §6.2, §7.

Plaintext JSON is intentional per the hard constraints — no keyring, no
encryption at this stage. HTTPS is still required for real endpoints; plain
http is allowed only on loopback, since the embeddings endpoint may be a
local server (§7).
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sealedlore.models.private import PrivateKeep
from sealedlore.models.prompt_edit import PromptEdit

DEFAULT_BASE_URL = "https://nano-gpt.com/api/v1"
# The storyteller a new setup starts with (Settings → Models, and the CLI's
# configure): it measured best on price for quality. The same id works on
# nano-gpt and OpenRouter.
DEFAULT_STORY_MODEL = "anthropic/claude-sonnet-4.6"

CacheControlMode = Literal["auto", "on", "off"]
CacheTtl = Literal["5m", "1h"]
SceneLedger = Literal["every_turn", "manual"]
LoreSelector = Literal["picker", "jev", "similarity"]

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def require_https(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS:
        return url
    raise ValueError(f"endpoint must use https (http is allowed only on loopback): {url!r}")


def _same_host(first: str, second: str) -> bool:
    a, b = urlparse(first), urlparse(second)
    return (a.scheme, (a.hostname or "").lower(), a.port) == (
        b.scheme,
        (b.hostname or "").lower(),
        b.port,
    )


class ProviderConfig(BaseModel):
    # A URL assigned later (Settings, the CLI) is checked like one parsed.
    model_config = ConfigDict(validate_assignment=True)

    name: str
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = 300.0
    # Escape hatch for endpoint-specific request fields (e.g. OpenRouter's
    # {"usage": {"include": true}}); merged into the payload last.
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        return require_https(value)


DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"


class EmbeddingProviderConfig(BaseModel):
    """The embeddings endpoint (§7). Separate from chat: it may be a local server.

    An empty `base_url` or `api_key` means "use the chat provider's", which is
    the common case when one endpoint serves both — see `Config.embeddings()`.
    """

    model_config = ConfigDict(validate_assignment=True)

    base_url: str = ""
    api_key: str = ""
    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int = 0
    timeout_seconds: float = 60.0
    # Some models want the query and the document marked differently. bge-m3
    # does not, which is why it is the default; keep these as data so a model
    # that does need them doesn't mean new code (nomic, E5, Qwen3).
    query_prefix: str = ""
    document_prefix: str = ""
    batch_size: int = 32

    @field_validator("base_url")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        return require_https(value) if value else value


class ModelPrice(BaseModel):
    """Listed prices in USD per million tokens, for estimating unreported costs."""

    prompt: float
    completion: float
    cache_read: float | None = None
    cache_write: float | None = None
    fetched_at: str | None = None


class Config(BaseModel):
    @field_validator("text_scale")
    @classmethod
    def _text_scale_in_range(cls, value: float) -> float:
        # Clamped, not refused: a hand-edited size must not set the whole
        # config aside.
        return max(0.9, min(2.0, value))

    providers: list[ProviderConfig] = Field(default_factory=list)
    active_provider_name: str | None = None
    # Lore retrieval (§7). Unconfigured means keyword matching only, silently.
    embedding_provider: EmbeddingProviderConfig | None = None
    lore_retrieval_k: int = 3
    # How many turns of the active path join the current message in the query
    # vector. Too few and the query loses the scene; too many and it drifts.
    lore_query_turns: int = 3
    # Measured against bge-m3, not guessed: its cosine scores sit in a narrow
    # band (~0.31–0.60 across relevant and irrelevant alike), so this is a
    # floor against obvious junk rather than a precision instrument. Relevant
    # entries scored 0.48+; 0.45 keeps them while cutting the bottom half.
    # Retune per model — the Lore panel shows the scores to tune against.
    lore_similarity_threshold: float = 0.45
    lore_token_cap: int = 2_000
    # A lorebook up to this share of the context budget is sent whole, in the
    # cached system block, instead of selected per turn (`lore_layout`). At
    # 25 entries (~3k tokens, 7% of 45k) whole cost less than retrieval; at 50
    # (~6.5k, 14%) the same; at 100 twice as much and a third of the budget.
    lore_whole_share: float = 0.1
    # How a lorebook past that share is selected from, per turn. Live on
    # Sonnet 4.6 at 100 entries both model methods beat
    # "similarity" (today's top-3 + keywords: 10-11 lore contradictions in 32
    # passages against 0-8) and tied with each other; real play decides.
    #   "picker": `lore_model` picks from candidates after each passage, in
    #       the background, and the author's turn adds its keyword hits.
    #   "jev": `lore_decision_model` (TypeSafe Jev, nano-gpt's /decisions)
    #       scores every entry just before the turn, ~0.8s, plus the same
    #       keyword hits.
    #   "similarity": embeddings and keywords, no extra calls.
    lore_selector: LoreSelector = "picker"
    lore_model: str = "deepseek/deepseek-v4.1-flash"
    lore_decision_model: str = "typesafe/jev-1.13"

    # Caching (§6.2). "auto" applies cache_control blocks only to models whose
    # ID matches one of the patterns below; "on"/"off" force it either way.
    cache_control_mode: CacheControlMode = "auto"
    # How long Anthropic keeps the cached prefix. Live logs (329 Claude turns):
    # 14% of the gaps between turns passed five minutes, only 2.8% an hour, and
    # the rewrites after them were 80% of what cache writes cost. An hour's
    # write costs ~1.9x base on nano-gpt against ~1x for five minutes.
    cache_ttl: CacheTtl = "1h"
    anthropic_model_patterns: list[str] = Field(
        default_factory=lambda: ["anthropic/", "claude-", "claude/"]
    )
    # Anthropic will not cache a segment below its minimum cacheable length, so
    # a breakpoint on a shorter segment is a wasted marker.
    min_cacheable_tokens: int = 1024
    # How many trailing user+assistant exchanges stay outside the cached prefix
    # so that regenerating a recent take doesn't discard a fresh cache write.
    cache_exchanges_outside_prefix: int = 1
    cast_token_cap: int = 8_000

    # The model that drafts a story from a premise and reviews its settings.
    # Blank means the story's own model; worth setting to a stronger one, since
    # these are occasional calls whose output the whole story then rests on.
    authoring_model: str | None = None

    # Listed prices per model, fetched from the endpoint's models list and
    # refreshed weekly. Used only when a response doesn't report its cost.
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)

    # Rolling per-model estimator corrections, updated from reported usage (§5.1).
    token_correction_factors: dict[str, float] = Field(default_factory=dict)

    # Archival (§5.2). Archival runs when the assembled prompt reaches the
    # budget, and takes whole chunks so the summary block stays byte-stable.
    auto_archive: bool = True
    archive_chunk_turns: int = 10
    summary_target_words: int = 250
    # How far under the budget archival works before it stops. Archiving only
    # until the prompt just fits leaves a few hundred tokens of headroom, so
    # the next few turns cross the budget again and every one of them pays for
    # a summary and a fresh cache write. Halving the prompt instead buys tens
    # of quiet turns for the same number of summaries over the story's life.
    archive_target_ratio: float = Field(default=0.5, ge=0.2, le=0.95)
    # Chapters never shrink on their own: one per ten exchanges, forever, all
    # of them in every prompt. Past this share of the budget the oldest are
    # merged into one shorter part, in the background (never while the author
    # waits), and the part replaces them the next time the summary block
    # changes anyway. The newest `summary_merge_keep` chapters stay as they are.
    summary_merge_ratio: float = Field(default=0.2, ge=0.05, le=0.6)
    summary_merge_keep: int = Field(default=3, ge=1)
    # A part's length as a share of the chapters it replaces. Measured on a
    # real story: a fixed 500 words for six chapters (a quarter) kept 0.42-0.52
    # of the facts still in play against the chapters' 0.80; half kept 0.65-0.77.
    summary_merge_share: float = Field(default=0.5, ge=0.2, le=0.9)
    # Keep the story ledger: point-form facts beside the chapters, updated at
    # each archival alongside the summary call (engine/ledger.py). Off by
    # default: it lifted the record's fact retention from 0.80 to 0.88 (three
    # judges), but Sonnet 4.6's recall of archived facts in play didn't move
    # (0.77 vs 0.80, and 0.74 vs 0.70 over a merged part; n=33, within noise),
    # for 1.2k more prompt tokens and a call per chapter.
    story_ledger: bool = False
    # Look for newly named characters in each archived chunk: the moment their
    # prose is about to be compressed away is when a card is worth having.
    suggest_characters: bool = True
    # Dice (§4.2): the roll chip in the transcript. Rolls are logged either way.
    show_dice_rolls: bool = True
    # View → Text size: every piece of text in the window, as a share of the
    # designed size (gui/text_size.py). For poor eyesight above all.
    text_scale: float = 1.0
    # View → Theme (gui/theme.py): "dark", the app's own look, or
    # "high_contrast". An unknown name is read as dark, never refused.
    theme: str = "dark"
    # View → Fonts…: families for the window, the story's text and the
    # Prompt tab's monospace. "" is the app's own.
    ui_font: str = ""
    story_font: str = ""
    mono_font: str = ""
    # Most supporting cards sent in one turn's tail.
    supporting_limit: int = 8
    # How far back a supporting character counts as still around. Sharing the
    # lore query's three turns made this a feedback loop: a character went
    # unmentioned for a turn, their card left the prompt, so the model was less
    # likely to use them, so they stayed gone — and the room emptied out.
    supporting_recall_turns: int = 10

    # The scene card keeps itself (engine/scene_update.py). After each passage
    # a small model reads what changed — who came in, who left, where the
    # scene moved, whether it is private — and the card follows; the Scene
    # panel shows the change with an Undo. A hand-kept roster was right three
    # times in 299 logged turns. "manual" leaves it to the Scene panel.
    scene_reads: SceneLedger = "every_turn"
    # The model for that read: an easy task, made on every turn, so a small
    # fast one is right. Blank means the story's summarisation model.
    scene_model: str | None = None

    # Stories with a plot (models/plot.py) keep a clock and a set of facts.
    # After each passage a small model reads how much time passed and which
    # facts the passage changed; the Plot panel shows it with an Undo. Off,
    # the clock and facts stand still unless the author sets them.
    plot_reads: bool = True
    # The model for that read, and for the director that runs events. Blank
    # means the scene model, then the story's summarisation model.
    plot_model: str | None = None
    # Whether the Plot tab and the notices name upcoming events. Off, the
    # author sees the clock and what has happened, nothing still to come.
    show_plot_spoilers: bool = False

    # Pictures (engine/image_prompt.py, providers/images.py). The image model
    # takes reference pictures: Seedream 5.0 Pro was the author's pick for
    # keeping characters and ships as drawn. A prompt is written by
    # `image_prompt_model` (blank: the story's own model, on the story's
    # cached prefix) and always shown to the author before anything is sent.
    image_model: str = "bytedance/seedream-v5.0-pro"
    image_prompt_model: str | None = None
    image_size: str = "16:9"
    image_count: int = Field(default=1, ge=1, le=4)
    # The endpoint's image listing (sizes, reference limits, prices), fetched
    # when Settings or the image dialog first needs it and kept a day.
    image_models: list[dict] = Field(default_factory=list)
    image_models_fetched_at: str | None = None

    # Private scenes (engine/session_private.py): a separate endpoint and model
    # the main provider never hears about — a local server (Ollama, LM Studio,
    # llama.cpp, http on loopback only) or a nano-gpt TEE model. A blank key
    # is taken from the chat provider only when the host is the same.
    private_provider: ProviderConfig | None = None
    # The private model's context budget. Small local models often run with
    # a 4-8k context (Ollama's default depends on VRAM), so this is its own.
    private_budget: int = 16_000
    # The last choice beside the Private button: keep the scene's messages on
    # disk, or in memory only until the app closes.
    private_keep: PrivateKeep = "memory"
    # Check a TEE model's attestation when a scene begins, and each reply's
    # signature against it.
    private_verify_tee: bool = True
    # "Don't show this again" on the note of what a private scene pauses.
    private_intro_seen: bool = False
    # The author's edits to the program's prompt texts for every story, by
    # key (engine/prompt_texts.py). A story's own edits win over these.
    prompt_edits: dict[str, PromptEdit] = Field(default_factory=dict)

    def private(self) -> ProviderConfig | None:
        """The private endpoint, with a blank key filled from the chat
        provider when both are on the same host."""
        settings = self.private_provider
        if settings is None or not settings.model:
            return None
        if settings.api_key:
            return settings
        chat = self.active_provider()
        if chat is not None and _same_host(settings.base_url, chat.base_url):
            return settings.model_copy(update={"api_key": chat.api_key})
        return settings

    def active_provider(self) -> ProviderConfig | None:
        if self.active_provider_name is None:
            return None
        return next((p for p in self.providers if p.name == self.active_provider_name), None)

    def embeddings(self) -> EmbeddingProviderConfig | None:
        """The embeddings endpoint with blanks filled from the chat provider.

        Returns None when embeddings aren't configured at all, which is the
        signal to fall back to keyword matching (§7).
        """
        settings = self.embedding_provider
        if settings is None or not settings.model:
            return None
        chat = self.active_provider()
        resolved = settings.model_copy()
        if not resolved.base_url:
            if chat is None:
                return None
            resolved.base_url = chat.base_url
        # The chat key only goes to the chat provider's own host: a blank key
        # beside a local or third-party embeddings server must not hand it over.
        if (
            not resolved.api_key
            and chat is not None
            and _same_host(resolved.base_url, chat.base_url)
        ):
            resolved.api_key = chat.api_key
        return resolved
