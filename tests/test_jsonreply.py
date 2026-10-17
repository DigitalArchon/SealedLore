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


def test_a_bracket_the_reply_forgot_to_close_is_closed():
    """Live (GLM 5.3 Flash furnishing a deck, Oct 17 2026): the reply ended
    "]]}" for "]}]}" and the deck was refused whole. A reply that left off
    only its last closer is closed too."""
    from sealedlore.engine.jsonreply import extract_json

    skipped = '{"rooms": [{"room": 0, "items": [{"name": "a canister", "w": 1}]]}'
    data = extract_json(skipped, dict, "furnishing")
    assert data == {"rooms": [{"room": 0, "items": [{"name": "a canister", "w": 1}]}]}
    unclosed = '```json\n{"rooms": [{"room": 13, "items": []}, {"room": 14, "items": []}]\n```'
    assert [r["room"] for r in extract_json(unclosed, dict, "furnishing")["rooms"]] == [13, 14]
    # With a trailing comma as well.
    assert extract_json('{"a": [1, 2,]', dict, "o") == {"a": [1, 2]}


def test_a_reply_cut_short_is_not_closed_into_a_whole_one():
    import pytest

    from sealedlore.engine.jsonreply import extract_json

    for cut in (
        '{"rooms": [{"room": 0, "purpose": "a store',  # inside a string
        '{"rooms": [{"room": 0, "items": [{"name": "a crate", "w": 1',  # not on a closer
        '{"a": {"b": {"c": [1]',  # three still open
    ):
        with pytest.raises(ValueError):
            extract_json(cut, dict, "furnishing")
    # A reply that corrects itself still means its last object, not its first.
    two = '{"location": "the barn"]}\n\nCorrected:\n\n{"location": "the yard"}'
    assert extract_json(two, dict, "scene object") == {"location": "the yard"}
