# Writing a plot that plays well

`PLOT_FORMAT.md` says what a plot file may hold. This guide is about how to
word it. A plot is read, a part at a time, by the program and by small model
calls that each see only what they need, and the wording of an item matters as
much as the item: a trigger the director can't judge from a passage waits for
ever, a fact meaning that's loose flips on the first stranger, an aftermath
that fits one outcome is told as fact after the other. The rules below come
from watching plots play. Each comes with an example in the style of the
samples (`Doomsville.md` and the others beside this file).

## Who reads what

| Part | Read by | When |
|---|---|---|
| `requires:`, `when:`, `at:`, `must happen:` | the program | every turn: exactly, never wrongly |
| `trigger:`, `pacing:` | the director | before a passage, beside the clock, the scene card, the last couple of exchanges and the author's new turn |
| the event's own text, a variant's `tell:` | the storyteller (and the director, to judge) | in the passage the event begins |
| the event's own text (not the variants') | the storyteller | in a lead-in, days ahead, with the names of whoever it brings taken out |
| `aftermath:` | the storyteller | for the rest of the story once the event is seen or learned of; the director, to judge a reveal |
| a `[watch]` fact's meaning | the reader after each passage | against the author's turn and that passage alone |
| a character's card | the storyteller and every read | from the start, or once an event brings them in |
| a place's name and `keywords:` | the program, the reader, lore | the name to follow the character; name and keywords to wake `at:` events and to choose lore |

Nobody sees the plot whole. The director never sees what happened ten turns
ago, the reader never sees the event's trigger, and the storyteller never sees
the plot at all until an event is passed on.

## Conditions or a trigger

**If the program can check it, put it in `requires:` or `at:`.** Code is never
wrong about a fact, a place or an event that has happened; a model reading
prose sometimes is, and an event whose conditions hold is asked about turn
after turn until it happens.

```markdown
<!-- Bad: a model has to find the repair in the last two passages -->
- trigger: The automaton has been repaired and the player's character sits with it.

<!-- Good: the repair is a watched fact; the trigger is only the moment -->
- requires: automaton_repaired
- trigger: The player's character sits with the automaton or asks it what it knows.
```

**Say where it can't happen.** A roadside ambush whose only condition is its
window is offered to the director on every turn the character spends in town.
`requires: at != Hellsville` means nobody is asked about it while they are in
town. For an event that
can only happen somewhere, `at:` does the same (see below).

**Don't spend the trigger on what is already settled.** Once a condition is
in `requires:`, don't say it again in the trigger; an event happens once, so
"hasn't happened yet" is never needed; and "a lull" is what `pacing: quiet
moment` means. What is left for the trigger is what only a reader of the
passage can tell: what the character is doing, where exactly, with whom.

## Writing a trigger the director can judge

The director sees the clock, where the character is, the scene card (where,
the situation, who is there), the last couple of exchanges and the author's new
turn. Nothing older, and nothing that isn't on the page.

**Name what a passage would show:** a place, an action, a person.

```markdown
<!-- Good -->
- trigger: The player's character reaches Sylvaranth and asks to see the queen, or tells the elves what is happening.

<!-- Bad: an inner state, and something that happened off the page -->
- trigger: The player's character has come to trust the townsfolk, and word of the fight has had time to spread.
```

**A trigger is a moment, not a stretch.** "Is travelling the roads" is true
for days on end, and a director that turned it down once has no new reason to
say yes the next turn. Say what the moment looks like, and how many count as
"nearly alone".

```markdown
<!-- Bad: true for days on end, and "nearly so" is anyone's guess -->
- trigger: The player's character is travelling the roads, alone or nearly so.

<!-- Good -->
- trigger: The player's character sets out on a road or across open country, or goes scavenging away from shelter, alone or with one or two others.
```

**Write for the company the character keeps.** "Alone" in a story whose two
leads always travel together waits for a moment that never comes. If what you
mean is "nobody hostile about", say that.

```markdown
<!-- Bad, for a knight and her guide who go everywhere together -->
- trigger: The player's character is alone by the fire at night.

<!-- Good -->
- trigger: The player's character makes camp for the night, alone or with their companion, with nobody hunting them close by.
```

**Don't wait on a line the author may never say.** "The player's character asks
someone what lies beyond the mountains" may never be asked. If the event
matters, give the world a way to it as well ("…or someone who has been beyond
the mountains is in the scene"), or a window.

**Several "or"s are fine when each is plain**, and of one kind: two places, or
two things the character might do. A trigger that mixes a place, a question the
author has to ask and a mood is three triggers, and the director has to guess
which matters.

## Pacing: quiet moment or any time

- **Quiet moment** waits for a lull: the character settling for the night,
  resting, travelling without incident, the author's turns growing short and
  routine. It never cuts into a fight, a chase or an important conversation,
  and a question the author's character has just asked is answered first. In a
  story that never slows (a chase from the first line) it can wait a long time:
  if the event matters, give the story somewhere to pause, or make it any time
  with a trigger.
- **Any time** begins as soon as its trigger is happening. With no trigger at
  all, it begins as soon as its conditions hold, without asking anyone.
- A quiet-moment event with no trigger waits for any lull at all.
- **What it costs.** While an event could begin, the director is asked before
  every passage, a short wait before the first word. An any-time event it
  turns down waits a turn or a few before it is asked again; a quiet-moment
  event and a lead-in are asked every turn. Tight `requires:` and `at:` lines
  are what keep that short.

**Don't fix the hour in a quiet-moment event.** Lulls fall at night as often as
not, so "the council will see them at noon today" can arrive at midnight. Leave
the hour to the story ("the council will see them as soon as they can come"),
or make the event any time with a trigger that says when.

## at:

**On an optional event, `at:` is a gate.** The event is offered only while the
character is at that place, or the author's turn or the last passage names it.
A place's `keywords:` count as naming it, so give it keywords only that place
answers to. A keyword that is an everyday word in your story ("the citadel" in
a story about reaching the citadel, "the city", "the rest") wakes its events on
every mention.

**Give an any-time `at:` event a trigger.** With none, it begins the moment it
is offered, and a place that is only named offers it: "the road to Hellsville"
in a passage would start the welcome five miles out.

```markdown
## Welcome to Hellsville
- at: Hellsville
- pacing: any time
- trigger: The player's character arrives at Hellsville, or is in Hellsville and hasn't yet met the man who runs it.
```

Once the event is seen to happen, `at:` also records the character there.

## Windows, must-happen, and conditions that fail

A timeline event always resolves when its window closes: on screen at the next
chance, off screen if that variant fits, or as the plot model judges best. So:

- **Give a must-happen event the condition that says the story has moved past
  it.** `requires: hellsville = standing` on the fall of a town means a story
  that burned the town some other way isn't attacked again.
- **An event that waits on a watched fact nothing in the plot sets may never
  come.** If only the storyteller's own choices can turn `hunted` to yes ("once
  the watch is hunting the player's character by name"), and the story makes
  the captain of the watch a friend, the event that needs it never comes. If it
  matters, have an earlier event `sets:` the fact, or give it a second way in.

## Lead-ins

`lead-in: yes` lets the storyteller show a sign of an event in the days before
its window: a rumour, a distant sound, a detail on the road. What it is shown
is **the event's own text**, the line under its heading, as something coming.

**Write that line as what is coming, so a sign of it is not the thing itself.**

```markdown
<!-- Good: a sign of a horde (tracks, a far bell, the dead drifting north) isn't the horde -->
A horde of the dead descends on Hellsville.

<!-- Bad: a hint of a stopped clock is a stopped clock -->
The great clock above the market stops for the first time in a century.
```

**Keep secrets out of it.** The names of whoever the event brings in are taken
out of a lead-in; nothing else is. A hidden place, a hidden piece of lore or the
plot's secret cause written into the event's own text reaches the storyteller
days early, and again when the event begins. Put what the story must find out
later in the event that reveals it.

```markdown
<!-- Bad: the hidden machine, and who works it, told days early -->
The baron's hidden calculating engine sends its order.

<!-- Good -->
Every automaton of the old pattern receives the same order at once.
```

A lead-in is offered from a few days before the window opens, turned down in a
fight or an intense conversation, and given once.

## tell:

- **Say what the world does, and who does it.** Never what the author's
  character does, says, thinks or feels: the storyteller won't write it, and an
  instruction it can't follow pulls the passage out of shape.
- **Leave open what the author decides.** "He can be lied to, bargained with,
  or trusted, but he notices everything." "How the player's character reacts is
  theirs to decide."
- **Name the people it brings.** Their cards come with the direction: that is
  what `brings:` is for.
- **Say what must hold for the scene to work**, plainly: "The player's character
  notices him only if the author has them explicitly scan the area… otherwise
  do not mention him until he makes himself known."
- **Keep the entrance for the event.** A character the storyteller already has
  a card for can be brought in by the storyteller first: an imp whose card says
  he "will help anyone for a price" may turn up offering bargains before the
  bargain's event comes. If someone's first meeting is the event, make them
  `hidden: yes` and `brings:` them.

## Aftermaths that hold for every outcome

The aftermath rides with the storyteller for the rest of the story once the
event is seen or learned of, whichever way it went, on screen or off.

```markdown
<!-- Good -->
- aftermath: A horde of the dead came down on Hellsville. If the town fell, it is burned out and the dead still walk its streets; if it was held, the scars of the fight are everywhere and its people are burying their dead.

<!-- Bad: true only if the defence went badly -->
- aftermath: Wolves attacked the village. Three cottages burned.
```

- **Make it agree with the tell.** If the tell has the clock stop for three
  heartbeats, the aftermath can't say it stood still for an hour.
- **Say what people know and say about it.** It is what the storyteller draws
  on when someone asks.

## Variants

- **Give the variants conditions that leave exactly one fitting**, whatever
  state the story is in: in the town, been but away, never been. Doomsville's
  fall is the model.
- An event-level `sets:` applies to every variant; a variant's own value for
  the same fact wins.
- An offscreen variant writes no prose. Whoever it brings joins the story only
  when the character learns of it, so its aftermath is what they will hear.

## Facts and [watch]

- **Watch only what the prose must decide.** A fact the events set is never
  misread; a watched fact is read after every passage by a model that sees only
  that passage.
- **The meaning is all the reader has.** Say what a passage would show when it
  changes, and what doesn't count.

```markdown
<!-- Bad: flips on the first person met -->
- has_companions: no [watch] — yes while someone travels or lives with the player's character

<!-- Good -->
- has_companions: no [watch] — yes once another living person (not an animal) has joined the player's character to travel on with them or to live alongside them, as a party or a place in a community; meeting someone, talking with them, sharing a meal or being shown the way is not enough; no if they are alone again
```

- **One fact, one question,** with values a passage can tell apart.
- **One step per value.** "Ally once a bargain is struck and kept" is read as
  ally the moment the bargain is struck: one passage can show a bargain struck,
  but no single passage shows it kept. If keeping it matters, make that its own
  value or its own event.
- **Don't make facts for what is tracked already:** whether an event happened,
  where the character is, where they have been.

## Characters, names and who is hidden

- **People are found by the parts of their names.** Don't let two characters
  share one (John and John Smith). Aliases are real names or one-of-a-kind
  epithets: "the automaton" or "the imp" answers to every automaton or imp the
  story mentions.
- **Hide anyone an event should introduce** (`hidden: yes`, and `brings:` on
  the event). Until then neither the storyteller nor the reads know they exist,
  so none can reach for them early.
- **The card is who they are**, not when they turn up: that belongs to the
  event.

## Places and lore

- **Make a place of anywhere a condition or an event depends on.** The
  program follows the character between places; a building, a street or a
  yard within a place is that place.
- **Keywords are other names for this place and no other** (see `at:`).
- **Hide a place the story should find** (`hidden: yes`), and reveal it with
  the event that finds it.
- **Lore is for what isn't somewhere to be**: a history, a people, how a thing
  works. The program never records the character as at a lore entry.
- **A place's text is what it is like.** When and how things happen there
  belong to events.
