"""The line above the transcript that always says how the open story or chat
is set up: where it is kept, which model it is on and how private that is,
the endpoint, the route, and a private scene when one is open. Read-only.

The author: a chat kept in memory only looked like any other, so someone
coming back to it later might not know closing it loses it.
"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtWidgets import QHBoxLayout, QWidget

from sealedlore.engine.routing import routable
from sealedlore.gui.status import ElidingLabel
from sealedlore.providers.private_catalog import privacy_label
from sealedlore.providers.tee import is_private_mode, is_tee

ROLE_NAMES = {
    "summarisation": "Summaries",
    "scene": "Scene read",
    "plot": "Plot",
    "lore": "Lore picks",
    "authoring": "Authoring",
    "image_prompt": "Picture prompts",
}


def _privacy(model: str, tee_state: str) -> str:
    if is_private_mode(model):
        return "🔐 end-to-end encrypted" + (f" ({tee_state})" if tee_state else "")
    if is_tee(model):
        return "TEE" + (f" ({tee_state})" if tee_state else "")
    return ""


def describe_session(session, *, tee_state: str = "") -> tuple[str, str]:
    """(the one line, its tooltip) for the open story or chat."""
    story = session.story
    provider = session.config.active_provider()
    base_url = provider.base_url if provider is not None else ""
    host = urlparse(base_url).hostname or "no endpoint set"
    model = session.model or "(no model)"
    span = session.open_span

    parts: list[str] = []
    tips: list[str] = []
    if story.chat:
        if session.memory_only:
            parts.append("Simple chat · In memory only, not saved")
            tips.append(
                "A simple chat kept in memory only: nothing of it is written on this "
                "computer, not even its log, and closing it loses it. Story → Export → "
                "Story backup… keeps a copy where you choose."
            )
        else:
            parts.append("Simple chat · Saved on disk")
            tips.append("A simple chat, saved on disk like any story.")
    else:
        parts.append("Story · Saved on disk")
        tips.append("A story, saved on disk.")

    privacy = _privacy(model, tee_state)
    parts.append(model + (f" · {privacy}" if privacy else ""))
    label, note = privacy_label(model)
    seen = f" {label}: {note}" if label else " The endpoint and the model's host see every message."
    tips.append(f"Model: {model} on {host}.{seen}")
    parts.append(f"via {host}")

    if routable(base_url, model):
        route = session.route_text("story")
        parts.append(f"{route} · paid" if route else "NanoGPT's routing")
        tips.append(
            f"Route: {route}, billed pay-as-you-go."
            if route
            else "Route: NanoGPT's own choice of host (the subscription's routing)."
        )

    if span is not None:
        where = "in memory only" if span.keep == "memory" else "on disk"
        word = "part" if story.chat else "scene"
        parts.insert(0, f"🔒 Private {word} open: {span.model}, {where}")
        tips.insert(
            0,
            f"A private {word} is open on {span.model}, kept {where}: nothing from it "
            "reaches any other model, and the rest of the story waits until it ends.",
        )

    if not story.chat:
        roles = []
        for role, name in ROLE_NAMES.items():
            role_model = session.role_model(role)
            route = session.route_text(role)
            roles.append(f"  {name}: {role_model}" + (f" ({route}, paid)" if route else ""))
        tips.append("The story's other calls:\n" + "\n".join(roles))
    return "  ·  ".join(parts), "\n\n".join(tips)


class SessionLine(QWidget):
    """One line, shortened with "…" rather than widening the window; the
    whole of it in the tooltip."""

    def __init__(self) -> None:
        super().__init__()
        self.label = ElidingLabel("")
        self.label.setObjectName("sessionLine")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label, 1)
        self.hide()

    def show_session(self, session, *, tee_state: str = "") -> None:
        if session is None:
            self.hide()
            return
        line, tip = describe_session(session, tee_state=tee_state)
        self.label.setText(line)
        self.label.setToolTip(tip)
        self.show()
