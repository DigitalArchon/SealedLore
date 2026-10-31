"""Where phone access keeps tor's files and sockets, and the torrc it runs on.

Tor runs with its own data folder inside SealedLore's (`phone/tor/`), its own
torrc and an empty defaults file, so the system's /etc/tor/torrc is never
read and a system tor service, if there is one, is never touched. The control
port and the phones' socket are unix sockets in a folder only this user can
open; a unix socket's path is at most 107 bytes, so a data folder deep in the
tree puts them in the runtime folder instead.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sealedlore.phone.tor_control import quoted

SOCKET_PATH_MOST = 100  # under the 107 a unix socket allows, with room
_UNSAFE = set(' \t\n"\\')
# The libraries Tor's expert bundle keeps beside its tor (no RUNPATH: it finds
# them by LD_LIBRARY_PATH, which only its own process is given).
BUNDLED_LIBRARIES = ("libevent-2.1.so.7", "libssl.so.3", "libcrypto.so.3")


@dataclass(frozen=True)
class PhonePaths:
    folder: Path  # <data>/phone, 0700
    data: Path  # tor's DataDirectory, 0700
    torrc: Path
    defaults: Path  # empty: so /etc/tor/torrc's defaults are never read
    sockets: Path  # where the two sockets are, 0700
    control: Path
    http: Path


def phone_paths(data_home: Path) -> PhonePaths:
    folder = data_home / "phone"
    sockets = folder
    if not _socket_safe(folder / "control.sock"):
        sockets = _runtime_folder(data_home)
    return PhonePaths(
        folder=folder,
        data=folder / "tor",
        torrc=folder / "torrc",
        defaults=folder / "torrc-defaults",
        sockets=sockets,
        control=sockets / "control.sock",
        http=sockets / "phone.sock",
    )


def _socket_safe(path: Path) -> bool:
    text = str(path)
    return len(text.encode()) <= SOCKET_PATH_MOST and not (set(text) & _UNSAFE)


def _runtime_folder(data_home: Path) -> Path:
    tag = hashlib.sha256(str(data_home).encode()).hexdigest()[:8]
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / f"sealedlore-{os.getuid() if hasattr(os, 'getuid') else 0}-{tag}"


def make_folders(paths: PhonePaths) -> None:
    for folder in (paths.folder, paths.data, paths.sockets):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)


def render_torrc(paths: PhonePaths, owner_pid: int) -> str:
    """Tor as phone access runs it: no SOCKS port (it makes no connections
    for anyone), control by unix socket and cookie only, and gone when
    SealedLore is (__OwningControllerProcess, besides TAKEOWNERSHIP)."""
    lines = [
        f"DataDirectory {quoted(str(paths.data))}",
        "SocksPort 0",
        f"ControlPort unix:{quoted(str(paths.control))}",
        "CookieAuthentication 1",
        f"__OwningControllerProcess {owner_pid}",
        "Log notice stdout",
        "AvoidDiskWrites 1",
    ]
    return "\n".join(lines) + "\n"


def onion_target(paths: PhonePaths) -> str:
    """ADD_ONION's target. Its arguments are split on spaces, so the socket
    path must have none: phone_paths moves the sockets where that holds."""
    if not _socket_safe(paths.http):
        raise ValueError(f"the phones' socket path can't be given to tor: {paths.http}")
    return f"unix:{paths.http}"


def find_tor(environ: dict[str, str] | None = None) -> Path | None:
    """The tor to run: the AppImage's own (AppRun sets SEALEDLORE_TOR), else
    one on PATH. None on Windows: phone access is Linux's for now."""
    if sys.platform == "win32":
        return None
    env = os.environ if environ is None else environ
    given = env.get("SEALEDLORE_TOR")
    if given and os.access(given, os.X_OK):
        return Path(given)
    found = shutil.which("tor", path=env.get("PATH"))
    return Path(found) if found else None


def bundled(tor: Path) -> bool:
    """The AppImage's own tor (build.sh's usr/lib/tor, with the bundle's
    libraries beside it): only a newer SealedLore can replace it."""
    return tor.parent.parts[-3:] == ("usr", "lib", "tor") and all(
        (tor.parent / name).exists() for name in BUNDLED_LIBRARIES
    )


def tor_environment(tor: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """A clean environment for tor: nothing of SealedLore's own (AppRun's
    LD_LIBRARY_PATH is the X11 helpers'), and the bundle's folder as its
    library path only when the bundle's libraries are there."""
    env = os.environ if environ is None else environ
    out = {key: env[key] for key in ("HOME", "LANG", "PATH") if key in env}
    out.setdefault("PATH", "/usr/bin:/bin")
    if all((tor.parent / name).exists() for name in BUNDLED_LIBRARIES):
        out["LD_LIBRARY_PATH"] = str(tor.parent)
    return out
