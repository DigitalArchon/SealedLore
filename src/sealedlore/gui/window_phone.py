"""Phone access on the window's side (sealedlore/phone/ is the rest).

The API's `Backend`, answered from the story on the GUI thread; the worker's
text passed on to the phones as it streams; the server's life. Tor
(gui/tor_service.py) only says where to listen and which onion address the
phones use.

- **Nothing a phone asks opens a dialog.** Every action checks first what
  would need the desktop's answer (a dialog open, a memory-only scene to
  leave, an attestation) and refuses with a reason the phone shows.
- **The story is read only when nothing runs.** While a passage is written
  the worker is its only writer, so what a phone reads is built when the
  window rebuilds its transcript (`_phone_settled`) and served from there.
- **A phone's turn is the desktop's own**: built from the composer's turn
  with the phone's choices put in, and sent through `_submit_turn`, queued
  during the background reads as the composer's is.
- Version 1 is for text stories and chats: the API refuses a story that
  keeps a map whatever is shared.
"""

from __future__ import annotations

import dataclasses
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QApplication

from sealedlore import __version__
from sealedlore.engine.dice import chip_text
from sealedlore.gui import image_codec
from sealedlore.gui.composer import GENERAL_SKILL, QUESTION, STORY_DEFAULT
from sealedlore.gui.ref_images import image_of
from sealedlore.gui.transcript import message_kind, speaker_label
from sealedlore.ids import utc_now_iso
from sealedlore.models.config import PhoneDevice
from sealedlore.models.node import CHAT_USER_ID
from sealedlore.phone import views
from sealedlore.phone.api import Api, PhoneTurn, Refused, Retake, StoryRow
from sealedlore.phone.page import page_files
from sealedlore.phone.pairing import Claim, Pairing, token_hash
from sealedlore.phone.torrc import find_tor, make_folders, phone_paths
from sealedlore.storage.library import story_listings
from sealedlore.storage.paths import data_home
from sealedlore.storage.repository import save_config

if TYPE_CHECKING:
    from sealedlore.gui.worker import GenerationWorker

TEXT_FLUSH_MS = 150
THINKING_EVERY_MS = 1000
THUMB_WIDTH = 480
_PICTURE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

WAITING_ON_DESKTOP = "The desktop is waiting for an answer. Answer it there first."
BUSY = "A passage is being written. Wait for it to finish."
ALREADY_QUEUED = "A turn is already waiting to go."
NO_ENDPOINT = "Set up an endpoint on the desktop first."
LEAVING_ASKS = "Leaving this story needs an answer on the desktop."
NOT_IN_PRIVATE = "That waits until the private scene ends."
NO_TOR = (
    "Phone access needs tor, and none was found. The AppImage brings its own; run from "
    "source, install tor (for example: sudo apt install tor). Only the program is used, "
    "never a tor service the system runs."
)
NO_TOR_WINDOWS = "Phone access is for Linux for now."
KEY_MISSING = (
    "The phone address's key is missing from the keychain, so phones can't find this "
    "computer at its address. Make a new address (File → Phone devices… → New address…) "
    "and pair the phones again."
)


class PhoneWindow:
    """Mixed into MainWindow."""

    def _init_phone(self) -> None:
        self._phone: _PhoneService | None = None
        self._tor = None  # gui/tor_service.TorService while phone access is on
        # Memory-only chats shared for this run: never written to the config,
        # which learns nothing from them.
        self._phone_memory_shared: set[str] = set()
        self.queued_turn_returned.connect(self._phone_turn_returned)

    # --- phone access on and off: tor and the server ----------------------------------

    def start_phone_access(self) -> str | None:
        """Phone access on for this run: the phones' server listening and
        tor started to give it its onion address. Why not, when it can't be."""
        if self._tor is not None:
            return None
        tor = find_tor()
        if tor is None:
            return NO_TOR_WINDOWS if sys.platform == "win32" else NO_TOR
        phone = self.config.phone
        if "phone" in self.config.keychain_slots and not phone.onion.api_key:
            # Never a new key over one that can't be read: every paired phone
            # would lose the address.
            return KEY_MISSING
        # QtNetwork on first use: phone access is off unless turned on.
        from sealedlore.gui.tor_service import TorService

        paths = phone_paths(self.root or data_home())
        make_folders(paths)
        self.start_phone_server(paths.http, "")
        service = TorService(tor, paths, key=phone.onion.api_key or None, parent=self)
        service.changed.connect(self._on_tor_changed)
        service.address.connect(self._on_tor_address)
        service.key_made.connect(self._on_tor_key)
        self._tor = service
        service.start()
        return None

    def stop_phone_access(self) -> None:
        """Off: the phones told, nothing listening, tor ended."""
        self.stop_phone_server()
        service, self._tor = self._tor, None
        if service is not None:
            service.changed.disconnect(self._on_tor_changed)
            service.stop()
            service.deleteLater()
        self.phone_access_changed.emit()

    @property
    def phone_step(self) -> str:
        """tor's step (gui/tor_service.py), "off" with phone access off."""
        return self._tor.step if self._tor is not None else "off"

    @property
    def phone_words(self) -> str:
        return self._tor.words if self._tor is not None else ""

    def _on_tor_changed(self, _step: str, _words: str) -> None:
        self.phone_access_changed.emit()

    def _on_tor_address(self, onion: str) -> None:
        if self._phone is not None:
            self._phone.onion = onion

    def _on_tor_key(self, key: str, service_id: str) -> None:
        """A key tor made: kept (in the keychain) so the address stays."""
        self.config.phone.onion.api_key = key
        self.config.phone.service_id = service_id
        save_config(self.config, root=self.root)

    # --- the server's life ---------------------------------------------------------

    @property
    def phone_on(self) -> bool:
        return self._phone is not None

    def start_phone_server(self, path: Path, onion: str) -> None:
        """Listen for phones on `path` (tor forwards the onion there)."""
        if self._phone is not None:
            return
        service = _PhoneService(self, onion)
        service.server.listen(path)
        self._phone = service
        self._phone_settled()

    def stop_phone_server(self) -> None:
        service, self._phone = self._phone, None
        if service is not None:
            service.close()

    def phone_answer_claim(self, claim_id: str, allow: bool) -> PhoneDevice | None:
        """The author's answer to a phone asking to be paired. Allowed, the
        phone is kept by its token's hash and told its token once."""
        if self._phone is None:
            return None
        claim = next((c for c in self._phone.pairing.waiting() if c.id == claim_id), None)
        token = self._phone.pairing.answer(claim_id, allow)
        if claim is None or token is None:
            return None
        device = PhoneDevice(
            id=uuid.uuid4().hex,
            name=claim.name,
            token_sha256=token_hash(token),
            paired_at=utc_now_iso(),
            last_seen_day=date.today().isoformat(),
        )
        self.config.phone.devices.append(device)
        save_config(self.config, root=self.root)
        return device

    def phone_revoke(self, device_id: str) -> None:
        self.config.phone.devices = [d for d in self.config.phone.devices if d.id != device_id]
        save_config(self.config, root=self.root)
        if self._phone is not None:
            self._phone.api.hub.drop_device(device_id, "revoked")

    def phone_shared(self, story_id: str) -> bool:
        return story_id in self.config.phone.shared_stories or story_id in self._phone_memory_shared

    def phone_share(self, story_id: str, shared: bool, *, memory: bool = False) -> None:
        """Share a story with the phones, or stop. A memory-only chat's choice
        lasts this run and is never written."""
        if memory:
            (self._phone_memory_shared.add if shared else self._phone_memory_shared.discard)(
                story_id
            )
        else:
            listed = [s for s in self.config.phone.shared_stories if s != story_id]
            self.config.phone.shared_stories = listed + ([story_id] if shared else [])
            save_config(self.config, root=self.root)
        self._phone_settled()

    # --- hooks from the window ---------------------------------------------------------

    def _phone_settled(self) -> None:
        """The window has rebuilt its view of the story (or there is none):
        the phones' view is rebuilt from the same state and they are told."""
        service = self._phone
        if service is None:
            return
        if self._busy:
            service.dirty = True
            return
        service.rebuild()

    def _phone_job_started(self, worker: GenerationWorker, *, streaming: bool, question) -> None:
        if self._phone is not None:
            self._phone.job_started(worker, streaming=streaming, question=question is not None)

    def _phone_job_finished(self) -> None:
        if self._phone is not None:
            self._phone.job_finished()

    def _phone_turn_sent(self, label: str, text: str, kind: str, origin: str) -> None:
        if self._phone is not None:
            self._phone.publish("turn", {"label": label, "text": text, "kind": kind, "by": origin})

    def _phone_turn_returned(self, origin: str, text: str) -> None:
        if self._phone is not None and origin == "phone":
            self._phone.publish("queued_returned", {"text": text})

    # --- what a phone may not do now -------------------------------------------------

    def _phone_refusal(self, *, sending: bool = False) -> None:
        """Raises Refused when the window can't act for a phone now."""
        if QApplication.activeModalWidget() is not None:
            raise Refused(409, WAITING_ON_DESKTOP)
        if self._busy:
            if self._queued is not None:
                raise Refused(409, ALREADY_QUEUED)
            if not (sending and self._can_queue()):
                raise Refused(409, BUSY)
        if self.config.active_provider() is None and not self.use_mock:
            raise Refused(409, NO_ENDPOINT)
        block = self.session.tee_block if self.session is not None else None
        if block:
            raise Refused(409, f"The private model can't be used now: {block}")

    def _phone_private_tag(self) -> str | None:
        session = self.session
        if session is None:
            return None
        span = session.open_span
        if span is not None:
            return "memory" if span.keep == "memory" else "scene"
        if session.memory_only:
            return "memory"
        if session.tee_chat:
            return "tee"
        return None


class _PhoneService(QObject):
    """The server, its API and what the phones are shown, while phone access is on."""

    def __init__(self, window, onion: str) -> None:
        super().__init__(window)
        self.window = window
        self.onion = onion
        self.pairing = Pairing()
        self.api = Api(
            _Backend(window, self),
            pairing=self.pairing,
            onion=lambda: self.onion,
            static=page_files(__version__),
        )
        # QtNetwork on first use: phone access is off unless turned on.
        from sealedlore.gui.phone_server import PhoneServer

        self.server = PhoneServer(self.api, self)
        self.entries: list[dict] = []
        self.branches: list[dict] = []
        self.picture_files: dict[str, str] = {}  # picture id -> its file, for what is shown
        self.version = 0
        self.dirty = False
        self._text: list[str] = []
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(TEXT_FLUSH_MS)
        self._flush.timeout.connect(self._send_text)
        self._thinking = QTimer(self)
        self._thinking.setSingleShot(True)
        self._thinking.setInterval(THINKING_EVERY_MS)
        # Called with each claim, for the desktop to ask the author
        # (gui/phone_dialog.py); without one, the claim waits.
        self.on_claim = None

    def close(self) -> None:
        self._flush.stop()
        self.pairing.close()
        self.server.close()
        self.deleteLater()

    def publish(self, event: str, data: dict, *, content: bool = True) -> None:
        self.api.hub.publish(event, data, content=content)

    def publish_state(self) -> None:
        self.publish("state", self.api.state_payload(), content=False)

    # --- the view ------------------------------------------------------------------

    def rebuild(self) -> None:
        self.dirty = False
        window = self.window
        self.entries, self.branches, self.picture_files = [], [], {}
        if window.session is not None and self.api.open_available():
            self.entries, self.picture_files = _entries(window)
            self.branches = views.branch_rows(window.session.branch_list())
        self.version += 1
        self.publish("changed", {"version": self.version})
        self.publish_state()

    # --- a job ---------------------------------------------------------------------------

    def job_started(self, worker: GenerationWorker, *, streaming: bool, question: bool) -> None:
        self._text.clear()
        worker.text_delta.connect(self.on_text)
        worker.reasoning_delta.connect(self.on_reasoning)
        worker.notice.connect(self.on_notice)
        worker.passage_done.connect(self.on_passage_done)
        worker.failed.connect(self.on_failed)
        self.publish("job_started", {"streaming": streaming, "question": question})
        self.publish_state()

    def job_finished(self) -> None:
        self._send_text()
        self.publish("job_finished", {})

    @Slot(str)
    def on_text(self, text: str) -> None:
        self._text.append(text)
        if not self._flush.isActive():
            self._flush.start()

    def _send_text(self) -> None:
        if self._text:
            self.publish("text", {"text": "".join(self._text)})
            self._text.clear()

    @Slot(str)
    def on_reasoning(self, _text: str) -> None:
        # That the model is thinking, never what: reasoning is kept with the
        # passage and shown nowhere else.
        if not self._thinking.isActive():
            self._thinking.start()
            self.publish("thinking", {})

    @Slot(str)
    def on_notice(self, text: str) -> None:
        self.publish("notice", {"text": text})

    @Slot()
    def on_passage_done(self) -> None:
        self._send_text()
        self.publish("passage_done", {})
        self.publish_state()

    @Slot(str)
    def on_failed(self, error: str) -> None:
        self._send_text()
        self.publish("failed", {"error": (error.splitlines() or ["It failed."])[0][:300]})


def _entries(window) -> tuple[list[dict], dict[str, str]]:
    """One entry per message on the path the author sees, private ones too
    (each tagged), with what the transcript shows beside it."""
    session = window.session
    path = session.full_path()
    chat = session.story.chat
    cast_by_id = {c.id: c for c in session.cast}
    archived = session.split().archived_ids
    take_of = session.take_positions()
    starts = session.branch_start_names()
    spans = {span.id: span for span in session.story.private_spans}
    show_rolls = window.config.show_dice_rolls
    pictures: dict[str, list] = {}
    for image in window._images:
        if image.anchor_node_id and not (image.card_id or image.background):
            pictures.setdefault(image.anchor_node_id, []).append(image)
    files: dict[str, str] = {}
    entries: list[dict] = []
    for index, node in enumerate(path):
        assistant = node.kind == "assistant"
        position, count = take_of(node.id) if assistant else (1, 1)
        shown = pictures.get(node.id, [])
        files.update({image.id: image.file for image in shown})
        entries.append(
            {
                "id": node.id,
                "kind": message_kind(node),
                "label": speaker_label(node, cast_by_id, chat),
                "text": node.content,
                "ooc": node.ooc or None,
                "roll": chip_text(node.meta.roll) if node.meta.roll and show_rolls else None,
                "take": [position, count] if count > 1 else None,
                "branch_start": starts.get(node.id),
                "archived": node.id in archived,
                "private": views.private_tag(
                    node.meta.private_span,
                    spans,
                    memory_chat=session.memory_only,
                    tee_chat=session.tee_chat,
                ),
                "private_summary": bool(node.meta.private_summary_of),
                "pictures": [image.id for image in shown],
                "asides": [
                    {"question": a.question, "answer": a.answer} for a in session.asides_at(node.id)
                ],
                "can_regenerate": assistant,
                "can_reroll": assistant and index == len(path) - 1 and node.meta.roll is not None,
            }
        )
    return entries, files


class _Backend:
    """The API's questions, answered by the window."""

    def __init__(self, window, service: _PhoneService) -> None:
        self.window = window
        self.service = service

    # --- what there is ------------------------------------------------------------

    def open_story(self) -> StoryRow | None:
        session = self.window.session
        if session is None:
            return None
        story = session.story
        return StoryRow(
            id=story.id,
            title=story.title,
            messages=len(self.service.entries),
            chat=story.chat,
            keeps_map=story.defaults.world_map,
        )

    def story_rows(self) -> list[StoryRow]:
        rows = [
            StoryRow(s.id, s.title, s.messages, s.chat, s.keeps_map)
            for s in story_listings(self.window.root)
            if s.error is None
        ]
        open_row = self.open_story()
        if open_row is not None and all(row.id != open_row.id for row in rows):
            rows.insert(0, open_row)  # a memory-only chat: on no disk
        return rows

    def shared_ids(self) -> set[str]:
        return set(self.window.config.phone.shared_stories) | self.window._phone_memory_shared

    def devices(self) -> list[tuple[str, str]]:
        return [(d.id, d.token_sha256) for d in self.window.config.phone.devices]

    def state(self) -> dict:
        window = self.window
        return {
            "busy": window._busy,
            "background": window._background,
            "queued": window._queued is not None,
            "desktop_waiting": QApplication.activeModalWidget() is not None,
            "private": window._phone_private_tag(),
            "version": self.service.version,
        }

    def transcript(self, before: str | None, limit: int) -> dict:
        answer = views.page(self.service.entries, before, limit)
        answer["version"] = self.service.version
        return answer

    def composer(self) -> dict:
        window = self.window
        composer = window.composer
        if window.in_chat:
            return {
                "chat": True,
                "speakers": [{"value": CHAT_USER_ID, "label": "You"}],
                "speaker": CHAT_USER_ID,
            }

        def items(combo, skip) -> list[dict]:
            return [
                {"value": str(combo.itemData(i)), "label": combo.itemText(i)}
                for i in range(combo.count())
                if combo.itemData(i) not in skip
            ]

        return {
            "chat": False,
            "speakers": items(composer.speaker, ()),
            "speaker": composer.speaker_id(),
            "question": QUESTION,
            "held": composer.held_character_id(),
            "lengths": items(composer.length, (STORY_DEFAULT, None)),
            "length_default": composer.length.itemText(0),
            "length": composer.response_style(),
            "outcomes": items(composer.agency, (None,)),
            "outcome_default": composer.agency.itemText(0),
            "outcome": composer.agency_override(),
            "story_outcome": window.session.story.defaults.agency_mode,
            "skills": items(composer.skill, (GENERAL_SKILL, None)),
            "difficulties": items(composer.difficulty, (None,)),
            "difficulty": "standard",
        }

    def branches(self) -> dict:
        return {"branches": self.service.branches}

    def picture(self, image_id: str, thumb: bool) -> tuple[bytes, str] | None:
        file = self.service.picture_files.get(image_id)
        session = self.window.session
        if file is None or session is None:
            return None
        data = session.pictures.read(file)
        if not data:
            return None
        if not thumb:
            return data, _PICTURE_TYPES.get(Path(file).suffix.lower(), "application/octet-stream")
        image = image_of(data)
        if image.isNull():
            return None
        if image.width() > THUMB_WIDTH:
            image = image.scaledToWidth(THUMB_WIDTH)
        return image_codec.encoded(image, "JPEG", 82), "image/jpeg"

    # --- what a phone may do -------------------------------------------------------

    def open(self, story_id: str) -> None:
        window = self.window
        if window._busy:
            raise Refused(409, BUSY)
        if QApplication.activeModalWidget() is not None:
            raise Refused(409, WAITING_ON_DESKTOP)
        session = window.session
        if session is not None and (session.memory_only or window._phone_private_tag() == "memory"):
            raise Refused(409, LEAVING_ASKS)
        # Opened once this answer is on its way: the phone hears of it by
        # its events, as it would of the desktop opening one.
        QTimer.singleShot(0, lambda: window.open_story(story_id))

    def send(self, turn: PhoneTurn, device: str) -> dict:
        window = self.window
        window._phone_refusal(sending=True)
        will_queue = window._busy
        if not window.in_chat and turn.speaker == QUESTION:
            window._submit_ask(turn.text, origin="phone")
            return {"queued": will_queue}
        if window.in_chat:
            request = window._chat_turn(turn.text)
        else:
            request = window._current_turn(turn.text)
            agency = turn.outcome or window.session.story.defaults.agency_mode
            rolls = agency == "dice" and turn.speaker == request.controlled_character_id
            request = dataclasses.replace(
                request,
                speaker_id=turn.speaker,
                response_style=turn.length,
                agency_mode=agency,
                dice_domain=turn.skill if rolls else None,
                difficulty=turn.difficulty or "standard",
                ooc=None,
            )
        window._submit_turn(turn.text, request, origin="phone")
        return {"queued": will_queue}

    def stop(self) -> None:
        if self.window._busy:
            self.window.stop_generation()

    def _shape(self, retake: Retake):
        shape = self.window._current_shape()
        if shape is None:  # a chat: the same message again
            return None
        return dataclasses.replace(
            shape,
            response_style=retake.length,
            agency_mode=retake.outcome or self.window.session.story.defaults.agency_mode,
        )

    def _assistant_on_path(self, node_id: str | None) -> str:
        ids = [e["id"] for e in self.service.entries if e["kind"] == "assistant"]
        if not ids or (node_id is not None and node_id not in ids):
            raise Refused(409, "That passage isn't in the story as it stands.")
        return node_id or ids[-1]

    def regenerate(self, retake: Retake) -> None:
        self.window._phone_refusal()
        node_id = self._assistant_on_path(retake.node_id)
        self.window._regenerate_node(node_id, self._shape(retake))

    def reroll(self, node_id: str) -> None:
        self.window._phone_refusal()
        entry = next((e for e in self.service.entries if e["id"] == node_id), None)
        if entry is None or not entry["can_reroll"]:
            raise Refused(409, "Only the latest roll can be rolled again.")
        self.window.reroll(node_id, self._shape(Retake(node_id)))

    def switch_take(self, node_id: str, step: int) -> None:
        self.window._phone_refusal()
        if self.window.session.in_private:
            raise Refused(409, NOT_IN_PRIVATE)
        entry = next((e for e in self.service.entries if e["id"] == node_id), None)
        if entry is None or entry["take"] is None:
            raise Refused(409, "That passage has no other take.")
        position, count = entry["take"]
        if not 1 <= position + step <= count:
            raise Refused(409, "There is no take that way.")
        self.window._switch_variant(node_id, step)

    def switch_branch(self, start_id: str) -> None:
        self.window._phone_refusal()
        if self.window.session.in_private:
            raise Refused(409, NOT_IN_PRIVATE)
        self.window._switch_branch(start_id or None)

    # --- the phones themselves -------------------------------------------------------

    def claimed(self, claim: Claim) -> None:
        if self.service.on_claim is not None:
            self.service.on_claim(claim)

    def seen(self, device: str) -> None:
        today = date.today().isoformat()
        for each in self.window.config.phone.devices:
            if each.id == device and each.last_seen_day != today:
                each.last_seen_day = today  # saved with the config's next save

    def forget(self, device: str) -> None:
        config = self.window.config
        config.phone.devices = [d for d in config.phone.devices if d.id != device]
        save_config(config, root=self.window.root)

    def warn(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 10_000)
