"""The public docs' links lead somewhere: every relative link names a file that
exists and every #anchor a heading in it, as GitHub makes them. The README's
old news moved into CHANGELOG.md (3.5.0b1), and the manual's Contents had left
out two of its sections (Lorebooks, Models SealedLore recommends and adjusts
for)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = sorted(
    [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md"]
    + list((ROOT / "docs").rglob("*.md"))
    + list((ROOT / "SampleStories").glob("*.md"))
)
CODE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
LINK = re.compile(r"\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: markup and punctuation gone, spaces as dashes."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _headings(path: Path) -> list[tuple[int, str]]:
    text = CODE.sub("", path.read_text(encoding="utf-8"))
    return [(len(m.group(1)), m.group(2)) for m in HEADING.finditer(text)]


def _anchors(path: Path) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    for _level, heading in _headings(path):
        slug = _slug(heading)
        found.add(f"{slug}-{seen[slug]}" if slug in seen else slug)
        seen[slug] = seen.get(slug, 0) + 1
    return found


def test_every_relative_link_and_anchor_in_the_public_docs_resolves():
    assert ROOT / "CHANGELOG.md" in PUBLIC and all(path.exists() for path in PUBLIC)
    broken: list[str] = []
    checked = 0
    for path in PUBLIC:
        for match in LINK.finditer(CODE.sub("", path.read_text(encoding="utf-8"))):
            target = match.group(1)
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue  # https:, mailto:: outside the repo
            checked += 1
            name, _, anchor = target.partition("#")
            linked = (path.parent / unquote(name)).resolve() if name else path
            if not linked.exists():
                broken.append(f"{path.relative_to(ROOT)}: {target} (no such file)")
            elif anchor and linked.suffix == ".md" and anchor not in _anchors(linked):
                broken.append(f"{path.relative_to(ROOT)}: {target} (no such heading)")
    assert checked >= 100, f"only {checked} links found: the docs or the pattern changed"
    assert not broken, "Links that lead nowhere:\n" + "\n".join(broken)


def test_the_manuals_contents_lists_every_section():
    manual = ROOT / "docs" / "manual.md"
    text = manual.read_text(encoding="utf-8")
    contents = text.split("## Contents", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"\]\(#([^)]+)\)", contents))
    sections = [h for level, h in _headings(manual) if level == 2 and h != "Contents"]
    missing = [h for h in sections if _slug(h) not in listed]
    assert len(sections) > 20 and not missing, f"Not in the manual's Contents: {missing}"
