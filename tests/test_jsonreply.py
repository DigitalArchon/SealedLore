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


def test_a_comma_before_the_closing_brace_keeps_the_whole_object():
    """Live (GLM 5.3, Oct 17 2026): the scene read ended with a comma after
    "scene_closed", and the reply was taken for its scene_closed alone, the
    move to the barn and Ruth staying behind lost without a word."""
    from sealedlore.engine.jsonreply import extract_json

    reply = (
        '{\n  "location": "The barn",\n  "left": [{"name": "Ruth", "how": "stayed"}],\n'
        '  "scene_closed": {"gist": "The welcome ended."},\n}'
    )
    data = extract_json(reply, dict, "scene object")
    assert data["location"] == "The barn" and data["left"][0]["name"] == "Ruth"
    assert extract_json('["a", "b",]', list, "list") == ["a", "b"]
    # A comma inside a string stays.
    assert extract_json('{"q": "yes, }",}', dict, "o") == {"q": "yes, }"}
