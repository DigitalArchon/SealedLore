"""Chutes E2EE's contract as read on Nov 14 2026 (tests/data/chutes_e2ee_contract.json):
the API's paths, the invoke headers, the HKDF labels and sizes of chutes-api's
api/e2e/router.py and chutes' entrypoint, and the fields read from the model
list, discovery and the evidence. SealedLoreDocs/tools/chutes_check diffs those
sources against the copies reviewed, and the live lists' fields against this.
A change on Chutes' side shows here or there before it breaks play."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import sealedlore.providers.chutes_e2ee as pure
import sealedlore.providers.chutes_provider as client

DATA = Path(__file__).resolve().parent / "data"
RECORD = json.loads((DATA / "chutes_e2ee_contract.json").read_text(encoding="utf-8"))


def test_the_framing_is_the_reviewed_one():
    sizes = RECORD["sizes"]
    assert (pure.PUBLIC_KEY_BYTES, pure.CIPHERTEXT_BYTES) == (
        sizes["public_key"],
        sizes["ciphertext"],
    )
    assert (pure.SHARED_SECRET_BYTES, pure.SALT_BYTES) == (sizes["shared_secret"], sizes["salt"])
    assert (pure.NONCE_BYTES, pure.TAG_BYTES, pure.KEY_BYTES) == (
        sizes["nonce"],
        sizes["tag"],
        sizes["key"],
    )
    info = RECORD["hkdf_info"]
    assert (pure.INFO_REQUEST, pure.INFO_RESPONSE, pure.INFO_STREAM) == (
        info["request"].encode(),
        info["response"].encode(),
        info["stream"].encode(),
    )
    assert pure.RESPONSE_KEY_FIELD == RECORD["response_key_field"]


def test_the_client_speaks_the_reviewed_api():
    paths = RECORD["paths"]
    assert client.CHUTES_ORIGIN == RECORD["origin"] and client.MODELS_URL == RECORD["models_url"]
    assert client.INVOKE_URL == RECORD["origin"] + paths["invoke"]
    assert client.MEASUREMENTS_URL == RECORD["origin"] + paths["measurements"]
    assert client.E2E_PATH == paths["model_path"]
    source = inspect.getsource(client)
    for path in ("discovery", "chute_evidence", "instance_evidence"):
        shape = paths[path].split("{")[0]
        assert shape in source, path
    for header in RECORD["invoke_headers"]:
        assert f'"{header}"' in source, header
    discovery = RECORD["discovery"]
    assert (client.MAX_INSTANCES, client.MAX_NONCES, client.NONCE_LIFE_SECONDS) == (
        discovery["max_instances"],
        discovery["max_nonces"],
        discovery["nonce_life_seconds"],
    )
    for field in discovery["fields"] + discovery["instance_fields"]:
        assert f'"{field}"' in source, field


def test_every_field_read_is_one_the_captures_and_the_record_know():
    models = json.loads((DATA / "chutes_models.json").read_text(encoding="utf-8"))
    evidence = json.loads((DATA / "chutes_evidence.json").read_text(encoding="utf-8"))
    listed = {k for item in models["data"] for k in item}
    assert listed <= set(RECORD["listing_fields"])
    assert set(RECORD["listing_read"]) <= listed
    source = inspect.getsource(pure.confidential_models)
    for field in RECORD["listing_read"] + RECORD["pricing_read"]:
        assert f'"{field}"' in source, field
    assert set(evidence["evidence"]) == set(RECORD["evidence_fields"])
    item = evidence["evidence"]["evidence"][0]
    assert set(item) == set(RECORD["evidence_item_fields"])
    assert set(RECORD["evidence_item_read"]) <= set(item)
    assert set(evidence["measurements"][0]) == set(RECORD["measurement_fields"])
