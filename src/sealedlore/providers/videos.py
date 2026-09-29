"""Clients for video endpoints: NanoGPT, OpenRouter (OpenAI's `/videos`
shape) and WaveSpeed. Every one is a job: sent, asked after, fetched.

NanoGPT (measured live, Sept 2026): `POST /api/generate-video` answers 202
with `runId` and the price it has just pre-charged (`cost`);
`GET /api/video/status?requestId=` gives IN_QUEUE / IN_PROGRESS / COMPLETED /
FAILED, and when done `data.output.video.url`, an unsigned CloudFront link
kept for 7 days. The finished status carries no cost, and the billing lookup
doesn't cover videos: the pre-charge is the bill (it matched the balance to
the cent). A start frame (`imageDataUrl`) and an end frame (`last_image`)
may be data URLs; `reference_images` must be http(s) links. It does not
refuse a value it doesn't know: `resolution: "9000p"` was taken and made at
the default, 720p, for $1.80. So nothing is sent that the model's own
listing doesn't offer (`VideoModelInfo.clean`).
The listing (`/api/v1/video-models?detailed=true`) is public and describes
every model's parameters (select, switch, number, text) and its pricing.

OpenRouter: `POST /videos` answers with an id and a polling URL;
`GET /videos/{id}` gives pending / in_progress / completed / failed /
cancelled / expired, `unsigned_urls` (on openrouter.ai, fetched with the
key) and `usage.cost`. `/videos/models` is public, with durations,
resolutions, ratios, frame support and `pricing_skus`.

WaveSpeed: see `providers/wavespeed.py`; one model per operation.

OpenRouter and WaveSpeed were built from their documentation (Sept 2026)
and are not yet checked live.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import media_api
from sealedlore.providers import wavespeed
from sealedlore.providers.base import ProviderError
from sealedlore.providers.http import make_client
from sealedlore.providers.media_http import MediaHttp

# A single request (a submit, a status, a download of tens of megabytes).
VIDEO_REQUEST_TIMEOUT = 300.0

# Parameters the dialog never shows. The app fills the pictures itself;
# the prompt goes in as approved, so nothing may rewrite it on the way
# (`PROMPT_REWRITERS` are switched off where they can be); web search and
# the rest are out of place in a story.
_FILLED = {
    "prompt",
    "image",
    "last_image",
    "reference_images",
    "reference_videos",
    "reference_audios",
    "audio",
    "left_audio",
    "right_audio",
    "mode",
    "multi_prompt",
    "element_list",
    "enable_web_search",
}
PROMPT_REWRITERS = {
    "enable_prompt_expansion",
    "prompt_expansion_mode",
    "prompt_optimizer",
    "prompt_upsampling",
    "prompt_upsampler",
    "enhancePrompt",
    "enhance_prompt",
    "prompt_extend",
}
# Words for a select option that turns a rewriter off.
_OFF_WORDS = ("off", "disabled", "disable", "none", "false", "no")


@dataclass(frozen=True)
class VideoParam:
    """One setting a model takes, as its listing describes it."""

    key: str
    label: str
    kind: str  # "select", "switch", "number", "text"
    options: tuple[tuple[str, str], ...] = ()  # (value, label)
    default: Any = None
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None

    def allows(self, value: Any) -> bool:
        if self.kind == "select":
            return str(value) in {v for v, _ in self.options}
        if self.kind == "switch":
            return isinstance(value, bool)
        if self.kind == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            low = self.minimum if self.minimum is not None else float("-inf")
            high = self.maximum if self.maximum is not None else float("inf")
            return low <= value <= high
        return isinstance(value, str)


@dataclass(frozen=True)
class VideoModelInfo:
    id: str
    name: str
    api: str
    description: str = ""
    params: tuple[VideoParam, ...] = ()
    # The listing's own pricing object, read by `engine.video_price`.
    pricing: dict = field(default_factory=dict)
    # What the model takes besides the prompt.
    text_to_video: bool = True
    start_frame: bool = False
    end_frame: bool = False
    max_references: int = 0
    nsfw: bool = False
    # Values the app must send whatever the author sees (a rewriter off).
    fixed: tuple[tuple[str, Any], ...] = ()
    # Where the model sits in its listing (WaveSpeed: the operation).
    kind: str = ""

    def param(self, key: str) -> VideoParam | None:
        return next((p for p in self.params if p.key == key), None)

    def defaults(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params if p.default not in (None, "")}

    def clean(self, chosen: dict[str, Any]) -> dict[str, Any]:
        """The settings to send: the model's defaults under what was chosen,
        keeping only values its listing offers. A value it doesn't know is
        dropped for the default rather than sent: NanoGPT quietly makes an
        unknown resolution at the default one, and bills for that."""
        values = self.defaults()
        for key, value in chosen.items():
            param = self.param(key)
            if param is not None and param.allows(value):
                values[key] = value
        values.update(dict(self.fixed))
        return values

    @property
    def audio_key(self) -> str | None:
        for key in ("generate_audio", "generateAudio", "enable_audio", "sound"):
            param = self.param(key)
            if param is not None and param.kind == "switch":
                return key
        return None


@dataclass(frozen=True)
class VideoJob:
    """A job the endpoint accepted: enough to ask after it again later,
    after a restart too."""

    id: str
    api: str
    base_url: str
    # What the endpoint said it charged on accepting (NanoGPT), if anything.
    charged: float | None = None
    # OpenRouter's polling URL.
    poll_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VideoStatus:
    state: str  # "waiting", "done", "failed"
    urls: tuple[str, ...] = ()
    cost: float | None = None
    error: str = ""
    detail: str = ""  # the endpoint's own word for where it is ("IN_QUEUE")


@dataclass(frozen=True)
class FrameInputs:
    """Pictures sent with a video, as data URLs (base64)."""

    start: str | None = None
    end: str | None = None
    references: tuple[str, ...] = ()


# --- listings ---------------------------------------------------------------------


def _options(raw: Any) -> tuple[tuple[str, str], ...]:
    options: list[tuple[str, str]] = []
    for option in raw or ():
        if isinstance(option, dict) and "value" in option:
            value = str(option["value"])
            options.append((value, str(option.get("label") or value)))
        elif isinstance(option, (str, int, float)) and not isinstance(option, bool):
            options.append((str(option), str(option)))
    return tuple(options)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _nanogpt_param(key: str, spec: dict) -> VideoParam | None:
    kind = str(spec.get("type") or "")
    kind = {"boolean": "switch", "integer": "number", "string": "text"}.get(kind, kind)
    if kind not in ("select", "switch", "number", "text"):
        return None
    options = _options(spec.get("options"))
    if kind == "select" and not options:
        return None
    default = spec.get("default")
    if kind == "select" and default is not None:
        default = str(default)
    return VideoParam(
        key=key,
        label=str(spec.get("label") or key.replace("_", " ").capitalize()),
        kind=kind,
        options=options,
        default=default,
        description=str(spec.get("description") or ""),
        minimum=_number(spec.get("min")),
        maximum=_number(spec.get("max")),
    )


def _off_value(param: VideoParam) -> Any:
    if param.kind == "switch":
        return False
    for value, label in param.options:
        if value.lower() in _OFF_WORDS or label.lower() in _OFF_WORDS:
            return value
    return None


def _split(params: list[VideoParam]) -> tuple[tuple[VideoParam, ...], tuple[tuple[str, Any], ...]]:
    """The settings the dialog shows, and the values fixed under them."""
    shown: list[VideoParam] = []
    fixed: list[tuple[str, Any]] = []
    for param in params:
        if param.key in PROMPT_REWRITERS:
            off = _off_value(param)
            if off is not None:
                fixed.append((param.key, off))
            continue
        if param.key == "enable_web_search":
            fixed.append((param.key, False))
            continue
        if param.key in _FILLED or param.key.startswith("lora_"):
            continue
        if param.kind == "text" and param.key != "negative_prompt":
            continue
        shown.append(param)
    return tuple(shown), tuple(fixed)


def parse_nanogpt_video_models(body: Any) -> list[VideoModelInfo]:
    items = body.get("data") if isinstance(body, dict) else body
    models: list[VideoModelInfo] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        supported = item.get("supported_parameters")
        raw = supported.get("parameters") if isinstance(supported, dict) else None
        params = [
            p
            for key, spec in (raw or {}).items()
            if isinstance(spec, dict) and (p := _nanogpt_param(key, spec)) is not None
        ]
        keys = set((raw or {}).keys())
        caps = item.get("capabilities")
        caps = caps if isinstance(caps, dict) else {}
        shown, fixed = _split(params)
        models.append(
            VideoModelInfo(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                api="nanogpt",
                description=str(item.get("description") or ""),
                params=shown,
                pricing=item.get("pricing") if isinstance(item.get("pricing"), dict) else {},
                text_to_video=bool(caps.get("text_to_video", True)),
                start_frame=bool(caps.get("image_to_video")),
                end_frame="last_image" in keys,
                # Its reference pictures must be links (HTTP 400 for a data
                # URL, live): there is nowhere to put the author's pictures.
                max_references=0,
                nsfw=bool(caps.get("nsfw")),
                fixed=fixed,
                kind=str(item.get("category") or ""),
            )
        )
    return models


def parse_openrouter_video_models(body: Any) -> list[VideoModelInfo]:
    items = body.get("data") if isinstance(body, dict) else body
    models: list[VideoModelInfo] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        params: list[VideoParam] = []
        durations = [str(d) for d in item.get("supported_durations") or () if isinstance(d, int)]
        if durations:
            params.append(
                VideoParam(
                    "duration",
                    "Duration",
                    "select",
                    tuple((d, f"{d} seconds") for d in durations),
                    default="5" if "5" in durations else durations[0],
                )
            )
        for key, label in (("resolution", "Resolution"), ("aspect_ratio", "Aspect ratio")):
            values = [str(v) for v in item.get(f"supported_{key}s") or () if isinstance(v, str)]
            if values:
                default = "16:9" if key == "aspect_ratio" and "16:9" in values else values[0]
                params.append(
                    VideoParam(key, label, "select", tuple((v, v) for v in values), default=default)
                )
        if item.get("generate_audio"):
            params.append(VideoParam("generate_audio", "Generate audio", "switch", default=True))
        if item.get("seed"):
            params.append(VideoParam("seed", "Seed", "number", minimum=0))
        frames = item.get("supported_frame_images") or ()
        models.append(
            VideoModelInfo(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                api="openrouter",
                description=str(item.get("description") or ""),
                params=tuple(params),
                pricing=dict(item.get("pricing_skus") or {}),
                start_frame="first_frame" in frames,
                end_frame="last_frame" in frames,
                max_references=4,
            )
        )
    return models


_WS_KINDS = ("text-to-video", "image-to-video")


def _schema_param(key: str, spec: dict) -> VideoParam | None:
    kind = spec.get("type")
    enum = spec.get("enum")
    label = str(spec.get("title") or key.replace("_", " ").capitalize())
    description = str(spec.get("description") or "")
    default = spec.get("default")
    if isinstance(enum, list) and enum:
        options = _options(enum)
        return VideoParam(
            key,
            label,
            "select",
            options,
            default=None if default is None else str(default),
            description=description,
        )
    if kind == "boolean":
        return VideoParam(key, label, "switch", default=default, description=description)
    if kind in ("integer", "number"):
        low, high = _number(spec.get("minimum")), _number(spec.get("maximum"))
        if key == "duration" and low is not None and high is not None and high - low <= 60:
            # A duration is a choice of whole seconds.
            values = tuple((str(s), f"{s} seconds") for s in range(int(low), int(high) + 1))
            return VideoParam(
                key,
                "Duration",
                "select",
                values,
                default=None if default is None else str(int(default)),
                description=description,
            )
        return VideoParam(
            key,
            label,
            "number",
            default=default,
            description=description,
            minimum=low,
            maximum=high,
        )
    if kind == "string":
        return VideoParam(key, label, "text", default=default, description=description)
    return None


def parse_wavespeed_video_models(items: list[dict]) -> list[VideoModelInfo]:
    models: list[VideoModelInfo] = []
    for item in items:
        kind = item.get("type")
        model_id = item.get("model_id")
        if kind not in _WS_KINDS or not isinstance(model_id, str):
            continue
        fields = wavespeed.request_schema(item)
        params = [p for k, s in fields.items() if (p := _schema_param(k, s)) is not None]
        shown, fixed = _split(params)
        refs = fields.get("reference_images") or fields.get("images")
        price = item.get("base_price")
        models.append(
            VideoModelInfo(
                id=model_id,
                name=str(item.get("name") or model_id),
                api="wavespeed",
                description=str(item.get("description") or ""),
                params=shown,
                pricing={"base_price": price} if isinstance(price, (int, float)) else {},
                text_to_video=kind == "text-to-video",
                start_frame="image" in fields,
                end_frame="last_image" in fields,
                max_references=int(refs.get("maxItems") or 4) if isinstance(refs, dict) else 0,
                fixed=fixed,
                kind=kind,
            )
        )
    return models


def list_video_models(
    base_url: str,
    *,
    api: str | None = None,
    api_key: str = "",
    client: httpx.Client | None = None,
    timeout_seconds: float = 30.0,
) -> list[VideoModelInfo]:
    """The endpoint's video models. Empty if it has none or won't say."""
    api = api or media_api(base_url)
    owned = client is None
    http = client or make_client(base_url, timeout=httpx.Timeout(timeout_seconds))
    root = base_url.rstrip("/")
    try:
        if api == "nanogpt":
            response = http.get(f"{root}/video-models", params={"detailed": "true"})
            return parse_nanogpt_video_models(response.json()) if response.status_code < 400 else []
        if api in ("openrouter", "openai"):
            response = http.get(f"{root}/videos/models")
            return (
                parse_openrouter_video_models(response.json()) if response.status_code < 400 else []
            )
        if api == "wavespeed":
            media = MediaHttp(base_url, api_key, timeout_seconds=timeout_seconds, client=http)
            return parse_wavespeed_video_models(wavespeed.models(media))
        return []
    except (httpx.HTTPError, ValueError, ProviderError):
        return []
    finally:
        if owned:
            http.close()


# --- the client ---------------------------------------------------------------------


def nanogpt_root(base_url: str) -> str:
    """NanoGPT's video paths sit under /api, beside its /api/v1."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def build_video_payload(
    info: VideoModelInfo, prompt: str, settings: dict[str, Any], frames: FrameInputs
) -> dict[str, Any]:
    """The request body in the endpoint's own shape. `prompt` goes in exactly
    as given; `settings` must already be `info.clean`ed."""
    if info.api == "nanogpt":
        payload: dict[str, Any] = {"model": info.id, "prompt": prompt, **settings}
        if frames.start:
            payload["imageDataUrl"] = frames.start
        if frames.end:
            payload["last_image"] = frames.end
        if frames.references:
            # The listing types it as text: a JSON array of image URLs.
            payload["reference_images"] = json.dumps(list(frames.references))
        return payload
    if info.api in ("openrouter", "openai"):
        payload = {"model": info.id, "prompt": prompt}
        for key, value in settings.items():
            payload[key] = int(value) if key == "duration" else value
        images = [
            {"type": "image_url", "image_url": {"url": url}, "frame_type": kind}
            for url, kind in ((frames.start, "first_frame"), (frames.end, "last_frame"))
            if url
        ]
        if images:
            payload["frame_images"] = images
        if frames.references:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": url}} for url in frames.references
            ]
        return payload
    # WaveSpeed: the model's own fields; pictures become URLs when sent.
    payload = {"model": info.id, "prompt": prompt}
    for key, value in settings.items():
        param = info.param(key)
        numeric = key == "duration" and param is not None and param.kind == "select"
        payload[key] = int(value) if numeric and str(value).isdigit() else value
    if frames.start:
        payload["image"] = frames.start
    if frames.end:
        payload["last_image"] = frames.end
    if frames.references:
        payload["reference_images"] = list(frames.references)
    return payload


def loggable_video_payload(payload: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """The payload for the API log: the prompt verbatim, pictures by name.
    `names`: "start", "end", "references" → what they are."""
    logged = dict(payload)
    for key in ("imageDataUrl", "image"):
        if key in logged:
            logged[key] = f"<{names.get('start', 'start frame')}>"
    if "last_image" in logged:
        logged["last_image"] = f"<{names.get('end', 'end frame')}>"
    for key in ("reference_images", "input_references"):
        if key in logged:
            logged[key] = f"<{names.get('references', 'reference pictures')}>"
    if "frame_images" in logged:
        logged["frame_images"] = [
            f"<{names.get('start' if f.get('frame_type') == 'first_frame' else 'end', '')}>"
            for f in logged["frame_images"]
        ]
    return logged


class VideoClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api: str | None = None,
        timeout_seconds: float = VIDEO_REQUEST_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        self.api = api or media_api(base_url)
        self.http = MediaHttp(base_url, api_key, timeout_seconds=timeout_seconds, client=client)

    def cancel(self) -> None:
        """Stop waiting. A video already accepted is still made and billed."""
        self.http.cancel()

    def close(self) -> None:
        self.http.close()

    def wait(self, seconds: float) -> None:
        self.http.wait(seconds)

    # -- submit ------------------------------------------------------------------------

    def submit(self, payload: dict[str, Any]) -> VideoJob:
        """Send a request. Once this returns, the video is being made and paid for."""
        if self.api == "nanogpt":
            body = self.http.json(
                "POST", f"{nanogpt_root(self.base_url)}/generate-video", json=payload, makes=True
            )
            run_id = body.get("runId") or body.get("id") if isinstance(body, dict) else None
            if not isinstance(run_id, str):
                raise ProviderError("the video was not accepted: no run id", body=str(body)[:2000])
            cost = body.get("cost")
            return VideoJob(
                id=run_id,
                api=self.api,
                base_url=self.base_url,
                charged=float(cost) if isinstance(cost, (int, float)) else None,
                raw=_scrubbed(body),
            )
        if self.api in ("openrouter", "openai"):
            body = self.http.json(
                "POST", f"{self.base_url.rstrip('/')}/videos", json=payload, makes=True
            )
            job_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(job_id, str):
                raise ProviderError("the video was not accepted: no job id", body=str(body)[:2000])
            return VideoJob(
                id=job_id,
                api=self.api,
                base_url=self.base_url,
                poll_url=str(body.get("polling_url") or ""),
                raw=_scrubbed(body),
            )
        body = dict(payload)
        model = str(body.pop("model", ""))
        for key in ("image", "last_image"):
            if isinstance(body.get(key), str):
                body[key] = self._uploaded(body[key], key)
        if isinstance(body.get("reference_images"), list):
            body["reference_images"] = [
                self._uploaded(url, f"reference-{i}")
                for i, url in enumerate(body["reference_images"], start=1)
            ]
        prediction = wavespeed.submit(self.http, model, body)
        return VideoJob(id=prediction.id, api=self.api, base_url=self.base_url)

    def _uploaded(self, url: str, name: str) -> str:
        if not url.startswith("data:"):
            return url
        head, _, encoded = url.partition(",")
        media_type = head[5:].split(";", 1)[0] or "image/png"
        return wavespeed.upload(
            self.http, base64.b64decode(encoded), f"{name}.{media_type.split('/')[-1]}", media_type
        )

    # -- status ------------------------------------------------------------------------

    def status(self, job: VideoJob) -> VideoStatus:
        if job.api == "nanogpt":
            body = self.http.json(
                "GET",
                f"{nanogpt_root(job.base_url)}/video/status",
                params={"requestId": job.id},
            )
            return nanogpt_status(body)
        if job.api in ("openrouter", "openai"):
            url = job.poll_url or f"{job.base_url.rstrip('/')}/videos/{job.id}"
            if url.startswith("/"):
                parsed = urlparse(job.base_url)
                url = f"{parsed.scheme}://{parsed.netloc}{url}"
            return openrouter_status(self.http.json("GET", url))
        prediction = wavespeed.result(self.http, job.id)
        if prediction.status == wavespeed.DONE:
            return VideoStatus("done", urls=prediction.outputs, detail=prediction.status)
        if prediction.status in wavespeed.FAILED:
            return VideoStatus("failed", error=prediction.error or prediction.status)
        return VideoStatus("waiting", detail=prediction.status)

    def fetch(self, url: str) -> bytes:
        """The finished video. The key goes only to the endpoint's own host
        (OpenRouter's content URLs need it); a storage host never gets it."""
        if not url.startswith("https://"):
            raise ProviderError("the video's link isn't https; not fetched")
        own = (urlparse(url).hostname or "") == (urlparse(self.base_url).hostname or "")
        return self.http.request("GET", url, authorised=own).content


def _scrubbed(body: Any) -> dict:
    """A reply for the log, without the account's balance."""
    if not isinstance(body, dict):
        return {}
    return {k: v for k, v in body.items() if "balance" not in k.lower()}


def nanogpt_status(body: Any) -> VideoStatus:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise ProviderError("the video's status had no data", body=str(body)[:2000])
    state = str(data.get("status") or "").upper()
    cost = data.get("cost")
    cost = float(cost) if isinstance(cost, (int, float)) else None
    if state == "COMPLETED":
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        urls = [u for u in output.get("videoUrls") or () if isinstance(u, str)]
        video = output.get("video")
        if isinstance(video, dict) and isinstance(video.get("url"), str):
            urls = [video["url"], *[u for u in urls if u != video["url"]]]
        if not urls:
            return VideoStatus("failed", error="finished, but no video came back")
        return VideoStatus("done", urls=(urls[0],), cost=cost, detail=state)
    if state in ("FAILED", "CANCELED", "CANCELLED"):
        error = data.get("userFriendlyError") or data.get("error") or state.lower()
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error)
        return VideoStatus("failed", error=str(error), detail=state)
    return VideoStatus("waiting", detail=state)


def openrouter_status(body: Any) -> VideoStatus:
    if not isinstance(body, dict):
        raise ProviderError("the video's status wasn't an object", body=str(body)[:2000])
    state = str(body.get("status") or "").lower()
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    cost = usage.get("cost")
    cost = float(cost) if isinstance(cost, (int, float)) else None
    if state == "completed":
        urls = tuple(u for u in body.get("unsigned_urls") or () if isinstance(u, str))
        if not urls:
            return VideoStatus("failed", error="finished, but no video came back")
        return VideoStatus("done", urls=urls[:1], cost=cost, detail=state)
    if state in ("failed", "cancelled", "expired"):
        return VideoStatus("failed", error=str(body.get("error") or state), cost=cost, detail=state)
    return VideoStatus("waiting", detail=state)
