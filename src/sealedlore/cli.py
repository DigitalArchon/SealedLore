"""Terminal client for SealedLore's engine.

Creates, lists, exports, imports and duplicates stories, drafts one from a
premise, and plays one in the terminal (`chat`), through the same engine the
desktop app uses. Handy for scripting and for checking the engine without a
window; run with --mock to try it without an endpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sealedlore import __version__
from sealedlore.engine.archival import chapter_number, turns_in
from sealedlore.engine.authoring import current_value, describe, format_value
from sealedlore.engine.dice import DIFFICULTY_ORDER, chance_for, chip_text, tier_for
from sealedlore.engine.drafting import StoryDraft
from sealedlore.engine.export import render_markdown
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_edits import texts_in_force
from sealedlore.engine.reasoning import config_params, config_reasoning
from sealedlore.engine.response_style import PRESETS, label_for
from sealedlore.engine.routing import config_route
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.models.character import Character
from sealedlore.models.config import (
    DEFAULT_BASE_URL,
    DEFAULT_STORY_MODEL,
    Config,
    ProviderConfig,
    recommended_models,
    require_https,
)
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import (
    DIRECTOR_SPEAKER_ID,
    NARRATOR_SPEAKER_ID,
    AgencyMode,
    Difficulty,
    NpcScope,
    ResponseStyle,
)
from sealedlore.models.story import Story
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError, TextDelta
from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.storage.archive import ArchiveError, import_archive, write_archive
from sealedlore.storage.images import all_image_files, reference_files
from sealedlore.storage.paths import stories_dir
from sealedlore.storage.repository import (
    StoryBundle,
    copy_story,
    delete_story,
    load_config,
    load_story_bundle,
    read_api_log,
    save_config,
    save_story_bundle,
)
from sealedlore.storage.scenario import (
    ScenarioError,
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)
from sealedlore.tree import active_path, sibling_position

AGENCY_MODES: tuple[AgencyMode, ...] = ("fiat", "plausible", "contested", "dice")

HELP = """\
Commands
  /help                  this list
  /quit                  leave (everything is already saved)
  /cast                  list the cast
  /cast add <name> | <summary>
  /hold <name>           take control of a character (the model may not write them)
  /present <name,...>    set who is in the scene (cast or anyone else)
  /scene <situation>     set the scene's situation line
  /where <location>      set the location
  /privacy on|off        who can know what happens here: only those present, or everyone
  /undo-scene            take back the scene change the last read made
  /speaker <name|narrator|director>
  /ooc <text>            attach an out-of-character addendum to the next turn
  /mode <fiat|plausible|contested|dice>
  /dice [skill|general] [trivial|easy|standard|hard|desperate]
                         what the next dice roll uses (skill = one of the held
                         character's tiers)
  /reroll                roll again for the last dice turn and write a new take
  /npc <all|choice|name,...>
  /len <preset|text>     length for the next turn: adaptive, brief, standard,
                         descriptive, literary — or your own wording
  /style <preset|text>   the story's default length (same choices)
  /regen                 another take on the last turn (a sibling, not a replacement)
  /begin [name]          start an unplayed story from its opening, playing <name>
  /undo                  delete the last exchange (your turn and its reply)
  /ask <question>        ask the model something out of character; saved as an aside
  /review <complaint>    have the model propose changes to the story's settings
                         (shown only; to apply, run the review in the app)
  /path                  the active path
  /inspect               the assembled prompt, section by section
  /budget                token budget breakdown
  /summaries             the chapter summaries standing in for archived turns
  /archive               archive the oldest chunk now, without waiting for the budget
  /lore                  list the lorebook
  /lore add <title> | <content> | <kw1,kw2>
  /retrieval             what lore the last turn injected, and why

Anything else is your turn, spoken by the current speaker."""


def build_provider(config: Config, *, mock: bool) -> ChatProvider:
    if mock:
        return MockChatProvider()
    provider_config = config.active_provider()
    if provider_config is None:
        raise SystemExit(
            "No provider configured. Run `sealedlore configure --api-key ...`, or pass --mock."
        )
    return OpenAICompatibleProvider(provider_config)


def cmd_configure(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    config = load_config(root=root)
    name = args.name
    try:
        # Assignment below skips the model's validator; check here so the
        # answer comes now and not at the next start.
        require_https(args.base_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    existing = next((p for p in config.providers if p.name == name), None)
    provider = existing or ProviderConfig(
        name=name, base_url=args.base_url, model=DEFAULT_STORY_MODEL
    )
    provider.base_url = args.base_url
    if args.api_key is not None:
        provider.api_key = args.api_key
    if args.model is not None:
        provider.model = args.model
    if existing is None:
        if not config.providers:
            # A first setup starts with the recommended models for the small
            # per-turn calls, as Settings does; a blank one already saved stays.
            for field, model in recommended_models(provider.base_url).items():
                if not getattr(config, field):
                    setattr(config, field, model)
        config.providers.append(provider)
    config.active_provider_name = name
    save_config(config, root=root)
    print(f"Saved provider {name!r} -> {provider.base_url} (model: {provider.model or 'unset'})")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    config = load_config(root=root)
    story = Story(title=args.title)
    if args.model:
        story.defaults.main_model = args.model
    elif (active := config.active_provider()) is not None:
        story.defaults.main_model = active.model
    save_story_bundle(StoryBundle(story=story), root=root)
    print(f"Created story {story.title!r}\n  id: {story.id}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    config = load_config(root=root)
    provider = build_provider(config, mock=args.mock)
    active = config.active_provider()
    chat_model = active.model if active else None
    model = args.model or config.authoring_model or chat_model or ""
    draft = StoryDraft(
        provider,
        args.premise,
        model,
        texts=texts_in_force(config.prompt_edits),
        route=config_route(config, "authoring", model),
        reasoning=config_reasoning(config, "side", model),
    )
    print(f"Drafting with {draft.model}…", flush=True)
    try:
        for _ in draft.stream():
            pass
        bundle, warnings = draft.build(main_model=chat_model, root=root)
    except (ProviderError, ValueError) as exc:
        print(f"Could not draft the story: {exc}", file=sys.stderr)
        return 1
    print(f"Created story {bundle.story.title!r}\n  id: {bundle.story.id}")
    print(f"  cast: {', '.join(c.name for c in bundle.cast)}")
    for warning in warnings:
        print(f"  left out: {warning}")
    print("  run `sealedlore chat <id>` and /begin <name> to start from the opening")
    return 0


def cmd_scenario_export(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    bundle = load_story_bundle(args.story_id, root=root)
    try:
        write_scenario(args.file, scenario_from_bundle(bundle, reference_files(bundle, root)))
    except ScenarioError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Exported {bundle.story.title!r} to {args.file}")
    return 0


def cmd_scenario_import(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    try:
        scenario = read_scenario(args.file)
    except ScenarioError as exc:
        print(exc, file=sys.stderr)
        return 1
    active = load_config(root=root).active_provider()
    bundle = bundle_from_scenario(scenario, main_model=active.model if active else None)
    save_story_bundle(bundle, root=root)
    print(f"Created story {bundle.story.title!r}\n  id: {bundle.story.id}")
    print("  run `sealedlore chat <id>` and /begin <name> to start from the opening")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    bundle = load_story_bundle(args.story_id, root=root)
    if args.markdown:
        # As the app exports it: a private scene's messages left out, its
        # approved summary standing in for them.
        path = StorySession.model_nodes(active_path(bundle.nodes, bundle.story.active_leaf_id))
        args.file.write_text(
            render_markdown(bundle.story.title, path, bundle.cast), encoding="utf-8"
        )
        print(f"Wrote {len(path)} messages of {bundle.story.title!r} to {args.file}")
        return 0
    write_archive(
        args.file,
        bundle,
        read_api_log(bundle.story.id, root=root),
        all_image_files(bundle.story.id, root),
    )
    print(f"Archived {bundle.story.title!r} ({len(bundle.nodes)} messages) to {args.file}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    try:
        bundle = import_archive(args.file, root=root)
    except ArchiveError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Imported {bundle.story.title!r}\n  id: {bundle.story.id}")
    return 0


def cmd_duplicate(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    try:
        source = load_story_bundle(args.story_id, root=root)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    title = f"{source.story.title} (copy)"
    if args.settings_only:
        bundle = fresh_playthrough(source, title, reference_files(source, root))
        save_story_bundle(bundle, root=root)
    else:
        bundle = copy_story(args.story_id, title=title, root=root)
    print(f"Created {bundle.story.title!r}\n  id: {bundle.story.id}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    try:
        bundle = load_story_bundle(args.story_id, root=root)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if not args.yes:
        # No desktop trash from a terminal, so this one is for good.
        answer = input(
            f"Permanently delete {bundle.story.title!r} ({len(bundle.nodes)} messages)? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Kept.")
            return 1
    delete_story(args.story_id, root=root)
    print(f"Deleted {bundle.story.title!r}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    directory = stories_dir(root)
    if not directory.exists():
        print("No stories yet.")
        return 0
    for entry in sorted(directory.iterdir()):
        try:
            bundle = load_story_bundle(entry.name, root=root)
        except (FileNotFoundError, ValueError):
            continue
        print(f"{bundle.story.id}  {bundle.story.title}  ({len(bundle.nodes)} nodes)")
    return 0


class Repl:
    def __init__(self, session: StorySession) -> None:
        self.session = session
        self.speaker_id: str = NARRATOR_SPEAKER_ID
        self.controlled_id: str | None = None
        self.pending_ooc: str | None = None
        self.npc_scope: NpcScope = session.story.defaults.npc_scope
        self.npc_scope_ids: tuple[str, ...] = ()
        self.agency_mode: AgencyMode = session.story.defaults.agency_mode
        self.length_hint: str | None = None
        self.response_style: ResponseStyle | None = None
        self.dice_domain: str | None = None
        self.difficulty: Difficulty = "standard"

    # --- helpers -----------------------------------------------------------

    def speaker_label(self) -> str:
        if self.speaker_id == NARRATOR_SPEAKER_ID:
            return "Narrator"
        if self.speaker_id == DIRECTOR_SPEAKER_ID:
            return "Director"
        character = next((c for c in self.session.cast if c.id == self.speaker_id), None)
        return character.name if character else "Unknown"

    def prompt_label(self) -> str:
        held = ""
        if self.controlled_id:
            character = next((c for c in self.session.cast if c.id == self.controlled_id), None)
            if character:
                held = f" holding {character.name}"
        return f"[{self.speaker_label()}{held} · {self.agency_mode}] "

    def turn_request(self, text: str) -> TurnRequest:
        return TurnRequest(
            speaker_id=self.speaker_id,
            user_text=text,
            controlled_character_id=self.controlled_id,
            ooc=self.pending_ooc,
            npc_scope=self.npc_scope,
            npc_scope_ids=self.npc_scope_ids,
            agency_mode=self.agency_mode,
            length_hint=self.length_hint,
            response_style=self.response_style,
            dice_domain=self.dice_domain,
            difficulty=self.difficulty,
        )

    def resolve(self, name: str) -> Character | None:
        character = self.session.character_by_name(name)
        if character is None:
            print(f"  no character named {name!r}")
        return character

    # --- main loop ---------------------------------------------------------

    def run(self) -> int:
        story = self.session.story
        print(f"{story.title}  ({len(self.session.nodes)} nodes) — /help for commands")
        while True:
            try:
                line = input(self.prompt_label())
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if self.command(line) is False:
                    return 0
                continue
            if self.session.in_private:
                # The terminal has no private model to send to, and a turn
                # attached here would sit under the scene unsent.
                print("  this story is in a private scene; end it in the app first")
                continue
            self.stream(self.session.send(self.turn_request(line)))
            self.pending_ooc = None
            self.length_hint = None
            self.response_style = None
            self.difficulty = "standard"
            self.report_roll()

    def stream(self, events, *, report: bool = True) -> None:
        print()
        try:
            for event in events:
                if isinstance(event, SessionNotice):
                    print(f"  · {event.text}\n")
                elif isinstance(event, TextDelta):
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
        except ProviderError as exc:
            print(f"\n  provider error: {exc}")
            if exc.body:
                print(f"  {exc.body[:500]}")
            return
        except KeyboardInterrupt:
            print("\n  stopped — partial text kept")
        print("\n")
        if not report:
            return
        result = self.session.last_result
        prompt = self.session.last_prompt
        if result and prompt:
            usage = result.usage
            print(
                f"  ~{prompt.budget.total} prompt tokens estimated, "
                f"{usage.prompt_tokens} reported "
                f"(cache: {usage.cache_read_tokens} read / "
                f"{usage.cache_creation_tokens} written), "
                f"{usage.completion_tokens} out, ${usage.cost:.4f}"
            )
            if prompt.needs_archival:
                print(
                    f"  budget: {len(prompt.excluded_node_ids)} oldest messages did not fit "
                    "and were left out of this prompt — /archive, or raise the budget"
                )

    def command(self, line: str) -> bool:
        parts = line[1:].split(maxsplit=1)
        name = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        session = self.session
        story = session.story

        if name in {"quit", "exit", "q"}:
            return False
        if name == "help":
            print(HELP)
        elif name == "cast":
            self.command_cast(rest)
        elif name == "hold":
            character = self.resolve(rest)
            if character:
                self.controlled_id = character.id
                self.speaker_id = character.id
                moved = session.hold(character.id)
                print(
                    f"  holding {character.name}" + ("; the scene moves to them" if moved else "")
                )
        elif name == "present":
            names = [item for item in (n.strip() for n in rest.split(",")) if item]
            scene = story.scene.model_copy(deep=True)
            scene.present_character_ids = []
            scene.present_others = []
            for item in names:
                character = session.character_by_name(item)
                if character is not None:
                    scene.present_character_ids.append(character.id)
                else:
                    # Anyone else named is in the room too, as written.
                    scene.present_others.append(item)
            scene.tracked = True
            session.set_scene(scene)
            print(f"  present: {', '.join(n for n in names) or '(nobody named)'}")
        elif name == "scene":
            scene = story.scene.model_copy(deep=True)
            scene.situation = rest or None
            session.set_scene(scene)
            print("  scene updated")
        elif name == "where":
            scene = story.scene.model_copy(deep=True)
            scene.location = rest or None
            session.set_scene(scene)
            print("  location updated")
        elif name in ("privacy", "private"):
            scene = story.scene.model_copy(deep=True)
            scene.privacy = {"on": "private", "off": "public"}.get(rest.lower())
            session.set_scene(scene)
            print(f"  privacy: {scene.privacy or 'not recorded'}")
        elif name == "undo-scene":
            print("  scene change undone" if session.undo_scene_update() else "  nothing to undo")
        elif name == "speaker":
            self.command_speaker(rest)
        elif name == "ooc":
            self.pending_ooc = rest or None
            print("  OOC attached to your next turn" if rest else "  OOC cleared")
        elif name == "dice":
            self.command_dice(rest)
        elif name == "reroll":
            try:
                self.stream(session.regenerate(reroll=True))
            except ValueError as exc:
                print(f"  {exc}")
            else:
                self.report_roll()
        elif name == "mode":
            if rest in AGENCY_MODES:
                self.agency_mode = rest  # type: ignore[assignment]
                print(f"  agency mode: {rest}")
            else:
                print(f"  modes: {', '.join(AGENCY_MODES)}")
        elif name == "npc":
            self.command_npc(rest)
        elif name == "len":
            self.command_length(rest)
        elif name == "style":
            self.command_style(rest)
        elif name == "regen":
            try:
                self.stream(session.regenerate())
            except ValueError as exc:
                print(f"  {exc}")
            else:
                leaf = story.active_leaf_id
                if leaf:
                    position, count = sibling_position(session.nodes, leaf)
                    print(f"  take {position} of {count}")
        elif name == "begin":
            self.command_begin(rest)
        elif name == "undo":
            self.command_undo()
        elif name == "ask":
            if rest:
                self.stream(session.ask(rest), report=False)
            else:
                print("  /ask <question>")
        elif name == "review":
            self.command_review(rest)
        elif name == "path":
            self.command_path()
        elif name == "inspect":
            self.command_inspect()
        elif name == "budget":
            self.command_budget()
        elif name == "summaries":
            self.command_summaries()
        elif name == "archive":
            self.command_archive()
        elif name == "lore":
            self.command_lore(rest)
        elif name == "retrieval":
            self.command_retrieval()
        else:
            print(f"  unknown command {name!r} — /help")

        session.save()
        return True

    def command_review(self, rest: str) -> None:
        if not rest:
            print("  /review <what you'd like to be different>")
            return
        session = self.session
        try:
            review = session.review_settings(rest)
        except (ProviderError, ValueError) as exc:
            print(f"  review failed: {exc}")
            return
        if review.summary:
            print(f"  {review.summary}")
        for change in review.changes:
            print(f"\n  • {describe(change)}")
            if change.reason:
                print(f"    why: {change.reason}")
            if change.warning:
                print(f"    warning: {change.warning}")
            print(f"    now: {format_value(current_value(session.bundle, change))[:300]}")
            print(f"    proposed: {format_value(change.value)[:300]}")
        if review.director_turn:
            print(f"\n  Director turn to send: {review.director_turn}")
        if review.history_bound:
            print("  (the reviewer thinks the story so far is part of the cause)")
        for note in review.cannot_fix:
            print(f"\n  can't fix with settings: {note}")
        for finding in review.app_findings:
            print(f"\n  finding about SealedLore: {finding.problem}")
            if finding.where:
                print(f"    where: {finding.where[:300]}")
            if finding.suggestion:
                print(f"    suggestion: {finding.suggestion}")
        if review.changes:
            print(
                "\n  Shown only: this review can't be applied from the terminal. Run the "
                "review in the app (Story → Review settings…) to pick changes and apply them."
            )

    def command_begin(self, rest: str) -> None:
        held: Character | None = None
        if rest:
            held = self.resolve(rest)
            if held is None:
                return
        try:
            self.stream(self.session.begin(held.id if held else None))
        except ValueError as exc:
            print(f"  {exc}")
            return
        self.controlled_id = held.id if held else None
        self.speaker_id = held.id if held else NARRATOR_SPEAKER_ID
        if not self.session.nodes:
            print("  no opening set — write the first turn")
        elif self.session.nodes[-1].meta.model is None:
            print(self.session.nodes[-1].content + "\n")

    def command_dice(self, rest: str) -> None:
        for word in rest.lower().split():
            if word in DIFFICULTY_ORDER:
                self.difficulty = word  # type: ignore[assignment]
            elif word == "general":
                self.dice_domain = None
            else:
                self.dice_domain = word
        held = next((c for c in self.session.cast if c.id == self.controlled_id), None)
        tier = tier_for(held, self.dice_domain)
        skill = self.dice_domain or "general"
        print(
            f"  next roll: {skill} ({tier}), {self.difficulty} — "
            f"{chance_for(tier, self.difficulty)}% to succeed"
        )
        if held and held.competence.tiers:
            print(f"  skills: {', '.join(f'{d} ({t})' for d, t in held.competence.tiers.items())}")

    def report_roll(self) -> None:
        path = self.session.path()
        roll = path[-1].meta.roll if path else None
        if roll is not None:
            print(f"  {chip_text(roll)}")

    def command_undo(self) -> None:
        path = self.session.path()
        if not path:
            print("  nothing to undo")
            return
        # The whole exchange: the reply and the turn that asked for it.
        start = path[-2] if len(path) > 1 and path[-1].kind == "assistant" else path[-1]
        if start.kind != "user":
            start = path[-1]
        deletion = self.session.delete_from(start.id)
        print(f"  deleted {len(deletion.node_ids)} message(s)")

    def command_cast(self, rest: str) -> None:
        session = self.session
        if rest.startswith("add "):
            payload = rest[4:]
            name, _, summary = payload.partition("|")
            character = Character(name=name.strip(), summary=summary.strip() or None)
            session.cast.append(character)
            print(f"  added {character.name}")
            return
        if not session.cast:
            print("  (no cast yet — /cast add <name> | <summary>)")
            return
        present = set(session.story.scene.present_character_ids)
        for character in session.cast:
            flags = []
            if character.id in present:
                flags.append("present")
            if character.id == self.controlled_id:
                flags.append("held by you")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {character.name}{suffix}")

    def command_length(self, rest: str) -> None:
        """One-off length for the next turn: a preset name, or your own words."""
        key = rest.strip().lower()
        self.response_style, self.length_hint = None, None
        if not key:
            print("  length: story default")
        elif key in PRESETS and key != "custom":
            self.response_style = key  # type: ignore[assignment]
            print(f"  length for the next turn: {PRESETS[key].label}")
        else:
            self.length_hint = rest.strip()
            print(f"  length for the next turn: {self.length_hint}")

    def command_style(self, rest: str) -> None:
        """The story's standing default. Changes the system block: one cache miss."""
        style = self.session.story.style
        key = rest.strip().lower()
        if not key:
            current = label_for(style.response_style)
            extra = f" ({style.length_target})" if style.response_style == "custom" else ""
            print(f"  story default length: {current}{extra}")
            return
        if key in PRESETS and key != "custom":
            style.response_style = key  # type: ignore[assignment]
        else:
            style.response_style = "custom"
            style.length_target = rest.strip()
        print(f"  story default length: {label_for(style.response_style)}")

    def command_speaker(self, rest: str) -> None:
        lowered = rest.lower()
        if lowered in {"narrator", "n"}:
            self.speaker_id = NARRATOR_SPEAKER_ID
        elif lowered in {"director", "d"}:
            self.speaker_id = DIRECTOR_SPEAKER_ID
        else:
            character = self.resolve(rest)
            if character is None:
                return
            self.speaker_id = character.id
        print(f"  speaker: {self.speaker_label()}")

    def command_npc(self, rest: str) -> None:
        lowered = rest.lower()
        if lowered in {"all", ""}:
            self.npc_scope, self.npc_scope_ids = "all", ()
        elif lowered in {"choice", "model", "model_choice"}:
            self.npc_scope, self.npc_scope_ids = "model_choice", ()
        else:
            names = [item for item in (n.strip() for n in rest.split(",")) if item]
            resolved = [self.resolve(item) for item in names]
            ids = tuple(c.id for c in resolved if c is not None)
            if not ids:
                return
            self.npc_scope, self.npc_scope_ids = "selected", ids
        print(f"  npc scope: {self.npc_scope}")

    def command_path(self) -> None:
        nodes = self.session.nodes
        for node in self.session.path():
            position, count = sibling_position(nodes, node.id)
            variant = f" ({position}/{count})" if count > 1 else ""
            preview = node.content.strip().replace("\n", " ")
            if len(preview) > 70:
                preview = preview[:67] + "..."
            print(f"  {node.kind[:6]:<6}{variant:<8} {preview}")

    def command_inspect(self) -> None:
        prompt = self.session.last_prompt or self.session.assemble(
            self.turn_request("(nothing typed yet)")
        )
        uses_cache = self.session.uses_cache_control()
        print(f"  model: {prompt.model or '(unset)'}  cache_control: {uses_cache}")
        for section in prompt.sections:
            print(f"  {section.name:<24} ~{section.tokens:>6} tokens")
        for marker in prompt.breakpoints:
            print(
                f"  breakpoint {marker.label:<10} message {marker.message_index} "
                f"part {marker.part_index}  "
                f"segment ~{marker.segment_tokens} / prefix ~{marker.prefix_tokens}"
            )
        payload = self.session.provider.build_payload(
            ChatRequest(
                model=self.session.model,
                messages=prompt.messages,
                params=config_params(self.session.config, "story", self.session.model),
                use_cache_control=uses_cache,
            )
        )
        print(json.dumps(payload, indent=2)[:4000])

    def command_budget(self) -> None:
        prompt = self.session.last_prompt
        if prompt is None:
            print("  nothing assembled yet — send a turn or /inspect")
            return
        report = prompt.budget
        for section, tokens in report.by_section.items():
            print(f"  {section:<12} ~{tokens:>6}")
        print(f"  total        ~{report.total} / {report.budget} ({report.utilization:.0%})")
        if report.warning:
            print("  warning: at or past 90% of the budget")
        if prompt.needs_archival:
            print(f"  {len(prompt.excluded_node_ids)} oldest messages excluded to fit")

    def command_summaries(self) -> None:
        session = self.session
        split = session.split()
        on_path = {summary.id for summary in split.summaries}
        if not session.summaries:
            print("  nothing archived yet")
        for summary in session.summaries:
            badges = []
            if summary.stale:
                badges.append("stale")
            if summary.hand_edited:
                badges.append("hand-edited")
            if summary.id not in on_path:
                badges.append("other branch")
            suffix = f"  [{', '.join(badges)}]" if badges else ""
            print(
                f"  chapter {chapter_number(session.summaries, summary)} "
                f"({len(summary.covered_node_ids)} messages){suffix}"
            )
            print(f"    {summary.content.strip()[:400]}")
        remaining = len(split.verbatim)
        print(f"  {remaining} messages still verbatim")

    def command_lore(self, rest: str) -> None:
        session = self.session
        if rest.startswith("add "):
            title, _, remainder = rest[4:].partition("|")
            content, _, keywords = remainder.partition("|")
            entry = LoreEntry(
                title=title.strip(),
                content=content.strip(),
                keywords=[word.strip() for word in keywords.split(",") if word.strip()],
            )
            session.bundle.lore.append(entry)
            print(f"  added {entry.title!r}")
            return
        if not session.bundle.lore:
            print("  (no lore yet — /lore add <title> | <content> | <kw1,kw2>)")
            return
        for entry in session.bundle.lore:
            marks = []
            if entry.always_on:
                marks.append("always on")
            if not entry.enabled:
                marks.append("disabled")
            if entry.keywords:
                marks.append("kw: " + ", ".join(entry.keywords))
            suffix = f"  [{'; '.join(marks)}]" if marks else ""
            print(f"  {entry.title}{suffix}")

    def command_retrieval(self) -> None:
        report = self.session.last_retrieval
        if report is None:
            print("  nothing retrieved yet — send a turn")
            return
        how = "semantic + keyword" if report.used_embeddings else "keyword matching"
        if report.fallback_reason:
            how = f"keyword matching (embeddings unavailable: {report.fallback_reason})"
        print(f"  {how}, ~{report.tokens} tokens")
        for injection in report.injected:
            print(f"    {injection.entry.title:<30} {injection.describe()}")
        for injection in report.dropped:
            print(f"    {injection.entry.title:<30} dropped, over the lore token cap")

    def command_archive(self) -> None:
        session = self.session
        chunk = session.next_chunk(allow_partial=True)
        if not chunk:
            print("  not enough history to archive — the recent turns stay verbatim")
            return
        print(f"  summarising {turns_in(chunk)} turns with {session.summarization_model}…")
        try:
            summary = session.archive(chunk)
        except ProviderError as exc:
            print(f"  provider error: {exc}")
            return
        print(f"  chapter {chapter_number(session.summaries, summary)}:")
        print(f"    {summary.content.strip()}")


def build_embeddings(config: Config) -> OpenAICompatibleEmbeddings | None:
    settings = config.embeddings()
    return None if settings is None else OpenAICompatibleEmbeddings(settings)


def cmd_chat(args: argparse.Namespace) -> int:
    root: Path | None = args.data_dir
    config = load_config(root=root)
    provider = build_provider(config, mock=args.mock)
    session = StorySession.load(
        args.story_id,
        config,
        provider,
        root=root,
        learn_corrections=not args.mock,
        embeddings=build_embeddings(config),
    )
    return Repl(session).run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sealedlore", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sealedlore {__version__}")
    parser.add_argument("--data-dir", type=Path, default=None, help="override the data directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="save provider settings")
    configure.add_argument("--name", default="default")
    configure.add_argument("--base-url", default=DEFAULT_BASE_URL)
    configure.add_argument("--api-key")
    configure.add_argument("--model")
    configure.set_defaults(func=cmd_configure)

    new = subparsers.add_parser("new", help="create a story")
    new.add_argument("title")
    new.add_argument("--model")
    new.set_defaults(func=cmd_new)

    generate = subparsers.add_parser(
        "generate", help="draft a whole story (world, cast, lore, opening) from a premise"
    )
    generate.add_argument("premise")
    generate.add_argument("--model", help="the drafting model (default: the authoring model)")
    generate.add_argument("--mock", action="store_true", help="use the scripted mock provider")
    generate.set_defaults(func=cmd_generate)

    listing = subparsers.add_parser("list", help="list stories")
    listing.set_defaults(func=cmd_list)

    scenario = subparsers.add_parser("scenario", help="export or import a base story")
    scenario_commands = scenario.add_subparsers(dest="scenario_command", required=True)
    export = scenario_commands.add_parser("export", help="write a story's setup to a file")
    export.add_argument("story_id")
    export.add_argument("file", type=Path)
    export.set_defaults(func=cmd_scenario_export)
    load = scenario_commands.add_parser("import", help="create a new story from a file")
    load.add_argument("file", type=Path)
    load.set_defaults(func=cmd_scenario_import)

    export_story = subparsers.add_parser(
        "export", help="write a story backup (everything), or Markdown with --markdown"
    )
    export_story.add_argument("story_id")
    export_story.add_argument("file", type=Path)
    export_story.add_argument(
        "--markdown", action="store_true", help="readable prose of the active branch only"
    )
    export_story.set_defaults(func=cmd_export)
    import_story = subparsers.add_parser("import", help="restore a story backup")
    import_story.add_argument("file", type=Path)
    import_story.set_defaults(func=cmd_import)

    duplicate = subparsers.add_parser("duplicate", help="copy a story (everything, by default)")
    duplicate.add_argument("story_id")
    duplicate.add_argument(
        "--settings-only", action="store_true", help="the same setup as a new, unplayed story"
    )
    duplicate.set_defaults(func=cmd_duplicate)
    delete = subparsers.add_parser("delete", help="delete a story permanently")
    delete.add_argument("story_id")
    delete.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    delete.set_defaults(func=cmd_delete)

    chat = subparsers.add_parser("chat", help="run a conversation in the terminal")
    chat.add_argument("story_id")
    chat.add_argument("--mock", action="store_true", help="use the scripted mock provider")
    chat.set_defaults(func=cmd_chat)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
