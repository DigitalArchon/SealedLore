"""SealedLore: collaborative fiction roleplay with an LLM."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sealedlore")
except PackageNotFoundError:  # run from a checkout without an install
    __version__ = "unknown"
