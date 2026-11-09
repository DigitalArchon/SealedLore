"""The AppImage build's own guarantees (packaging/appimage/): the commit built as
committed whatever git's settings, nothing of this machine's uv settings in a lock,
every floor an .in file sets met by its lock, the tokenizer fetched as every other
download is, and `build.sh check`. Nothing here builds anything or uses the network:
the scripts' own blocks are run on stand-ins."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APPIMAGE = ROOT / "packaging" / "appimage"
BUILD = (APPIMAGE / "build.sh").read_text()

needs_bash_and_git = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git"
)


def block(text: str, start: str, end: str) -> str:
    """The script's text from `start` to the first `end` after it, both included."""
    first = text.index(start)
    return text[first : text.index(end, first) + len(end)]


# --- the commit, as committed ------------------------------------------------------------


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text))


def _locked() -> dict[str, str]:
    lock = (APPIMAGE / "requirements.lock").read_text()
    return {
        name.lower(): version
        for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", lock, re.MULTILINE)
    }


@needs_bash_and_git
def test_a_lock_or_a_build_takes_no_uv_setting_from_this_machine():
    """UV_NO_CONFIG skips only uv's config files: an index, a find-links folder or an
    insecure host named in the environment would still be used. tools.sh, read by
    lock.sh and build.sh both, drops every UV_ variable first."""
    planted = {
        "UV_INDEX_URL": "https://mirror.example/simple",
        "UV_EXTRA_INDEX_URL": "https://mirror.example/extra",
        "UV_DEFAULT_INDEX": "https://mirror.example/simple",
        "UV_INDEX": "mirror=https://mirror.example/simple",
        "UV_FIND_LINKS": "/tmp/wheels",
        "UV_INSECURE_HOST": "mirror.example",
        "UV_VERSION": "0.0.1",
    }
    script = (
        f"set -euo pipefail\nHERE={shlex.quote(str(APPIMAGE))}\n"
        f"source {shlex.quote(str(APPIMAGE / 'tools.sh'))}\n"
        '{ env | grep "^UV_" || true; } | cut -d= -f1 | sort\necho "pinned $UV_VERSION"'
    )
    env = dict(planted, PATH=os.environ.get("PATH", ""))
    said = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout.split()
    version = re.search(r'^UV_VERSION="([^"]+)"', (APPIMAGE / "tools.sh").read_text(), re.M)
    assert said == ["pinned", version.group(1)], said
