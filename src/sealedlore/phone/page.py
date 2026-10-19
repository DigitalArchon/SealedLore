"""The phone page's files, as the server hands them out (phone/static/).

Read once when phone access starts. index.html carries the version and a link
to the source code (the AGPL asks that a network user can get it), written in
here: the page runs no inline script.
"""

from __future__ import annotations

from importlib import resources

FILES = {
    "/index.html": ("index.html", "text/html"),
    "/app.js": ("app.js", "application/javascript"),
    "/app.css": ("app.css", "text/css"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
SOURCE = "https://github.com/DigitalArchon/SealedLore"


def page_files(version: str, source: str = SOURCE) -> dict[str, tuple[bytes, str]]:
    folder = resources.files("sealedlore.phone") / "static"
    out: dict[str, tuple[bytes, str]] = {}
    for path, (name, content_type) in FILES.items():
        data = (folder / name).read_bytes()
        if name == "index.html":
            text = data.decode("utf-8").replace("@VERSION@", version).replace("@SOURCE@", source)
            data = text.encode("utf-8")
        out[path] = (data, content_type)
    return out
