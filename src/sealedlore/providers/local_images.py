"""Pictures drawn on a server of the author's own: ComfyUI and AUTOMATIC1111 /
Forge (the author, Nov 20 2026, after reading how other roleplay apps draw).

Both run on the author's computer or local network as a rule (plain http is
allowed there, `models.config.require_https`), cost nothing a picture, and
know nothing of a "model" in the listing's sense: they have checkpoints.
A checkpoint is offered as a model (its name as the server gives it), sized
by its family (`native_sizes`), and taking one picture with the prompt.

AUTOMATIC1111 and its forks (Forge, reForge, SD.Next) answer `/sdapi/v1/*`
once started with `--api`: `txt2img`, `img2img` with the picture as
`init_images` and `denoising_strength`, the checkpoint by
`override_settings`, and `--api-auth user:password` as HTTP Basic (the key
written "user:password"). The reply's pictures are base64 PNGs.

ComfyUI runs a workflow in its API format (`{node id: {class_type,
inputs}}`, what Export (API) writes). The built-in ones draw from words and
from a picture (`builtin_workflow`) with any ordinary checkpoint; one of the
author's own is filled at its `{{placeholders}}` (`fill`), for a model the
built-in graph can't drive (FLUX, an edit model). A picture sent with the
prompt is uploaded first (`/upload/image`), the workflow queued (`/prompt`),
its history polled until done, and each output fetched from `/view`. The
built-in workflows end in PreviewImage, so nothing piles up in ComfyUI's
output folder; the uploaded picture stays in its input folder, as anything
uploaded to ComfyUI does.

Stop shuts the request's socket (`MediaHttp.cancel`) and then asks the server
to stop drawing: AUTOMATIC1111's `/interrupt` (it draws one request at a
time, so the one under way is ours), ComfyUI's queue entry deleted and, only
while ours is the one running, its `/interrupt`.

Before anything is drawn the checkpoint is looked up on the server, so a
model chosen for another endpoint (the settings' defaults are NanoGPT's) is
said in words, not an error from deep in a graph.

Pure of the GUI; the HTTP is the picture endpoint's own (`MediaHttp`).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import is_local_host, is_loopback_host
from sealedlore.providers.base import ProviderError, StreamCancelled, checked_key
from sealedlore.providers.http import reply_json
from sealedlore.providers.media_http import MediaHttp, MediaTimedOut
from sealedlore.storage.atomic import outside_json

# What each service is called in messages.
SERVICE_NAMES = {"comfyui": "ComfyUI", "a1111": "AUTOMATIC1111 / Forge"}
# How often ComfyUI's history is asked after a queued workflow.
POLL_SECONDS = 1.0
# How long asking a server to stop drawing is waited for, after a Stop.
STOP_SECONDS = 5.0
# A workflow file past this is not one (ComfyUI's own run to tens of kB).
WORKFLOW_MAX_CHARS = 2_000_000
# Pictures a workflow can be given: {{image}}, {{image2}} … {{image9}}.
MAX_WORKFLOW_PICTURES = 9
# The model a workflow of the author's draws with when it names its own
# (no {{model}} in it): offered as the one model there is.
WORKFLOW_MODEL = "Your workflow"

# The sizes a checkpoint draws well at, by its family. SDXL and what came
# after it (Pony, Illustrious, FLUX, SD3) were trained around a megapixel in
# these shapes; Stable Diffusion 1.5 around half a megapixel, and drawn
# larger it repeats its subject.
LARGE_SIZES = (
    "1024x1024",
    "896x1152",
    "832x1216",
    "768x1344",
    "640x1536",
    "1152x896",
    "1216x832",
    "1344x768",
    "1536x640",
)
SMALL_SIZES = ("512x512", "576x768", "512x768", "768x576", "768x512")
_LARGE_FAMILY = re.compile(
    r"xl|pony|illustrious|noob|flux|sd3|qwen|hidream|chroma|aura|kolors|playground|cascade",
    re.IGNORECASE,
)
_SMALL_FAMILY = re.compile(r"sd[-_ ]?1[._-]?5|sd15|v1[-_.]5", re.IGNORECASE)

# What ComfyUI's built-in workflows sample with unless `extra_body` says
# otherwise: settings that suit SDXL-family checkpoints and do for 1.5.
COMFY_DEFAULTS: dict[str, Any] = {
    "steps": 25,
    "cfg": 6.0,
    "sampler_name": "dpmpp_2m",
    "scheduler": "karras",
    "negative_prompt": "",
}
# AUTOMATIC1111's API samples 50 steps unless told: twice what a picture needs.
A1111_DEFAULTS: dict[str, Any] = {"steps": 25, "cfg_scale": 6.0}

# The placeholders a workflow may hold, each replaced by the value it names.
PLACEHOLDERS = (
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "seed",
    "model",
    "steps",
    "cfg",
    "sampler_name",
    "scheduler",
    "strength",
    "batch",
    "image",
    *(f"image{n}" for n in range(2, MAX_WORKFLOW_PICTURES + 1)),
)
_SLOT = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class LocalSettings:
    """What a server of the author's own needs beyond its address and key
    (`ResolvedMedia`)."""

    workflow: str = ""
    workflow_edit: str = ""
    strength: float = 0.6
    extra_body: dict[str, Any] | None = None

    @property
    def extra(self) -> dict[str, Any]:
        return dict(self.extra_body or {})


def local_settings(media: Any) -> LocalSettings:
    """A resolved picture endpoint's (`ResolvedMedia`) settings for a server
    of the author's own; the defaults for any other."""
    return LocalSettings(
        workflow=getattr(media, "workflow", "") or "",
        workflow_edit=getattr(media, "workflow_edit", "") or "",
        strength=float(getattr(media, "strength", 0.6) or 0.6),
        extra_body=dict(getattr(media, "extra_body", None) or {}),
    )


class WorkflowProblem(ValueError):
    """A workflow file that ComfyUI couldn't run: said before anything is sent."""


# --- sizes --------------------------------------------------------------------


def native_sizes(model: str) -> tuple[str, ...]:
    """The sizes `model` (a checkpoint's name) draws well at."""
    if _LARGE_FAMILY.search(model):
        return LARGE_SIZES
    if _SMALL_FAMILY.search(model):
        return SMALL_SIZES
    return LARGE_SIZES


def dimensions(size: str, model: str) -> tuple[int, int]:
    """`size` in pixels: "WxH" as it is (to a multiple of 8, as the samplers
    need), an aspect ratio ("16:9", NanoGPT's way) as the checkpoint's own
    size nearest it, anything else (a tier, "1k") its square."""
    sizes = [_pixels(s) for s in native_sizes(model)]
    text = size.strip().lower().replace("*", "x")
    if "x" in text:
        w, _, h = text.partition("x")
        if w.isdigit() and h.isdigit() and int(w) >= 64 and int(h) >= 64:
            return int(w) // 8 * 8, int(h) // 8 * 8
    if ":" in text:
        a, _, b = text.partition(":")
        try:
            wanted = float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            wanted = 1.0
        return min(sizes, key=lambda wh: abs(wh[0] / wh[1] - wanted))
    return sizes[0]


def _pixels(size: str) -> tuple[int, int]:
    w, _, h = size.partition("x")
    return int(w), int(h)


def _info(model_id: str, name: str, pictures: int, local: bool) -> Any:
    from sealedlore.providers.images import ImageModelInfo

    return ImageModelInfo(
        id=model_id,
        name=name,
        resolutions=native_sizes(model_id),
        max_inputs=pictures,
        max_outputs=4,
        # Nothing a picture on one's own computer or network; somewhere else
        # (a rented server) what it costs isn't the listing's to say.
        per_image={"*": 0.0} if local else {},
        description="",
    )


def is_local(base_url: str) -> bool:
    return is_local_host(urlparse(base_url).hostname or "")


def where_drawn(api: str, base_url: str) -> str:
    """Where a picture is drawn, for the dialogs: "on this computer" or "on
    your own network" for a server of one's own there; "" for anywhere else."""
    if api not in SERVICE_NAMES:
        return ""
    host = urlparse(base_url).hostname or ""
    if is_loopback_host(host):
        return "on this computer"
    return "on your own network" if is_local_host(host) else ""


# --- the key --------------------------------------------------------------------


def auth_headers(api: str, key: str, base_url: str) -> dict[str, str]:
    """The Authorization for a server of one's own: AUTOMATIC1111's
    `--api-auth` is HTTP Basic, written "user:password" as the key; any
    other key (a proxy in front of ComfyUI) a bearer token."""
    if not key:
        return {}
    whose = urlparse(base_url).hostname or "this server"
    key = checked_key(key, whose)
    if api == "a1111" and ":" in key:
        token = base64.b64encode(key.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": f"Bearer {key}"}


# --- workflows ------------------------------------------------------------------


def read_workflow(text: str) -> dict[str, Any]:
    """A workflow of the author's, checked to be one ComfyUI's API runs."""
    text = text.strip()
    if not text:
        raise WorkflowProblem("the workflow is empty")
    if len(text) > WORKFLOW_MAX_CHARS:
        raise WorkflowProblem("the workflow is far larger than any ComfyUI workflow")
    try:
        data = outside_json(text)
    except ValueError as exc:
        raise WorkflowProblem(f"the workflow isn't JSON ({exc})") from None
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        raise WorkflowProblem(
            "this is the workflow as ComfyUI saves it for its editor, not for its API: "
            "in ComfyUI use Workflow → Export (API) (with dev mode on in older versions, "
            "Save (API Format)) and choose that file"
        )
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
        data = data["prompt"]  # a /prompt request body saved whole
    if not isinstance(data, dict) or not data:
        raise WorkflowProblem("the workflow holds no nodes")
    for node_id, node in data.items():
        if not (
            isinstance(node, dict)
            and isinstance(node.get("class_type"), str)
            and isinstance(node.get("inputs"), dict)
        ):
            raise WorkflowProblem(f"node {node_id} in the workflow has no class_type or inputs")
    return data


def placeholders(workflow: dict[str, Any]) -> list[str]:
    """The placeholders in a workflow, in the order PLACEHOLDERS names them;
    any it doesn't know are kept after, for the settings to point out."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str):
            found.update(m.group(1) for m in _SLOT.finditer(value))
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for node in workflow.values():
        visit(node.get("inputs"))
    known = [name for name in PLACEHOLDERS if name in found]
    return known + sorted(found - set(PLACEHOLDERS))


def pictures_taken(workflow: dict[str, Any]) -> int:
    """How many pictures a workflow can be given: its {{image}}, {{image2}}…"""
    names = set(placeholders(workflow))
    count = 0
    for n in range(1, MAX_WORKFLOW_PICTURES + 1):
        if ("image" if n == 1 else f"image{n}") in names:
            count = n
        else:
            break
    return count


def fill(workflow: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """The workflow with each placeholder replaced: a string that is only a
    placeholder takes the value as it is (a number stays a number), one
    inside other text its text. A placeholder with no value is left: ComfyUI
    then says which node it is in."""

    def filled(value: Any) -> Any:
        if isinstance(value, str):
            whole = _SLOT.fullmatch(value.strip())
            if whole and whole.group(1) in values:
                return values[whole.group(1)]
            return _SLOT.sub(
                lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), value
            )
        if isinstance(value, dict):
            return {key: filled(item) for key, item in value.items()}
        if isinstance(value, list):
            return [filled(item) for item in value]
        return value

    return {
        node_id: {**node, "inputs": filled(node.get("inputs", {}))}
        for node_id, node in workflow.items()
    }


def builtin_workflow(*, with_picture: bool) -> dict[str, Any]:
    """ComfyUI's own default graph, to draw from words with any ordinary
    checkpoint, or from a picture (scaled to the size asked for, then
    sampled at `strength`)."""
    graph: dict[str, Any] = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "{{model}}"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "clip": ["1", 1]}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "{{negative_prompt}}", "clip": ["1", 1]},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": "{{seed}}",
                "steps": "{{steps}}",
                "cfg": "{{cfg}}",
                "sampler_name": "{{sampler_name}}",
                "scheduler": "{{scheduler}}",
                "denoise": "{{strength}}" if with_picture else 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["10" if with_picture else "4", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}},
    }
    if with_picture:
        graph["8"] = {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}}
        graph["9"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["8", 0],
                "upscale_method": "lanczos",
                "width": "{{width}}",
                "height": "{{height}}",
                "crop": "center",
            },
        }
        graph["11"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
        graph["10"] = {
            "class_type": "RepeatLatentBatch",
            "inputs": {"samples": ["11", 0], "amount": "{{batch}}"},
        }
    else:
        graph["4"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": "{{width}}", "height": "{{height}}", "batch_size": "{{batch}}"},
        }
    return graph


def workflow_for(settings: LocalSettings, *, with_picture: bool) -> tuple[dict[str, Any], bool]:
    """The workflow a request runs, and whether it is the author's own."""
    text = settings.workflow_edit if with_picture else settings.workflow
    if text.strip():
        return read_workflow(text), True
    return builtin_workflow(with_picture=with_picture), False


# The " [e9476a1372]" AUTOMATIC1111 adds to a checkpoint's title once it has
# hashed it.
_HASH_SUFFIX = re.compile(r"\s*\[[0-9a-f]{8,}\]$")


def checkpoint_name(title: str) -> str:
    """An AUTOMATIC1111 checkpoint by its file's own name, as it stays: its
    title gains " [hash]" once the server has hashed it (live, Forge, after
    the first picture drawn with it), and a model chosen before then would
    no longer be found. The server takes a checkpoint by its file name as by
    its title."""
    return _HASH_SUFFIX.sub("", title.strip())


def _model_slot(workflow: dict[str, Any]) -> tuple[str, str] | None:
    """The node and input a workflow takes its model at ({{model}}), so the
    server can say which models it has for it."""
    for node in workflow.values():
        for name, value in node.get("inputs", {}).items():
            slot = _SLOT.fullmatch(value.strip()) if isinstance(value, str) else None
            if slot is not None and slot.group(1) == "model":
                return node["class_type"], name
    return None


# --- listing --------------------------------------------------------------------


def list_models(
    api: str,
    base_url: str,
    key: str,
    settings: LocalSettings,
    *,
    get: Callable[[str], httpx.Response],
) -> list[Any]:
    """The server's checkpoints as picture models. `get` fetches a URL with
    the server's Authorization (the listing's client, or the job's).
    Raises ProviderError, in words, when the server can't say."""
    root = base_url.rstrip("/")
    local = is_local(base_url)
    if api == "a1111":
        body = _json(get(f"{root}/sdapi/v1/sd-models"), "AUTOMATIC1111 / Forge", hint=_A1111_HINT)
        names = [
            checkpoint_name(str(item.get("title") or item.get("model_name")))
            for item in (body if isinstance(body, list) else ())
            if isinstance(item, dict) and (item.get("title") or item.get("model_name"))
        ]
        return [_info(name, name, 1, local) for name in dict.fromkeys(names)]
    # ComfyUI: the models the workflow drawing from words takes, at its
    # {{model}}; a workflow of the author's with none draws with its own.
    words, _own = workflow_for(settings, with_picture=False)
    picture_flow, own_edit = workflow_for(settings, with_picture=True)
    slot = _model_slot(words)
    # The built-in way from a picture needs a checkpoint, which a workflow
    # naming its own model doesn't give it.
    takes = pictures_taken(picture_flow) if (slot is not None or own_edit) else 0
    if slot is None:
        return [_info(WORKFLOW_MODEL, "Your workflow (the model it names)", takes, local)]
    names = comfy_options(get, root, *slot)
    return [_info(name, name, takes, local) for name in names]


def comfy_options(
    get: Callable[[str], httpx.Response], root: str, node: str, field: str
) -> list[str]:
    """The choices ComfyUI offers for a node's input (its checkpoints, say)."""
    body = _json(get(f"{root}/object_info/{node}"), "ComfyUI", hint=_COMFY_HINT)
    try:
        spec = body[node]["input"]
        entry = (spec.get("required") or {}).get(field) or (spec.get("optional") or {}).get(field)
    except (KeyError, TypeError, AttributeError):
        entry = None
    if not isinstance(entry, list) or not entry:
        raise ProviderError(f"ComfyUI has no {node} node with an input {field}")
    choices = entry[0]
    if choices == "COMBO" and len(entry) > 1 and isinstance(entry[1], dict):
        choices = entry[1].get("options")  # the newer way ComfyUI writes a choice
    return [str(c) for c in choices or () if isinstance(c, (str, int, float))]


_A1111_HINT = "is it running, started with --api?"
_COMFY_HINT = "is ComfyUI running?"


def _json(response: httpx.Response, service: str, *, hint: str) -> Any:
    if response.status_code == 404:
        raise ProviderError(f"{service} answered 404 at {_where(response)}: {hint}")
    if response.status_code == 401:
        raise ProviderError(
            f"{service} refused the request (HTTP 401): put its user name and password "
            "in the API key field, written user:password"
        )
    if response.status_code >= 400:
        raise ProviderError(
            f"{service} answered HTTP {response.status_code}: {response.text[:300]}",
            status_code=response.status_code,
        )
    try:
        return reply_json(response)
    except ValueError as exc:
        raise ProviderError(
            f"{service} sent something that isn't JSON from {_where(response)}"
        ) from exc


def _where(response: httpx.Response) -> str:
    try:
        return str(response.request.url).split("?", 1)[0]
    except RuntimeError:
        return "its address"


# --- drawing --------------------------------------------------------------------


class LocalDrawing:
    """One request to a server of the author's own, on the picture job's
    `MediaHttp` (so Stop reaches it at once)."""

    def __init__(
        self, api: str, http: MediaHttp, base_url: str, key: str, settings: LocalSettings
    ) -> None:
        self.api = api
        self.http = http
        self.base_url = base_url
        self.root = base_url.rstrip("/")
        self.key = key
        self.settings = settings
        self._headers = auth_headers(api, key, base_url)
        self._prompt_ids: list[str] = []
        self.service = SERVICE_NAMES[api]

    # The HTTP, with the server's own Authorization.

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(self._headers)
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        try:
            return self.http.request(method, url, headers=headers, **kwargs)
        except StreamCancelled:
            raise
        except ProviderError as exc:
            raise self._said(exc) from exc

    def _said(self, exc: ProviderError) -> ProviderError:
        """A server's refusal, or its silence, in words the author can act on."""
        if isinstance(exc, MediaTimedOut):
            return exc
        text = str(exc)
        if exc.status_code is None and " failed: " in text:
            hint = _A1111_HINT if self.api == "a1111" else _COMFY_HINT
            return ProviderError(f"Nothing answered at {self.root}: {hint} ({text})")
        if exc.status_code == 404 and self.api == "a1111":
            return ProviderError(f"AUTOMATIC1111 / Forge answered 404: {_A1111_HINT}")
        if exc.status_code == 401:
            return ProviderError(
                f"{self.service} refused the request (HTTP 401): put its user name and "
                "password in the API key field, written user:password"
            )
        if self.api == "comfyui" and exc.status_code == 400 and exc.body:
            return ProviderError(f"ComfyUI couldn't run the workflow: {comfy_errors(exc.body)}")
        return exc

    def _get(self, url: str) -> httpx.Response:
        try:
            return self.http.request("GET", url, headers=dict(self._headers))
        except ProviderError as exc:
            if isinstance(exc, StreamCancelled):
                raise
            if exc.status_code is not None:
                # The listing's own words for a refusal (404, 401…).
                return httpx.Response(
                    exc.status_code,
                    content=(exc.body or "").encode("utf-8"),
                    request=httpx.Request("GET", url),
                )
            raise self._said(exc) from exc

    def check_model(self, model: str, *, with_picture: bool) -> None:
        """Refuse, in words, a model the server hasn't got."""
        if self.api == "comfyui":
            workflow, _own = workflow_for(self.settings, with_picture=with_picture)
            slot = _model_slot(workflow)
            if slot is None:
                return  # the workflow names its own
            if model == WORKFLOW_MODEL:
                raise ProviderError(
                    "The workflow for "
                    + ("a picture sent with the prompt" if with_picture else "drawing from words")
                    + " asks for a model ({{model}}), but the one chosen is your workflow's "
                    "own. Give both workflows a {{model}}, or neither (Settings → Images)."
                )
            names = comfy_options(self._get, self.root, *slot)
        else:
            names = [
                info.id
                for info in list_models(
                    self.api, self.base_url, self.key, self.settings, get=self._get
                )
            ]
        if checkpoint_name(model) not in {checkpoint_name(name) for name in names}:
            listed = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
            raise ProviderError(
                f"{self.service} has no model called {model!r}. Choose one of its own in "
                "Settings → Images (Choose beside each model lists them)"
                + (f": {listed}." if listed else ".")
            )

    def draw(self, payload: dict[str, Any]) -> tuple[list[bytes], dict[str, Any]]:
        """The pictures and what to keep of the reply."""
        model = str(payload.get("model") or "")
        references = [str(url) for url in payload.get("images") or ()]
        self.check_model(model, with_picture=bool(references))
        try:
            if self.api == "a1111":
                return self._a1111(payload, model, references)
            return self._comfy(payload, model, references)
        except StreamCancelled:
            self.stop()
            raise

    # AUTOMATIC1111 / Forge.

    def _a1111(
        self, payload: dict[str, Any], model: str, references: list[str]
    ) -> tuple[list[bytes], dict[str, Any]]:
        width, height = dimensions(str(payload.get("size") or ""), model)
        n = max(1, int(payload.get("n") or 1))
        body: dict[str, Any] = {
            **A1111_DEFAULTS,
            "prompt": payload.get("prompt", ""),
            "width": width,
            "height": height,
            "batch_size": n,
            "n_iter": 1,
            "seed": -1,
            "send_images": True,
            "save_images": False,
            "override_settings": {"sd_model_checkpoint": checkpoint_name(model)},
            # Kept loaded: put back after every request, the next one would
            # load it again (a checkpoint takes seconds to minutes).
            "override_settings_restore_afterwards": False,
        }
        body.update(self.settings.extra)
        path = "txt2img"
        if references:
            path = "img2img"
            body["init_images"] = [_base64_of(references[0])]
            body.setdefault("denoising_strength", self.settings.strength)
            body.setdefault("resize_mode", 1)  # crop and resize to the size asked
        reply = self._request("POST", f"{self.root}/sdapi/v1/{path}", json=body, makes=True)
        data = _reply_dict(reply, self.service)
        images = []
        for item in data.get("images") or ():
            if isinstance(item, str):
                decoded = _decode(item)
                if decoded:
                    images.append(decoded)
        # Extensions (ControlNet's maps) add pictures after the batch.
        images = images[:n]
        if not images:
            raise ProviderError(f"{self.service} sent no picture back")
        kept: dict[str, Any] = {"service": "a1111", "size": f"{width}x{height}"}
        info = data.get("info")
        if isinstance(info, str):
            try:
                parsed = outside_json(info)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("all_seeds"), list):
                kept["seeds"] = parsed["all_seeds"][:n]
        return images, kept

    # ComfyUI.

    def _comfy(
        self, payload: dict[str, Any], model: str, references: list[str]
    ) -> tuple[list[bytes], dict[str, Any]]:
        workflow, own = workflow_for(self.settings, with_picture=bool(references))
        names = set(placeholders(workflow))
        width, height = dimensions(str(payload.get("size") or ""), model)
        n = max(1, int(payload.get("n") or 1))
        extra = {**COMFY_DEFAULTS, **self.settings.extra}
        values: dict[str, Any] = {
            "prompt": payload.get("prompt", ""),
            "negative_prompt": str(extra.get("negative_prompt") or ""),
            "width": width,
            "height": height,
            "model": model,
            "steps": extra["steps"],
            "cfg": extra["cfg"],
            "sampler_name": extra["sampler_name"],
            "scheduler": extra["scheduler"],
            "strength": self.settings.strength,
            "batch": n,
        }
        for index, url in enumerate(references[:MAX_WORKFLOW_PICTURES], start=1):
            values["image" if index == 1 else f"image{index}"] = self._upload(url, index)
        # A workflow with no {{batch}} draws one picture a run: run it n times.
        runs = 1 if "batch" in names else n
        seeds: list[int] = []
        images: list[bytes] = []
        for _ in range(runs):
            seed = secrets.randbelow(2**32)
            seeds.append(seed)
            prompt_id = self._queue(fill(workflow, {**values, "seed": seed}))
            images += self._collect(prompt_id)
        if not images:
            raise ProviderError(
                "ComfyUI finished the workflow but it made no picture: it needs a "
                "SaveImage or PreviewImage node"
            )
        kept = {
            "service": "comfyui",
            "workflow": "yours" if own else "built-in",
            "size": f"{width}x{height}",
            "seeds": seeds,
        }
        return images, kept

    def _upload(self, url: str, index: int) -> str:
        data, media_type = _data_url_bytes(url)
        ext = media_type.split("/")[-1] or "png"
        name = f"sealedlore-{secrets.token_hex(8)}-{index}.{ext}"
        reply = self._request(
            "POST",
            f"{self.root}/upload/image",
            files={"image": (name, data, media_type)},
            data={"type": "input", "overwrite": "true"},
            makes=True,
        )
        body = _reply_dict(reply, self.service)
        stored = str(body.get("name") or name)
        sub = str(body.get("subfolder") or "")
        return f"{sub}/{stored}" if sub else stored

    def _queue(self, workflow: dict[str, Any]) -> str:
        client_id = secrets.token_hex(8)
        reply = self._request(
            "POST",
            f"{self.root}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            makes=True,
        )
        body = _reply_dict(reply, self.service)
        errors = body.get("node_errors")
        if errors:
            raise ProviderError(
                f"ComfyUI couldn't run the workflow: {comfy_errors(json.dumps(body))}"
            )
        prompt_id = body.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ProviderError("ComfyUI queued the workflow but gave it no id")
        self._prompt_ids.append(prompt_id)
        return prompt_id

    def _collect(self, prompt_id: str) -> list[bytes]:
        waited = 0.0
        while True:
            body = _reply_dict(
                self._request("GET", f"{self.root}/history/{prompt_id}"), self.service
            )
            entry = body.get(prompt_id)
            if isinstance(entry, dict):
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                if status.get("status_str") == "error":
                    raise ProviderError(f"ComfyUI stopped with an error: {_comfy_failure(status)}")
                outputs = entry.get("outputs")
                if isinstance(outputs, dict) and (outputs or status.get("completed")):
                    self._prompt_ids.remove(prompt_id)
                    return [self._view(item) for item in _output_images(outputs)]
            if waited > self.http.timeout_seconds:
                raise MediaTimedOut("ComfyUI: no picture after the time allowed")
            self.http.wait(POLL_SECONDS)
            waited += POLL_SECONDS

    def _view(self, item: dict[str, Any]) -> bytes:
        params = {
            "filename": str(item.get("filename") or ""),
            "subfolder": str(item.get("subfolder") or ""),
            "type": str(item.get("type") or "output"),
        }
        return self._request("GET", f"{self.root}/view", params=params).content

    # Stopping.

    def stop(self) -> None:
        """Ask the server to stop drawing what was asked for: quick, and never
        raising, since the picture is given up either way. On the job's own
        client, past its Stop (which shut the socket of the request it was
        waiting on, and refuses any more through `MediaHttp`)."""
        client = self.http._get_client()
        headers = {**self._headers, "Content-Type": "application/json"}
        quick = httpx.Timeout(STOP_SECONDS)
        try:
            if self.api == "a1111":
                client.post(f"{self.root}/sdapi/v1/interrupt", headers=headers, timeout=quick)
                return
            pending = list(self._prompt_ids)
            if not pending:
                return
            client.post(
                f"{self.root}/queue", json={"delete": pending}, headers=headers, timeout=quick
            )
            queue = client.get(f"{self.root}/queue", headers=self._headers, timeout=quick)
            running = reply_json(queue).get("queue_running") if queue.status_code < 400 else []
            # ComfyUI's /interrupt stops whatever is running, someone else's
            # too on a shared server: only while it is ours.
            if any(
                isinstance(item, list) and len(item) > 1 and item[1] in pending
                for item in running or ()
            ):
                client.post(
                    f"{self.root}/interrupt",
                    json={"prompt_id": pending[0]},
                    headers=headers,
                    timeout=quick,
                )
        except (httpx.HTTPError, ProviderError, ValueError, AttributeError, OSError):
            pass


def _output_images(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    """The pictures a finished workflow made: those it saved, else those it
    only previewed (a workflow with both gives each picture twice)."""
    found: list[dict[str, Any]] = []
    for node in outputs.values():
        if isinstance(node, dict):
            found += [item for item in node.get("images") or () if isinstance(item, dict)]
    saved = [item for item in found if item.get("type") == "output"]
    return saved or found


def comfy_errors(body: str) -> str:
    """ComfyUI's refusal of a workflow (its /prompt 400), in a line or two."""
    try:
        data = outside_json(body)
    except ValueError:
        return body[:300]
    if not isinstance(data, dict):
        return body[:300]
    said: list[str] = []
    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        said.append(str(error["message"]))
    for node_id, node in (data.get("node_errors") or {}).items():
        if not isinstance(node, dict):
            continue
        for item in node.get("errors") or ():
            if isinstance(item, dict):
                detail = f": {item['details']}" if item.get("details") else ""
                said.append(
                    f"{node.get('class_type', 'node')} {node_id}: {item.get('message')}{detail}"
                )
    return "; ".join(said)[:600] or body[:300]


def _comfy_failure(status: dict[str, Any]) -> str:
    for message in status.get("messages") or ():
        if isinstance(message, list) and len(message) == 2 and message[0] == "execution_error":
            data = message[1] if isinstance(message[1], dict) else {}
            return f"{data.get('node_type', 'a node')}: {data.get('exception_message', '')}".strip()
    return "it didn't say why"


def _reply_dict(response: httpx.Response, service: str) -> dict[str, Any]:
    try:
        body = reply_json(response)
    except ValueError as exc:
        raise ProviderError(f"{service} sent something that isn't JSON") from exc
    if not isinstance(body, dict):
        raise ProviderError(f"{service} sent {type(body).__name__}, not what was asked")
    return body


def _data_url_bytes(url: str) -> tuple[bytes, str]:
    if not url.startswith("data:"):
        raise ProviderError("a picture for a server of your own must be sent with the request")
    head, _, encoded = url.partition(",")
    media_type = head[5:].split(";", 1)[0] or "image/png"
    return base64.b64decode(encoded), media_type


def _base64_of(url: str) -> str:
    """A picture as AUTOMATIC1111 takes it: its base64 alone."""
    return url.partition(",")[2] if url.startswith("data:") else url


def _decode(encoded: str) -> bytes | None:
    if encoded.startswith("data:"):
        encoded = encoded.partition(",")[2]
    try:
        return base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        return None
