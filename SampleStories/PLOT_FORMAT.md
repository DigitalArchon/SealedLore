# SealedLore plot files

A plot file is a Markdown document that sets up a whole story: its world,
opening, characters and places, and optionally a **plot**. The plot is the set
of facts the story keeps track of and the events a director brings about when
the time and the moment are right. `Doomsville.md` is a worked example.

Import one with **File → Import scenario…** to start a new story, or with
**Story → Import plot…** to give an existing story just its facts and events.
Anything the importer can't read is listed with its line number. Errors stop
the import; notes don't.

You don't have to write the format by hand. **File → Plot file editor…** has
a form for everything below, picks conditions, `brings:`, `reveals:`, `sets:`
and `at:` from what the file already holds, and runs the importer over the
file as you go, so its problems list is what an import would say. It opens
hand-written files too, and keeps what it can't make sense of as written so
the importer can point at it.

## How the file is laid out

- Top-level sections are `# Headings`: World (or World info, Setting),
  Opening (or Start), Characters (or Cast), Places, Lore, Facts, Timeline and
  Events (or Optional events). All are optional, and they can come in any
  order. Any other `#` heading is reported and ignored.
- Inside a section, each item is a `## Heading`. An event's variants are
  `### Headings`.
- Settings are `- key: value` lines placed **straight after** a heading. The
  first line that isn't a setting ends them, and everything from there on is
  prose. That means a bullet list further down a description stays part of the
  description.
- An indented line continues the setting above it. Use this for long `tell:`
  text.
- `<!-- comments -->` are ignored.

## Settings at the top

```markdown
---
title: Doomsville
description: One line for the story list.
start: day 1, 15:30          # the clock at the first passage
agency: contested            # fiat | plausible | contested | dice
world activity: normal       # quiet | normal | eventful
perspective: whole story     # whole story | with character
length: adaptive             # adaptive | brief | standard | descriptive | literary (never custom)
---
```

A key the importer doesn't know is noted and ignored, so there is no
front-matter key for the picture style: set that in the app.

## World and Opening

`# World` is the world bible, taken as written, including any sub-headings.

`# Opening` holds the notes the storyteller writes the first passage from.
These settings go straight after the heading:

| key | meaning |
|---|---|
| `mode` | `generate` (default): the model writes the opening from the notes. `as written` (or `written`): the notes *are* the first passage. |
| `location`, `time`, `situation` | Where the starting scene is. |

## Characters

```markdown
## John
- role: player          # player | storyteller (default) | supporting | author
- choose: start         # play one of the "choose: start" characters (also "at start", "one", "yes"); the others never appear
- present: yes          # in the opening scene
- aliases: Johnny, the soldier
- canon: Night of the Living Dead (1968)   # an established character from a known work; blank if invented
- voice: Clipped, few words.
- summary: One line; otherwise the first sentence of the description is used.
- hidden: yes           # kept out of the story until an event brings them in
- picture: pictures/john.png | front view   # a reference picture (see Pictures); one line each
The description.
```

- **player**: the author can play them, and the storyteller writes them on
  turns when the author is playing someone else.
- **storyteller**: the storyteller always writes them.
- **author**: only the author ever writes them.
- **supporting**: a supporting card rather than a cast member. It is kept for
  continuity and shown to the storyteller only when the character comes up.

**Hidden characters** (`hidden: yes`) are kept from every model, and from your
Cast tab, until an event brings them in (`brings:`, below). Until then the
storyteller doesn't know they exist, so it can't reach for them early. Once
the event happens they join the cast for the rest of that branch. Keep a
character's description to who they are; when and how they turn up belongs to
the event that brings them.

Names are matched by their *parts* when the scene read and the supporting
cards look for who a passage mentions. The importer warns when two
characters share one (John / John Smith), because one can then be mistaken
for the other.

## Places

Each `## Place` becomes a lore entry, found by its name and by any `keywords:`
you add. Set `always: yes` to have it sent every turn, and `hidden: yes` to keep
it out of the story until an event reveals it (`reveals:`). A place can have
reference pictures (`picture:`, see Pictures).

The places are also where the program tracks the player's character: after
each passage it records which place they are at (or none), and every place
they have been. Events test that with `at = Place` and `visited Place`, so
there's no need for facts like "at_hellsville".

## Lore

`# Lore` holds background the storyteller needs now and then but that isn't
somewhere to be: how a technology works, a history, a people. Entries take the
same settings as places (`keywords`, `always`, `hidden`, `picture`), and an event can
`reveals:` a hidden one, but the program never records the character as being
at one, and `at =` / `visited` can't name them.

**Each playable character has their own place.** `at = …` and `visited …`
are about whoever the author is holding; switch characters and the program
follows, and switching back finds the first one where they were. To test a
particular character whoever is held, put their name first:
`John at = Hellsville`, `Jane visited The Hidden Caves`. A character's place
is only read from the prose while the author holds them, so progress they
make off-screen (a search told as cutaways while the author plays someone
else) is better kept in a fact that the events set.

## Pictures

A character, a place or a lore entry can come with reference pictures, so
that pictures generated in the story draw them the same way each time. A
plot file is text, so the pictures are files in a folder beside it, each named
on a line of its own among the item's settings:

```markdown
## John
- role: player
- picture: pictures/john-front.png | front view, in his winter coat
- picture: pictures/john-side.png
```

- The path is written from the plot file's own folder, with forward slashes.
  It must stay inside that folder: a path that is absolute, or that climbs
  out with `..`, stops the import.
- After `|` comes the caption: what the picture shows. It is optional.
- PNG, JPEG, WebP, BMP and GIF are read.
- An item may have as many `picture:` lines as you like.
- **Share the folder with the file.** A story with pictures is the `.md` and
  its `pictures` folder together (zipped, or as they are).

When the file is imported, each picture is copied into the new story and put
on its character's card or its entry, with any hidden data in it (where and
when a photograph was taken) removed. A picture that is missing or can't be
read is noted and left out; it never stops the import. The plot file editor
keeps the folder for you: **Add…** under Pictures copies a picture into it.

## Facts

Facts are what the story keeps track of in the program's own records, not just
in the prose. Events test them, and set them with `sets:`.

```markdown
- elena: in hellsville (in hellsville, fleeing east, dead) — what became of Elena
- has_companions: no [watch] — yes while at least one other living person travels with them
```

The format is `name: starting value (allowed values) [watch] — meaning`
(`[watched]` also works).

- A fact whose starting value is yes or no can only be yes or no.
- **Only `[watch]` facts are read from the prose.** After each passage a small
  model checks them against what the prose shows. Every other fact changes
  only when an event sets it, or when you do. Watch as few as you can: a
  model judging facts it has no need to see gets them wrong.
- The meaning matters most for watched facts. It is all the model has to go on,
  so say *when* it changes.
- Whether an event has happened is already tracked (`<event> happened`), and so
  are places (`at`, `visited`): don't make facts for those.
- You can see and correct every fact while you play.

## Events

The `# Timeline` section holds events with a window of days. The `# Events`
section holds optional events, which have conditions but no dates.

```markdown
## The fall of Hellsville
- when: day 30–50         # a day is chosen at random within it (see below for optional events)
- pacing: quiet moment    # quiet moment (default; also "lull") | any time (also "anytime", "immediately")
- lead-in: yes            # the storyteller may be told ahead of time, so it can foreshadow
- requires: hellsville = standing
- trigger: In plain words, what has to be happening. The director judges it.
- aftermath: What is left afterwards, and what people say. Passed on when it becomes relevant.
- sets: hellsville = fallen
- brings: Marcus Webb     # hidden characters it brings into the story
- reveals: The Old Mill   # hidden places it reveals
- must happen: yes        # timeline events: see "Every timeline event resolves" below
- at: Hellsville          # where it happens: once seen, the player's character is there
What the storyteller is told should happen. It can also be given as `- tell:`.

### The player's character is in town
- requires: at = Hellsville
- tell: How it goes in this case.
- sets: hellsville = under attack

### Never visited
- requires: not visited Hellsville
- offscreen: yes          # happens without the player's character: no prose, only the facts
```

`when:` takes a range (`day 30–50`, `day 30 to 50`) or one day (`day 12`).
An optional event can have `when:` too: it can't happen before the first day,
and once the last has passed it is skipped, rather than waited for, if its
conditions don't hold then. It is never forced, and `must happen:` on an
optional event is noted and ignored. A variant (`###`) takes the same
settings as its event: `requires`, `tell`, `sets`, `trigger`, `brings`,
`reveals`, `at` and `offscreen`.

**Conditions** (`requires:`) are all checked by the program, never by a model.
A variant's conditions apply on top of the event's own.

| Condition | Meaning |
|---|---|
| `fact = value` (or `fact is value`) | The fact has this value. |
| `fact != value` (or `is not`, `isn't`) | The fact has any other value. |
| `flag` / `not flag` | Short for `flag = yes` / `flag = no`. |
| `at = Place` / `at != Place` (or `at Place`, `not at Place`) | The player's character is (isn't) at that place. |
| `visited Place` / `not visited Place` (or `has visited`, `never visited`) | They have (haven't) ever been there. |
| `Name at = Place`, `Name visited Place` | The same, about that character whoever the author holds. |
| `<event> happened` (or `has happened`) | The event has happened. |
| `<event> not happened` (or `hasn't happened`, `has not happened`) | The event hasn't happened. |

Separate several conditions with commas or "and". Anything fuzzy goes in
`trigger:`.

**Variants** (`###`) are the different ways an event can go. When it happens,
the variant whose conditions hold is the one used. An event-level `sets:`
applies to every variant; a variant's own value for the same fact wins.

**How an event happens:**

1. From the event's chosen day (timeline events), or once its conditions hold
   (optional events), the director watches for the right moment. With
   `pacing: quiet moment`, that means a lull: short turns from the author, or
   the character settling down for the night. Nothing intense gets interrupted.
2. The director tells the storyteller what should happen. You don't see this
   instruction unless you look at the prompt in the Inspector. The storyteller
   still writes the scene, and never decides what your character does.
   When the event starts, the storyteller is also given the full cards of
   whoever it `brings:` and whatever it `reveals:`.
3. Once the prose shows the event happened, its `sets:` apply, and what it
   brings joins the story for good. An offscreen variant's `sets:` apply the
   moment it fires, and brings nobody in (nobody was met) until the author's
   character learns of it. Then whoever it brings, and any hidden character
   or place its `aftermath:` names, join the story.

**Every timeline event resolves.** When a timeline event's window closes and it
still hasn't happened:

- if an on-screen variant fits, it happens at the next chance, whatever the moment;
- otherwise, if an offscreen variant fits, it happens silently, dated to its own day;
- otherwise the plot model picks the variant that best fits the story so far, and
  that one happens.

The exception is an event with its own `requires:` (not just its variants'),
such as "only if they are alone": it applies only if they hold, and is skipped
otherwise. `must happen: yes` or `no` overrides either default. Even a
must-happen event is skipped if its own `requires:` no longer hold when its
window closes. The story has moved past it: give the fall of a town
`requires: town = standing`, and a story that burned the town some other way
won't have it attacked again. An event it requires that simply hasn't happened
yet is different: the event waits for it, and its window runs on to the day
that event happens, so one late event doesn't take everything after it with it.

**Skipped over.** When the story jumps well past an event's window (more than
three days), the event isn't started as a scene that late. The storyteller's
model is asked, in one extra call, how each skipped-over event went in the
time that passed, fitted to what the story says about that time. The next
passage then treats it as the past, and the account is kept for the rest of
the story. A Director or Narration turn that says time goes by ("Three weeks
pass.", "By day 40, the snow has come.") moves the clock before anything is
written, so the passage that tells the skip already knows what happened in it;
a day given as a plan ("they must be ready by day 40") doesn't. If the story shows an event can't have happened yet (the character
never left the place it would have taken them from), it begins on-screen
instead. An event sent to the storyteller without asking the director (its
window closed, or "any time" with no trigger) that the prose still hasn't
shown after six passages is taken as having happened, so the facts always
reach an outcome. One the director began that the prose still hasn't shown
after six passages goes back to waiting, to be begun again when the moment
suits.

The director is a small extra call before a turn, made only while some event
could happen or be foreshadowed. An optional event whose conditions are already
met keeps the director asking every turn until it happens, so tight
`requires:` lines save calls and time.

You can write an event yourself: a Director or Narration turn that tells it
("The next day, the Dominion attack again.") counts it as happened, with what
it sets and brings, so it isn't scheduled again later. It has to be told in
your own turn, not only in the storyteller's passage. If the program misses one,
mark it happened in the Plot tab.

Each event happens at most once per playthrough. Every branch keeps its own
clock, facts and events.
