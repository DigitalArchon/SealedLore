"""What a video will cost, worked out before it is sent. Pure.

No endpoint quotes a video before it is made except WaveSpeed (whose quote
is a network call, made by the dialog, not here), so the price comes from
the model's listing. The listings price in dozens of shapes; the ones read
here cover every Seedance model on NanoGPT and OpenRouter and most others.
A shape not read here gives no price at all, never a guess: the dialog then
says so and asks the author to accept an unknown price.

Checked live on NanoGPT (Sept 2026): Seedance 2.5 at 480p for 5 seconds
listed $0.18 a second, pre-charged $0.90, and the balance fell by $0.90;
4 seconds from a start frame, $0.72 as listed. But Seedance 2.0 Fast, whose
listing prices it in a "seedance20-turbo-dynamic" shape at $0.0706 a second
at 720p ($0.35 for 5 s), was pre-charged $0.60 for 5 s from a start and an
end frame. So none of the "raw" shapes is read: no price beats a wrong
one. Whatever the quote, the job records what the endpoint says it charged,
and says so when that is more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sealedlore.providers.videos import VideoModelInfo

# A picture from Seedream 5.0 Pro, the default image model, for the
# comparison the dialog makes ("about N pictures").
PICTURE_PRICE = 0.09
# Seedance's frame rate, for OpenRouter's per-token Seedance prices.
SEEDANCE_FPS = 24


@dataclass(frozen=True)
class VideoQuote:
    amount: float | None
    # How it was reached ("$0.18 a second × 5 s"), or why there is none.
    basis: str


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seconds(settings: dict[str, Any], pricing: dict) -> float | None:
    for key in ("duration", "seconds"):
        value = _float(settings.get(key))
        if value is not None:
            return value
    for key in ("default_duration", "defaultDuration"):
        value = _float(pricing.get(key))
        if value is not None:
            return value
    return None


def _resolution(settings: dict[str, Any], pricing: dict) -> str | None:
    value = settings.get("resolution")
    if value is None:
        value = pricing.get("default_resolution") or pricing.get("defaultResolution")
    return str(value) if value is not None else None


def _audio_on(info: VideoModelInfo, settings: dict[str, Any]) -> bool:
    key = info.audio_key
    return bool(settings.get(key)) if key is not None else False


def _per_second(rate: float, seconds: float, note: str = "") -> VideoQuote:
    extra = f" ({note})" if note else ""
    return VideoQuote(rate * seconds, f"${rate:.4g} a second × {seconds:g} s{extra}")


def quote_video(
    info: VideoModelInfo,
    settings: dict[str, Any],
    *,
    start_frame: bool = False,
    references: int = 0,
) -> VideoQuote:
    """The price of one video with these (cleaned) settings."""
    if info.api == "nanogpt":
        return _nanogpt(info, settings, start_frame=start_frame)
    if info.api in ("openrouter", "openai"):
        return _openrouter(info, settings, start_frame=start_frame)
    if info.api == "wavespeed":
        return VideoQuote(None, "WaveSpeed prices each request itself")
    return VideoQuote(None, "this endpoint lists no prices")


def _nanogpt(info: VideoModelInfo, settings: dict[str, Any], *, start_frame: bool) -> VideoQuote:
    pricing = info.pricing
    raw = pricing.get("raw") if isinstance(pricing.get("raw"), dict) else None
    source = raw if raw is not None else pricing
    seconds = _seconds(settings, source)
    resolution = _resolution(settings, source)
    audio = _audio_on(info, settings)

    def by_res(table: Any) -> float | None:
        if isinstance(table, dict) and resolution is not None:
            return _float(table.get(resolution))
        return None

    if raw is not None:
        # The "raw" shapes are NanoGPT's own formulas, and one of them (a
        # Seedance 2.0 Fast) said $0.35 for a video it charged $0.60.
        kind = str(raw.get("type") or "")
        return VideoQuote(None, f"its listing's price ({kind}) proved wrong live, so it isn't read")

    rate = by_res(pricing.get("per_second_by_resolution"))
    if rate is not None and seconds is not None:
        return _per_second(rate, seconds)
    table = pricing.get("image_to_video_per_second" if start_frame else "text_to_video_per_second")
    rate = by_res(table)
    if rate is not None and seconds is not None:
        return _per_second(rate, seconds)
    per_duration = pricing.get("per_duration")
    if isinstance(per_duration, dict) and seconds is not None:
        price = _float(per_duration.get(str(int(seconds))))
        if price is not None:
            multiplier = _float(pricing.get("audio_multiplier")) if audio else None
            if multiplier:
                basis = f"${price:g} a video × {multiplier:g} for audio"
                return VideoQuote(price * multiplier, basis)
            return VideoQuote(price, f"${price:g} a video of {seconds:g} s")
    if "high_resolution_per_second" not in pricing:
        rate = _float(pricing.get("per_second"))
        if rate is not None and seconds is not None:
            return _per_second(rate, seconds)
    price = _float(pricing.get("per_video"))
    if price is not None:
        return VideoQuote(price, f"${price:g} a video")
    price = by_res(pricing.get("per_resolution"))
    if price is not None and not pricing.get("frame_multiplier"):
        return VideoQuote(price, f"${price:g} a video at {resolution}")
    base = by_res(pricing.get("base_prices_by_resolution"))
    if base is not None and seconds is not None:
        overrides = pricing.get("duration_overrides")
        override = overrides.get(str(int(seconds))) if isinstance(overrides, dict) else None
        if isinstance(override, dict) and _float(override.get(resolution)) is not None:
            base = _float(override.get(resolution))
            return VideoQuote(base, f"${base:g} a video")
        multipliers = pricing.get("duration_multipliers")
        if isinstance(multipliers, dict):
            factor = _float(multipliers.get(str(int(seconds))))
            if factor is not None:
                return VideoQuote(base * factor, f"${base:g} × {factor:g} for {seconds:g} s")
        factor = _float(pricing.get("duration_multiplier"))
        durations = sorted(
            int(v)
            for v, _ in (info.param("duration").options if info.param("duration") else ())
            if str(v).isdigit()
        )
        if factor is not None and durations:
            # The shortest duration is the base; the longest costs `factor` times.
            scale = 1.0 if seconds <= durations[0] else factor
            return VideoQuote(base * scale, f"${base:g} × {scale:g} for {seconds:g} s")
    price = _float(pricing.get("base_price"))
    base_seconds = _float(pricing.get("base_duration"))
    extra = _float(pricing.get("per_extra_second"))
    if price is not None and base_seconds is not None and extra is not None and seconds is not None:
        total = price + max(0.0, seconds - base_seconds) * extra
        return VideoQuote(total, f"${price:g} for {base_seconds:g} s, ${extra:g} a second more")
    return VideoQuote(None, "its listing prices it in a way the app can't read")


_SHORT_SIDE = {
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "768p": 768,
    "1080p": 1080,
    "1k": 1080,
    "2k": 1440,
    "4k": 2160,
}


def seedance_tokens(resolution: str, aspect_ratio: str, seconds: float) -> float | None:
    """Seedance's own count: width × height × frames / 1024."""
    short = _SHORT_SIDE.get(resolution.lower())
    try:
        w, h = (float(x) for x in aspect_ratio.split(":"))
    except ValueError:
        return None
    if short is None or w <= 0 or h <= 0:
        return None
    width, height = (short * w / h, short) if w >= h else (short, short * h / w)
    return width * height * SEEDANCE_FPS * seconds / 1024


def _openrouter(info: VideoModelInfo, settings: dict[str, Any], *, start_frame: bool) -> VideoQuote:
    skus = {k: _float(v) for k, v in info.pricing.items()}
    skus = {k: v for k, v in skus.items() if v is not None}
    seconds = _float(settings.get("duration"))
    resolution = str(settings.get("resolution") or "").lower()
    audio = _audio_on(info, settings)
    if seconds is None:
        return VideoQuote(None, "no duration chosen")
    audio_words = ["with_audio", ""] if audio else ["without_audio", ""]
    mode = "image_to_video_" if start_frame else "text_to_video_"
    res_words = [resolution, ""] if resolution else [""]

    def find(stem: str) -> tuple[str, float] | None:
        for prefix in (mode, ""):
            for sound in audio_words:
                for res in res_words:
                    key = f"{prefix}{stem}" + (f"_{sound}" if sound else "")
                    key += f"_{res}" if res else ""
                    if key in skus:
                        return key, skus[key]
        return None

    found = find("duration_seconds")
    if found is not None:
        return _per_second(found[1], seconds)
    for stem in ("cents_per_video_output_second", "cents_per_second_output"):
        found = find(stem)
        if found is not None:
            return _per_second(found[1] / 100, seconds)
    for key in ([f"video_tokens_{resolution}"] if resolution else []) + (
        ["video_tokens"] if audio else ["video_tokens_without_audio", "video_tokens"]
    ):
        if key in skus:
            ratio = str(settings.get("aspect_ratio") or "16:9")
            tokens = seedance_tokens(resolution or "720p", ratio, seconds)
            if tokens is None:
                break
            return VideoQuote(
                skus[key] * tokens, f"{tokens:,.0f} video tokens at ${skus[key] * 1e6:g} a million"
            )
    return VideoQuote(None, "its listing prices it in a way the app can't read")


def pictures_worth(amount: float) -> int:
    """About how many pictures the same money would draw."""
    return max(1, round(amount / PICTURE_PRICE))
