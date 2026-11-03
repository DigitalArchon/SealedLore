"""A story's picture style and its portraits' framing (engine/picture_styles.py,
engine/portraits.py; the author, Nov 3 2026: head and shoulders the default,
upper body and full body as well, and a style a story chooses: gritty and
realistic, clean and polished sci-fi, glamorous, anime and so on)."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.engine.image_prompt import build_image_question
from sealedlore.engine.images import run_image_request
from sealedlore.engine.picture_styles import (
    BY_ID,
    DEFAULT_LABEL,
    REALISTIC,
    STYLES,
    picture_words,
    read_style,
    story_style,
    style_key,
    style_label,
    style_note,
    style_words,
    write_style,
)
from sealedlore.engine.plot_file import read_plot_document, render_plot_markdown
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.portraits import (
    FRAMINGS,
    portrait_style,
    reframe_change,
    restyle_prompt,
    with_lead,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, TEXTS, PromptTexts
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config
from sealedlore.models.image import ImageRef
from sealedlore.providers.images import ImageModelInfo, ImageReply
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import save_story_bundle
from sealedlore.storage.scenario import (
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)

SAMPLES = Path(__file__).resolve().parents[1] / "SampleStories"
DOOMSVILLE = SAMPLES / "Doomsville.md"
EXTENDED = Path(__file__).parent / "data" / "doomsville_extended.md"
NO_WRITING = DEFAULT_TEXTS["portraits.no_writing"]


def png() -> bytes:
    """A 1x1 PNG, made by hand."""
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")

    header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes([0, 90, 90, 90])))
        + chunk(b"IEND", b"")
    )


def doomsville(tmp_path: Path, replies: list[str]) -> tuple[StorySession, MockChatProvider]:
    bundle = bundle_from_scenario(parse_plot_markdown(DOOMSVILLE.read_text()).scenario)
    save_story_bundle(bundle, root=tmp_path)
    provider = MockChatProvider(replies)
    session = StorySession(
        bundle,
        Config(),
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    return session, provider


def test_a_style_is_a_ready_made_one_the_author_s_own_words_or_the_default():
    gritty = DEFAULT_TEXTS["pictures.styles.gritty"]
    assert style_words("gritty", "", DEFAULT_TEXTS) == gritty.rstrip(".")
    # A ready-made style wins over words left from before.
    assert style_words("anime", "oil paint", DEFAULT_TEXTS).startswith("Anime style")
    assert style_words("", "  oil   paint. ", DEFAULT_TEXTS) == "oil paint"
    assert style_words("", "", DEFAULT_TEXTS) == ""
    # A newer version's style this one doesn't know: the author's words, else the default.
    assert style_words("noir", "ink wash", DEFAULT_TEXTS) == "ink wash"
    # Its words are a prompt text, so an edit to them is what is used.
    edited = PromptTexts({"pictures.styles.anime": "Cel-shaded anime"})
    assert style_words("anime", "", edited) == "Cel-shaded anime"
    assert style_label("comic", "") == "Comic book"
    assert style_label("", "oil paint") == "oil paint"
    assert style_label("", "") == DEFAULT_LABEL
    # Every style has its words, and those that needed one, a note to the writer.
    assert all(style.key in TEXTS for style in STYLES)
    assert style_note("glamorous") and style_note("anime") and not style_note("gritty")
    assert style_note("") == ""


def test_a_plot_file_names_a_style_by_its_name_or_in_its_own_words():
    for style in STYLES:
        assert read_style(style.label) == (style.id, "")
        assert read_style(style.label.upper()) == (style.id, "")
        assert read_style(style.id) == (style.id, "")
        assert read_style(write_style(style.id, "")) == (style.id, "")
    assert read_style("  muted   painterly art ") == ("", "muted painterly art")
    assert write_style("", "muted painterly art") == "muted painterly art"
    assert write_style("", "") == ""
    # Doomsville is gritty and realistic, the extended sample in its own words.
    scenario = parse_plot_markdown(DOOMSVILLE.read_text()).scenario
    assert (scenario.picture_style, scenario.image_style) == ("gritty", "")
    parsed = parse_plot_markdown(EXTENDED.read_text())
    assert parsed.scenario.picture_style == ""
    assert parsed.scenario.image_style == "muted painterly concept art, cold winter light"
    assert not [p for p in parsed.problems if "picture_style" in p.message]


def test_the_plot_editor_keeps_the_style_line():
    for path in (DOOMSVILLE, EXTENDED):
        document = read_plot_document(path.read_text()).document
        rendered = render_plot_markdown(document).text
        assert document.picture_style and f"picture style: {document.picture_style}" in rendered
        again = read_plot_document(rendered).document
        assert again.picture_style == document.picture_style


def test_the_style_goes_with_scenario_files_and_restart(tmp_path: Path):
    bundle = bundle_from_scenario(parse_plot_markdown(DOOMSVILLE.read_text()).scenario)
    assert bundle.story.picture_style == "gritty"
    bundle.story.picture_style = "anime"
    path = tmp_path / "mine.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(bundle, {}))
    assert bundle_from_scenario(read_scenario(path)).story.picture_style == "anime"
    assert fresh_playthrough(bundle, "Again", {}).story.picture_style == "anime"
    bundle.story.picture_style, bundle.story.image_style = "", "ink wash"
    again = fresh_playthrough(bundle, "Again", {})
    assert (again.story.picture_style, again.story.image_style) == ("", "ink wash")


def test_a_written_prompt_starts_with_its_style_line_and_says_no_writing():
    lead = portrait_style("Anime style: flat colours", DEFAULT_TEXTS, framing="full")
    assert lead == "Anime style: flat colours, full body, the whole figure from head to feet"
    # Left out or shortened (GLM 5.3 did both, live), the whole line goes first.
    assert with_lead("A tall woman.", lead, NO_WRITING) == f"{lead}. A tall woman. {NO_WRITING}"
    shortened = "Anime style portrait. A tall woman."
    assert with_lead(shortened, lead, NO_WRITING).startswith(f"{lead}. Anime style portrait.")
    # Copied, in any case or punctuation: left as it is.
    copied = (
        "anime style - flat colours; full body, the whole figure from head to feet. "
        f"A tall woman. {NO_WRITING}"
    )
    assert with_lead(copied, lead, NO_WRITING) == copied
    assert with_lead("  ", lead, NO_WRITING) == ""  # a lore entry with nothing to see


def test_the_writer_is_given_the_style_its_note_and_the_framing(tmp_path: Path):
    reply = json.dumps({"portraits": [{"name": "John", "prompt": "A tired man."}]})
    session, provider = doomsville(tmp_path, [reply, reply])
    john = next(c for c in session.cast if c.name == "John")
    # Doomsville's own: gritty, head and shoulders.
    prompts, _ = session.write_card_prompts([john])
    system = provider.payloads[-1]["messages"][0]["content"]
    gritty = DEFAULT_TEXTS["pictures.styles.gritty"]
    assert f"this style: {gritty}, head and shoulders." in system
    assert prompts[john.id] == f"{gritty}, head and shoulders. A tired man. {NO_WRITING}"
    # One chosen in the dialog, not yet the story's: anime, its note, full length.
    prompts, _ = session.write_card_prompts([john], framing="full", style=("anime", ""))
    system = provider.payloads[-1]["messages"][0]["content"]
    assert DEFAULT_TEXTS["pictures.styles.anime"] in system
    assert DEFAULT_TEXTS["pictures.styles.anime.note"] in system
    assert DEFAULT_TEXTS["portraits.framing.full"] in system
    assert prompts[john.id].startswith(DEFAULT_TEXTS["pictures.styles.anime"])
    assert session.story.picture_style == "gritty"  # the story's is unchanged


def test_a_portrait_is_drawn_at_its_framing_s_size_and_keeps_its_framing(tmp_path: Path):
    session, _provider = doomsville(tmp_path, ["x"])
    john = next(c for c in session.cast if c.name == "John")
    klein = ImageModelInfo(
        id="k", name="Klein", resolutions=("768x768", "1024x1024", "1536x1536", "2048x2048")
    )
    sizes = {
        framing: session.card_request(
            john, "A man.", model="k", info=klein, first=False, framing=framing
        ).size
        for framing in FRAMINGS
    }
    assert sizes == {"head": "1024x1024", "torso": "1024x1024", "full": "1536x1536"}
    request = session.card_request(
        john, "A man.", model="k", info=klein, first=False, framing="full"
    )
    assert request.card_framing == "full"

    class Images:
        api = "nanogpt"

        def generate(self, payload):
            return ImageReply(images=[png()], raw={}, cost=0.01)

    records = run_image_request(Images(), request, store=session.pictures)
    session.pictures.add_images(records)
    session.take_card_pictures()
    whole = john.reference_images[-1]
    assert whole.framing == "full"
    # A change of it (a board's gap, a set of expressions) shows as much of him.
    edit = session.card_request(john, "Angry.", model="k", info=klein, first=False, source=whole)
    assert (edit.size, edit.card_framing) == ("1536x1536", "full")


def test_a_picture_of_the_story_is_written_in_the_story_s_style(tmp_path: Path):
    def question(style: str, note: str) -> str:
        return build_image_question(
            direction="",
            held_name=None,
            style=style,
            choices=[],
            max_refs=0,
            style_note=note,
        )

    anime = DEFAULT_TEXTS["pictures.styles.anime"]
    note = DEFAULT_TEXTS["pictures.styles.anime.note"]
    asked = question(anime, note)
    assert f"- Style: {anime}." in asked and f"- {note}" in asked
    # The default: the writer chooses, and no note.
    free = question("", note)
    assert DEFAULT_TEXTS["pictures.style.free"] in free and note not in free
    session, _provider = doomsville(tmp_path, ["x"])
    assert story_style(session.story).startswith("Gritty, photorealistic")
    assert BY_ID["gritty"].label == "Gritty and realistic"


def test_a_story_s_default_style_is_realistic_throughout_and_a_picture_may_have_its_own(
    tmp_path: Path,
):
    """The author: "Default style should be the same throughout the story, but
    the person can always choose to change for things like a picture of the
    story". A simple chat has no style: its writer chooses."""
    session, _provider = doomsville(tmp_path, ["x"])
    default = DEFAULT_TEXTS["pictures.style.default"].rstrip(".")
    session.story.picture_style = ""
    assert picture_words(session.story) == (default, "")
    session.story.picture_style = "gritty"
    assert picture_words(session.story)[0].startswith("Gritty, photorealistic")
    # One picture in another style, its note too.
    words, note = picture_words(session.story, style=("anime", ""))
    assert words.startswith("Anime style") and note == DEFAULT_TEXTS["pictures.styles.anime.note"]
    assert picture_words(session.story, style=("", "ink wash")) == ("ink wash", "")
    assert picture_words(session.story, style=("", ""))[0] == default
    session.story.mode = "chat"
    session.story.picture_style = ""
    assert picture_words(session.story) == ("", "")


def test_a_picture_of_the_story_opens_with_its_style_the_story_s_or_its_own(tmp_path: Path):
    reply = json.dumps({"references": [], "prompt": "Two figures by a fire."})
    session, provider = doomsville(tmp_path, [reply, reply, json.dumps({"prompt": "He turns."})])
    gritty = style_words("gritty", "", DEFAULT_TEXTS)
    assert session.write_image_prompt("").prompt == f"{gritty}. Two figures by a fire."
    draft = session.write_image_prompt("", style=("anime", ""))
    anime = style_words("anime", "", DEFAULT_TEXTS)
    assert draft.prompt == f"{anime}. Two figures by a fire."
    asked = provider.requests[-1].messages[-1].text
    assert f"- Style: {anime}." in asked and DEFAULT_TEXTS["pictures.styles.anime.note"] in asked
    video = session.write_video_prompt("", duration="5", audio=False, style=("comic", ""))
    assert video == f"{style_words('comic', '', DEFAULT_TEXTS)}. He turns."


def test_restyle_and_reframe_prompts_keep_what_the_picture_shows():
    anime = style_words("anime", "", DEFAULT_TEXTS)
    prompt = restyle_prompt(anime, keep="person", change="Make them smile")
    assert prompt.startswith(
        f"Redraw this picture entirely in this style: {anime}. Make them smile."
    )
    assert DEFAULT_TEXTS["pictures.restyle.person"] in prompt and prompt.endswith(NO_WRITING)
    # Not an edit's "change nothing else", which kept John a photograph.
    assert "change nothing else" not in prompt
    for keep in ("thing", "scene"):
        assert DEFAULT_TEXTS[f"pictures.restyle.{keep}"] in restyle_prompt(anime, keep=keep)
    assert reframe_change("full") == DEFAULT_TEXTS["pictures.reframe.full"]
    assert "waist up" in reframe_change("torso")
    assert style_key("anime", "x") == "anime"
    assert style_key("", " ink  wash ") == "ink wash"
    assert style_key("", "") == REALISTIC


def test_a_restyled_picture_takes_the_old_one_s_place_and_the_old_one_is_kept_unlabelled(
    tmp_path: Path,
):
    session, _provider = doomsville(tmp_path, ["x"])
    john = next(c for c in session.cast if c.name == "John")
    for ref_id, look in (("portrait", "winter coat"), ("armour", "combat armour"), ("u", "")):
        session.pictures.write(f"images/refs/{ref_id}.png", png())
        john.reference_images.append(
            ImageRef(id=ref_id, file=f"images/refs/{ref_id}.png", look=look, expression="neutral")
        )
    portrait, armour = john.reference_images[0], john.reference_images[1]
    count = len(john.reference_images)
    klein = ImageModelInfo(id="k", name="Klein", resolutions=("1024x1024",), max_inputs=10)

    class Images:
        api = "nanogpt"

        def generate(self, payload):
            return ImageReply(images=[png()], raw={}, cost=0.01)

    requests = [
        session.card_request(
            john,
            session.card_restyle_prompt(john, "Anime style"),
            model="k",
            info=klein,
            first=False,
            source=ref,
            look=ref.look,
            expression=ref.expression,
            framing=ref.framing,
            style="anime",
            replaces=ref.id,
        )
        for ref in (portrait, armour)
    ]
    assert all(r.card_replaces and r.card_style == "anime" for r in requests)
    at = john.reference_images.index(armour)
    for request in requests:
        session.pictures.add_images(run_image_request(Images(), request, store=session.pictures))
    session.take_card_pictures()
    pictures = john.reference_images
    assert len(pictures) == count + 2
    # The new ones in the old ones' places, labelled as they were, in the style.
    assert pictures[0].style == "anime" and pictures[0].id != portrait.id
    assert pictures[at].style == "anime" and pictures[at].look == "combat armour"
    # The old ones at the end, unlabelled: the board's tray.
    assert pictures[-2:] == [portrait, armour]
    assert (portrait.look, portrait.expression, armour.look) == ("", "", "")
    # An edit of a restyled picture keeps its style.
    edit = session.card_request(
        john, "Angry.", model="k", info=klein, first=False, source=pictures[0]
    )
    assert edit.card_style == "anime"


def test_a_picture_s_style_is_described_in_words_and_its_bytes_never_logged(tmp_path: Path):
    words = '"Japanese woodblock print, bold ink outlines, flat muted colours."'
    session, provider = doomsville(tmp_path, [words])
    found = session.describe_picture_style(png(), "image/png", model="seeing/model")
    assert found == "Japanese woodblock print, bold ink outlines, flat muted colours"
    sent = provider.requests[-1]
    part = sent.messages[0].parts[0]
    assert part.text == DEFAULT_TEXTS["pictures.style.from_picture"]
    assert part.image.startswith("data:image/png;base64,") and sent.model == "seeing/model"
    from sealedlore.storage.repository import read_api_log

    log = read_api_log(session.story.id, root=tmp_path)
    kinds = [entry["kind"] for entry in log]
    assert "style_from_picture_request" in kinds and "style_from_picture_response" in kinds
    assert "base64," not in json.dumps(log)
    session2, _ = doomsville(tmp_path / "b", ["   "])
    import pytest

    with pytest.raises(ValueError):
        session2.describe_picture_style(png(), "image/png", model="m")
