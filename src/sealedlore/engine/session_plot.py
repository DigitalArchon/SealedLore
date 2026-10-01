"""The plot's side of a session: visibility, the director, the reads, corrections.

A mixin for `StorySession` (engine/session.py), split out because the session
had grown past three thousand lines and this part has a clean seam: it is
everything that exists only when `story.plot` does. It uses the session's own
state (`story`, `bundle`, `cast`, `supporting`, `nodes`, `provider`, `config`,
`last_result`, `unreported_usage`, `rng`) and its helpers (`path`, `split`,
`save`, `_log`, `priced`, `_character`, `supporting_for`, `assembly_options`,
`uses_cache_control`). The plot file format is `SampleStories/PLOT_FORMAT.md`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass, replace

from sealedlore.engine.archival import (
    render_chunk,
)
from sealedlore.engine.chronicle import (
    armed,
    carried,
    chronicle_at,
    eligible,
    force,
    happen,
    initial_chronicle,
    introduced,
    is_late,
    last_day,
    note_asked,
    note_skipped,
    resolve_due,
    resting,
    schedule,
    scheduled_minutes,
    set_place,
    settle_unconfirmed,
    take_viewpoint,
    variant_named,
)
from sealedlore.engine.chronicle import describe_changes as describe_chronicle
from sealedlore.engine.chronicle import set_fact as change_fact
from sealedlore.engine.chronicle_read import READ_MAX_TOKENS as CHRONICLE_MAX_TOKENS
from sealedlore.engine.chronicle_read import build_read_messages as build_chronicle_messages
from sealedlore.engine.chronicle_read import parse_read as parse_chronicle
from sealedlore.engine.chronicle_read import stated_skip
from sealedlore.engine.director import (
    DIRECTOR_MAX_TOKENS,
    GAP_MAX_TOKENS,
    NOT_YET,
    automatic_directions,
    build_choice_messages,
    build_director_messages,
    build_gap_request,
    engagement,
    held_for_answer,
    merge,
    parse_choice,
    parse_director,
    parse_gap,
    render_direction_block,
    render_story_time_block,
)
from sealedlore.engine.prompt import (
    TurnRequest,
    assemble_question,
)
from sealedlore.engine.retrieval import held_back
from sealedlore.engine.rules import render_cast_sheet
from sealedlore.engine.scene_state import (
    log_on_path,
)
from sealedlore.engine.scene_update import (
    render_card,
)
from sealedlore.engine.validators import (
    TITLES,
    mentions,
)
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import (
    DIRECTOR_SPEAKER_ID,
    NARRATOR_SPEAKER_ID,
    Node,
)
from sealedlore.models.plot import (
    MINUTES_PER_DAY,
    Chronicle,
    Direction,
    EventDef,
    EventState,
    EventVariant,
    Plot,
)
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
)

# The model list the review checks suggestions against; refetched after this.


@dataclass(frozen=True)
class SessionNotice:
    """Something worth telling the author mid-turn — archival, mostly.

    Yielded alongside the provider's stream events so the CLI and the GUI
    worker can surface it on the same path they already use for deltas.
    """

    text: str


@dataclass
class _ChronicleRead:
    """A chronicle read that has been prepared but not yet answered."""

    node: Node
    before: Chronicle
    author_text: str
    places: list[str]
    request: ChatRequest
    log_ref: str
    author_states_facts: bool
    # The least the clock may end at: the author's own stated skip, already
    # moved before the passage was written (`direct_turn`).
    at_least: int | None = None


class PlotRuntime:
    def hidden_ids(self, path: Sequence[Node] | None = None, *, pending: bool = False) -> set[str]:
        """Characters and lore the plot keeps from every model at this point.

        `pending` also counts as brought in whoever a directed event is
        bringing in: the passage that introduces them has to be read and
        checked with them in it, before the read confirms the event. The
        storyteller's cast block leaves `pending` off, so its cached prefix
        only changes when an event is confirmed.
        """
        plot = self.story.plot
        if plot is None:
            return set()
        hidden = {c.id for c in (*self.cast, *self.supporting) if c.plot_hidden}
        hidden |= {entry.id for entry in self.bundle.lore if entry.plot_hidden}
        if not hidden:
            return set()
        chronicle = chronicle_at(self.path() if path is None else path, plot)
        shown = set(chronicle.brought_in)
        if pending:
            for event in plot.events:
                status = chronicle.events.get(event.id)
                if status is not None and status.state == "directed":
                    shown.update(introduced(event, variant_named(event, status.variant)))
        return hidden - shown

    def being_introduced(self) -> list[Character]:
        """Whoever a directed event is bringing into the story at the leaf."""
        arriving = self.hidden_ids() - self.hidden_ids(pending=True)
        return [c for c in (*self.cast, *self.supporting) if c.id in arriving]

    def visible_cast(
        self, path: Sequence[Node] | None = None, *, pending: bool = False
    ) -> list[Character]:
        """The cast as the models see it: less anyone the plot hasn't brought in."""
        hidden = self.hidden_ids(path, pending=pending)
        return [c for c in self.cast if c.id not in hidden] if hidden else list(self.cast)

    def plot_places(self) -> list[str]:
        """The plot's places the story has brought in, for `at` and `visited`."""
        plot = self.story.plot
        if plot is None:
            return []
        hidden = self.hidden_ids(pending=True)
        return [place.name for place in plot.places if place.lore_id not in hidden]

    def _places_mentioned(self, user_node: Node, history: Sequence[Node]) -> set[str]:
        """The plot's places the author's turn or the last passage names, by
        name or by their lore keywords."""
        plot = self.story.plot
        if plot is None:
            return set()
        last = next((node for node in reversed(history) if node.kind == "assistant"), None)
        text = user_node.content + "\n" + (last.content if last is not None else "")
        lore = {entry.id: entry for entry in self.bundle.lore}
        found: set[str] = set()
        for place in plot.places:
            entry = lore.get(place.lore_id)
            words = [place.name, *(entry.keywords if entry is not None else [])]
            if any(
                word.strip() and re.search(rf"\b{re.escape(word.strip())}\b", text, re.IGNORECASE)
                for word in words
            ):
                found.add(place.name)
        return found

    def visible_supporting(self) -> list[Character]:
        hidden = self.hidden_ids(pending=True)
        return [c for c in self.supporting if c.id not in hidden] if hidden else self.supporting

    def visible_lore(self) -> list[LoreEntry]:
        hidden = self.hidden_ids() | self.held_back_ids()
        return [e for e in self.bundle.lore if e.id not in hidden] if hidden else self.bundle.lore

    def held_back_ids(self, path: Sequence[Node] | None = None) -> set[str]:
        """Lore marked "until mentioned" that the author hasn't named on this
        path yet. Not the plot's, but kept from the models the same way.

        Read from `path()`, so a mention inside a private scene reveals
        nothing, there or after: that it was named would be scene text."""
        return held_back(self.bundle.lore, self.path() if path is None else path)

    @property
    def plot_model(self) -> str:
        """The model that keeps the plot's clock and facts, and directs its events."""
        return self.config.plot_model or self.scene_model

    def attach_plot(self, plot: Plot) -> None:
        """Give the story a plot's facts and events, replacing any it had.

        A story already under way starts the plot where it is: the clock at
        the plot's start and every fact at its first value, from the leaf on.
        Earlier passages keep whatever an old plot recorded, fitted to this
        one when read.
        """
        self._refuse_in_private("Attaching a plot")
        self.story.plot = plot.model_copy(deep=True)
        path = self.path()
        if path:
            path[-1].meta.chronicle = initial_chronicle(self.story.plot)
        self.save()

    @property
    def chronicle(self) -> Chronicle | None:
        """The plot's state at the leaf, or None for a story without a plot."""
        plot = self.story.plot
        return chronicle_at(self.path(), plot) if plot is not None else None

    def read_after_turn(self) -> Iterator[SessionNotice]:
        """The reads after a passage (`_read_after_turn`), then a note for any
        model they caught reasoning though asked for nothing."""
        yield from self._read_after_turn()
        passage = self.last_result.node if self.last_result is not None else None
        for note in self.reasoning_notices(passage):
            yield SessionNotice(note)

    def _read_after_turn(self) -> Iterator[SessionNotice]:
        """The scene read, then the chronicle read, after a passage.

        Neither uses the other's answer, so with a provider that can make a
        second call alongside (`sibling()`), the chronicle's request goes out
        while the scene is read: live, the two in series were ~3.3s of
        background work after every passage, about half of it waiting for the
        second call to start. Only the network call leaves this thread; the
        results are applied here, in the usual order, so there is still one
        writer of session state. A scripted provider has no sibling, and its
        replies are taken in order as before.
        """
        if self.story.chat:  # a simple chat is read by nobody
            return
        make_sibling = getattr(self.provider, "sibling", None)
        pending = self._prepare_chronicle_read() if callable(make_sibling) else None
        if pending is None or self.config.scene_reads != "every_turn":
            yield from self.update_scene_after_turn()
            yield from self.update_chronicle_after_turn(pending)
            return
        side = make_sibling()
        box: dict[str, object] = {}

        def call() -> None:
            try:
                box["reply"] = side.complete(pending.request)
            except BaseException as exc:  # noqa: BLE001 - re-raised on this thread below
                box["error"] = exc

        thread = threading.Thread(target=call, name="chronicle-read", daemon=True)
        thread.start()
        try:
            yield from self.update_scene_after_turn()
        finally:
            # A Stop closes this generator and cancels both providers, which
            # ends the call; don't leave without it.
            thread.join()
        yield from self.update_chronicle_after_turn(pending, box)

    def _prepare_chronicle_read(self) -> _ChronicleRead | None:
        """The chronicle read's request for the latest passage, logged; None if
        there is nothing to read."""
        plot = self.story.plot
        if plot is None or not self.config.plot_reads:
            return None
        result = self.last_result
        if result is None or result.node is None or result.stopped_early:
            return None
        node = result.node
        if not node.content.strip():
            return None
        path = self._path_to(node.id)
        before = chronicle_at(path[:-1], plot)
        author = path[-2] if len(path) > 1 and path[-2].kind == "user" else None
        # The author's turn moved the clock before the passage (`direct_turn`):
        # the read still counts from before the turn, as its prompt says, and
        # may not end before the day the turn moved it to.
        skipped = (
            author.meta.chronicle.elapsed
            if author is not None and author.meta.chronicle is not None
            else 0
        )
        at_least = None
        if skipped > 0:
            at_least = before.minutes - before.minutes % MINUTES_PER_DAY
            before.minutes -= skipped
        author_text = (
            render_chunk([author], self.cast, texts=self.texts) if author is not None else ""
        )
        held = self._character(node.meta.controlled_character_id)
        take_viewpoint(before, node.meta.controlled_character_id)
        places = self.plot_places()
        request = ChatRequest(
            model=self.plot_model,
            extra_body=self.route_for("plot"),
            messages=build_chronicle_messages(
                plot=plot,
                chronicle=before,
                author_turn=author_text,
                passage=node.content,
                places=places,
                held=held.name if held is not None else None,
                texts=self.texts,
            ),
            params=self.side_params(self.plot_model, max_tokens=CHRONICLE_MAX_TOKENS),
        )
        log_ref = self._log(
            "chronicle_request",
            {"node_id": node.id, "payload": self.provider.build_payload(request)},
        )
        return _ChronicleRead(
            node=node,
            before=before,
            author_text=author_text,
            places=places,
            request=request,
            log_ref=log_ref,
            author_states_facts=author is not None
            and author.speaker_id in (DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID),
            at_least=at_least,
        )

    def update_chronicle_after_turn(
        self, pending: _ChronicleRead | None = None, fetched: dict[str, object] | None = None
    ) -> Iterator[SessionNotice]:
        """Read how much time the latest passage took and which facts it changed.

        Never costs the author their turn: a failed read leaves the clock and
        the facts as they were, and says so. `fetched` is the reply (or the
        error) of a call already made alongside the scene read.
        """
        plot = self.story.plot
        pending = pending or self._prepare_chronicle_read()
        if plot is None or pending is None:
            return
        node, before, log_ref = pending.node, pending.before, pending.log_ref

        yield SessionNotice(f"Reading the clock and facts with {self.plot_model}…")
        try:
            if fetched is None:
                text, completed = self.provider.complete(pending.request)
            elif "error" in fetched:
                raise fetched["error"]  # type: ignore[misc]
            else:
                text, completed = fetched["reply"]  # type: ignore[misc]
            self._log(
                "chronicle_response",
                {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, self.plot_model))
            delta = parse_chronicle(text)
        except (ProviderError, ValueError) as exc:
            begun = self._confirm_by_introductions(node, before, plot)
            also = (
                " " + ", ".join(event.title for event in begun) + " taken as begun: the passage "
                "brings in who it brings."
                if begun
                else ""
            )
            yield SessionNotice(
                f"Couldn't read the clock and facts from this passage ({exc}). "
                "They are as they were; set them in the Plot panel if they moved." + also
            )
            return

        after = delta.applied_to(
            before,
            plot,
            passage=node.content,
            author_turn=pending.author_text,
            node_id=node.id,
            places=pending.places,
            author_states_facts=pending.author_states_facts,
            scene_location=self.story.scene.location or "",
            at_least=pending.at_least,
        )
        settled = settle_unconfirmed(
            plot, after, [event_id for event_id, _ in delta.happened], node_id=node.id
        )
        if settled:
            self._log(
                "chronicle_settled",
                {"request_log_ref": log_ref, "events": [event.id for event in settled]},
            )
        node.meta.chronicle = after
        self.save()
        if delta.refused:
            self._log("chronicle_refused", {"request_log_ref": log_ref, "refused": delta.refused})
        words = describe_chronicle(after)
        yield SessionNotice(f"Plot: {words}" if words else "Plot: no time passed, no change")

    def _confirm_by_introductions(
        self, node: Node, before: Chronicle, plot: Plot
    ) -> list[EventDef]:
        """With no read, a directed event whose people the passage names has begun.

        Live (GLM 5.3, 429s): a priest's visit was begun and written, the
        read failed, and the director began it again three passages later —
        the priest arrived twice. Only what the event brings counts: a name
        the plot kept hidden until now is evidence no general word can be.
        """
        people = {c.id: c for c in [*self.cast, *self.supporting]}
        after = before.model_copy(
            update={"source": "story", "elapsed": 0, "elapsed_quote": "", "changes": []},
            deep=True,
        )
        begun: list[EventDef] = []
        for event in plot.events:
            status = after.events.get(event.id)
            if status is None or status.state != "directed":
                continue
            variant = variant_named(event, status.variant)
            brought = [people[i] for i in introduced(event, variant) if i in people]
            if not any(mentions(node.content, person) for person in brought):
                continue
            happen(after, event, variant or event.variants[0], node_id=node.id, revealed=True)
            begun.append(event)
        if begun:
            node.meta.chronicle = after
            self.save()
        return begun

    def plot_days(self) -> dict[str, int]:
        """Each timed event's day in this playthrough, drawn the first time it's needed."""
        plot = self.story.plot
        if plot is None:
            return {}
        days = schedule(plot, self.story.plot_schedule, self.rng)
        if days != self.story.plot_schedule:
            self.story.plot_schedule = days
            self.save()
        return days

    def _directed(
        self, turn: TurnRequest, directions: Sequence[Direction], path: Sequence[Node]
    ) -> TurnRequest:
        """The turn with the plot's clock and this passage's instruction in its tail."""
        plot = self.story.plot
        if plot is None:
            return turn
        held = self._character(turn.controlled_character_id)
        return replace(
            turn,
            story_time=render_story_time_block(plot, chronicle_at(path, plot), texts=self.texts),
            direction=render_direction_block(
                [self._unnamed_lead_in(direction) for direction in directions],
                held.name if held else None,
                introductions=self._introductions(directions),
                texts=self.texts,
            ),
            direction_begins=any(direction.kind == "begin" for direction in directions),
        )

    def _unnamed_lead_in(self, direction: Direction) -> Direction:
        """A lead-in without the names of the people its event will bring in.

        They are still hidden, and a name is an invitation. Live (GLM 5.3):
        "Coming, not yet: [a titled officer] arrives at [the setting]…" came
        back as him arriving, announced by name, three events early.
        """
        plot = self.story.plot
        event = plot.event(direction.event_id) if plot is not None else None
        if direction.kind != "lead_in" or event is None:
            return direction
        people = {c.id: c for c in [*self.cast, *self.supporting]}
        forms = sorted(
            {
                form
                for item_id in introduced(event, None)
                if (person := people.get(item_id)) is not None
                for form in (person.name, *person.aliases)
                if form.strip()
            },
            key=len,
            reverse=True,
        )
        text = direction.text
        rank = r"(?:(?:" + "|".join(sorted(TITLES)) + r")\.?\s+)?"
        for form in forms:
            text = re.sub(rf"\b{rank}{re.escape(form)}\b", "someone", text, flags=re.IGNORECASE)
        return direction.model_copy(update={"text": text})

    def _introductions(self, directions: Sequence[Direction]) -> str:
        """The cards of whoever and wherever a beginning event brings in.

        They are hidden from the storyteller until then, so the passage that
        introduces them needs them in full; once the event is seen to happen
        they join the cast and the lore for good (`Chronicle.brought_in`).
        """
        plot = self.story.plot
        if plot is None:
            return ""
        ids: list[str] = []
        for direction in directions:
            if direction.kind != "begin" or (event := plot.event(direction.event_id)) is None:
                continue
            ids += introduced(event, variant_named(event, direction.variant))
        return self._cards(ids)

    def _choose_variant(
        self, event: EventDef, chronicle: Chronicle, history: Sequence[Node]
    ) -> EventVariant:
        """The plot model's pick of how an overdue guaranteed event went.

        Falls back to the first offscreen way, then the first, if the call
        fails: the plot reaches an outcome either way.
        """
        fallback = next((v for v in event.variants if v.offscreen), event.variants[0])
        verbatim = list(self.split(history).verbatim)
        request = ChatRequest(
            model=self.plot_model,
            extra_body=self.route_for("plot"),
            messages=build_choice_messages(
                event=event,
                chronicle=chronicle,
                recent=render_chunk(verbatim[-4:], self.visible_cast(), texts=self.texts),
                texts=self.texts,
            ),
            params=self.side_params(self.plot_model, max_tokens=DIRECTOR_MAX_TOKENS),
        )
        log_ref = self._log(
            "variant_request",
            {"event_id": event.id, "payload": self.provider.build_payload(request)},
        )
        try:
            text, completed = self.provider.complete(request)
        except ProviderError:
            return fallback
        self.unreported_usage.append(self.priced(completed.usage, self.plot_model))
        self._log(
            "variant_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        try:
            return parse_choice(text, event) or fallback
        except ValueError:
            return fallback

    def direct_turn(
        self, turn: TurnRequest, user_node: Node, history: Sequence[Node]
    ) -> Generator[SessionNotice, None, TurnRequest]:
        """Before the storyteller writes: what code decides, then the director if needed.

        Offscreen outcomes and closed windows are settled in code
        (`resolve_due`). The director is asked only when something is armed,
        and never when an event is beginning without asking. What changed is
        a snapshot on the author's turn; the instruction is kept there too,
        for Regenerate. A failed call never costs the turn.
        """
        plot = self.story.plot
        if plot is None or self.story.chat:  # a simple chat has no director
            return turn
        days = self.plot_days()
        chronicle = carried(chronicle_at(history, plot))
        chronicle.source = "story"
        # Conditions are about the character the author now holds.
        switched = take_viewpoint(chronicle, turn.controlled_character_id)
        skipped = self._author_skip(user_node, chronicle)
        resolution = resolve_due(plot, chronicle, days, node_id=user_node.id)
        fired = list(resolution.happened)
        gap = list(resolution.gap)
        for event in resolution.undecided:
            # A guaranteed event reached the end of its window with no way of
            # it fitting: the plot model picks the outcome, code applies it.
            yield SessionNotice(f"Settling an overdue plot event ({self.plot_model})…")
            variant = self._choose_variant(event, chronicle, history)
            force(chronicle, event, variant, days, node_id=user_node.id)
            if chronicle.events[event.id].state == "happened":
                fired.append(event)
            elif is_late(event, chronicle):
                gap.append((event, variant))
        gap_directions: list[Direction] = []
        if gap:
            yield SessionNotice(
                f"Fitting what happened in the time that passed into the story ({self.model})…"
            )
            gap_directions, failure = self._fit_gap(gap, chronicle, days, turn, user_node, history)
            if failure:
                yield SessionNotice(
                    f"Couldn't ask how the skipped-over events went ({failure}); "
                    "the storyteller is given the plot's own words for them."
                )
        changed = (
            switched or skipped or resolution.changed or bool(resolution.undecided) or bool(gap)
        )
        candidates = held_for_answer(
            armed(plot, chronicle, days, mentioned=self._places_mentioned(user_node, history)),
            user_node.content,
        )
        directions = automatic_directions(candidates)
        confirmed: list[str] = []
        consulted = False
        # Where, and who is there: an arrival is a new moment for a resting event.
        scene = self.story.scene
        scene_now = " | ".join(
            [
                scene.location or "",
                *sorted(scene.present_character_ids),
                *sorted(scene.present_others),
            ]
        )
        if candidates and not directions:
            if all(resting(item, chronicle, scene_now) for item in candidates):
                # Everything armed was declined a turn or two ago, in this
                # same scene: no call, and no second's wait before the passage.
                note_skipped(candidates, chronicle)
                candidates = []
                changed = True
        if candidates and not directions:
            consulted = True
            yield SessionNotice(f"Consulting the director ({self.plot_model})…")
            try:
                reply = self._ask_director(turn, user_node, history, chronicle, candidates, days)
            except (ProviderError, ValueError) as exc:
                yield SessionNotice(
                    f"Couldn't consult the director ({exc}); this turn goes ahead without."
                )
            else:
                directions = merge(automatic_directions(candidates), reply.directions)
                confirmed = reply.happened
                begun = {d.event_id for d in directions if d.kind == "begin"}
                note_asked(candidates, begun, chronicle, scene_now)
                changed = True
        directions = [*gap_directions, *directions]

        for event_id in confirmed:
            event = plot.event(event_id)
            status = chronicle.events[event_id]
            variant = variant_named(event, status.variant) or event.variants[0]
            happen(chronicle, event, variant, node_id=status.node_id, revealed=True)
        for direction in directions:
            status = chronicle.events[direction.event_id]
            if direction.kind == "begin":
                if status.state != "directed":
                    # Only when first passed on: one sent again each turn keeps
                    # counting, or it could never be settled.
                    status.node_id = user_node.id
                    status.unconfirmed_passages = 0
                status.state = "directed"
                status.variant = direction.variant
            elif direction.kind == "lead_in":
                status.state = "led_in"
            elif direction.kind == "reveal":
                # Once the author's character learns of it, whoever it concerns
                # is part of the story: live, the fall's aftermath named Elena,
                # who then walked in on day 168 with no card.
                status.revealed = True
                event = plot.event(direction.event_id)
                self._bring_in(
                    chronicle,
                    [
                        *introduced(event, variant_named(event, status.variant)),
                        *self._hidden_named_in(event.aftermath),
                    ],
                )

        if changed or fired or directions or confirmed:
            user_node.meta.chronicle = chronicle
        user_node.meta.direction = list(directions)
        self.save()
        words = self._direction_words(fired, directions, confirmed)
        if words:
            yield SessionNotice(words)
        elif consulted:
            yield SessionNotice("Plot: the director was consulted.")
        return self._directed(turn, directions, [*history, user_node])

    @staticmethod
    def _author_skip(user_node: Node, chronicle: Chronicle) -> bool:
        """Move the clock to where a Director or Narration turn says time goes.

        The author's own statement, so the director, the due events and any
        skipped-over ones are settled against it before the passage telling
        the skip is written. Recorded as the turn's `elapsed`, which the read
        after the passage counts back from. Returns whether it moved.
        """
        if user_node.speaker_id not in (DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID):
            return False
        skip = stated_skip(user_node.content, chronicle.minutes)
        if skip is None or skip[0] <= chronicle.minutes:
            return False
        target, sentence = skip
        chronicle.elapsed = target - chronicle.minutes
        chronicle.elapsed_quote = sentence
        chronicle.minutes = target
        return True

    def _fit_gap(
        self,
        gap: Sequence[tuple[EventDef, EventVariant]],
        chronicle: Chronicle,
        days: dict[str, int],
        turn: TurnRequest,
        user_node: Node,
        history: Sequence[Node],
    ) -> tuple[list[Direction], str]:
        """Record skipped-over events as happened, each with an account of how.

        The account comes from the storyteller's own model, asked on the same
        cached prefix as the turn (like an aside), because it has the whole
        story to fit it into. A failed call falls back to the event's own
        words; the events happen either way. Returns the directions and any
        failure to report.
        """
        held = self._character(turn.controlled_character_id)
        dated = sorted(
            (
                (event, variant, scheduled_minutes(event, days, chronicle.minutes, chronicle))
                for event, variant in gap
            ),
            key=lambda item: item[2],
        )
        ids = [item_id for event, variant, _ in dated for item_id in introduced(event, variant)]
        # What the story says of the skipped time: the author's turn that
        # skips it, when the clock was moved on it (`_author_skip`), else the
        # passage that covered the skip and the turn that asked for it.
        told = [user_node] if chronicle.elapsed else list(history[-2:])
        skipped = render_chunk(told, self.visible_cast(), texts=self.texts) if told else ""
        question = build_gap_request(
            dated,
            held_name=held.name if held else None,
            now=chronicle.minutes,
            introductions=self._cards(ids),
            skipped=skipped,
            texts=self.texts,
        )
        accounts: dict[str, str] = {}
        failure = ""
        try:
            accounts = self._ask_gap(question, [*history, user_node], [e.id for e, _, _ in dated])
        except (ProviderError, ValueError) as exc:
            failure = str(exc)
        directions: list[Direction] = []
        for event, variant, at in dated:
            if accounts.get(event.id) == NOT_YET:
                # The story shows it hasn't happened: it begins now, overdue.
                status = chronicle.events[event.id]
                status.not_in_gap = True
                status.overdue = True
                continue
            account = accounts.get(event.id) or " ".join(
                (variant.tell or event.tell or event.title).split()
            )
            happen(
                chronicle,
                event,
                variant,
                node_id=user_node.id,
                revealed=True,
                at_minutes=at,
                moves=False,
            )
            chronicle.events[event.id].account = account
            self._bring_in(chronicle, self._hidden_named_in(f"{event.aftermath} {account}"))
            directions.append(
                Direction(
                    event_id=event.id,
                    kind="gap",
                    variant=variant.title or None,
                    text=account,
                    why=f"skipped over: due by day {last_day(event, chronicle) or '?'}",
                )
            )
        return directions, failure

    def _ask_gap(self, question: str, path: Sequence[Node], ids: Sequence[str]) -> dict[str, str]:
        split = self.split(path)
        prompt = assemble_question(
            story=self.story,
            cast=self.visible_cast(path),
            history_nodes=split.verbatim,
            question="",
            summaries=split.summaries,
            standing_lore=self.lore_layout().standing,
            supporting=self.supporting_for(path[-1].content, path),
            on_file=self.visible_supporting(),
            scene_log=log_on_path(self.story.scene_log, path),
            estimator=self.estimator,
            options=self.assembly_options(),
            rendered=question,
        )
        request = ChatRequest(
            model=self.model,
            extra_body=self.route_for("story"),
            messages=prompt.messages,
            params=self.side_params(self.model, max_tokens=GAP_MAX_TOKENS),
            use_cache_control=self.uses_cache_control(),
            cache_ttl=self.config.cache_ttl,
        )
        log_ref = self._log("gap_request", {"payload": self.provider.build_payload(request)})
        text, completed = self.provider.complete(request)
        self._log(
            "gap_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, self.model))
        return parse_gap(text, ids)

    def _cards(self, ids: Sequence[str]) -> str:
        people = {c.id: c for c in [*self.cast, *self.supporting]}
        places = {entry.id: entry for entry in self.bundle.lore}
        blocks: list[str] = []
        for item_id in dict.fromkeys(ids):
            if (character := people.get(item_id)) is not None:
                blocks.append(render_cast_sheet(character, include_full_description=True))
            elif (entry := places.get(item_id)) is not None:
                blocks.append(f"{entry.title}: {entry.content}")
        return "\n\n".join(blocks)

    def _hidden_named_in(self, text: str) -> list[str]:
        """Hidden characters and places a piece of plot text names."""
        if not text.strip():
            return []
        found = [
            c.id for c in (*self.cast, *self.supporting) if c.plot_hidden and mentions(text, c)
        ]
        lowered = text.lower()
        found += [
            entry.id
            for entry in self.bundle.lore
            if entry.plot_hidden and entry.title.lower() in lowered
        ]
        return found

    @staticmethod
    def _bring_in(chronicle: Chronicle, ids: Sequence[str]) -> None:
        for item_id in ids:
            if item_id not in chronicle.brought_in:
                chronicle.brought_in.append(item_id)

    def _direction_words(
        self, fired: Sequence, directions: Sequence[Direction], confirmed: Sequence[str]
    ) -> str:
        """What the director did, named only with spoilers on."""
        if not (fired or directions or confirmed):
            return ""
        if not self.config.show_plot_spoilers:
            return "Plot: the director was consulted."
        plot = self.story.plot
        assert plot is not None
        parts = [f"{event.title} happened out of sight" for event in fired]
        parts += [f"{plot.event(event_id).title} had begun" for event_id in confirmed]
        for direction in directions:
            title = plot.event(direction.event_id).title
            variant = f" ({direction.variant})" if direction.variant else ""
            label = {
                "begin": "begins",
                "lead_in": "foreshadowed",
                "reveal": "comes to light",
                "gap": "happened in the time that passed",
            }
            parts.append(f"{title}{variant} {label[direction.kind]}")
        return "Plot: " + "; ".join(parts)

    def _ask_director(
        self,
        turn: TurnRequest,
        user_node: Node,
        history: Sequence[Node],
        chronicle: Chronicle,
        candidates: Sequence,
        days: dict[str, int],
    ):
        plot = self.story.plot
        assert plot is not None
        held = self._character(turn.controlled_character_id)
        verbatim = list(self.split(history).verbatim)
        request = ChatRequest(
            model=self.plot_model,
            extra_body=self.route_for("plot"),
            messages=build_director_messages(
                chronicle=chronicle,
                armed=candidates,
                days=days,
                held_name=held.name if held else "no one in particular",
                scene=render_card(
                    self.story.scene, cast=self.visible_cast(), held_id=held.id if held else None
                ),
                recent=render_chunk(verbatim[-4:], self.visible_cast(), texts=self.texts),
                author_turn=render_chunk([user_node], self.visible_cast(), texts=self.texts),
                engagement_line=engagement(history, user_node.content),
                texts=self.texts,
            ),
            params=self.side_params(self.plot_model, max_tokens=DIRECTOR_MAX_TOKENS),
        )
        log_ref = self._log(
            "director_request",
            {"node_id": user_node.id, "payload": self.provider.build_payload(request)},
        )
        text, completed = self.provider.complete(request)
        self.unreported_usage.append(self.priced(completed.usage, self.plot_model))
        reply = parse_director(text, candidates)
        self._log(
            "director_response",
            {
                "request_log_ref": log_ref,
                "text": text,
                "usage": completed.raw_usage,
                "directions": [d.model_dump() for d in reply.directions],
                "refused": reply.refused,
            },
        )
        return reply

    def mark_event(self, event_id: str, state: EventState) -> None:
        """The author's say on an event: it happened, skip it, or it's still to come."""
        plot = self.story.plot
        if plot is None or (event := plot.event(event_id)) is None:
            raise ValueError(f"no event {event_id}")
        chronicle = carried(chronicle_at(self.path(), plot))
        if state == "happened":
            fitting = eligible(event, chronicle) or event.variants
            node = self.path()[-1] if self.path() else None
            happen(chronicle, event, fitting[0], node_id=node.id if node else None, revealed=True)
        else:
            chronicle.events[event_id] = chronicle.events[event_id].model_copy(
                update={"state": state, "overdue": False}
            )
        self._set_chronicle(chronicle)

    def set_place(self, place: str | None) -> None:
        """Correct where the author's character is (None: none of the plot's places)."""
        plot = self.story.plot
        if plot is None:
            raise ValueError("this story has no plot")
        if place is not None and place not in [p.name for p in plot.places]:
            raise ValueError(f"no place called {place}")
        chronicle = carried(chronicle_at(self.path(), plot))
        if set_place(chronicle, place):
            self._set_chronicle(chronicle)

    def bring_in(self, item_id: str) -> None:
        """The author brings a hidden character or place into the story from here on."""
        plot = self.story.plot
        if plot is None:
            raise ValueError("this story has no plot")
        if not self.path():
            raise ValueError("the story hasn't begun; there is no passage to set it on")
        chronicle = carried(chronicle_at(self.path(), plot))
        if item_id not in chronicle.brought_in:
            chronicle.brought_in.append(item_id)
            self._set_chronicle(chronicle)

    def chronicle_changed_here(self) -> bool:
        """Whether the leaf carries a chronicle the read set, which Undo can take back."""
        path = self.path()
        return (
            bool(path)
            and path[-1].meta.chronicle is not None
            and path[-1].meta.chronicle.source == "story"
        )

    def undo_chronicle_update(self) -> bool:
        """Take back what the read did at the leaf: the clock and facts before it stand."""
        self._refuse_in_private("Undoing a plot change")
        if not self.chronicle_changed_here():
            return False
        self.path()[-1].meta.chronicle = None
        self.save()
        return True

    def _set_chronicle(self, chronicle: Chronicle) -> None:
        """The author's correction, as a snapshot on the leaf. It replaces a read's."""
        # Frozen in a private scene, like the scene card (see `set_scene`).
        self._refuse_in_private("Correcting the plot")
        path = self.path()
        if not path:
            raise ValueError("the story hasn't begun; there is no passage to set it on")
        path[-1].meta.chronicle = chronicle
        self.save()

    def set_clock(self, minutes: int) -> None:
        """Correct the story's clock. Before the first passage, it moves the start."""
        self._refuse_in_private("Correcting the clock")
        plot = self.story.plot
        if plot is None:
            raise ValueError("this story has no plot")
        if not self.path():
            plot.start_minutes = minutes
            self.save()
            return
        chronicle = carried(chronicle_at(self.path(), plot))
        chronicle.minutes = minutes
        self._set_chronicle(chronicle)

    def set_fact(self, name: str, value: str) -> None:
        """Correct a fact from here on."""
        plot = self.story.plot
        if plot is None or (definition := plot.fact(name)) is None:
            raise ValueError(f"no fact called {name}")
        if definition.values and value not in definition.values:
            raise ValueError(f"{name} can't be {value}")
        chronicle = carried(chronicle_at(self.path(), plot))
        if change_fact(chronicle, name, value):
            self._set_chronicle(chronicle)
