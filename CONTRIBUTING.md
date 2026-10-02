# Contributing to SealedLore

Thanks for looking. This page is how the project works and the rules a change
has to keep. What the app does, feature by feature, is in the
[manual](docs/manual.md).

## Setting up

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # ~1 minute, no network, no display needed
ruff check . && ruff format --check .
```

The tests run the Qt parts offscreen; on a machine without a display set
`QT_QPA_PLATFORM=offscreen`. On Debian and Ubuntu the GUI itself also needs
`libxcb-cursor0` (see the manual's
[System packages](docs/manual.md#system-packages)). `sealedlore-gui --mock`
runs the app against a scripted model, so you can try a change with no
endpoint. `tools/smoke/` drives the real window against a fake endpoint (its
README says how), and [docs/building.md](docs/building.md) covers the
AppImage.

On Windows, call `.venv\Scripts\python` rather than activating the venv
(PowerShell refuses `Activate.ps1` by default). Linux is the version the
project is built on: anything for Windows goes behind
`sys.platform == "win32"` and must leave Linux unchanged.

## The two rules that everything else serves

1. The model never speaks, thinks or acts for the character the author is
   playing.
2. Nobody enters a scene by shortcut: nobody is simply there, nobody
   perceives a scene from outside it, nobody acts on what they cannot know.
   Arrivals, shown happening, are wanted.

A change that weakens either one in the prompt, or adds a post-generation
filter that silently alters a reply, will be turned down however good the
rest of it is. "Never silently discard or alter a response" is a rule too.

## Where things live

| Package | Holds | Imports |
|---|---|---|
| `sealedlore.models` | pydantic models | nothing of ours |
| `sealedlore.storage` | JSON persistence, atomic writes | models |
| `sealedlore.engine` | prompt assembly, budget, archival, the session | models, storage, providers |
| `sealedlore.providers` | the OpenAI-compatible client, the mock, TEE checks | models |
| `sealedlore.gui` | everything PySide6 | all of the above |

`tests/test_module_boundaries.py` enforces the last row: only `gui` may
import Qt, so the engine keeps running headless (the CLI and the tests rely
on it). `assemble_prompt` is a pure function of story state: no network, no
clock, no GUI. Every prompt text the program sends is an entry in
`engine/prompt_texts.py`; renderers take the texts and hold no wording of
their own.

Storage is flat JSON with atomic writes (`storage/atomic.py`). API keys live
in the system keychain through `keyring` (`storage/keychain.py`); the rest of
the app sees them in `Config` as before, since only `repository.load_config`
and `save_config` know. Tests run on an in-memory keychain (conftest), never
the real one. These are decisions, not oversights. Every
httpx client comes from `providers/http.py:make_client`, which refuses
redirects off HTTPS and keeps a proxy from carrying loopback traffic: never
construct `httpx.Client` directly.

## What bites

These are the rules people most often trip over. Each has a test, and each
came from a failure in real use.

- **Prompt wording changes need a measurement.** Every default in
  `prompt_texts.py` carries a comment saying why it is what it is, usually
  with numbers from a live comparison. Several plausible rewrites of the
  rules were tried and lost. A PR that changes a default should say what it
  was tried against and how it did: the same turns replayed with the old and
  the new wording, on more than one model, the passages judged blind.
- **Private scenes leak nothing.** Anything that reads recent story nodes for
  a model must go through `session.path()` / `_path_to` / `render_chunk`,
  never the raw tree, or a private scene's text reaches another model.
  `test_nothing_from_a_private_scene_reaches_another_model` checks every side
  call.
- **Dependencies stay as they are** (`PySide6`, `httpx`, `tiktoken`, `numpy`,
  `pydantic`, `tinfoil`, and `pytest`/`ruff` for development). Open an issue
  before adding one.

### Prompt caching

The prompt is laid out for Anthropic prompt caching: a stable prefix, then a
volatile tail. A change that moves a byte of the prefix between turns makes
every turn pay full price.

- **Nothing turn-specific goes in the system block.**
  `test_system_block_is_identical_across_different_turns` is the check.
- **Retrieved lore goes in the tail**, after the recent story, never up with
  the world. Lore sent every turn (a small lorebook, or entries marked always
  on) is the exception, because it doesn't change.
- **The tail is appended to the final user message**, never sent as a system
  message of its own.
- **Blocks of one message are joined with `PART_JOINER` at the start of each
  later block.** Anthropic joins text blocks with nothing between them.
  Putting the separator first means adding a block never changes the bytes of
  an earlier one.
- **At most four cache breakpoints.** The third sits one exchange before the
  tail-bearing message (`cache_exchanges_outside_prefix`), so regenerating a
  recent passage doesn't throw a cache write away. A segment below
  `min_cacheable_tokens` gets no breakpoint of its own.
- **Whether the cast's full descriptions are sent depends on the cast's own
  size** (`cast_token_cap`), never on how full this turn's budget is.
- **The summary block changes only when a chunk is archived.** Archival takes
  whole chunks. Archiving a few messages at a time rewrites the cached prefix
  on consecutive turns.

### Qt

- **Never touch a widget off the GUI thread.** Generation runs on a `QThread`
  worker and sends deltas by signal. While it runs, the worker is the only
  writer of session state.
- **Clean up from `thread.finished`, never from the worker's own
  `finished`.** Dropping the last Python reference to a worker while its
  signal is still being delivered frees it under Qt and segfaults.
- **Never show a widget before it has a parent.** Shown parentless, it is a
  window: on X11 each one makes and destroys a native window.
- **`QMessageBox.setCheckBox` doesn't take ownership in PySide6.** Give the
  checkbox the box as its parent, or Python crashes.
- **Never use `QStatusBar`'s own messages.** They hide and re-show the bar's
  widgets at the wrong moments; `status.NoticeStatusBar` swaps in a notice
  label instead.
- **No app-wide event filters in Python.** Every event in the window would go
  through Python.
- **Look up a message's widget with `TranscriptView.message_widget`**, not
  `findChildren`: the transcript builds only a window of messages and moves
  it as needed.
- **Fill fields from story data through `gui/fields.py`** (`set_field_text`,
  `line_edit`), so a long value shows its start, not its tail.
- **Colours in `style.qss` are theme names** (`@panel`), never hex values:
  add a new one to every theme (`test_every_theme_has_every_colour_and_no_other`).
- **Keep the window narrow.** A combo box asks for the width of its widest
  item; use `Composer._compact` and `_fit_popup` so it sizes to its
  selection. The window's minimum width should stay under a laptop's 1,366
  pixels at 100% text size.
- In offscreen tests: `deleteLater` leaves old widgets until deferred deletes
  are flushed; `QProgressDialog.cancel()` from code does not emit `canceled`
  (click its button); scroll positions need the app's stylesheet loaded.

## Making a change

- Keep a PR to one thing, and say in it what changed and why. The commit
  history is written in plain sentences; keep it that way.
- Add or adjust a test alongside the change. Tests never reach the network:
  drive the engine with `MockChatProvider` (pass `learn_corrections=False`).
- Words in the UI follow the glossary below. Never "node", "fork" or
  "segment" in anything a user reads.
- `tests/test_readme_labels.py` walks the real window: a menu path or field
  the README or the manual names must exist. If you rename a control, the
  docs and that test will tell you.
- If a rule here turns out to be wrong in practice, say so in the PR and
  propose the alternative rather than working around it quietly.

## Glossary

One term per concept, used the same way in the app, the docs, the CLI and
the code's comments:

- **Message** is any entry in the story; a **passage** is the model's, a
  **turn** the author's. A **take** is another version of one passage,
  swapped in place.
- **Branch**: a named line of the story, made by New branch from here…; the
  first is **Main**.
- **Chapter**: the summary that stands in for archived prose (`Summary`). A
  **part** is several chapters merged. **Archive now** summarises; a **story
  backup** is the export of everything.
- **Playing**: the character the author writes (`held_character_id`,
  `hold()` in code). **Speaking as**: who a turn's text lands as. **Model
  voices**: who the storyteller may voice (`npc_scope`).
- **Question**: an out-of-character question to the model (`Aside` in code).
- **Direction**: the author's out-of-character instruction turn. The plot's
  **director** (`engine/director.py`) is a different thing; its notices say
  "Plot: …".
- **Who can know** (everyone in time, or only those present) is the scene's
  privacy; a **private scene** is the separate-model feature.
- The **Prompt** page shows what the storyteller was sent. The **story
  model** is the storyteller (`main_model` in code); the manual's "Models"
  table names the others.

## Reporting a problem

Help → About SealedLore shows the version and the data folder. A story's
`api_log.jsonl` holds every prompt and reply, so check it before attaching
it; `crash.log` in the data folder holds only stack traces and is safe to
send.

## Licence and contributions

SealedLore is licensed to everyone under the GNU Affero General Public
License v3 or later ([LICENSE](LICENSE)). Digital Archon can also license
SealedLore's own code on other terms by separate agreement (see the README's
Licence section). To do both, Digital Archon needs permission to license
every contribution both ways. So, by submitting a contribution (a pull
request, patch or file), you agree that:

1. you wrote it, or have the right to submit it under these terms;
2. it is licensed to the project and to everyone under the AGPL-3.0-or-later;
3. you also grant Digital Archon a perpetual, worldwide, non-exclusive,
   royalty-free licence to use, change, distribute and sublicense it under
   any other terms, including a commercial licence.

You keep the copyright in your contribution. If you can't agree to point 3,
say so in the pull request and we'll talk before merging.
