def test_a_reply_that_corrects_itself_means_its_last_object():
    from sealedlore.engine.jsonreply import extract_json

    reply = '{"location": null}\n\nWait — the scene moved. Corrected:\n\n{"location": "the yard"}'
    assert extract_json(reply, dict, "scene object") == {"location": "the yard"}


def test_dialogue_quoted_with_its_marks_unescaped_still_parses():
    from sealedlore.engine.jsonreply import extract_json

    reply = (
        '{"happened": [{"event": "e", "quote": "The river took the truck," he said '
        'softly, "and left her standing there."}]}'
    )
    data = extract_json(reply, dict, "record")
    assert data["happened"][0]["event"] == "e"
    assert "he said softly" in data["happened"][0]["quote"]
