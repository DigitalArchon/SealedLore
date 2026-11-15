"""JSON from outside the app is held to 200 levels as it is read
(`storage.atomic.outside_json`), never a crash: an endpoint's reply or
stream event, an attestation, a phone's request, a lorebook, a prompt set,
a model's JSON answer (the DA apps' standard, Nov 14 2026). Before, only
imported story files were (`nests_deeper`); a reply was read to any depth,
and on Python 3.11 one past about 1,000 levels raised a RecursionError the
caller never caught."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from sealedlore.storage.atomic import MAX_NESTING, NestedTooDeep, outside_json

SOURCE = Path(__file__).resolve().parents[1] / "src" / "sealedlore"


def nested(levels: int) -> str:
    """A JSON object `levels` deep (each level a list, the last an object)."""
    return "[" * (levels - 1) + "{}" + "]" * (levels - 1)


@pytest.mark.parametrize("levels", [MAX_NESTING + 1, 5000, 200_000])
def test_past_the_limit_is_json_that_cant_be_read(levels):
    """5,000 levels is a RecursionError on Python 3.11 (about 1,000) and
    read on 3.12; 200,000 is past either. Both are refused alike, as a
    JSONDecodeError, which every reader already handles."""
    with pytest.raises(NestedTooDeep) as caught:
        outside_json(nested(levels))
    assert isinstance(caught.value, json.JSONDecodeError)
    assert "deeper than 200 levels" in str(caught.value)


def test_to_the_limit_is_read():
    assert outside_json(nested(MAX_NESTING)) is not None
    assert outside_json(b'{"a": [1, 2]}') == {"a": [1, 2]}


def _provider(body: bytes, content_type: str = "application/json"):
    from sealedlore.models.config import ProviderConfig
    from sealedlore.providers.openai_compat import OpenAICompatibleProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    config = ProviderConfig(name="t", base_url="https://example.test/api/v1", api_key="sk-x")
    return OpenAICompatibleProvider(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_a_models_list_nested_past_the_limit_is_not_json():
    from sealedlore.providers.base import ProviderError

    with pytest.raises(ProviderError, match="wasn't JSON"):
        _provider(nested(MAX_NESTING + 1).encode()).fetch_model_prices()


def test_a_stream_event_nested_past_the_limit_is_malformed():
    from sealedlore.messages import ContentPart, PromptMessage
    from sealedlore.providers.base import ChatRequest, ProviderError

    event = '{"choices": ' + nested(MAX_NESTING) + "}"  # an object, one level past
    stream = f"data: {event}\n\ndata: [DONE]\n\n".encode()
    provider = _provider(stream, "text/event-stream")
    request = ChatRequest(
        model="m", messages=[PromptMessage(role="user", parts=(ContentPart(text="hi"),))]
    )
    with pytest.raises(ProviderError, match="malformed"):
        list(provider.stream(request))


def test_an_attestation_nested_past_the_limit_is_refused():
    from sealedlore.providers.tee import TeeClient, TeeRefused

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=nested(MAX_NESTING + 1).encode())

    client = TeeClient(
        "https://nano-gpt.com/api/v1",
        "k",
        "TEE/x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TeeRefused, match="non-JSON"):
        client.attest()


def test_a_phones_request_nested_past_the_limit_is_its_error():
    from sealedlore.phone.http import HttpError, Request

    request = Request(
        method="POST",
        path="/api/turn",
        query={},
        headers={"content-type": "application/json"},
        body=nested(MAX_NESTING + 1).encode(),
        version="HTTP/1.1",
    )
    with pytest.raises(HttpError) as caught:
        request.json()
    assert caught.value.status == 400


def test_a_lorebook_or_a_prompt_set_nested_past_the_limit_says_so(tmp_path: Path):
    from sealedlore.storage.prompt_sets import PromptSetError, read_prompt_set
    from sealedlore.storage.tavern_lore import LorebookError, json_of

    deep = '{"entries": ' + nested(MAX_NESTING) + "}"
    with pytest.raises(LorebookError, match="nested deeper than any lorebook"):
        json_of(deep.encode(), "book.json")
    path = tmp_path / "set.sealedlore-prompts.json"
    path.write_text('{"format": "sealedlore-prompts", "edits": ' + nested(MAX_NESTING) + "}")
    with pytest.raises(PromptSetError, match="nested deeper than any prompt set"):
        read_prompt_set(path)


def test_a_models_json_answer_nested_past_the_limit_is_never_taken_whole():
    """What a read takes from a reply nested past the limit is at most its
    last whole object within it (`extract_json` reads a self-correcting
    reply so), never the deep one, and never a RecursionError."""
    from sealedlore.engine.jsonreply import extract_json
    from sealedlore.storage.atomic import nests_deeper

    try:
        taken = extract_json('{"a": ' + nested(5000) + "}", dict, "scene")
    except ValueError:
        return
    assert not nests_deeper(taken)


# --- every outside parse goes through the limit ------------------------------------

# Files whose json.loads reads what isn't from outside, or is held otherwise:
# the app's own data folder, readers that check `nests_deeper` beside it (and
# catch RecursionError), and DA Toolkit's canonical dcap.py and nras.py (kept
# alike, test_shared_code.py), whose every error the TEE client refuses as
# evidence that can't be read.
HELD_OTHERWISE = {
    "storage/atomic.py": "the helper itself, and read_json of the app's own files",
    "storage/archive.py": "nests_deeper beside it",
    "storage/collection_backup.py": "nests_deeper beside it",
    "storage/scenario.py": "nests_deeper beside it",
    "storage/repository.py": "nests_deeper beside it",
    "storage/picture_store.py": "a store's own records, read back from a checked backup",
    "storage/template_pictures.py": "the data folder's own list",
    "providers/dcap.py": "DA Toolkit's copy; refused by the TEE client",
    "providers/nras.py": "DA Toolkit's copy; refused by the TEE client",
    # Hyprland's own answer about its windows, its size capped and its depth
    # checked first (_deepest); a GTK path SealedLore, using the Keeper alone,
    # never runs.
    "gui/recentguard.py": "DA Assistant's copy; Hyprland's answer, held by _deepest",
}


# A decoder's own raw_decode reads any depth: only where the text's depth was
# held to the limit before it.
RAW_DECODE_HELD = {
    "engine/jsonreply.py": "extract_json refuses a reply nested past the limit first",
}


def _unchecked_parses(path: Path, name: str = "") -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        owner = func.value.id if isinstance(func.value, ast.Name) else ""
        if func.attr in ("loads", "load") and owner == "json":
            argument = node.args[0] if node.args else None
            copy = (
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Attribute)
                and argument.func.attr == "dumps"
            )
            if not copy:  # json.loads(json.dumps(x)) copies what was read
                found.append(f"line {node.lineno}: json.{func.attr}")
        elif func.attr == "raw_decode" and name not in RAW_DECODE_HELD:
            found.append(f"line {node.lineno}: raw_decode")
        elif func.attr == "json" and not node.args and not node.keywords:
            if owner != "request":  # the phone's own Request.json (outside_json)
                found.append(f"line {node.lineno}: {owner or '…'}.json()")
    return found


def test_every_json_from_outside_is_read_through_the_limit():
    offenders = {}
    for path in sorted(SOURCE.rglob("*.py")):
        name = path.relative_to(SOURCE).as_posix()  # the names above, on Windows too
        if name in HELD_OTHERWISE:
            continue
        found = _unchecked_parses(path, name)
        if found:
            offenders[name] = found
    assert offenders == {}, (
        "parse JSON from outside with storage.atomic.outside_json (or providers.http.reply_json "
        f"for a reply): {offenders}"
    )
