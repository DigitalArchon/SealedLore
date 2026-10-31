"""The Stories list's search: everything in every story, nothing a plot keeps back."""

from __future__ import annotations

import threading
from pathlib import Path

from sealedlore.models.aside import Aside
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.plot import Plot
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.models.supporting import SupportingCast
from sealedlore.storage.repository import StoryBundle, append_api_log, save_story_bundle
from sealedlore.storage.story_search import IN_THE_STORY, TITLE, search_stories


def passage(text: str, node_id: str = "n1", ooc: str | None = None) -> Node:
    return Node(id=node_id, kind="assistant", speaker_id="__narrator__", content=text, ooc=ooc)


def save(root: Path, story_id: str, **parts) -> None:
    title = parts.pop("title", "A story")
    story = Story(id=story_id, title=title, **parts.pop("story", {}))
    save_story_bundle(StoryBundle(story=story, **parts), root=root)


def found(root: Path, query: str, **kwargs) -> dict:
    return {hit.story_id: hit for hit in search_stories(query, root, **kwargs)}


def test_it_finds_words_in_everything_a_story_is_made_of(tmp_path):
    save(tmp_path, "message", nodes=[passage("The watchtower stood alone.")])
    save(tmp_path, "note", nodes=[passage("Nothing.", ooc="Mention the watchtower later.")])
    chapter = Summary(content="They reached the watchtower.", covered_node_ids=[])
    save(tmp_path, "chapter", summaries=[chapter])
    save(tmp_path, "question", asides=[Aside(question="Who built the watchtower?", answer="?")])
    save(tmp_path, "world", story={"world_bible": "A watchtower overlooks the plain."})
    save(tmp_path, "card", cast=[Character(name="Jo", full_description="Keeps the watchtower.")])
    aside = SupportingCast(characters=[Character(name="Sam", summary="Watchtower cook.")])
    save(tmp_path, "supporting", supporting=aside)
    save(tmp_path, "lore", lore=[LoreEntry(title="Old post", content="The watchtower fell.")])
    save(tmp_path, "no", nodes=[passage("Only the plain.")])
    hits = found(tmp_path, "watchtower")
    assert set(hits) == {
        "message",
        "note",
        "chapter",
        "question",
        "world",
        "card",
        "supporting",
        "lore",
    }
    assert hits["message"].where == IN_THE_STORY
    assert "watchtower" in hits["message"].snippet
    assert (hits["chapter"].where, hits["world"].where, hits["lore"].where) == (
        "Chapters",
        "World",
        "Lore",
    )


def test_every_word_must_be_in_the_story_and_the_phrase_is_shown_when_it_is_there(tmp_path):
    save(
        tmp_path,
        "both",
        nodes=[
            passage("The tower is old.", "n1"),
            passage("Far off, the old tower of Hellsville leans.", "n2"),
        ],
    )
    save(tmp_path, "one", nodes=[passage("The tower.")])
    hits = found(tmp_path, "old tower")
    assert set(hits) == {"both"}
    assert "old tower of Hellsville" in hits["both"].snippet
    assert hits["both"].find == "old tower"
    # Apart, Find goes to the first word found.
    assert found(tmp_path, "leans tower")["both"].find == "tower"


def test_case_and_curly_quotes_dont_matter(tmp_path):
    save(tmp_path, "s", nodes=[passage("“Don’t go,” she said — HELLSVILLE burned.")])
    assert set(found(tmp_path, "hellsville")) == {"s"}
    assert set(found(tmp_path, "don't")) == {"s"}
    assert "Don’t go" in found(tmp_path, '"don\'t go,"')["s"].snippet, "shown as written"


def test_a_title_is_a_match_with_nothing_for_find_to_go_to(tmp_path):
    save(tmp_path, "s", title="Hellsville Rising", nodes=[passage("Rain.")])
    hit = found(tmp_path, "hellsville")["s"]
    assert (hit.where, hit.snippet, hit.find) == (TITLE, "", "")


def test_words_in_a_title_and_a_passage_show_the_passage(tmp_path):
    save(tmp_path, "s", title="Hellsville", nodes=[passage("The watchtower stood.")])
    hit = found(tmp_path, "hellsville watchtower")["s"]
    assert hit.where == IN_THE_STORY and "watchtower" in hit.snippet


def test_a_snippet_is_cut_at_words_and_says_so(tmp_path):
    long = "word " * 80 + "marker " + "word " * 80
    save(tmp_path, "s", nodes=[passage(long)])
    snippet = found(tmp_path, "marker")["s"].snippet
    assert snippet.startswith("…word") and snippet.endswith("word…") and "marker" in snippet
    assert len(snippet) < 150


def test_what_a_plot_keeps_back_is_found_only_with_spoilers_on(tmp_path):
    """A search must not be how the author learns who is coming."""
    hidden = Character(name="The Hollow King", plot_hidden=True)
    secret = LoreEntry(title="Ninth seal", content="The seal breaks.", plot_hidden=True)
    save(tmp_path, "s", cast=[hidden], lore=[secret], story={"plot": Plot()})
    assert not found(tmp_path, "hollow king") and not found(tmp_path, "seal")
    assert set(found(tmp_path, "hollow king", spoilers=True)) == {"s"}
    assert set(found(tmp_path, "seal", spoilers=True)) == {"s"}


def test_the_api_log_is_not_searched_and_a_broken_story_is_left_out(tmp_path):
    save(tmp_path, "good", nodes=[passage("Rain.")])
    append_api_log("good", {"kind": "request", "text": "zebra"}, root=tmp_path)
    save(tmp_path, "broken", nodes=[passage("zebra")])
    (tmp_path / "stories" / "broken" / "story.json").write_text("{not json", encoding="utf-8")
    save(tmp_path, "torn", nodes=[passage("Rain.")])
    (tmp_path / "stories" / "torn" / "nodes.json").write_text("[{torn", encoding="utf-8")
    assert not found(tmp_path, "zebra")
    assert set(found(tmp_path, "rain")) == {"good"}, "a torn file is passed over, not fatal"


def test_nothing_to_look_for_finds_nothing_and_a_stop_ends_it(tmp_path):
    save(tmp_path, "s", nodes=[passage("Rain.")])
    assert not found(tmp_path, "   ")
    assert not found(tmp_path / "nowhere", "rain")
    stop = threading.Event()
    stop.set()
    assert not found(tmp_path, "rain", stop=stop)
