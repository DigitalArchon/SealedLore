"""Write the AppImage's third-party notices: the template with its package table.

Run by build.sh with the bundled Python, so importlib.metadata sees exactly the
packages the AppImage ships:

    python notices.py <template> <out>

Each package's licence files are pointed to where pip left them in its dist-info.
A package whose wheel carries none must be named in EXTRA_TEXTS with a text
from licenses/, or the build fails: a lock bump must not ship a package with
no licence text beside it.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

MARKER = "@PYTHON_PACKAGES@"
# Wheels that ship no licence file, and the text that covers them.
EXTRA_TEXTS = {
    # Qt for Python is LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; the
    # AppImage takes it under the LGPL-3.0, which builds on the GPL-3.0's text.
    "pyside6-essentials": ["LGPL-3.0.txt", "GPL-3.0.txt"],
    "shiboken6": ["LGPL-3.0.txt", "GPL-3.0.txt"],
    # MIT; the licence is in its source repository (tinfoilsh/encrypted-http-body-protocol).
    "tinfoil-ehbp": ["tinfoil-ehbp.txt"],
}
LICENCE_FILE = re.compile(r"(^|/)(licen[cs]e|copying|notice|authors)[^/]*$", re.I)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def licence_of(meta: md.PackageMetadata, first_text: Path | None) -> str:
    if expression := meta.get("License-Expression"):
        return expression
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    first = (meta.get("License") or "").strip().splitlines()
    if first:
        return first[0][:60]
    # No licence in the metadata: the text's own title ("Apache License").
    if first_text:
        for line in first_text.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                return line.strip()[:60]
    return "see its licence text"


def rows() -> list[str]:
    found, missing = [], []
    for dist in md.distributions():
        name = normalise(dist.metadata["Name"])
        if name == "sealedlore":
            continue
        # Paths relative to site-packages, as the notices' heading says.
        texts = sorted(
            f for f in dist.files or [] if ".dist-info/" in str(f) and LICENCE_FILE.search(str(f))
        )
        files = [str(f) for f in texts]
        if len(files) > 3:  # numpy's sixteen: name the folder
            files = [f"{Path(files[0]).parts[0]}/licenses/ ({len(files)} files)"]
        files += [f"texts/{t}" for t in EXTRA_TEXTS.get(name, [])]
        if not files:
            missing.append(name)
            continue
        first = Path(dist.locate_file(texts[0])) if texts else None
        licence = licence_of(dist.metadata, first)
        where = "<br>".join(f"`{f}`" for f in files)
        found.append(f"| {dist.metadata['Name']} | {dist.version} | {licence} | {where} |")
    if missing:
        sys.exit(
            "no licence text for: "
            + ", ".join(sorted(missing))
            + " (add one to packaging/appimage/licenses and EXTRA_TEXTS)"
        )
    header = ["| Package | Version | Licence | Licence text |", "|---|---|---|---|"]
    return header + sorted(found, key=str.lower)


def main() -> None:
    template, out = Path(sys.argv[1]), Path(sys.argv[2])
    text = template.read_text(encoding="utf-8")
    if text.count(MARKER) != 1:
        sys.exit(f"{template} must hold {MARKER} exactly once")
    out.write_text(text.replace(MARKER, "\n".join(rows())), encoding="utf-8")


if __name__ == "__main__":
    main()
