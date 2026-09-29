"""WaveSpeed's REST API (https://api.wavespeed.ai/api/v3), pictures and video.

Every model is its own path: `POST {base}/{model id}` with the model's own
fields answers at once with a prediction id, and
`GET {base}/predictions/{id}/result` says how it went, with `outputs` (URLs)
once `completed`. Pictures sent as input are URLs: they go up first through
`POST {base}/media/upload/binary`. `GET {base}/models` lists every model with
its type, base price and request schema (a key is needed), and
`POST {base}/model/price` prices one request before it is sent.

Built from WaveSpeed's documentation (Sept 2026); not yet checked live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sealedlore.providers.base import ProviderError
from sealedlore.providers.media_http import MediaHttp

DONE = "completed"
FAILED = frozenset({"failed", "cancelled", "timeout", "deleted"})


@dataclass(frozen=True)
class Prediction:
    id: str
    status: str
    outputs: tuple[str, ...] = ()
    error: str = ""


def _data(body: Any, what: str) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise ProviderError(f"WaveSpeed's {what} reply had no data", body=str(body)[:2000])
    return data


def submit(http: MediaHttp, model: str, body: dict[str, Any]) -> Prediction:
    url = f"{http.base_url.rstrip('/')}/{model.strip('/')}"
    data = _data(http.json("POST", url, json=body, makes=True), "submit")
    if not isinstance(data.get("id"), str):
        raise ProviderError("WaveSpeed accepted the request but gave no id", body=str(data)[:2000])
    return _prediction(data)


def result(http: MediaHttp, prediction_id: str) -> Prediction:
    url = f"{http.base_url.rstrip('/')}/predictions/{prediction_id}/result"
    return _prediction(_data(http.json("GET", url), "result"))


def _prediction(data: dict) -> Prediction:
    outputs = tuple(o for o in data.get("outputs") or () if isinstance(o, str))
    return Prediction(
        id=str(data.get("id") or ""),
        status=str(data.get("status") or "").lower(),
        outputs=outputs,
        error=str(data.get("error") or ""),
    )


def upload(http: MediaHttp, data: bytes, name: str, media_type: str) -> str:
    """Put a picture where the model can fetch it; its URL."""
    url = f"{http.base_url.rstrip('/')}/media/upload/binary"
    headers = {"Authorization": f"Bearer {http.api_key}"} if http.api_key else {}
    body = http.json("POST", url, files={"file": (name, data, media_type)}, headers=headers)
    link = _data(body, "upload").get("download_url")
    if not isinstance(link, str) or not link.startswith("https://"):
        raise ProviderError("WaveSpeed's upload gave no https link", body=str(body)[:2000])
    return link


def price(http: MediaHttp, model: str, inputs: dict[str, Any]) -> float | None:
    """What WaveSpeed says this request will cost. Its reply carries a price
    and a discounted price whose meaning the docs leave unclear (their own
    example discounts $0.45 to $0.00), so the higher is taken: a quote may
    overstate, never understate."""
    url = f"{http.base_url.rstrip('/')}/model/price"
    data = _data(http.json("POST", url, json={"model_id": model, "inputs": inputs}), "price")
    values = [
        float(data[key])
        for key in ("price", "discounted_price")
        if isinstance(data.get(key), (int, float))
    ]
    return max(values) if values else None


def models(http: MediaHttp) -> list[dict]:
    body = http.json("GET", f"{http.base_url.rstrip('/')}/models")
    items = body.get("data") if isinstance(body, dict) else None
    return [item for item in items or () if isinstance(item, dict)]


def request_schema(item: dict) -> dict[str, dict]:
    """A listed model's request fields: name → JSON schema."""
    api_schema = item.get("api_schema")
    schemas = api_schema.get("api_schemas") if isinstance(api_schema, dict) else None
    for schema in schemas or ():
        if not isinstance(schema, dict):
            continue
        request = schema.get("request_schema")
        props = request.get("properties") if isinstance(request, dict) else None
        if isinstance(props, dict):
            return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}
