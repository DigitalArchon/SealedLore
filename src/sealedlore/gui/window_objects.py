"""The window's side of Objects… (gui/objects_panel.py, engine/
session_objects.py; the author, Oct 18 2026: make objects by saying what
they are, place or remove any object, mainly to set an area up at the start)
and of making a card for someone the story has by name alone (AfterDark:
Suzuka, named in the opening, had none). A mixin for `MainWindow`."""

from __future__ import annotations

from PySide6.QtCore import QTimer

from sealedlore.engine.session import SessionNotice
from sealedlore.gui.objects_panel import ObjectsPanel


class ObjectsWindow:
    _objects_panel: ObjectsPanel | None = None

    def _wire_objects(self) -> None:
        self.area_view.objects_requested.connect(self.open_objects)
        self.area_view.setup_offer_dismissed.connect(self._objects_offer_dismissed)
        self.area_view.tool_stopped.connect(self._objects_tool_stopped)
        self.area_view.card_requested.connect(self.make_card)
        self.people_page.card_requested.connect(self.make_card)
        self.people_page.name_requested.connect(self._people_name)

    def _people_name(self, label: str) -> None:
        """Name… on the People page: the map's correction, as on the map."""
        if self.session is None or self._correction_blocked():
            return
        self._correct_name(label)
        self._refresh_people()

    # --- the panel ---------------------------------------------------------------

    def open_objects(self) -> None:
        session = self.session
        if session is None or not session.map_first:
            return
        if session.in_private:
            self.statusBar().showMessage("A private scene is open: the map is paused.", 5000)
            return
        panel = self._objects_panel
        if panel is None:
            panel = ObjectsPanel(self)
            panel.offer_for = self._objects_offer_words
            panel.parse = session.parse_objects
            panel.picture_of = lambda choice: (
                self.session.object_picture(choice) if self.session is not None else None
            )
            panel.create_requested.connect(self._objects_create)
            panel.place_chosen.connect(lambda name: self.area_view.set_placing(name or None))
            panel.remove_toggled.connect(self.area_view.set_removing)
            panel.forget_requested.connect(self._objects_forget)
            self._objects_panel = panel
            panel.place_beside(self)
        session.objects_offered()
        self.area_view.set_setup_offer(None)
        # The play view is where they are placed.
        if self.right_tabs.currentWidget() is not self.world_view:
            self.right_tabs.setCurrentWidget(self.world_view)
        if hasattr(self.world_view, "go_local"):
            self.world_view.go_local()
        # The price and the model's name are the image listing's.
        self.image_catalog.ensure()
        # Shown first: a hidden panel isn't filled, and the lists came up
        # empty every time it opened.
        panel.show()
        self._sync_objects_panel()
        panel.raise_()
        panel.what.setFocus()

    def _sync_objects_panel(self) -> None:
        panel = self._objects_panel
        session = self.session
        if panel is None or not panel.isVisible() or session is None:
            return
        panel.set_choices(session.object_choices())

    def _close_objects(self) -> None:
        """The story closes: the panel goes with it."""
        panel = self._objects_panel
        if panel is not None:
            panel.close()
            panel.deleteLater()
            self._objects_panel = None
        self.area_view.set_placing(None)
        self.area_view.set_removing(False)

    def _objects_offer_words(self, names: list[str]) -> tuple[str, bool]:
        session = self.session
        if session is None:
            return "", False
        offer = session.objects_offer(names)
        listed = ", ".join(names)
        have = [n for n in names if n not in offer.to_draw]
        if offer.problem is not None:
            return (
                f"Makes {listed}. {offer.problem} Until a picture is drawn, each stands in as "
                "the tile set's nearest thing.",
                False,
            )
        if not offer.to_draw:
            return f"The tile set has {listed} already: added to your objects.", True
        count = len(offer.to_draw)
        each = (
            f"${offer.price_each:.3f} each"
            if offer.price_each is not None
            else "at a price the listing doesn't give"
        )
        model = session.model_name(offer.model)
        words = f"Draws {count} picture{'s' if count > 1 else ''} with {model}, {each}"
        if have:
            words += f"; the tile set has {', '.join(have)} already"
        return words + ".", True

    def _objects_create(self, names: list[str]) -> None:
        session = self.session
        if session is None or not names:
            return
        drawing = session.create_objects(names)
        panel = self._objects_panel
        if panel is not None:
            if drawing:
                panel.say(
                    f"Drawing {', '.join(drawing)}: each shows here when it is in. They can be "
                    "placed now, standing in as the nearest thing until then."
                )
            else:
                panel.say(f"Added {', '.join(names)}: choose one and click on the map.")
        self._sync_objects_panel()

    def _objects_forget(self, name: str) -> None:
        if self.session is not None and self.session.forget_object(name):
            self._sync_objects_panel()

    def _objects_tool_stopped(self) -> None:
        if self._objects_panel is not None:
            self._objects_panel.tool_stopped()

    def _objects_offer_dismissed(self) -> None:
        if self.session is not None:
            self.session.objects_offered()
        self.area_view.set_setup_offer(None)

    # --- a card for someone with none ---------------------------------------------

    def make_card(self, name: str, to_cast: bool = False) -> None:
        """A card for someone the story names with none, written from the
        story by the scan for new characters (else the name alone), then
        opened on the Cast page, where their portrait and avatar are."""
        session = self.session
        if session is None or not name.strip():
            return
        if self._busy:
            self.statusBar().showMessage(
                "A card can be made once the story's current job is done.", 6000
            )
            return
        if session.in_private:
            self.statusBar().showMessage("Not in a private scene.", 5000)
            return
        if self.config.active_provider() is not None or self.use_mock:
            session.provider = self._current_provider()
        made: dict[str, object] = {}

        def job():
            card, filled = session.card_for(name, to_cast=to_cast)
            made["card"], made["filled"] = card, filled
            yield SessionNotice(f"A card for {card.name}.")

        def after() -> None:
            card = made.get("card")
            if card is None or self.session is not session:
                return
            self.refresh_panels()
            self._refresh_people()
            self._open_profile(card.id)
            words = (
                f"A card for {card.name}, written from the story: check it, and draw a portrait."
                if made.get("filled")
                else f"A card for {card.name}: the story says little of them yet, so fill it in."
            )
            self.statusBar().showMessage(words, 10000)
            self._sync_world()
            self.refresh_area()

        self._after_job = after
        self._start(
            job,
            streaming=False,
            busy_text=f"Writing a card for {name} from what the story says of them…",
            job="Card",
        )

    def _run_after_job(self) -> None:
        after, self._after_job = getattr(self, "_after_job", None), None
        if after is not None:
            QTimer.singleShot(0, after)
