"""No crash dump holds what the app holds: first thing at every entry point
(the window, gui/app.py, and the command line, cli.py).

A native crash must not write the process's memory to disk: a memory-only
chat or scene lives nowhere else, and a story's prompts are in it. As every
DA app does it (the DA apps' standard, Nov 14 2026), on Linux:

- not dumpable (PR_SET_DUMPABLE 0): the system's collector reads the flag,
  not the limit. Measured on Mint 22.3 (systemd 255, core_pattern piping to
  systemd-coredump): with RLIMIT_CORE 0 alone the process was dumped, since
  a piped collector never sees the limit; undumpable, it wasn't. A debugger
  from another process can't attach either;
- RLIMIT_CORE's soft limit 1: the kernel skips a core whose limit is exactly
  1 ("RLIMIT_CORE is set to 1, aborting core"), even of a dumpable process,
  which a program started from here is again once it is executed. The hard
  limit is left as it is, so nothing is taken that can't be given back;
- /proc/self/coredump_filter 0: whatever dumps anyway writes no memory
  (DA Privacy Kit's). Inherited by every program the app starts.

faulthandler is unaffected: it writes from inside the process (crash.log,
gui/app.py). Failures are ignored: a guard must never stop the app starting.

Windows has none of these. Its error reporting may still keep or send a
minidump whatever the app asks, which the manual says; this only keeps its
dialog away. (`ctypes.CDLL(None)` raises TypeError there.)
"""

from __future__ import annotations

import sys

PR_SET_DUMPABLE = 4
# The kernel aborts a core of a process whose RLIMIT_CORE is exactly this.
CORE_LIMIT = 1
COREDUMP_FILTER = "/proc/self/coredump_filter"
# Windows: no critical-error box, and no Windows Error Reporting dialog.
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002


def no_core_dumps() -> None:
    """This process (and what it starts) writes no core of its memory."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
        except (OSError, AttributeError):
            pass
        return
    linux = sys.platform.startswith("linux")
    if linux:
        try:
            with open(COREDUMP_FILTER, "w", encoding="ascii") as handle:
                handle.write("0")
        except OSError:
            pass
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        # Elsewhere (no Linux kernel to read 1 so) no core at all, as before.
        wanted = CORE_LIMIT if linux else 0
        # Never above the hard limit, which can't be raised: at 0 it stays 0.
        soft = wanted if hard == resource.RLIM_INFINITY or hard >= wanted else hard
        resource.setrlimit(resource.RLIMIT_CORE, (soft, hard))
    except (ImportError, ValueError, OSError):
        pass
    if not linux:
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass
