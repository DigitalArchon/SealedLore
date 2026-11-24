"""The AppImage build's own guarantees (packaging/appimage/): the commit built as
committed whatever git's settings, nothing of this machine's uv settings in a lock,
every floor an .in file sets met by its lock, what of the packages ships (and that
what ships has what it needs), the tokenizer fetched as every other
download is, and `build.sh check`. Nothing here builds anything or uses the network:
the scripts' own blocks are run on stand-ins."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APPIMAGE = ROOT / "packaging" / "appimage"
BUILD = (APPIMAGE / "build.sh").read_text(encoding="utf-8")

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
    lock = (APPIMAGE / "requirements.lock").read_text(encoding="utf-8")
    return {
        name.lower(): version
        for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", lock, re.MULTILINE)
    }


def test_the_lock_meets_every_floor_the_in_file_sets():
    """A relock keeps what the lock already pins, so a floor set for an advisory
    (certifi's root; pyasn1's and dnspython's denials of service; cryptography's
    50.0.2, the other apps' version) is only as good as the relock after it."""
    floors = re.findall(
        r"^([A-Za-z0-9_.-]+)>=([0-9.]+)$",
        (APPIMAGE / "requirements.in").read_text(encoding="utf-8"),
        re.M,
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
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    floors = re.findall(
        r"^([A-Za-z0-9_.-]+)>=([0-9.]+)$",
        (APPIMAGE / "requirements.in").read_text(encoding="utf-8"),
        re.M,
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
    version = re.search(
        r'^UV_VERSION="([^"]+)"', (APPIMAGE / "tools.sh").read_text(encoding="utf-8"), re.M
    )
    assert said == ["pinned", version.group(1)], said


@needs_bash_and_git
def test_upgrade_re_resolves_the_lot_and_nothing_else_does():
    lock = (APPIMAGE / "lock.sh").read_text(encoding="utf-8")
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
    wanted = (APPIMAGE / "requirements.in").read_text(encoding="utf-8")
    for name in ("tinfoil", "tinfoil-ehbp", "pyhpke"):
        assert f"{name}=={locked[name]}" in wanted


NOT_SHIPPED = {"huggingface-hub", "hf-xet", "fsspec", "pyyaml", "tqdm", "click", "filelock"}


def test_the_hub_and_what_only_it_brings_are_resolved_but_never_shipped():
    """tokenizers needs huggingface-hub by its metadata, for Tokenizer.from_pretrained,
    which the app never calls: the hub and what nothing else needs of it (hf-xet's
    12 MB of native code among them) are left out of the lock, and the build
    installs only what the lock names. The hub is held in requirements.in, so a
    relock resolves the same tree and moves nothing that ships to suit a newer one."""
    lock_sh = (APPIMAGE / "lock.sh").read_text(encoding="utf-8")
    named = re.search(r"^NOT_SHIPPED=\(([^)]*)\)$", lock_sh, re.M)
    assert named and set(named.group(1).split()) == NOT_SHIPPED
    assert '--no-emit-package "$package"' in lock_sh
    assert 'compile requirements.in requirements.lock "${left_out[@]}"' in lock_sh
    locked = _locked()
    assert not NOT_SHIPPED & set(locked)
    assert {"tokenizers", "onnxruntime"} <= set(locked)
    wanted = (APPIMAGE / "requirements.in").read_text(encoding="utf-8")
    assert re.search(r"^huggingface-hub==[0-9.]+$", wanted, re.M)
    assert "--no-deps" in BUILD
    assert '--require-hashes -r "$SRC/packaging/appimage/requirements.lock"' in BUILD


def _requires_check(tmp_path: Path, dists: dict[str, list[str]]) -> subprocess.CompletedProcess:
    """build.sh's check that every shipped package's requirements are shipped, run
    on a folder of stand-in packages: name-version -> its Requires-Dist lines."""
    code = block(BUILD, "<<'REQUIRES'\n", "\nREQUIRES\n").split("\n", 1)[1].rsplit("\n", 2)[0]
    site = tmp_path / "site"
    for spec, requires in dists.items():
        name, version = spec.rsplit("-", 1)
        info = site / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir(parents=True)
        lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        lines += [f"Requires-Dist: {line}" for line in requires]
        (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = tmp_path / "requires.py"
    script.write_text(code, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(script), str(site)], capture_output=True, text=True
    )


def test_the_build_stops_if_a_package_it_ships_needs_one_the_lock_left_out(tmp_path: Path):
    """--no-deps never notices a requirement left out: a relock that left out
    something a shipped package needs would ship it broken. Only the hub for
    tokenizers, and PySide6 for SealedLore (Essentials stands in), are excused."""
    shipped = {
        "sealedlore-3.5.0": ["PySide6<6.12,>=6.6", "httpx>=0.27"],
        "tokenizers-0.23.3": ["huggingface-hub<3.0,>=0.16.4", "pytest; extra == 'testing'"],
        "httpx-0.28.1": ["anyio", "click==8.*; extra == 'cli'"],
        "anyio-4.15.1": [],
    }
    passed = _requires_check(tmp_path / "met", shipped)
    assert passed.returncode == 0, passed.stderr
    unmet = dict(shipped, **{"rich-15.0.0": ["click>=8"], "openai-3.36.1": ["anyio>=5"]})
    failed = _requires_check(tmp_path / "unmet", unmet)
    assert failed.returncode == 1
    assert "rich needs click>=8, which isn't installed" in failed.stderr
    assert "openai needs anyio>=5, and anyio is 4.15.1" in failed.stderr
    assert "NOT_SHIPPED" in failed.stderr


def test_the_builds_embeddings_check_runs_the_apps_own_path(tmp_path: Path):
    """The build proves the built-in embeddings still load and run without the hub
    and onnxruntime's model tools, through local_embeddings._model itself: a
    tokenizer read from its file, an ONNX session on the CPU. Run here on this
    venv's onnxruntime and tokenizers, as the build runs it on the AppImage's."""
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    step = block(BUILD, 'log "Trimming what the app never loads"', 'log "X11 helper libraries')
    assert 'rm -rf "$SITE"/onnxruntime/{transformers,quantization,tools}' in step
    code = block(BUILD, "<<'EMBEDDINGS'\n", "\nEMBEDDINGS\n").split("\n", 1)[1].rsplit("\n", 2)[0]
    assert "local_embeddings._model(folder)" in code
    script = tmp_path / "embeddings.py"
    script.write_text(code, encoding="utf-8")
    folder = tmp_path / "check"
    folder.mkdir()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
    ran = subprocess.run(
        [sys.executable, str(script), str(folder)], env=env, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr
    assert "['CPUExecutionProvider'], its tokenizer read" in ran.stdout


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
    source = Path(tiktoken.__file__).read_text(encoding="utf-8")
    assert url.startswith("https://") and url.endswith(f"/{DEFAULT_ENCODING}.tiktoken")
    assert f'"{url}",\n        expected_hash="{pinned}"' in source, "tiktoken's own sum"
    step = block(BUILD, 'log "Tokenizer data"', 'log "Licences and third-party notices"')
    assert 'fetch "$TIKTOKEN_URL"' in step and "sha1sum" in step
    assert "requests" not in step
    assert hashlib.sha1(url.encode()).hexdigest()  # the name tiktoken looks for


# --- build.sh check -----------------------------------------------------------------------


def _check(tmp_path: Path, same: bool, zsync_same: bool = True) -> subprocess.CompletedProcess:
    """build.sh's check, its two builds stand-ins that write an AppImage, its sum and
    its .zsync: the same bytes each time, or not."""
    check = block(BUILD, "    check)\n", "        exit 1\n")
    here = tmp_path / "here"
    here.mkdir(parents=True)
    (here / "build.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'test "$1" = build\nmkdir -p "$DIST"\n'
        'if [ -e "$CACHE_DIR/pip" ] && [ "$BUILD_DIR" != "${BUILD_DIR%/b}" ]; then\n'
        '    echo "the second build had the first one\'s package cache" >&2; exit 3\nfi\n'
        + (
            'printf same > "$DIST/SealedLore-1-x86_64.AppImage"\n'
            if same
            else 'printf "%s" "$BUILD_DIR" > "$DIST/SealedLore-1-x86_64.AppImage"\n'
        )
        + (
            'printf z > "$DIST/SealedLore-1-x86_64.AppImage.zsync"\n'
            if zsync_same
            else 'printf "%s" "$BUILD_DIR" > "$DIST/SealedLore-1-x86_64.AppImage.zsync"\n'
        )
        + 'cd "$DIST" && name=SealedLore-1-x86_64.AppImage && sha256sum "$name" > "$name.sha256"\n'
    )
    (here / "build.sh").chmod(0o755)
    cache = tmp_path / "cache"
    (cache / "pip").mkdir(parents=True)
    (cache / "uv").mkdir()
    (cache / "debs").mkdir()
    (cache / "debs" / "x.deb").write_bytes(b"deb")
    (cache / "python.AppImage").write_bytes(b"base")
    dist = tmp_path / "dist"
    script = (
        f"set -euo pipefail\nHERE={shlex.quote(str(here))}\nCACHE={shlex.quote(str(cache))}\n"
        f'DIST={shlex.quote(str(dist))}\ncase "check" in\n{check};;\nesac\n'
    )
    return subprocess.run(
        ["bash", "-c", script], env={"PATH": os.environ.get("PATH", "")}, capture_output=True,
        text=True,
    )  # fmt: skip


@needs_bash_and_git
def test_check_builds_twice_apart_and_compares(tmp_path: Path):
    passed = _check(tmp_path / "one", same=True)
    assert passed.returncode == 0, passed.stderr
    assert "Reproducible: both builds are" in passed.stdout
    assert (tmp_path / "one/dist/SealedLore-1-x86_64.AppImage").read_text(
        encoding="utf-8"
    ) == "same"
    failed = _check(tmp_path / "two", same=False)
    assert failed.returncode == 1 and "NOT reproducible" in failed.stderr
    a = (tmp_path / "two/dist/check-a/SealedLore-1-x86_64.AppImage").read_text(encoding="utf-8")
    b = (tmp_path / "two/dist/check-b/SealedLore-1-x86_64.AppImage").read_text(encoding="utf-8")
    assert a.endswith("/a") and b.endswith("/b"), "each in a folder of its own"
    # The same AppImage with a .zsync that differs is not a reproducible build either:
    # an updater reads the .zsync.
    zsync = _check(tmp_path / "three", same=True, zsync_same=False)
    assert zsync.returncode == 1 and "NOT reproducible" in zsync.stderr


# --- updates ------------------------------------------------------------------------------


def test_the_appimage_names_where_its_updates_are_and_a_release_carries_them():
    """The update information appimagetool writes into the runtime names this
    repository's latest release and a .zsync whose name matches the AppImage's; the
    .zsync is made again from the AppImage dated to the commit (appimagetool's own is
    dated with the moment it ran); and the release attaches it."""
    info = re.search(r'^UPDATE_INFO="([^"]+)"', BUILD, re.M).group(1)
    kind, owner, repo, tag, pattern = info.split("|")
    assert (kind, owner, repo, tag) == (
        "gh-releases-zsync",
        "DigitalArchon",
        "SealedLore",
        "latest",
    )
    name = re.search(r'^OUTPUT="\$DIST/(.+)"$', BUILD, re.M).group(1)
    assert re.fullmatch(
        pattern.replace(".", r"\.").replace("*", ".+"),
        name.replace("$VERSION", "3.1.0b10") + ".zsync",
    )
    pack = block(BUILD, 'log "Packing"', 'cat "$OUTPUT.sha256"')
    assert '-u "$UPDATE_INFO"' in pack
    stamp = pack.index('touch -d "@$SOURCE_DATE_EPOCH" "$OUTPUT"')
    remake = pack.index('zsyncmake" -u "$NAME" -o "$NAME.zsync" "$NAME"')
    assert stamp < remake < pack.index('sha256sum "$NAME"')
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '"dist/$APPIMAGE.zsync"' in release
