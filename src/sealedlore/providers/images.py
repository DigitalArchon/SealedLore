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
as `input_references` image parts; base64 back, cost in `usage.cost`. Its
public `/images/models` gives each model's parameters; prices are per
endpoint (`/images/models/{id}/endpoints`), fetched for the chosen model.

WaveSpeed (`providers/wavespeed.py`): a prediction, polled; references are
uploaded first and sent as URLs.

OpenRouter and WaveSpeed were built from their documentation (Sept 2026)
and are not yet checked live.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import httpx

from sealedlore.models.config import media_api
from sealedlore.providers import wavespeed
from sealedlore.providers.base import ProviderError
from sealedlore.providers.http import make_client
from sealedlore.providers.media_http import MediaHttp

# Seedream at 2k took ~20-60s live; a queue on the provider's side can add more.
IMAGE_TIMEOUT_SECONDS = 300.0
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
        sizes = [r for r in _enum(params.get("aspect_ratio")) if r != "auto"] + _enum(
            params.get("resolution")
        )
        models.append(
            ImageModelInfo(
                id=item["id"],
                name=str(item.get("name") or item["id"]),
                resolutions=tuple(sizes),
                max_inputs=_range_max(params.get("input_references")),
                max_outputs=max(1, _range_max(params.get("n"))),
                per_image=openrouter_image_prices(item),
                description=str(item.get("description") or ""),
            )
        )
    return models


def openrouter_image_prices(item: dict) -> dict[str, float]:
    """A model's price per picture from its endpoints record (the dearest
    endpoint's, since the router may pick any)."""
    prices = [
        float(entry["cost_usd"])
        for endpoint in item.get("endpoints") or ()
        if isinstance(endpoint, dict)
        for entry in endpoint.get("pricing") or ()
        if isinstance(entry, dict)
        and entry.get("billable") == "output_image"
        and isinstance(entry.get("cost_usd"), (int, float))
    ]
    return {"*": max(prices)} if prices else {}


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
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.api = api or media_api(base_url)
        self.http = MediaHttp(base_url, api_key, timeout_seconds=timeout_seconds, client=client)
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
        if self.api == "wavespeed":
            return self._wavespeed(payload)
        response = self.http.request("POST", self.endpoint, json=payload, makes=True)
        try:
            body = response.json()
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
                raise ProviderError("WaveSpeed: no picture after the time allowed")
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
    wanted: str = "",
) -> list[ImageModelInfo]:
    """The endpoint's picture models. Empty if it has none or won't say.
    `wanted`: a model whose prices are fetched too where they are per model
    (OpenRouter)."""
    api = api or media_api(base_url)
    owned = client is None
    http = client or make_client(base_url, timeout=httpx.Timeout(timeout_seconds))
    root = base_url.rstrip("/")
    try:
        if api == "nanogpt":
            response = http.get(f"{root}/image-models", params={"detailed": "true"})
            return parse_image_models(response.json()) if response.status_code < 400 else []
        if api == "openrouter":
            response = http.get(f"{root}/images/models")
            if response.status_code >= 400:
                return []
            body = response.json()
            items = body.get("data") if isinstance(body, dict) else body
            for item in items if isinstance(items, list) else ():
                if isinstance(item, dict) and item.get("id") == wanted:
                    priced = http.get(f"{root}/images/models/{wanted}/endpoints")
                    if priced.status_code < 400 and isinstance(priced.json(), dict):
                        item["endpoints"] = priced.json().get("endpoints") or []
            return parse_openrouter_image_models(items)
        if api == "wavespeed":
            media = MediaHttp(base_url, api_key, timeout_seconds=timeout_seconds, client=http)
            return parse_wavespeed_image_models(wavespeed.models(media))
        return []
    except (httpx.HTTPError, ValueError, ProviderError):
        return []
    finally:
        if owned:
            http.close()
