"""The files every DA app keeps alike, held to their canonical copy (the DA
apps' security standard, Nov 14 2026): dcap.py, nras.py and ethsig.py are
DA Toolkit's (`src/datoolkit/llm/`). They may differ only in their imports
and their module docstring (which names the paths of the app they are in);
anything else is drift, which fails here rather than waiting for a review.
Skipped where DA Toolkit's repository isn't beside this one, as on CI.
(sdk_guard.py is SealedLore's own canonical copy: DA Toolkit's test holds
its copy to it.)"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "src" / "sealedlore" / "providers"
TOOLKIT = Path(__file__).resolve().parents[2] / "DAToolkit" / "src" / "datoolkit" / "llm"
SHARED = ("dcap.py", "nras.py", "ethsig.py")


def comparable(path: Path) -> list[str]:
    """The file's lines but its module docstring and the imports of its own
    app (`from sealedlore…`, `from .…`): what the copies must hold alike."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    dropped: set[int] = set()
    first = tree.body[0] if tree.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        dropped.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").split(".")[0] in ("sealedlore", "datoolkit")
        ):
            dropped.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [line for number, line in enumerate(text.splitlines(), 1) if number not in dropped]


@pytest.mark.skipif(not TOOLKIT.is_dir(), reason="DA Toolkit's repository isn't beside this one")
@pytest.mark.parametrize("name", SHARED)
def test_the_shared_file_is_da_toolkits_copy(name):
    ours, canonical = comparable(HERE / name), comparable(TOOLKIT / name)
    if ours != canonical:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(canonical, ours, "DA Toolkit", "SealedLore", lineterm="", n=1)
        )
        pytest.fail(f"{name} has drifted from DA Toolkit's copy:\n{diff[:4000]}")


def test_only_imports_and_the_docstring_are_left_out(tmp_path: Path):
    """What the comparison forgives, and nothing more: a changed line
    anywhere but there is still a difference."""
    one = tmp_path / "one.py"
    other = tmp_path / "other.py"
    one.write_text(
        '"""Docstring naming providers/tee.py."""\n\nimport hashlib\n\n'
        "from sealedlore.providers.base import ProviderError\n\nLIMIT = 1\n",
        encoding="utf-8",
    )
    other.write_text(
        '"""Docstring naming llm/tee.py,\nover two lines."""\n\nimport hashlib\n\n'
        "from .secure_http import ProviderError, make_client\n\nLIMIT = 1\n",
        encoding="utf-8",
    )
    assert comparable(one) == comparable(other)
    other.write_text(other.read_text(encoding="utf-8").replace("LIMIT = 1", "LIMIT = 2"))
    assert comparable(one) != comparable(other)
    other.write_text(
        other.read_text(encoding="utf-8").replace("import hashlib", "import hmac"),
        encoding="utf-8",
    )
    assert comparable(one) != comparable(other), "a standard library import is no app's own"
