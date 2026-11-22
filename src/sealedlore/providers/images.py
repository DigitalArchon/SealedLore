"""Clients for picture endpoints: NanoGPT (and other OpenAI-compatible
`/images/generations`), OpenRouter's `/images`, and WaveSpeed.

NanoGPT: one synchronous POST per request: the prompt, a size from the
model's own list, and reference pictures as data URLs in `imageDataUrls`
(nano-gpt's field; a model that takes images switches to editing them). The
reply holds each picture as base64 or a short-lived URL, and usually a
`cost`. `list_image_models` reads its public `/image-models` listing: each
model's sizes, how many pictures it takes and makes, and its price per
picture by size. Measured Sept 2026 against nano-gpt.

OpenRouter: `POST /images` with `aspect_ratio` or `resolution`, references
as `input_references` image parts; base64 back (`b64_json` and its
`media_type`), cost in `usage.cost`. Its public `/images/models` gives each
model's parameters; prices are per endpoint (`/images/models/{id}/endpoints`),
fetched for the chosen model. Run live Nov 15 2026: a picture from words and
an edit with a reference (Gemini 3.1 Flash Lite Image, $0.034 each). Only an
endpoint the account's data policy allows serves a model: with zero data
retention on, most picture models answered 404 ("ZDR violation").

WaveSpeed (`providers/wavespeed.py`): a prediction, polled; references are
uploaded first and sent as URLs. Built from its documentation (Sept 2026)
and not yet checked live.

ComfyUI and AUTOMATIC1111 / Forge (`providers/local_images.py`): a server of
the author's own, its checkpoints listed as models, nothing charged.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from sealedlore.call_log import CALLS, MEDIA_SERVICES, PICTURE, plain_protection
from sealedlore.models.config import LOCAL_IMAGE_APIS, media_api
from sealedlore.providers import wavespeed
from sealedlore.providers.base import ProviderError, StreamCancelled
from sealedlore.providers.http import make_client, reply_json
from sealedlore.providers.local_images import (
    LocalDrawing,
    LocalSettings,
    auth_headers,
    is_local,
    list_models,
)
from sealedlore.providers.media_http import MediaHttp, MediaTimedOut
from sealedlore.providers.privacy import MEDIA_WAY_OUT, plain_refusal

# How long a picture is waited for. NanoGPT's endpoint is synchronous and
# sends nothing until the picture is done: Seedream 5.0 Pro took 109-150 s
# live, and once past five minutes, when the old limit gave up and the
# picture was made and charged for anyway (Oct 2026). Stop still ends the
# wait at once (MediaHttp.cancel), so a long limit costs nothing.
IMAGE_TIMEOUT_SECONDS = 1200.0
# How often a WaveSpeed picture is asked after.
POLL_SECONDS = 2.0


@dataclass(frozen=True)
class ImageModelInfo:
    id: str
    name: str
    resolutions: tuple[str, ...] = ()
    max_inputs: int = 0
    max_outputs: int = 1
    # USD per picture, by size ("*": whatever the size).
    per_image: dict[str, float] = field(default_factory=dict)
    description: str = ""

    @property
    def accepts_images(self) -> bool:
        return self.max_inputs > 0

    def price(self, size: str) -> float | None:
        value = self.per_image.get(size)
        if value is None and self.per_image:
            # A size the listing prices under another name: the usual price.
            values = sorted(self.per_image.values())
            value = values[len(values) // 2]
        return value


@dataclass(frozen=True)
class ImageReply:
    images: list[bytes]
    raw: dict[str, Any]
    cost: float | None


def parse_image_models(body: Any) -> list[ImageModelInfo]:
    """NanoGPT's listing, and the shape the app keeps any listing in."""
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    models: list[ImageModelInfo] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        params = item.get("supported_parameters")
        params = params if isinstance(params, dict) else {}
        resolutions = params.get("resolutions")
        if isinstance(resolutions, dict):  # the typed form: {type, values, default}
            resolutions = resolutions.get("values")
        pricing = item.get("pricing")
        per_image = pricing.get("per_image") if isinstance(pricing, dict) else None
        models.append(
            ImageModelInfo(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                resolutions=tuple(r for r in resolutions or () if isinstance(r, str)),
                max_inputs=_int(params.get("max_input_images")),
                max_outputs=max(
                    1, _int(params.get("max_output_images") or params.get("max_images"))
                ),
                per_image={
                    str(size): float(price)
                    for size, price in (per_image or {}).items()
                    if isinstance(price, (int, float))
                },
                description=str(item.get("description") or ""),
            )
        )
    return models


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _enum(spec: Any) -> list[str]:
    """The values of an OpenRouter `{type: enum, values}` or a JSON schema enum."""
    if not isinstance(spec, dict):
        return []
    values = spec.get("values") if "values" in spec else spec.get("enum")
    return [str(v) for v in values or () if isinstance(v, (str, int, float))]


def _range_max(spec: Any) -> int:
    if not isinstance(spec, dict):
        return 0
    return _int(spec.get("max") if "max" in spec else spec.get("maxItems"))


def parse_openrouter_image_models(body: Any) -> list[ImageModelInfo]:
    """OpenRouter's `/images/models`. A size is an aspect ratio or a
    resolution tier, as on NanoGPT: whichever is chosen is sent, the other
    left to the model."""
    items = body.get("data") if isinstance(body, dict) else body
    models: list[ImageModelInfo] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        params = item.get("supported_parameters")
        params = params if isinstance(params, dict) else {}
        tiers = _enum(params.get("resolution"))
        sizes = [r for r in _enum(params.get("aspect_ratio")) if r != "auto"] + tiers
        models.append(
            ImageModelInfo(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                resolutions=tuple(sizes),
                max_inputs=_range_max(params.get("input_references")),
                max_outputs=max(1, _range_max(params.get("n"))),
                per_image=openrouter_image_prices(item, tiers),
                description=str(item.get("description") or ""),
            )
        )
    return models


def openrouter_image_prices(item: dict, tiers: Sequence[str] = ()) -> dict[str, float]:
    """A model's price a picture from its endpoints record (the dearest
    endpoint's, since the router may pick any), by resolution tier where it
    is priced by tier. Live (Nov 15 2026) OpenRouter prices a picture by the
    picture ("image"), by the megapixel or by the image token: only the
    first is a price a picture, so a model priced otherwise has none (Gemini
    3.1 Flash Lite Image lists $0.00003 a token and charged $0.0336 for a
    1:1 picture; read as a picture's price it was a thousand times too low).
    A tier's price is keyed as the model lists the tier ("1K" for "1k")."""
    by_name = {tier.lower(): tier for tier in tiers}
    prices: dict[str, float] = {}
    for endpoint in item.get("endpoints") or ():
        if not isinstance(endpoint, dict):
            continue
        for entry in endpoint.get("pricing") or ():
            if not (
                isinstance(entry, dict)
                and entry.get("billable") == "output_image"
                and entry.get("unit", "image") == "image"
                and isinstance(entry.get("cost_usd"), (int, float))
                and not isinstance(entry.get("cost_usd"), bool)
            ):
                continue
            variant = entry.get("variant")
            if variant is None:
                size = "*"
            elif isinstance(variant, str) and variant.lower() in by_name:
                size = by_name[variant.lower()]
            else:
                continue  # priced by something no size here names
            prices[size] = max(prices.get(size, 0.0), float(entry["cost_usd"]))
    return prices


def parse_wavespeed_image_models(items: list[dict]) -> list[ImageModelInfo]:
    """WaveSpeed's `/models`, its picture models: sizes and reference
    limits from each one's request schema, price from `base_price`."""
    models: list[ImageModelInfo] = []
    for item in items:
        if item.get("type") not in ("text-to-image", "image-to-image"):
            continue
        model_id = item.get("model_id")
        if not isinstance(model_id, str):
            continue
        fields = wavespeed.request_schema(item)
        sizes = _enum(fields.get("size")) or _enum(fields.get("aspect_ratio"))
        refs = fields.get("images") or fields.get("image")
        max_inputs = _range_max(refs) or (1 if isinstance(refs, dict) else 0)
        price = item.get("base_price")
        models.append(
            ImageModelInfo(
                id=model_id,
                name=str(item.get("name") or model_id),
                resolutions=tuple(sizes),
                max_inputs=max_inputs,
                max_outputs=1,
                per_image={"*": float(price)} if isinstance(price, (int, float)) else {},
                description=str(item.get("description") or ""),
            )
        )
    return models


def data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def build_image_payload(
    model: str,
    prompt: str,
    size: str,
    n: int,
    references: list[str],
    api: str = "nanogpt",
) -> dict[str, Any]:
    """The request body, in the endpoint's own shape. `prompt` goes in
    exactly as given."""
    if api == "openrouter":
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": n}
        if size:
            payload["aspect_ratio" if ":" in size else "resolution"] = size
        if references:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": url}} for url in references
            ]
        return payload
    if api == "wavespeed":
        payload = {"model": model, "prompt": prompt}
        if size:
            payload["aspect_ratio" if ":" in size else "size"] = size
        if references:
            payload["images"] = list(references)
        return payload
    if api in LOCAL_IMAGE_APIS:
        # What the request asks for, in the app's own terms: the server's
        # own request is made from it (providers/local_images.py).
        payload = {"model": model, "prompt": prompt, "size": size, "n": n}
        if references:
            payload["images"] = list(references)
        return payload
    payload = {
        "model": model,
        "prompt": prompt,
        "n": n,
        "size": size,
        "response_format": "b64_json",
    }
    if references:
        payload["imageDataUrls"] = list(references)
    return payload


def loggable_payload(payload: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """The payload for the API log: the prompt verbatim, pictures by name."""
    logged = dict(payload)
    for key in ("imageDataUrls", "input_references", "images"):
        if key in logged:
            logged[key] = [f"<{name}>" for name in names]
    return logged


class ImageClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api: str | None = None,
        timeout_seconds: float = IMAGE_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        key_withheld: str = "",
        sealed_api: str = "auto",
        local: LocalSettings | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.api = api or media_api(base_url)
        # A server of the author's own: its workflows and settings.
        self.local = local or LocalSettings()
        self.http = MediaHttp(
            base_url,
            api_key,
            timeout_seconds=timeout_seconds,
            client=client,
            key_withheld=key_withheld,
            sealed_api=sealed_api,
        )
        self._wake = self.http._wake

    @property
    def endpoint(self) -> str:
        if self.api == "openrouter":
            return self.base_url.rstrip("/") + "/images"
        return self.base_url.rstrip("/") + "/images/generations"

    def cancel(self) -> None:
        """Stop waiting. The provider may already have made (and billed) the picture."""
        self.http.cancel()

    def close(self) -> None:
        self.http.close()

    def generate(self, payload: dict[str, Any]) -> ImageReply:
        """The picture made, kept in the run's call log (call_log.py)."""
        call = CALLS.start(
            PICTURE,
            str(payload.get("model") or ""),
            endpoint=MEDIA_SERVICES.get(self.api, self.api),
            address=self.base_url,
            protection=plain_protection(self.base_url),
        )
        try:
            if self.api in LOCAL_IMAGE_APIS:
                reply = self._own_server(payload)
            elif self.api == "wavespeed":
                reply = self._wavespeed(payload)
            else:
                reply = self._made(payload)
        except StreamCancelled:
            CALLS.finish(call, "stopped")
            raise
        except Exception as exc:
            CALLS.finish(call, "failed", detail=str(exc).splitlines()[0] if str(exc) else "")
            raise
        count = len(reply.images)
        CALLS.finish(
            call, "done", detail=f"{count} picture{'s' if count != 1 else ''}", cost=reply.cost
        )
        return reply

    def _made(self, payload: dict[str, Any]) -> ImageReply:
        response = self.http.request("POST", self.endpoint, json=payload, makes=True)
        try:
            body = reply_json(response)
        except ValueError as exc:
            raise ProviderError(f"{self.endpoint} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise ProviderError(f"{self.endpoint} returned {type(body).__name__}")
        images = [self._image(item) for item in body.get("data") or [] if isinstance(item, dict)]
        images = [image for image in images if image]
        if not images:
            raise ProviderError("the image reply held no pictures", body=response.text[:2000])
        cost = body.get("cost")
        usage = body.get("usage")
        if not isinstance(cost, (int, float)) and isinstance(usage, dict):
            cost = usage.get("cost")
        raw = {key: value for key, value in body.items() if key != "data"}
        return ImageReply(
            images=images, raw=raw, cost=float(cost) if isinstance(cost, (int, float)) else None
        )

    def _own_server(self, payload: dict[str, Any]) -> ImageReply:
        """ComfyUI or AUTOMATIC1111 / Forge: nothing charged on this computer
        or its network; somewhere else, no price known."""
        if self.http.key_withheld:
            raise ProviderError(self.http.key_withheld)
        drawing = LocalDrawing(self.api, self.http, self.base_url, self.api_key, self.local)
        images, kept = drawing.draw(payload)
        return ImageReply(images=images, raw=kept, cost=0.0 if is_local(self.base_url) else None)

    def _wavespeed(self, payload: dict[str, Any]) -> ImageReply:
        body = dict(payload)
        model = str(body.pop("model", ""))
        refs = body.get("images")
        if refs:
            body["images"] = [self._uploaded(url, i) for i, url in enumerate(refs, start=1)]
        prediction = wavespeed.submit(self.http, model, body)
        waited = 0.0
        while prediction.status != wavespeed.DONE:
            if prediction.status in wavespeed.FAILED:
                raise ProviderError(
                    f"WaveSpeed: the picture {prediction.status}"
                    + (f": {prediction.error}" if prediction.error else "")
                )
            if waited > self.http.timeout_seconds:
                raise MediaTimedOut("WaveSpeed: no picture after the time allowed")
            self.http.wait(POLL_SECONDS)
            waited += POLL_SECONDS
            prediction = wavespeed.result(self.http, prediction.id)
        images = [self._fetch(url) for url in prediction.outputs]
        images = [image for image in images if image]
        if not images:
            raise ProviderError("the image reply held no pictures")
        return ImageReply(images=images, raw={"id": prediction.id}, cost=None)

    def _uploaded(self, url: str, index: int) -> str:
        if not url.startswith("data:"):
            return url
        head, _, encoded = url.partition(",")
        media_type = head[5:].split(";", 1)[0] or "image/png"
        ext = media_type.split("/")[-1]
        return wavespeed.upload(
            self.http, base64.b64decode(encoded), f"reference-{index}.{ext}", media_type
        )

    def _image(self, item: dict[str, Any]) -> bytes | None:
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            if encoded.startswith("data:"):
                encoded = encoded.partition(",")[2]
            try:
                return base64.b64decode(encoded)
            except ValueError:
                return None
        url = item.get("url")
        if isinstance(url, str):
            return self._fetch(url)
        return None

    def _fetch(self, url: str) -> bytes | None:
        """A picture at a short-lived https link: fetched at once, never
        stored, and never handed the key (it is someone's storage)."""
        if not url.startswith("https://"):
            return None
        try:
            reply = self.http.request("GET", url, authorised=False)
        except ProviderError as exc:
            raise ProviderError(f"couldn't fetch the picture: {exc}") from exc
        return reply.content


def list_image_models(
    base_url: str,
    *,
    api: str | None = None,
    api_key: str = "",
    client: httpx.Client | None = None,
    timeout_seconds: float = 30.0,
    sealed_api: str = "auto",
    wanted: str = "",
    local: LocalSettings | None = None,
    key_withheld: str = "",
) -> list[ImageModelInfo]:
    """The endpoint's picture models. Empty if it has none or won't say.
    `wanted`: a model whose prices are fetched too where they are per model
    (OpenRouter). `local`: a server of the author's own's workflows (the
    models a ComfyUI workflow takes are the ones offered). `key_withheld`:
    why the endpoint's key, or the password in its address, isn't sent
    (`ResolvedMedia.key_withheld`): nothing is asked then, as nothing is
    drawn. The address still holds the password, and httpx sends it."""
    if key_withheld:
        raise ProviderError(key_withheld)
    # Never from an end-to-end encrypted endpoint (providers/privacy.py),
    # by its host or what an endpoint at its address speaks (`sealed_api`,
    # `Config.sealed_api_at`): a plain listing would go to it in the clear,
    # WaveSpeed's with the key.
    refusal = plain_refusal(
        base_url, "the picture models' listing", api=sealed_api, way_out=MEDIA_WAY_OUT
    )
    if refusal:
        raise ProviderError(refusal)
    api = api or media_api(base_url)
    owned = client is None
    http = client or make_client(base_url, timeout=httpx.Timeout(timeout_seconds))
    root = base_url.rstrip("/")
    try:
        if api == "nanogpt":
            response = http.get(f"{root}/image-models", params={"detailed": "true"})
            return parse_image_models(reply_json(response)) if response.status_code < 400 else []
        if api == "openrouter":
            response = http.get(f"{root}/images/models")
            if response.status_code >= 400:
                return []
            body = reply_json(response)
            items = body.get("data") if isinstance(body, dict) else body
            for item in items if isinstance(items, list) else ():
                if isinstance(item, dict) and item.get("id") == wanted:
                    priced = http.get(f"{root}/images/models/{wanted}/endpoints")
                    if priced.status_code < 400 and isinstance(reply_json(priced), dict):
                        item["endpoints"] = reply_json(priced).get("endpoints") or []
            return parse_openrouter_image_models(items)
        if api in LOCAL_IMAGE_APIS:
            return list_own_models(base_url, api=api, api_key=api_key, client=http, local=local)
        if api == "wavespeed":
            media = MediaHttp(
                base_url,
                api_key,
                timeout_seconds=timeout_seconds,
                client=http,
                sealed_api=sealed_api,
            )
            return parse_wavespeed_image_models(wavespeed.models(media))
        return []
    except (httpx.HTTPError, ValueError, ProviderError):
        return []
    finally:
        if owned:
            http.close()


def list_own_models(
    base_url: str,
    *,
    api: str,
    api_key: str = "",
    client: httpx.Client | None = None,
    local: LocalSettings | None = None,
    timeout_seconds: float = 30.0,
    key_withheld: str = "",
) -> list[ImageModelInfo]:
    """A server of the author's own's checkpoints, as picture models. Raises
    ProviderError in words when it can't say (Settings → Images' Check),
    where `list_image_models` gives an empty list. `key_withheld`: as
    `list_image_models`; nothing is sent."""
    if key_withheld:
        raise ProviderError(key_withheld)
    owned = client is None
    http = client or make_client(base_url, timeout=httpx.Timeout(timeout_seconds))
    headers = auth_headers(api, api_key, base_url)
    try:
        return list_models(
            api,
            base_url,
            api_key,
            local or LocalSettings(),
            get=lambda url: http.get(url, headers=headers),
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Nothing answered at {base_url.rstrip('/')} ({exc})") from exc
    finally:
        if owned:
            http.close()
