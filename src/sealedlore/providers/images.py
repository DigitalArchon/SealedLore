"""Client for an OpenAI-compatible image endpoint (nano-gpt's /images/generations).

One synchronous POST per request: the prompt, a size from the model's own
list, and reference pictures as data URLs in `imageDataUrls` (nano-gpt's
field; a model that takes images switches to editing them). The reply holds
each picture as base64 or a short-lived URL, and usually a `cost`.

`list_image_models` reads the endpoint's public `/image-models` listing: each
model's sizes, how many pictures it takes and makes, and its price per
picture by size. Measured Sept 2026 against nano-gpt.
"""

from __future__ import annotations

import base64
import socket
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from sealedlore.providers.base import ProviderError, StreamCancelled
from sealedlore.providers.http import make_client
from sealedlore.providers.openai_compat import (
    RETRY_ATTEMPTS,
    _retry_delay,
    _seconds,
    _shut_down,
)

# Seedream at 2k took ~20-60s live; a queue on the provider's side can add more.
IMAGE_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ImageModelInfo:
    id: str
    name: str
    resolutions: tuple[str, ...] = ()
    max_inputs: int = 0
    max_outputs: int = 1
    # USD per picture, by size.
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


def data_url(data: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def build_image_payload(
    model: str, prompt: str, size: str, n: int, references: list[str]
) -> dict[str, Any]:
    """The request body. `prompt` goes in exactly as given."""
    payload: dict[str, Any] = {
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
    if "imageDataUrls" in logged:
        logged["imageDataUrls"] = [f"<{name}>" for name in names]
    return logged


class ImageClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = IMAGE_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._cancelled = False
        self._socket: socket.socket | None = None
        self._wake = threading.Event()

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/images/generations"

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(
                self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
                # A fresh connection per request, so cancel() can find its socket.
                limits=httpx.Limits(max_keepalive_connections=0),
                # A signed picture URL may redirect; the guard in make_client
                # refuses one that would leave https.
                follow_redirects=True,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        if event_name not in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if sock is None:
            return
        with self._lock:
            self._socket = sock
            cancelled = self._cancelled
        if cancelled:
            _shut_down(sock)

    def cancel(self) -> None:
        """Stop waiting. The provider may already have made (and billed) the picture."""
        with self._lock:
            self._cancelled = True
            sock = self._socket
        self._wake.set()
        _shut_down(sock)

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def generate(self, payload: dict[str, Any]) -> ImageReply:
        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                return self._post(payload)
            except ProviderError as exc:
                delay = _retry_delay(exc)
                if delay is None or attempt == RETRY_ATTEMPTS:
                    raise
            if self._wake.wait(delay) or self._cancelled:
                raise StreamCancelled()
        raise AssertionError("unreachable")

    def _post(self, payload: dict[str, Any]) -> ImageReply:
        if self._cancelled:
            raise StreamCancelled()
        try:
            response = self._get_client().post(
                self.endpoint,
                json=payload,
                headers=self._headers(),
                extensions={"trace": self._trace},
            )
        except (httpx.HTTPError, OSError) as exc:
            if self._cancelled:
                raise StreamCancelled() from exc
            raise ProviderError(f"{self.endpoint} failed: {exc or type(exc).__name__}") from exc
        if response.status_code >= 400:
            error = ProviderError(
                f"{self.endpoint} returned HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
                body=response.text,
            )
            error.retry_after = _seconds(response.headers.get("retry-after"))
            raise error
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
        if isinstance(url, str) and url.startswith("https://"):
            # Signed and short-lived: fetched at once, never stored.
            try:
                reply = self._get_client().get(url)
            except httpx.HTTPError as exc:
                raise ProviderError(f"couldn't fetch the picture: {exc}") from exc
            if reply.status_code < 400:
                return reply.content
        return None


def list_image_models(
    base_url: str, *, client: httpx.Client | None = None, timeout_seconds: float = 30.0
) -> list[ImageModelInfo]:
    """The endpoint's image models, from its public listing. Empty if it has none."""
    url = base_url.rstrip("/") + "/image-models"
    owned = client is None
    http = client or make_client(base_url, timeout=httpx.Timeout(timeout_seconds))
    try:
        response = http.get(url, params={"detailed": "true"})
        if response.status_code >= 400:
            return []
        return parse_image_models(response.json())
    except (httpx.HTTPError, ValueError):
        return []
    finally:
        if owned:
            http.close()
