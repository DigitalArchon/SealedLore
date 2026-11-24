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


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
