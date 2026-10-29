"""Qt's image decoding and encoding happen one at a time (gui/image_codec.py).

Decoding on two Python threads at once hung the test suite for good (Oct 17
2026): the play view's paint thread decoding sprites beside the GUI thread
decoding a thumbnail deadlocked inside Qt, and two worker threads decoding
together aborted the process. The race below runs in a subprocess, since a
regression would hang the test instead of failing it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "src" / "sealedlore" / "gui"

RACE = """
import os, sys, threading, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
from PySide6.QtWidgets import QApplication
from sealedlore.gui.image_codec import encoded, image_from_data
from sealedlore.gui.ref_images import image_of

app = QApplication([])
sets = Path(sys.argv[1])
data = [p.read_bytes() for p in sorted(sets.rglob("*")) if p.suffix in (".webp", ".jpg")][:12]
assert len(data) >= 4, "no sample pictures to decode"
stop = time.monotonic() + float(sys.argv[2])
counts = [0, 0, 0]


def worker(i):
    while time.monotonic() < stop:
        for d in data:
            assert not image_from_data(d).isNull()
            counts[i] += 1


threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in (0, 1)]
for t in threads:
    t.start()
while time.monotonic() < stop:
    for d in data[:4]:
        image = image_of(d)
        assert not image.isNull()
        assert encoded(image, "PNG")
        counts[2] += 1
for t in threads:
    t.join(10)
assert not any(t.is_alive() for t in threads), counts
print("decodes", counts)
"""


def test_decoding_on_two_threads_beside_the_gui_thread_never_hangs(tmp_path):
    """Two threads decode the samples' pictures while the main
    thread decodes and encodes: a few seconds of it, in a subprocess with a
    deadline. Before the lock this deadlocked within seconds, every time."""
    pytest.importorskip("PySide6")
    script = tmp_path / "race.py"
    script.write_text(RACE)
    sets = ROOT / "SampleStories" / "pictures"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    try:
        done = subprocess.run(
            [sys.executable, str(script), str(sets), "3"],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
            cwd=tmp_path,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"decoding on two threads hung:\n{exc.stdout}\n{exc.stderr}")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("decodes")


def test_every_decode_and_encode_in_the_gui_goes_through_the_codec_lock():
    """No `QImage.fromData`, `QImageReader.read` or `QImage.save` to a buffer
    anywhere in the GUI but gui/image_codec.py: a new decode site on a
    thread would bring the deadlock back."""
    raw = re.compile(r"QImage\.fromData\(|reader\.read\(\)|\.save\(buffer")
    found = []
    for path in sorted(GUI.glob("*.py")):
        if path.name == "image_codec.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if raw.search(line):
                found.append(f"{path.name}:{number}: {line.strip()}")
    assert not found, "\n".join(found)
