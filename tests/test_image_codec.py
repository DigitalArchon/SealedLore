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
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if raw.search(line):
                found.append(f"{path.name}:{number}: {line.strip()}")
    assert not found, "\n".join(found)


# --- only the pictures the app takes are decoded ---------------------------------------


def _svg_naming(local: Path) -> bytes:
    """A drawing that pulls in a picture file on this computer by its path."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="64" height="64"><rect width="64" height="64" fill="#ff0000"/>'
        f'<image x="0" y="0" width="64" height="64" xlink:href="{local}"/></svg>'
    ).encode()


def _local_picture(tmp_path: Path) -> Path:
    from PySide6.QtGui import QColor, QImage

    from sealedlore.gui.image_codec import encoded

    image = QImage(64, 64, QImage.Format_RGB32)
    image.fill(QColor(0, 255, 0))
    local = tmp_path / "private" / "local.png"
    local.parent.mkdir()
    local.write_bytes(encoded(image, "PNG"))
    return local


def test_a_drawing_named_as_a_picture_is_never_drawn(tmp_path):
    """An SVG saved as "jane.png" (beside a plot file, a card's icon, a
    scenario's picture) was drawn by Qt's SVG reader, pulling in a picture
    file on this computer it named by path: the story kept its pixels."""
    pytest.importorskip("PySide6")
    from sealedlore.gui.image_codec import image_from_data
    from sealedlore.gui.ref_images import clean_incoming, image_of, load_image, picture_bytes

    svg = _svg_naming(_local_picture(tmp_path))
    beside = tmp_path / "pictures" / "jane.png"
    beside.parent.mkdir()
    beside.write_bytes(svg)
    assert image_of(svg).isNull() and image_from_data(svg).isNull()
    assert load_image(beside).isNull()
    with pytest.raises(ValueError):
        picture_bytes(beside)  # a plot file's picture line: left out, with a note
    assert clean_incoming({"images/refs/a.png": svg})["images/refs/a.png"] == svg, "not redrawn"
    pdf = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
    assert image_of(pdf).isNull()


def test_a_cards_drawing_of_an_icon_isnt_kept_as_its_portrait(tmp_path):
    pytest.importorskip("PySide6")
    import io
    import json
    import zipfile

    from sealedlore.gui.ref_images import clean_incoming
    from sealedlore.storage import collection, tavern_cards
    from sealedlore.storage.picture_store import MemoryPictures

    card = {
        "spec": "chara_card_v3",
        "data": {
            "name": "Jane",
            "description": "A farmer.",
            "assets": [
                {"type": "icon", "name": "main", "uri": "embeded://icon/main.png", "ext": "png"}
            ],
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("card.json", json.dumps(card))
        archive.writestr("icon/main.png", _svg_naming(_local_picture(tmp_path)))
    path = tmp_path / "jane.charx"
    path.write_bytes(buffer.getvalue())
    store = MemoryPictures(lambda kind, body: "")
    character, notes = collection.kept_from_card(
        tavern_cards.read_card(path),
        store,
        clean=lambda data: clean_incoming({"picture": data}).get("picture", data),
    )
    assert character.reference_images == []
    assert any("format SealedLore doesn't keep" in note for note in notes)


def test_every_picture_the_app_takes_still_reads(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QColor, QImage

    from sealedlore.gui.image_codec import encoded
    from sealedlore.gui.ref_images import image_of, load_image

    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor(10, 20, 30))
    pictures = {fmt: encoded(image, fmt) for fmt in ("PNG", "JPEG", "WEBP", "BMP", "TIFF")}
    # Qt reads GIF, and writes none: one pixel, by hand.
    pictures["GIF"] = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
        b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )
    for fmt, data in pictures.items():
        shown = image_of(data)
        assert not shown.isNull(), fmt
        path = tmp_path / f"picture.{fmt.lower()}"
        path.write_bytes(data)
        assert load_image(path).size() == shown.size(), fmt
    # By its bytes, never its name: a JPEG named .png still reads.
    misnamed = tmp_path / "photo.png"
    misnamed.write_bytes(encoded(image, "JPEG"))
    assert not load_image(misnamed).isNull()
