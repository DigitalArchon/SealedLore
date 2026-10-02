"""API keys in the system keychain, config.json as the fallback.

The author (Oct 2026): keys belong in the keychain, in plain text only on a
machine that has none, and the author is told where a key goes as they enter
it. Through `keyring`, as the author's DAToolkit does: on Linux the Secret
Service (GNOME Keyring, KWallet), on Windows the Credential Manager. Without
a session bus it fails at once, where libsecret hung (Oct 2 2026).

The rest of the app never sees this: `repository.load_config` fills each
key in from the keychain and `save_config` takes it out again, leaving ""
in config.json and the key's slot in `Config.keychain_slots`. A key the
keychain couldn't give back is never deleted from it, since nothing here
knows what it was.

`Config.key_storage = "file"` keeps keys in config.json by choice: for the
CLI and test setups (the author's call).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from sealedlore.models.config import Config
from sealedlore.storage.paths import data_home

SERVICE = "SealedLore"


class KeychainError(Exception):
    """The keychain is missing, locked and not unlocked, or refused."""


class HasKey(Protocol):
    api_key: str


def slots(config: Config) -> Iterator[tuple[str, HasKey]]:
    """Every key the config holds, by slot: the name it has in the keychain."""
    for provider in config.providers:
        yield f"provider:{provider.name}", provider
    if config.embedding_provider is not None:
        yield "embeddings", config.embedding_provider
    yield "images", config.image_endpoint
    yield "videos", config.video_endpoint
    if config.private_provider is not None:
        yield "private", config.private_provider


def keys_in_file(config: Config) -> bool:
    """Keys config.json still holds though the keychain is chosen: from
    before the keychain, or a save it refused."""
    if config.key_storage != "keychain":
        return False
    return any(
        holder.api_key and slot not in config.keychain_slots for slot, holder in slots(config)
    )


def account(slot: str, root: Path | None) -> str:
    """The slot's name in the keychain. Another data folder (a test setup,
    `--data-dir`) has its own, so it can never overwrite the real keys."""
    if root is None:
        return slot
    try:
        if Path(root).resolve() == data_home().resolve():
            return slot
        return f"{slot} @ {Path(root).resolve()}"
    except OSError:
        return f"{slot} @ {root}"


# Why the last save couldn't put the keys in the keychain (they stayed in
# config.json), or None: for Settings to say.
last_problem: str | None = None

# What this process read from or wrote to the keychain, account → key: a
# save that finds the same key writes nothing, and a key is deleted only
# when it is known (the author emptied the field), never when it couldn't
# be read.
_known: dict[str, str] = {}
_lock = threading.Lock()


_backend: Any = None
_problem: tuple[str | None] | None = None


def _keyring() -> Any:
    """The platform's own backend, named rather than discovered: keyring's
    discovery reads every installed package's entry points, ~40 ms of the
    ~110 ms reading a key cost before the first paint (Oct 2 2026)."""
    global _backend
    if _backend is None:
        # Imported only when a key is read or written: ~60 ms.
        if sys.platform == "win32":
            from keyring.backends import Windows  # noqa: PLC0415

            _backend = Windows.WinVaultKeyring()
        elif sys.platform == "darwin":
            from keyring.backends import macOS  # noqa: PLC0415

            _backend = macOS.Keyring()
        else:
            from keyring.backends import SecretService  # noqa: PLC0415

            _backend = SecretService.Keyring()
    return _backend


def use_backend(backend: Any) -> None:
    """Put another backend in place (tests: never the real keychain)."""
    global _backend, _problem
    _backend, _problem = backend, None
    forget()


def problem() -> str | None:
    """Why no keychain can be used here, or None when one can. Asked once a
    process: the Secret Service's answer opens a connection each time."""
    global _problem
    if _problem is None:
        try:
            priority = _keyring().priority
        except Exception as exc:  # noqa: BLE001 - any backend failure is reportable
            _problem = (f"no system keychain was found ({exc})",)
        else:
            _problem = (None if priority >= 1 else "no system keychain was found",)
    return _problem[0]


def where() -> str:
    """Where a key goes on this machine, in the author's words."""
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "the macOS Keychain"
    return "the system keychain (GNOME Keyring or KWallet)"


def known(slot: str, root: Path | None) -> bool:
    with _lock:
        return account(slot, root) in _known


def read(slot: str, root: Path | None) -> str | None:
    """The key, or None when the keychain has none. Raises KeychainError."""
    name = account(slot, root)
    try:
        value = _keyring().get_password(SERVICE, name)
    except Exception as exc:  # noqa: BLE001 - keyring raises many kinds
        raise KeychainError(str(exc) or type(exc).__name__) from exc
    if value is not None:
        with _lock:
            _known[name] = value
    return value


def write(slot: str, root: Path | None, value: str) -> None:
    """Raises KeychainError. Nothing is sent when the keychain has it already."""
    name = account(slot, root)
    with _lock:
        if _known.get(name) == value:
            return
    try:
        _keyring().set_password(SERVICE, name, value)
    except Exception as exc:  # noqa: BLE001
        raise KeychainError(str(exc) or type(exc).__name__) from exc
    with _lock:
        _known[name] = value


def remove(slot: str, root: Path | None) -> None:
    """Best effort: a key left behind is harmless, a failed save is not."""
    name = account(slot, root)
    with _lock:
        _known.pop(name, None)
    try:
        _keyring().delete_password(SERVICE, name)
    except Exception:  # noqa: BLE001, S110 - absent already, or unreachable
        pass


def forget() -> None:
    """Forget what this process knows (tests)."""
    with _lock:
        _known.clear()
