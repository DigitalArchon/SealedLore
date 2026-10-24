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

from sealedlore.models.generation import GenerationParams, ReasoningLevel
from sealedlore.models.private import PrivateKeep
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.route import ModelRoute

DEFAULT_BASE_URL = "https://nano-gpt.com/api/v1"
# The storyteller a new setup starts with (Settings → Models, and the CLI's
# configure): it measured best on price for quality. The same id works on
# nano-gpt and OpenRouter.
DEFAULT_STORY_MODEL = "anthropic/claude-sonnet-4.6"

# The models a new setup on NanoGPT starts with for the small calls made on
# every turn, and what Settings → Models → Use recommended models fills in.
# Left blank, each of these falls back to the story model, which is the most
# expensive place to run a read of a few hundred tokens. The ids are
# NanoGPT's: on any other endpoint the fields stay blank.
RECOMMENDED_HOST = "nano-gpt.com"
RECOMMENDED_MODELS: dict[str, str] = {
    # The scene read is as good as the reasoning behind it: of the models
    # tried on judged reads, only those that reason kept the roster right
    # when someone came or went, and this one was the best of them that the
    # subscription includes (re-checked Oct 2026).
    "scene_model": "z-ai/glm-5.3",
    # The plot's director, which runs before the turn (the author's choice,
    # Oct 17 2026): re-measured, the models tried came out within a few wrong
    # answers of each other, and this one is included in the subscription and
    # waits ~2.5s. The plot's clock read runs on the scene model.
    "plot_model": "deepseek/deepseek-v4.1-flash",
    "lore_model": "deepseek/deepseek-v4.1-flash",
}


class RecommendedModel(BaseModel):
    """One model offered beside a field, with what it is good and bad at."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    note: str
    # Shown after the label in brackets: "default", "recommended", "premium".
    tag: str = ""


# The image models offered under Recommended beside a field (Settings →
# Images, the portraits dialog), the default first. Measured in
# SealedLoreDocs design/images.md and design/map.md (Oct 15 2026).
RECOMMENDED_IMAGE_MODELS: dict[str, tuple[RecommendedModel, ...]] = {
    "portrait_model": (
        # Six characters (three Doomsville, AfterDark's Suzuka, Brasshaven's
        # automaton, the Silverwood's dwarf), two blind Claude judges
        # (SealedLoreDocs design/images.md, Oct 18 2026): FLUX.3 Klein ahead
        # of FLUX.2 klein 9B on five of six. The author: FLUX.3 Klein (not its
        # Base, made for another purpose) the default, Seedream 5.0 Pro the
        # best. Each id is the model's own: a picture sent goes to its edit
        # version (engine/image_variants.py).
        RecommendedModel(
            id="blackforestlabs/flux-3/klein/text-to-image",
            tag="default",
            label="FLUX.3 Klein",
            note="Ahead of FLUX.2 klein on five of six characters for both blind judges, at "
            "about $0.009 a portrait and in 4-7 seconds. Square portraits. Takes up to ten "
            "pictures (its edit version is sent for you when one goes).",
        ),
        RecommendedModel(
            id="bytedance/seedream-v5.0-pro",
            tag="best",
            label="Seedream 5.0 Pro",
            note="The best pictures of those offered, tall portraits and up to ten pictures "
            "(its edit version is sent for you when one goes), at about $0.09 a portrait "
            "($0.045 at 1k): ten times FLUX.3 Klein.",
        ),
        RecommendedModel(
            id="flux-2-klein-9b",
            label="FLUX.2 klein 9B",
            note="The earlier default: tall portraits at about $0.01, but last on five of six "
            "characters against FLUX.3 Klein (people looked older than their cards).",
        ),
        RecommendedModel(
            id="z-image-turbo",
            label="Z Image Turbo",
            note="As good or better for a single portrait (about $0.012), but it can't be "
            "given reference pictures, so it can't keep a character the same from one "
            "picture to the next.",
        ),
        RecommendedModel(
            id="recraft-ai/recraft-v4.1-flash/text-to-image",
            label="Recraft V4.1 Flash",
            note="The most striking portraits of those tried, with cinematic light, at about "
            "$0.008 a portrait. Like Z Image Turbo it can't be given reference pictures, so "
            "it can't keep a character the same from one picture to the next.",
        ),
        RecommendedModel(
            id="bytedance/seedream-v5.0-flash",
            label="Seedream 5.0 Flash",
            note="Very finished portraits and up to ten reference pictures, at about $0.027 a "
            "portrait: between FLUX.3 Klein and Seedream 5.0 Pro.",
        ),
    ),
    # Editing a card's picture (Edit…): given John's portrait and a scar,
    # twenty years older and a suit at dinner. Picked in the Edit dialog;
    # the card's own model is used when it can take the picture.
    "picture_edit": (
        # John's portrait with a scar, twenty years older, a suit at dinner:
        # two blind judges ranked FLUX.3 Klein Edit first on all three, and
        # FLUX.2 klein last (it changed his face twice); Oct 18 2026.
        RecommendedModel(
            id="blackforestlabs/flux-3/klein/text-to-image",
            label="FLUX.3 Klein",
            tag="default",
            note="Its edit version (sent for you, since the picture goes) was first on all "
            "three edits tried (a scar, twenty years older, a suit at dinner) for both blind "
            "judges, keeping the face and everything else, at about $0.009.",
        ),
        RecommendedModel(
            id="bytedance/seedream-v5.0-pro",
            label="Seedream 5.0 Pro",
            tag="best",
            note="The best pictures of those offered, at about $0.09 (its edit version is sent "
            "for you); not measured on these three edits.",
        ),
        RecommendedModel(
            id="qwen-image",
            label="Qwen Image",
            note="Made all three and kept the face, about $0.03 (free within a NanoGPT "
            "subscription's daily allowance); the suit came out as a wider shot.",
        ),
        RecommendedModel(
            id="bytedance/seedream-v5.0-flash",
            label="Seedream 5.0 Flash",
            note="Made all three, the subtlest scar and the most finished suit, about $0.027 "
            "(its edit version, sent for you).",
        ),
        RecommendedModel(
            id="flux-2-klein-9b",
            label="FLUX.2 klein 9B",
            note="The earlier default, about $0.016 with the picture: made the changes, but "
            "changed John's face on two of three against FLUX.3 Klein.",
        ),
    ),
}


# The text models offered under Recommended beside Settings → Models' fields,
# from what was measured on blind-judged replays and live play (re-measured
# for every role on Oct 17 2026). Each note says roughly what was found and
# why the model is offered. DEFAULT_STORY_MODEL and RECOMMENDED_MODELS come
# first.
RECOMMENDED_TEXT_MODELS: dict[str, tuple[RecommendedModel, ...]] = {
    "story": (
        RecommendedModel(
            id=DEFAULT_STORY_MODEL,
            label="Claude Sonnet 4.6",
            tag="default",
            note="Best on price for quality: about 1¢ a turn on a map story and 2-3¢ on a "
            "long story with a warm cache. It keeps the rules (no held character written and "
            "no shortcut in 128 judged passages), writes short passages on the map (about 65 "
            "words), and made a few errors fewer than Opus over whole stories.",
        ),
        RecommendedModel(
            id="anthropic/claude-opus-5.5",
            label="Claude Opus 5.5",
            tag="premium",
            note="The better writer on every blind judge, Claude and not: preferred in about 9 "
            "turns of 10 by the Claude judges and more often than not by DeepSeek, with more "
            "specific detail and fuller answers. About 2.4 times Sonnet's cost (2.3¢ a map "
            "turn, ~5¢ on a long story), passages twice as long on the map (about 135 "
            "words), and no fewer errors over whole stories. It counts about half as many "
            "tokens again as the app estimates.",
        ),
        RecommendedModel(
            id="z-ai/glm-5.3",
            label="GLM 5.3",
            note="Included in a NanoGPT subscription, and the model most of SealedLore's own "
            "testing plays on. Not ranked against Sonnet or Opus; sent its lowest reasoning "
            "level.",
        ),
    ),
    "scene_model": (
        RecommendedModel(
            id=RECOMMENDED_MODELS["scene_model"],
            label="GLM 5.3",
            tag="recommended",
            note="A scene read needs a model that reasons: 89 of 94 rosters right and 31-32 of "
            "36 comings and goings on judged reads, where models that don't reason got 72-78. "
            "About 8s after the passage, now and then 40s; included in a NanoGPT subscription.",
        ),
        RecommendedModel(
            id="moonshotai/kimi-k3",
            label="Kimi K3",
            tag="premium",
            note="As accurate as GLM 5.3 (88 of 94, 32 of 36) and steadier (rarely over 17s), "
            "but outside the subscription: about 0.3¢ a read.",
        ),
        RecommendedModel(
            id="deepseek/deepseek-v4-pro:thinking",
            label="DeepSeek V4 Pro, thinking",
            note="As accurate as GLM 5.3 when it thinks, at two to three times the wait.",
        ),
    ),
    "plot_model": (
        RecommendedModel(
            id=RECOMMENDED_MODELS["plot_model"],
            label="DeepSeek V4.1 Flash",
            tag="recommended",
            note="As sound a director as any tried: on 69 logged plot questions asked twice, "
            "both blind judges found 3 wrong answers in 138, about 2.5s before the turn, and "
            "included in a NanoGPT subscription.",
        ),
        RecommendedModel(
            id="mistralai/mistral-medium-3.1",
            label="Mistral Medium 3.1",
            note="As sound (6 wrong answers in 138) and the quickest, about 1s before the "
            "turn, but priced per call: about 0.05¢.",
        ),
        RecommendedModel(
            id="z-ai/glm-5.3",
            label="GLM 5.3",
            note="As sound (3 wrong in 138) and included in the subscription, but about 6s "
            "before the turn; it used to begin events too readily and now does so less.",
        ),
    ),
    "lore_model": (
        RecommendedModel(
            id=RECOMMENDED_MODELS["lore_model"],
            label="DeepSeek V4.1 Flash",
            tag="recommended",
            note="Picks the lore each turn needs, after the passage: with 100 lore entries it "
            "cut contradictions of the lore from about 10 in 32 passages to a few, tied with "
            "nano-gpt's Jev, and it is included in a NanoGPT subscription.",
        ),
        RecommendedModel(
            id="mistralai/mistral-medium-3.5",
            label="Mistral Medium 3.5",
            tag="premium",
            note="Ranked first most often in live play (13-15 times of 32 against DeepSeek's "
            "7-9), with the fewest contradictions of the lore, but outside the subscription.",
        ),
    ),
}


def recommended_model(role: str, model: str) -> RecommendedModel | None:
    """The recommendation for `model` in a role (a Config field name, or
    "story"), or None if it isn't one."""
    choices = RECOMMENDED_IMAGE_MODELS.get(role) or RECOMMENDED_TEXT_MODELS.get(role, ())
    return next((r for r in choices if r.id == model), None)


def on_nanogpt(base_url: str) -> bool:
    """The endpoint is NanoGPT's (whose model ids and routes these are)."""
    host = (urlparse(base_url.strip()).hostname or "").lower()
    return host == RECOMMENDED_HOST or host.endswith("." + RECOMMENDED_HOST)


def recommended_models(base_url: str) -> dict[str, str]:
    """The recommended model for each role on this endpoint: `Config` field
    name → model id. Empty for an endpoint whose ids we don't know."""
    return dict(RECOMMENDED_MODELS) if on_nanogpt(base_url) else {}


CacheControlMode = Literal["auto", "on", "off"]
CacheTtl = Literal["5m", "1h"]
SceneLedger = Literal["every_turn", "manual"]
LoreSelector = Literal["picker", "jev", "similarity"]
# How much the world designer puts in each region of open country
# (engine/regions.py OPEN_COUNTRY): Settings → Models → Open country.

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


# The protocol the chat endpoint speaks: OpenAI's /chat/completions (NanoGPT,
# OpenRouter, a local server) or Anthropic's own Messages API
# (providers/anthropic.py). "auto" reads it from the host (`chat_api`).
ChatApi = Literal["auto", "openai", "anthropic"]
CHAT_API_LABELS: dict[str, str] = {
    "auto": "From the address",
    "openai": "OpenAI-compatible",
    "anthropic": "Anthropic",
}
ANTHROPIC_HOST = "api.anthropic.com"
_CHAT_HOSTS = {ANTHROPIC_HOST: "anthropic"}


def chat_api(base_url: str, api: str = "auto") -> str:
    """The protocol a chat endpoint speaks: `api` unless "auto", otherwise
    Anthropic's for its own host and OpenAI's for anything else."""
    if api != "auto":
        return api
    host = (urlparse(base_url.strip()).hostname or "").lower()
    for known, name in _CHAT_HOSTS.items():
        if host == known or host.endswith("." + known):
            return name
    return "openai"


# A model reference may name the endpoint it is reached through:
# "<endpoint name>::<model id>" (the author, Oct 2026: NanoGPT and Anthropic
# both serve Opus 5.5, and a key for each means a choice). A bare id means
# the default endpoint (`Config.active_provider_name`), so every config and
# story from before keeps working unchanged. Endpoint names may hold spaces;
# model ids hold "/", "." and ":" ("anthropic/claude-sonnet-4.6:thinking"),
# never two colons.
ENDPOINT_SEPARATOR = "::"


def split_model(ref: str) -> tuple[str | None, str]:
    """(the endpoint named, or None for the default; the bare model id)."""
    if ENDPOINT_SEPARATOR not in ref:
        return None, ref
    name, model = ref.split(ENDPOINT_SEPARATOR, 1)
    return name.strip() or None, model


def qualify(endpoint_name: str | None, model_id: str, default: str | None = None) -> str:
    """The reference for `model_id` on `endpoint_name`: bare when that is the
    default endpoint (or no endpoint), so a default endpoint's models read as
    they always did. A blank id stays blank."""
    if not model_id or endpoint_name is None or endpoint_name == default:
        return model_id
    return f"{endpoint_name}{ENDPOINT_SEPARATOR}{model_id}"


def model_label(ref: str) -> str:
    """How a reference reads to the author: "claude-opus-5-5 · Anthropic"
    for a model on a named endpoint, the id alone on the default one."""
    name, model = split_model(ref)
    return f"{model} · {name}" if name else model


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
    # What the endpoint speaks (Settings → Endpoints → Speaks); "auto" reads
    # it from the address.
    api: ChatApi = "auto"

    @field_validator("base_url")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        return require_https(value)

    def speaks(self) -> str:
        """The protocol this endpoint speaks, "openai" or "anthropic" (`chat_api`)."""
        return chat_api(self.base_url, self.api)


# The protocol a picture or video endpoint speaks. "auto" reads it from the
# host (`media_api`); anything unknown is taken as OpenAI's own shape.
MediaApi = Literal["auto", "nanogpt", "openai", "openrouter", "wavespeed"]
MEDIA_API_LABELS: dict[str, str] = {
    "auto": "From the address",
    "nanogpt": "NanoGPT",
    "openai": "OpenAI-compatible",
    "openrouter": "OpenRouter",
    "wavespeed": "WaveSpeed",
}
_MEDIA_HOSTS = {
    "nano-gpt.com": "nanogpt",
    "openrouter.ai": "openrouter",
    "wavespeed.ai": "wavespeed",
}
WAVESPEED_BASE_URL = "https://api.wavespeed.ai/api/v3"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def media_api(base_url: str, api: str = "auto") -> str:
    """The protocol a picture or video endpoint speaks: `api` unless "auto",
    otherwise the one its host is known for."""
    if api != "auto":
        return api
    host = (urlparse(base_url.strip()).hostname or "").lower()
    for known, name in _MEDIA_HOSTS.items():
        if host == known or host.endswith("." + known):
            return name
    return "openai"


class MediaEndpoint(BaseModel):
    """Where pictures or videos are made: an endpoint and key of their own, so
    chat can be on one service, pictures on another and video on a third,
    each with a key whose spending can be capped on its own."""

    model_config = ConfigDict(validate_assignment=True)

    base_url: str = ""
    api_key: str = ""
    api: MediaApi = "auto"

    @field_validator("base_url")
    @classmethod
    def _check_scheme(cls, value: str) -> str:
        return require_https(value) if value else value


class ResolvedMedia(BaseModel):
    """A media endpoint with its blanks filled in and its protocol known."""

    base_url: str
    api_key: str
    api: str
    # The video key is the chat's or the pictures' too: every video warns.
    shared_key: bool = False


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


class ModelReasoning(BaseModel):
    """What SealedLore knows about one model's reasoning: what the endpoint
    lists (fetched with its prices, kept a week), and whether it reasoned
    when nothing asked it to (engine/reasoning.py)."""

    # None: not known (not listed, or not fetched yet).
    reasons: bool | None = None
    efforts: list[str] = Field(default_factory=list)
    # What it does when a request says nothing, as NanoGPT's web listing states
    # it (providers/reasoning_defaults.py): reasons or not, and at what effort.
    # None: not stated, which is most models.
    reasons_by_default: bool | None = None
    default_effort: str | None = None
    # The defaults were looked for (an endpoint without them counts): facts
    # from before they were read are fetched again.
    defaults_checked: bool = False
    fetched_at: str | None = None
    # When a reply reasoned though its request asked for nothing. For a week
    # after, "as low as possible" asks this model for its lowest level.
    unasked_at: str | None = None


class BadHost(BaseModel):
    """A host the author marked bad for one model (Help → Model trouble): it
    is never offered as a host to choose, until allowed again."""

    reason: str = ""
    at: str | None = None


class WindowLayout(BaseModel):
    """The main window as the author last left it (gui/window_layout.py).

    The size is the unmaximised one, so leaving full screen gives it back. A
    dock width of 0 has never been set by hand: the window shares its width
    out by proportion instead."""

    width: int = 0
    height: int = 0
    maximized: bool = False
    stories_width: int = 0
    inspector_width: int = 0
    # The composer's height, as the divider above it was last dragged; 0 is
    # its natural height.
    composer_height: int = 0
    # The Cast page's list of names, as its divider was last dragged; 0 is
    # as tall as its names.
    cast_list_height: int = 0


class PhoneDevice(BaseModel):
    """A phone paired for phone access (sealedlore/phone/): its token is
    never kept, only the token's sha256."""

    id: str
    name: str = "A phone"
    token_sha256: str
    paired_at: str = ""
    # The day it was last seen (YYYY-MM-DD), not the time: enough to tell an
    # old phone to revoke from one in use.
    last_seen_day: str = ""


class OnionKey(BaseModel):
    """The onion service's private key, as tor's ADD_ONION gives it
    ("ED25519-V3:…"). Named `api_key` so the keychain keeps it as it keeps
    the endpoints' keys (storage/keychain.py `slots`)."""

    api_key: str = ""


class PhoneAccess(BaseModel):
    """Playing a shared story from a phone's Tor Browser (gui/window_phone.py).

    Off unless turned on: `start_on_launch` is only whether it starts with the
    app; File → Phone access turns it on or off for one run and writes
    nothing here. Nothing is shared until the author shares it, and which
    stories are shared stays on this machine: a backup, a copy or a scenario
    never carries it."""

    start_on_launch: bool = False
    notify_connect: bool = True
    shared_stories: list[str] = Field(default_factory=list)
    devices: list[PhoneDevice] = Field(default_factory=list)
    onion: OnionKey = Field(default_factory=OnionKey)
    # The onion address's 56 letters, without ".onion": known with the key.
    service_id: str = ""


class Config(BaseModel):
    @field_validator("text_scale")
    @classmethod
    def _text_scale_in_range(cls, value: float) -> float:
        # Clamped, not refused: a hand-edited size must not set the whole
        # config aside.
        return max(0.9, min(2.0, value))

    providers: list[ProviderConfig] = Field(default_factory=list)
    active_provider_name: str | None = None
    # Where API keys are kept (storage/keychain.py): "keychain" is the system
    # keychain when the machine has one, else config.json in plain text;
    # "file" is config.json by choice (the CLI, test setups).
    key_storage: Literal["keychain", "file"] = "keychain"
    # The keys config.json leaves blank because the keychain holds them.
    keychain_slots: list[str] = Field(default_factory=list)
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
    # Recall (engine/recall.py): the chapters a merged part stands for, or
    # those and the archived exchanges, brought back into the tail when a
    # Question (or, with recall_in "everywhere", a turn) is about them.
    # "off" sends nothing. Chapters by default: Questions about merged
    # chapters went 0.45-0.50 -> 0.56-0.67 (Oct 2026).
    recall: Literal["off", "chapters", "exchanges"] = "chapters"
    # Where recall is used: an out-of-character Question only, or story turns
    # too. Measured (Oct 2026): it answered Questions about merged chapters
    # far better, and in turns it brought old states back as present.
    recall_in: Literal["questions", "everywhere"] = "questions"
    # A plot story's chapter headings carry the days they span, from the
    # plot's clock ("Chapter 4 · days 12–15"). Never without a plot. On by
    # default (Oct 2026): over three judges, 47 time errors against 57.
    plot_chapter_days: bool = True
    recall_k: int = Field(default=2, ge=1, le=8)
    recall_token_cap: int = Field(default=1_500, ge=100)
    # What a Question recalled rides the next this many author turns on its
    # branch, so play doesn't contradict what the author was just told (the
    # author, Oct 2026). 0 turns it off.
    recall_carry_turns: int = Field(default=3, ge=0, le=20)
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

    # The model that writes chapters and merges them, for every story that
    # has none of its own (`Story.defaults.summarization_model`). Blank means
    # the story's model. It was per story only, so Settings could not set it
    # with no story open, unlike every other model.
    summarization_model: str | None = None

    # The model that drafts a story from a premise and reviews its settings.
    # Blank means the story's own model; worth setting to a stronger one, since
    # these are occasional calls whose output the whole story then rests on.
    authoring_model: str | None = None

    # The storyteller's sampling settings, for every story (Settings →
    # Generation). They were each story's own, and so could not be set with
    # no story open; the side calls keep their own fixed limits.
    generation: GenerationParams = Field(default_factory=GenerationParams)
    # How much to ask models to reason: the story model's turns (passages,
    # questions, private turns), and every other call. "least" is as little
    # as each model allows, which is also the fastest (engine/reasoning.py).
    reasoning_story: ReasoningLevel = "least"
    reasoning_side: ReasoningLevel = "least"
    # Per model: its listed reasoning levels and whether it reasons unasked.
    model_reasoning: dict[str, ModelReasoning] = Field(default_factory=dict)
    # Per model, the hosts the author marked bad (by NanoGPT's host id). Only
    # manual choice avoids them: nothing is sent to exclude them, since any
    # provider field bills pay-as-you-go even on the subscription.
    bad_hosts: dict[str, dict[str, BadHost]] = Field(default_factory=dict)

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
    # The window's size and dock widths at the last close; None until then.
    window: WindowLayout | None = None
    # Whether the Stories list shows while a story is open: put away by
    # default, for the story's room (the author, Oct 15 2026); Stories above
    # the transcript or View → Stories list brings it back, and that sticks.
    stories_list: bool = False
    # Whether the composer's second row (More: model voices, outcome, a
    # private scene, an OOC note) is open. Put away by default (Oct 18 2026):
    # eight controls before the first turn confused a new player.
    composer_more: bool = False
    # Whether the Inspector shows the Prompt page (View → Prompt page): what
    # the storyteller was sent, for checking the model's work. Off by default.
    prompt_page: bool = False
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
    # The model for the director that runs events (the read above runs on the
    # scene model). Blank means the scene model, then the story's
    # summarisation model.
    plot_model: str | None = None
    # Whether the Plot tab and the notices name upcoming events. Off, the
    # author sees the clock and what has happened, nothing still to come.
    show_plot_spoilers: bool = False

    # A picture of the place the scene is at, kept on its lore entry (the
    # author, Oct 24 2026), shown behind the story's text while the scene is
    # there, in place of the story's own background; the story's own comes
    # back where the place has none. Settings → Images.
    location_backgrounds: bool = True
    # A visual story's Look (engine/look.py): the model that is shown a part
    # of the area and says what the player sees and finds there. Blank, the
    # story model; one that reads no pictures is sent the words alone.
    look_model: str | None = None
    # A visual story's people (engine/npc.py): the model that speaks for
    # each person from their sheet and chooses what they do. Blank, the
    # story model.
    npc_model: str | None = None
    # A visual story's plot keeper (engine/keeper.py): the model that turns
    # the plot's events, a Director line or an unusual action into a script
    # the program applies. Blank, the story model.
    keeper_model: str | None = None

    # Pictures (engine/image_prompt.py, providers/images.py). The image model
    # takes reference pictures: Seedream 5.0 Pro was the author's pick for
    # keeping characters and ships as drawn. A prompt is written by
    # `image_prompt_model` (blank: the story's own model, on the story's
    # cached prefix) and always shown to the author before anything is sent.
    image_model: str = "bytedance/seedream-v5.0-pro"
    # A portrait of each character (engine/portraits.py), offered at a
    # story's start and on the Cast page, every prompt shown first. FLUX.3
    # Klein beat FLUX.2 klein 9B on five of six characters for two blind
    # judges at $0.009 (Oct 18 2026); with a picture to work from its edit
    # version is sent (engine/image_variants.py). RECOMMENDED_IMAGE_MODELS:
    # the others, with what each is good at.
    portrait_model: str = "blackforestlabs/flux-3/klein/text-to-image"
    image_prompt_model: str | None = None
    # NanoGPT's host for each role's model (models/route.py), keyed by
    # `ROUTE_ROLES`; missing means the subscription's routing.
    model_routes: dict[str, ModelRoute] = Field(default_factory=dict)
    image_size: str = "16:9"
    image_count: int = Field(default=1, ge=1, le=4)
    # The endpoint's image listing (sizes, reference limits, prices), fetched
    # when Settings or the image dialog first needs it and kept a day.
    image_models: list[dict] = Field(default_factory=list)
    image_models_fetched_at: str | None = None
    # Which endpoint that listing came from: another endpoint's is stale.
    image_models_source: str | None = None
    # Where pictures are made. Blank address and key are the chat
    # endpoint's (the key only on the chat's own host), as before this
    # setting existed.
    image_endpoint: MediaEndpoint = Field(default_factory=MediaEndpoint)

    # Video (engine/videos.py). Far dearer than pictures, so it is off until
    # a key is entered here, and that key is never taken from the chat's:
    # a key of its own can have its own daily spending cap. A key that is
    # the chat's or the pictures' too may be entered by hand, and then
    # every video warns before it is sent (`ResolvedMedia.shared_key`).
    video_endpoint: MediaEndpoint = Field(
        default_factory=lambda: MediaEndpoint(base_url=DEFAULT_BASE_URL)
    )
    video_model: str = "bytedance/seedance-2.5"
    # The last settings chosen in the video dialog, by the model's own
    # parameter names ("resolution", "duration"…): the next video starts
    # from them where the model has them.
    video_params: dict[str, Any] = Field(
        default_factory=lambda: {"resolution": "480p", "duration": "5"}
    )
    video_prompt_model: str | None = None
    video_models: list[dict] = Field(default_factory=list)
    video_models_fetched_at: str | None = None
    video_models_source: str | None = None

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
    # Phone access through Tor (sealedlore/phone/, gui/window_phone.py).
    phone: PhoneAccess = Field(default_factory=PhoneAccess)

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

    def images(self) -> ResolvedMedia | None:
        """Where pictures are made, blanks filled from the chat endpoint.
        None when there is nowhere to send them."""
        settings = self.image_endpoint
        chat = self._chat_to_inherit()
        base_url = settings.base_url or (chat.base_url if chat is not None else "")
        if not base_url:
            return None
        key = settings.api_key
        # The chat key only goes to the chat provider's own host.
        if not key and chat is not None and _same_host(base_url, chat.base_url):
            key = chat.api_key
        return ResolvedMedia(base_url=base_url, api_key=key, api=media_api(base_url, settings.api))

    def videos(self) -> ResolvedMedia | None:
        """Where videos are made. None until a video key is entered: nothing
        is inherited, so video spend is never on a key that wasn't meant
        for it."""
        settings = self.video_endpoint
        if not settings.base_url or not settings.api_key.strip():
            return None
        chat = self.active_provider()
        others = {chat.api_key if chat is not None else "", self.image_endpoint.api_key}
        return ResolvedMedia(
            base_url=settings.base_url,
            api_key=settings.api_key,
            api=media_api(settings.base_url, settings.api),
            shared_key=settings.api_key in (others - {""}),
        )

    def active_provider(self) -> ProviderConfig | None:
        """The default endpoint: where a bare model id is sent."""
        if self.active_provider_name is None:
            return None
        return self.endpoint_named(self.active_provider_name)

    def endpoint_named(self, name: str) -> ProviderConfig | None:
        return next((p for p in self.providers if p.name == name), None)

    def endpoint_for(self, ref: str) -> ProviderConfig | None:
        """The endpoint a model reference is reached through: the one it
        names, else the default. None when it names one that isn't set up
        (never the default in its place: the id and the key would be
        another endpoint's) or there is no default."""
        name, _model = split_model(ref)
        return self.active_provider() if name is None else self.endpoint_named(name)

    def bare_model(self, ref: str) -> str:
        """The model id without its endpoint, as the endpoint knows it."""
        return split_model(ref)[1]

    def qualify(self, endpoint_name: str | None, model_id: str) -> str:
        """`qualify` against this config's default endpoint."""
        return qualify(endpoint_name, model_id, self.active_provider_name)

    def _chat_to_inherit(self) -> ProviderConfig | None:
        """The chat endpoint, for pictures and embeddings to take a blank
        address or key from; none when it is Anthropic's own API, which
        makes no pictures and no vectors."""
        chat = self.active_provider()
        return None if chat is None or chat.speaks() == "anthropic" else chat

    def embeddings(self) -> EmbeddingProviderConfig | None:
        """The embeddings endpoint with blanks filled from the chat provider.

        Returns None when embeddings aren't configured at all, which is the
        signal to fall back to keyword matching (§7).
        """
        settings = self.embedding_provider
        if settings is None or not settings.model:
            return None
        chat = self._chat_to_inherit()
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
