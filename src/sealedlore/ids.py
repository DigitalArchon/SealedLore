"""Shared helpers for IDs and timestamps used across all models.

All IDs are UUID4 strings and all timestamps are ISO 8601 UTC, per the domain
model spec, so every model reaches for the same two functions rather than
rolling its own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id() -> str:
    return str(uuid.uuid4())


# Ids that are the same every time something is read from the same words: a
# plot file's map places, so art made for them once (a sample's, shipped
# beside it) matches every import of the file.
_STABLE = uuid.UUID("5f0c6b2e-4d2a-4b1b-9c0e-3a7e2d1f8a9b")


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(_STABLE, "\x1f".join(parts)))


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
