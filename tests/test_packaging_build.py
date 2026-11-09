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
import tomllib
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


def test_the_lock_meets_every_floor_the_in_file_sets():
    """A relock keeps what the lock already pins, so a floor set for an advisory
    (certifi's root; pyasn1's and dnspython's denials of service; cryptography's
    50.0.2, the other apps' version) is only as good as the relock after it."""
    floors = re.findall(
        r"^([A-Za-z0-9_.-]+)>=([0-9.]+)$", (APPIMAGE / "requirements.in").read_text(), re.M
    )
    named = dict(floors)
    assert named["pyasn1"] == "0.6.5" and named["dnspython"] == "2.9.0"
    assert named["cryptography"] == "50.0.2" and named["certifi"] == "2026.10.19"
    locked = _locked()
    for name, floor in floors:
        version = locked[re.sub(r"[-_.]+", "-", name).lower()]
        assert _version(version) >= _version(floor), f"{name}=={version}, below {floor}"


def test_a_source_install_has_the_same_floors():
    """pip install reads pyproject.toml, never the AppImage's lock."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    floors = re.findall(
        r"^([A-Za-z0-9_.-]+)>=([0-9.]+)$", (APPIMAGE / "requirements.in").read_text(), re.M
    )
    for name, floor in floors:
        assert f"{name}>={floor}" in deps, f"pyproject.toml lacks {name}>={floor}"


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


@needs_bash_and_git
def test_upgrade_re_resolves_the_lot_and_nothing_else_does():
    lock = (APPIMAGE / "lock.sh").read_text()
    chooses = block(lock, 'upgrade=""', "\nfi\n")
    for value, flag in (("", ""), ("0", ""), ("1", "--upgrade")):
        env = {"PATH": os.environ.get("PATH", ""), "UPGRADE": value}
        said = subprocess.run(
            ["bash", "-c", f'set -euo pipefail\n{chooses}echo "[${{upgrade:+$upgrade}}]"'],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert said == f"[{flag}]"
    assert '${upgrade:+"$upgrade"}' in lock


def test_the_encryption_stack_moves_with_no_relock():
    """tinfoil, tinfoil-ehbp and pyhpke stay exactly as pinned, whatever a relock
    does (test_private_mode.py holds them to pyproject.toml)."""
    locked = _locked()
    wanted = (APPIMAGE / "requirements.in").read_text()
    for name in ("tinfoil", "tinfoil-ehbp", "pyhpke"):
        assert f"{name}=={locked[name]}" in wanted


# --- the tokenizer ------------------------------------------------------------------------
