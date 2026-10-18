"""A picture model's text or edit version, chosen for what is sent (the author,
Oct 18 2026: FLUX.3 Klein's edit id fails with no picture, live HTTP 400; its
text id takes a picture, but its edits were judged below the edit id's)."""

from __future__ import annotations

from sealedlore.engine.image_variants import (
    edit_only,
    edit_versions,
    fitting_model,
    text_versions,
)
from sealedlore.models.image import ImageRef
from sealedlore.providers.images import ImageModelInfo
from tests.test_portraits import session_on

K3 = "blackforestlabs/flux-3/klein/text-to-image"
K3E = "blackforestlabs/flux-3/klein/edit-image"


def listed(*ids: str, takes: int = 10) -> list[ImageModelInfo]:
    return [ImageModelInfo(id=i, name=i, max_inputs=takes) for i in ids]


def test_the_versions_are_read_from_the_ids():
    assert edit_only(K3E) and not edit_only(K3)
    assert text_versions(K3E)[0] == K3
    assert edit_versions(K3)[0] == K3E
    assert text_versions("bytedance/seedream-v5.0-flash/edit") == [
        "bytedance/seedream-v5.0-flash/text-to-image",
        "bytedance/seedream-v5.0-flash",
    ]
    assert "nano-banana-pro-edit" in edit_versions("nano-banana-pro")
    assert text_versions("nano-banana-pro-edit") == ["nano-banana-pro"]
    assert text_versions("flux-2-dev-image-to-image") == ["flux-2-dev"]
    assert text_versions("qwen-image-2.1/edit-lora") == ["qwen-image-2.1/text-to-image-lora"]
    assert edit_versions(K3E) == []


def test_no_picture_goes_to_the_text_version_and_a_picture_to_the_edit_version():
    both = listed(K3, K3E)
    assert fitting_model(K3E, 0, both) == K3, "an edit version given no picture fails"
    assert fitting_model(K3, 1, both) == K3E, "the edit version edits better"
    assert fitting_model(K3, 0, both) == K3 and fitting_model(K3E, 2, both) == K3E
    # A listing older than the model: the edit version would fail for certain.
    assert fitting_model(K3E, 0, []) == K3
    # Listed, with no text version listed: left as chosen.
    assert fitting_model("bria/fibo-edit-1.5/edit", 0, listed("bria/fibo-edit-1.5/edit")) == (
        "bria/fibo-edit-1.5/edit"
    )
    # A plain id that takes pictures edits as itself (Seedream, chosen for
    # keeping characters); one that takes none goes to its edit version.
    pro = "bytedance/seedream-v5.0-pro"
    assert fitting_model(pro, 1, listed(pro, f"{pro}/edit")) == pro
    turbo = listed("z-image-turbo", takes=0) + listed("z-image-turbo-image-to-image")
    assert fitting_model("z-image-turbo", 1, turbo) == "z-image-turbo-image-to-image"
    # None listed: as chosen (FLUX.2 klein 9B takes pictures itself).
    assert fitting_model("flux-2-klein-9b", 1, listed("flux-2-klein-9b")) == "flux-2-klein-9b"
    # An edit version that takes no picture is no edit version.
    assert fitting_model(K3, 1, listed(K3) + listed(K3E, takes=0)) == K3


def test_a_card_s_request_goes_to_the_version_its_pictures_call_for(tmp_path):
    session, _provider = session_on(tmp_path, [])
    session.config.image_models = [
        {"id": m, "supported_parameters": {"max_input_images": 10}} for m in (K3, K3E)
    ]
    john = session.cast[0]
    session.pictures.write("images/refs/j.png", b"\x89PNG\r\n\x1a\n")
    portrait = ImageRef(id="j", file="images/refs/j.png", caption="portrait")
    new = session.card_request(john, "A man.", model=K3E, info=None, first=True)
    assert new.model == K3 and new.references == ()
    edited = session.card_request(
        john, "Edit this picture.", model=K3, info=None, first=False, source=portrait
    )
    assert edited.model == K3E and len(edited.references) == 1
    assert edited.listed_price is None or edited.listed_price >= 0
