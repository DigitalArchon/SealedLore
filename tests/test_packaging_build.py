"""The AppImage build's own guarantees (packaging/appimage/): the commit built as
committed whatever git's settings, nothing of this machine's uv settings in a lock,
every floor an .in file sets met by its lock, the tokenizer fetched as every other
download is, and `build.sh check`. Nothing here builds anything or uses the network:
the scripts' own blocks are run on stand-ins."""

from __future__ import annotations

import hashlib
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


@needs_bash_and_git
def test_the_commit_is_built_as_committed_whatever_gits_settings(tmp_path: Path):
    """A verifier's core.autocrlf, a smudge filter their attributes name: git archive
    hands each file through them, so the same commit made a different AppImage. The
    build reads the files from git's objects instead, and sets their modes itself
    whatever the umask. (DA Assistant's test of its copy, d4153ff.)"""
    export = block(BUILD, 'git -C "$REPO" ls-tree -r -z HEAD', "\ndone\n")
    (tmp_path / "attributes").write_text("*.txt filter=pear\n")
    (tmp_path / "gitconfig").write_text(
        f"[core]\n\tautocrlf = true\n\tattributesFile = {tmp_path}/attributes\n"
        '[filter "pear"]\n\tsmudge = sed s/apple/pear/\n\tclean = cat\n'
        "[user]\n\tname = T\n\temail = t@example.org\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }
    repo = tmp_path / "repo"
    committed = {"notes.txt": b"apple\nbanana\n", "tools/run.sh": b"#!/bin/sh\necho a\n"}

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True
        ).stdout

    repo.mkdir()
    git("init", "-q")
    for name, data in committed.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(data)
    (repo / "tools/run.sh").chmod(0o755)
    git("add", "-A")
    git("commit", "-q", "-m", "files")

    archived = tmp_path / "archived"
    archived.mkdir()
    subprocess.run(["tar", "-x", "-C", str(archived)], input=git("archive", "HEAD"), check=True)
    assert (archived / "notes.txt").read_bytes() != committed["notes.txt"], "git archive changes it"

    def export_into(out: Path) -> subprocess.CompletedProcess:
        script = f"set -euo pipefail\numask 077\nREPO={shlex.quote(str(repo))}\n"
        script += f"SRC={shlex.quote(str(out))}\n{export}"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    out = tmp_path / "source"
    assert export_into(out).returncode == 0
    assert {name: (out / name).read_bytes() for name in committed} == committed
    modes = {name: (out / name).stat().st_mode & 0o777 for name in committed}
    assert modes == {"notes.txt": 0o644, "tools/run.sh": 0o755}

    (repo / "link").symlink_to("notes.txt")
    git("add", "-A")
    git("commit", "-q", "-m", "a link")
    refused = export_into(tmp_path / "again")
    assert refused.returncode != 0 and "link is a blob of mode 120000" in refused.stderr


# --- the locks ----------------------------------------------------------------------------


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


def test_the_tokenizer_is_fetched_as_every_other_download_is():
    """tiktoken's own fetch is requests.get, which follows a redirect anywhere, plain
    http included. The build fetches the file with tools.sh's fetch (curl, https
    only, its sum checked) into the name tiktoken caches it under, so nothing in the
    AppImage differs from what tiktoken wrote."""
    tiktoken = pytest.importorskip("tiktoken_ext.openai_public")
    from sealedlore.engine.tokens import DEFAULT_ENCODING

    url = re.search(r'^TIKTOKEN_URL="([^"]+)"', BUILD, re.M).group(1)
    pinned = re.search(r'^TIKTOKEN_SHA256="([^"]+)"', BUILD, re.M).group(1)
    source = Path(tiktoken.__file__).read_text()
    assert url.startswith("https://") and url.endswith(f"/{DEFAULT_ENCODING}.tiktoken")
    assert f'"{url}",\n        expected_hash="{pinned}"' in source, "tiktoken's own sum"
    step = block(BUILD, 'log "Tokenizer data"', 'log "Licences and third-party notices"')
    assert 'fetch "$TIKTOKEN_URL"' in step and "sha1sum" in step
    assert "requests" not in step
    assert hashlib.sha1(url.encode()).hexdigest()  # the name tiktoken looks for


# --- build.sh check -----------------------------------------------------------------------
