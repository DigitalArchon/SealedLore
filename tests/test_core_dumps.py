"""No crash dump holds what the app holds (sealedlore/core_dumps.py): not
dumpable, RLIMIT_CORE's soft limit 1 with the hard limit as it was, and
coredump_filter 0, first thing in the window and on the command line; and
crash.log is still written. Each in a process of its own: this one stays as
it is (conftest stands in for the command line's guard)."""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
from pathlib import Path

import pytest

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's guard")

PROBE = """
import ctypes, resource, sys
from sealedlore.core_dumps import no_core_dumps
no_core_dumps()
libc = ctypes.CDLL(None, use_errno=True)
soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
with open("/proc/self/coredump_filter", encoding="ascii") as handle:
    kept = int(handle.read().strip(), 16)
print(libc.prctl(3, 0, 0, 0, 0), soft, hard, kept)  # PR_GET_DUMPABLE
"""


def _probe(hard: int | None = None) -> tuple[int, int, int, int]:
    def limit() -> None:
        if hard is not None:
            resource.setrlimit(resource.RLIMIT_CORE, (min(hard, 1000), hard))

    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        preexec_fn=limit,
        check=True,
    )
    dumpable, soft, kept_hard, kept = (int(word) for word in done.stdout.split())
    return dumpable, soft, kept_hard, kept


@linux_only
def test_a_process_is_left_undumpable_its_core_limit_one_and_its_memory_filtered_out():
    _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    assert _probe() == (0, 1, hard, 0)


@linux_only
def test_the_hard_limit_is_left_as_it_was():
    """At 0 the kernel's collector is still handed the dump through a pipe
    (systemd-coredump never reads the limit): a soft limit of 1 is what the
    kernel itself refuses. The hard one, which no process can raise again,
    is left for the programs this one starts (tor)."""
    assert _probe(hard=5000) == (0, 1, 5000, 0)
    assert _probe(hard=0) == (0, 0, 0, 0), "never above a hard limit of 0"


def test_the_window_guards_itself_before_anything_else(monkeypatch):
    """Before the arguments are read: `--version` and a bad argument end the
    process there, and so could anything after."""
    from sealedlore.gui import app

    called: list[str] = []
    monkeypatch.setattr(app, "_no_core_dumps", lambda: called.append("guard"))
    with pytest.raises(SystemExit):
        app.main(["sealedlore-gui", "--version"])
    assert called == ["guard"]


def test_the_command_line_guards_itself_before_anything_else(monkeypatch):
    """The command line plays stories and chats too (`sealedlore chat`): it
    had no guard at all."""
    from sealedlore import cli

    called: list[str] = []
    monkeypatch.setattr(cli, "no_core_dumps", lambda: called.append("guard"))
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert called == ["guard"]


CRASH = """
import faulthandler, sys
from pathlib import Path
from sealedlore.gui import app
app._no_core_dumps()
app._log_crashes(Path(sys.argv[1]))
faulthandler._sigsegv()
"""


@linux_only
def test_a_crash_is_still_logged_in_crash_log(tmp_path: Path):
    """faulthandler writes from inside the process, so not being dumpable
    takes nothing from crash.log."""
    folder = tmp_path / "data"
    done = subprocess.run(
        [sys.executable, "-c", CRASH, str(folder)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert done.returncode == -signal.SIGSEGV, done.stderr
    log = (folder / "crash.log").read_text(encoding="utf-8")
    assert "Fatal Python error: Segmentation fault" in log
    assert "<string>" in log, "the Python stack, as before"
