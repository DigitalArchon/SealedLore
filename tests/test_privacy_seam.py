"""Every decision about a model's privacy goes through providers/privacy.py.

Whether a model is end-to-end encrypted, TEE or neither is the endpoint's to
say as well as the id's: Tinfoil's and Chutes' endpoints seal every model,
whatever it is called. Reading the `private/` or `TEE/` prefix anywhere else
decides by the id alone, and a sealed endpoint's model would be taken for a
plain one (sent through a plain client, routed, never attested). A new
call site must ask `privacy_of` / `privacy_for` / `StorySession.privacy`.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "sealedlore"

# Where the id may be read: the module that decides, and NanoGPT's own code
# (its prefix tests, its TEE/ client, its Private Mode client, which refuses
# any model that isn't its `private/` one, and its catalog's twins, which are
# its names for one model).
ALLOWED = {
    "providers/privacy.py",
    "providers/tee.py",
    "providers/private_mode.py",
    "providers/private_catalog.py",
}
# NanoGPT's tests by the id, and the helpers and wrappers that judge by the
# id alone (by import: `_kind_of` is a common name elsewhere).
ID_TESTS = {"is_tee", "is_private_mode"}
ID_ONLY_WRAPPERS = {
    "sealedlore.providers.tee": {"move_refusal", "_kind_of"},
    "sealedlore.providers.private_catalog": {"privacy_label", "_kind"},
}
PREFIXES = ("private/", "tee/")


def _prefix(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(PREFIXES)


def findings(source: str) -> list[str]:
    """Where `source` decides privacy by the id, as "line: what"."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            wrappers = ID_ONLY_WRAPPERS.get(node.module or "", set())
            for alias in node.names:
                if alias.name in ID_TESTS or alias.name in wrappers:
                    found.append(f"{node.lineno}: imports {alias.name} from {node.module}")
        elif isinstance(node, ast.Name) and node.id in ID_TESTS:
            found.append(f"{node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in ID_TESTS:
            found.append(f"{node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().lower() in PREFIXES:
                found.append(f"{node.lineno}: the prefix {node.value!r}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("startswith", "removeprefix", "partition", "split")
        ):
            for arg in node.args:
                values = arg.elts if isinstance(arg, ast.Tuple) else [arg]
                if any(isinstance(v, ast.Constant) and _prefix(v.value) for v in values):
                    found.append(f"{node.lineno}: .{node.func.attr} on a model's prefix")
    return found


def test_no_module_decides_privacy_by_the_model_id():
    offending = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in ALLOWED:
            continue
        for finding in findings(path.read_text(encoding="utf-8")):
            offending.append(f"{relative}:{finding}")
    assert not offending, (
        "privacy decided by the model's id, not through providers/privacy.py:\n"
        + "\n".join(offending)
    )


def test_the_scan_finds_what_it_looks_for():
    """Each way of reading the prefix is caught; a docstring or a full id
    that merely names one is not."""
    caught = {
        "from sealedlore.providers.tee import is_tee": 1,
        "from sealedlore.providers.tee import move_refusal": 1,
        "from sealedlore.providers.tee import _kind_of": 1,
        "from sealedlore.providers.private_catalog import privacy_label": 1,
        "x = tee.is_private_mode(model)": 1,
        'sealed = model.lower().startswith("private/")': 1,
        'kind = model.startswith(("TEE/", "x/"))': 1,
        'prefix = "TEE/"': 1,
    }
    for source, count in caught.items():
        assert len(findings(source)) >= count, source
    for source in (
        '"""A `private/` model is sealed."""',
        'placeholder = "e.g. llama3.1:8b, or TEE/glm-5.3-flash"',
        "from sealedlore.providers.privacy import move_refusal, privacy_of",
        'RELAY = "/api/v1/private/tinfoil"',
    ):
        assert findings(source) == [], source
