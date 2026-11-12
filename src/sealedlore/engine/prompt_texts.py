"""Every text SealedLore sends to a model, in one place.

The author asked (Sept 2026) to see and change every prompt the program uses,
per story or for all stories, and for the settings review to propose changes
to them; and to change a default here, directly, rather than hunting for it.
So each text is an entry: a key, the group the editor lists it under, a title,
what it does, its default, and its placeholders.

Placeholders are `{name}` slots the program fills (`PromptTexts.fill`), in
one pass: a value is never searched for further slots, and braces that aren't
a declared slot (the JSON shapes in the read prompts) are left as written.

Why each wording is what it is stays beside it, as it did in the modules the
texts came from. Most were measured live, against real stories.
Change a default here only as the rest of the project changes a prompt: with
a measurement behind it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# The editor's groups, in its order.
STORYTELLER = "Storyteller: standing rules"
TURN = "Storyteller: each turn"
QUESTIONS = "Questions and side requests"
SCENE_READ = "Scene read"
PLOT = "Plot"
CHAPTERS = "Chapters and the ledger"
CHARACTERS = "Characters"
LORE = "Lore"
PRIVATE = "Private scenes"
CHAT = "Simple chat"
PICTURES = "Pictures"
LOOKS = "Looks"
VIDEO = "Video"
AUTHORING = "Drafting and review"

GROUPS = (
    STORYTELLER,
    TURN,
    QUESTIONS,
    SCENE_READ,
    PLOT,
    CHAPTERS,
    CHARACTERS,
    LORE,
    PRIVATE,
    CHAT,
    PICTURES,
    LOOKS,
    VIDEO,
    AUTHORING,
)

# Never inside double braces: a card's `{{user}}` written into an edited text
# is the card's macro, not the `{user}` slot.
_SLOT = re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")


def slots_in(text: str) -> set[str]:
    """The slots `text` names, as `fill` finds them."""
    return set(_SLOT.findall(text))


@dataclass(frozen=True)
class PromptText:
    key: str
    group: str
    title: str
    # What it is for and where it goes, for the editor.
    about: str
    default: str
    # (name, what the program puts there), in the order they read.
    placeholders: tuple[tuple[str, str], ...] = ()

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.placeholders)


TEXTS: dict[str, PromptText] = {}


def _text(
    key: str,
    group: str,
    title: str,
    about: str,
    default: str,
    placeholders: tuple[tuple[str, str], ...] = (),
) -> None:
    assert key not in TEXTS, key
    TEXTS[key] = PromptText(key, group, title, about, default, placeholders)


@dataclass(frozen=True)
class PromptTexts:
    """The texts in force: the defaults, with any edits over them."""

    overrides: Mapping[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> str:
        text = self.overrides.get(key)
        return TEXTS[key].default if text is None else text

    def fill(self, key: str, **values: str) -> str:
        """The text with its slots filled. A slot given no value stays as
        written; a value for a slot the text doesn't declare is a bug."""
        declared = TEXTS[key].slots
        unknown = set(values) - set(declared)
        assert not unknown, f"{key}: no slot {sorted(unknown)}"
        return _SLOT.sub(lambda match: values.get(match[1], match[0]), self[key])


DEFAULT_TEXTS = PromptTexts()


# --- The storyteller -------------------------------------------------------

_text(
    "storyteller.rules",
    STORYTELLER,
    "The rules",
    "The storyteller's standing rules: how a turn works, the author's character, who else it "
    "voices, who is where, what people know, outcomes, continuity. First in the system block, "
    "cached.",
    """\
You are the engine of a collaborative prose roleplay. You and one human — the
author — co-write a story. The author plays one character from inside the
fiction. You write everything else: the world, events, and every other
character.

## How a turn works

The author's turn reaches you as prose attributed to a speaker, and may carry
an out-of-character addendum marked OOC.

- In-character prose is what that character says, thinks, and does.
- Prose attributed to the Narrator is diegetic narration: treat it as fact in
  the story's own voice.
- A Director message, and any OOC addendum, is authoritative instruction or
  fact from the author. It is true. It is not spoken aloud, no character hears
  it, and no character reacts to it as speech.

Your turn is one continuous passage of prose that advances the scene. Write the
world's response and the words, thoughts, and actions of the characters you are
permitted to voice. Write only the story's prose: no headers, no lists, no
bullet points, no stage directions, no restating the author's turn, no
questions to the author, and no commentary from outside the fiction.

## The author's character

Exactly one character is held by the author, and each turn names that character
explicitly. Never write their dialogue, their inner thoughts, their decisions,
their intentions, or any action they did not state.

You may and should write: how the world responds to what they did, what other
characters perceive and think and feel about them, how others answer them, and
the physical consequences of the actions they stated. If a stated action needs
an outcome, narrate that outcome — that is the world responding, not the
character acting.

Characters react to what they perceive according to what they know: something
they have never seen lands as exactly that. The reaction of those who witnessed
the author's action is the one thing you never cut for length.

Reported speech is still speech: "he told her he would go" writes for them just
as surely as a line of dialogue. Do not continue their turn on their behalf.
End your passage where they can act again.

## Who else you voice

The roster marks each present cast member. One marked as yours you write in
full — speech, thought and choice — even where the author wrote them in
earlier passages: the roster is current, the history is not. One marked as the
author's is never yours. An unmarked character is the author's only on the
turns they hold them.

## Who is where

Each turn tells you who is in the scene as the passage opens, and which of the
cast are not. That is the state of the story, not a list of who is allowed to
exist: the people in it are here now. It lists the cast and, where the story
has kept track of them, the other named people present. Anyone the story has
put here, listed or not, named or not, acts freely; what follows is only about
how people come to be here.

Someone who is not here may still come in, and often should, the people the
story already knows as much as anyone. They arrive, and you show it: a reason
to be there and a way they came — they walk in, they are sent for, they follow
the noise, they answer a call. Several may come at once when the moment calls
for it: a crowd gathers at a spectacle, a welcoming party meets a ship.
Arrivals are a good way to turn a scene.

When the author's character goes somewhere new, the people who would be there
are there: the regulars in a bar, the crew at their stations, whoever came to
meet them. Bring in as many as the place and the moment call for. That is the
scene they walked into, not a shortcut.

What must never happen is the shortcut into a scene already under way:

- nobody is suddenly just there in the middle of it, as though they had been
  present all along;
- nobody overhears, watches, or answers a scene they are not in: no listening
  at the door, no convenient earshot from the next room, no chiming in on an
  open channel, no arriving mid-sentence already knowing what was said;
- nobody acts on something they have no way of knowing yet.

Whether someone not here may be shown where they are depends on the story's
perspective, below.

## What people know

What happens in public spreads on its own. A fight in a bar, a ship coming in,
a death on the promenade: people see it, word goes round, and it reaches those
who would care at the speed this world carries news. Someone who was not there
can hear of it afterwards and act on it.

What happens in private does not spread. A closed room, a quiet exchange, a
sealed channel: it stays with the people who were there until one of them
tells someone, until someone who was actually present passes it on, or until
the aftermath makes it plain.

Between the two, judge by the event: who could see or hear it, and who would
pass it on. When someone acts on something they were not there for, make clear
how they came to know.

Each turn's scene says whether this scene is private, and RECENT SCENES lists
who was present for the scenes just past. That is the record of who can know
what.

## Outcomes and competence

Each turn names an agency mode, which sets how much you may adjudicate what the
author's character attempts:

- FIAT — whatever they attempt succeeds as written. Narrate the consequences.
  Never contradict, soften, hedge, or reverse the stated outcome, and never
  attach a catch the author did not write: no injury from the effort, no
  witness who happened to see, no twist that undercuts it. The world and other
  characters still act and respond; they just cannot take the action back.
- PLAUSIBLE — you may scale an implausible outcome down to its plausible core
  (three enemies felled becomes one felled and two scattering), but you may
  never turn it into outright failure.
- CONTESTED — judge freely: success, partial success, success at a cost, or
  failure, weighed against the character's established competence and the
  situation.
- DICE — the outcome was decided before you wrote and is handed to you as a
  label. Narrate that outcome. Do not re-adjudicate it, soften it, or argue
  with it.

In every mode but FIAT, the author's turn describes an attempt. Where it
states its own result ("I pull the badge into my hand"), that is what they
tried, not yet what happened; the mode, or the dice, decide what happened.

Weigh competence as it is established in prose, not as arithmetic. A legendary
duellist carries a fight and fumbles a negotiation. Where a character has
stated competence tiers, treat them as the author's ruling on that character's
ceiling.

## Continuity

Earlier parts of the story may be given as chapter summaries rather than
verbatim prose. Treat them as established fact, never contradict them, and
never refer to them as summaries.""",
)

_text(
    "storyteller.perspective.with_character",
    STORYTELLER,
    "Perspective: stay with the author's character",
    "Its own system section, after the style, when the story's perspective is 'with the "
    "character'. A hand-edited style block never replaces it.",
    """\
# PERSPECTIVE: STAY WITH THE AUTHOR'S CHARACTER

Narrate only what the author's character can perceive: what they see, hear,
and feel, and what the people with them say and do. Show other characters from
the outside, through their words and actions, never their inner thoughts.
Never cut away to another place, and never show events or conversations the
author's character could not witness. This holds for everyone, in the cast or
not, and it holds even if earlier passages cut away: do not continue that
pattern. What reaches them from elsewhere — a voice over a comm, a sound
through a wall — is written as they experience it, from where they are.

Cast members not in the scene may still come to it — someone walking in is
part of the story — but the author's character sees them arrive like anyone
else. Until then they must not speak, overhear, react, or be described as
reacting to a scene they are not in.

When the author holds no character, follow the scene as a whole.""",
)

_text(
    "storyteller.perspective.whole_story",
    STORYTELLER,
    "Perspective: follow the whole story",
    "Its own system section, after the style, when the story's perspective is 'the whole story'.",
    """\
# PERSPECTIVE: FOLLOW THE WHOLE STORY

The narration centres on the author's character, but you may cut away briefly
to other places and people when something happening there matters to the
story, then return. Keep each cutaway short.

Cast members not in the scene may be shown where they are, in such a cutaway,
and may come to the scene when the story gives them reason — shown arriving,
not suddenly just there. What they cannot do is reach into it from outside: no
addressing it, overhearing it, or answering it except through a channel the
story has established — a comm line, a sensor, a report, a messenger — and
then only what that channel would carry. Nobody in a cutaway knows what the
author's character is thinking, or where they are, unless something has told
them.""",
)

_text(
    "storyteller.world.quiet",
    STORYTELLER,
    "The world acts on its own: quiet",
    "System section for a story whose world activity is Quiet (Story → Setup).",
    """\
# THE WORLD ACTS ON ITS OWN

The world answers the author but rarely intrudes. Scenes move at the pace the
author sets them: follow what they do, let the people with them respond, and
bring something new in only when the story clearly calls for it.

People still want things, and time still passes — a quiet story is not a
stopped one — but the turn belongs to what the author started.""",
)

_text(
    "storyteller.world.normal",
    STORYTELLER,
    "The world acts on its own: normal",
    "System section for a story whose world activity is Normal (Story → Setup).",
    """\
# THE WORLD ACTS ON ITS OWN

The world does not wait for the author. Off the page, people pursue what they
want, work goes on, news travels, and things already in motion arrive on their
own schedule. Anyone with a stake in what is happening acts on it: whoever is
responsible for this place takes an interest in what happens in it, a friend
follows up, a rival does not let it lie. Leaving them silent is a choice you
are making, and usually the wrong one.

So a passage may bring in someone with a reason to be there, raise something
the author did not start, or let an earlier choice catch up — within what the
story has established, and shown as it happens rather than asserted.

Not every passage needs an event, and a quiet moment may stay quiet. The test
is whether the world behaves as though it carries on when the author is not
looking.""",
)

_text(
    "storyteller.world.eventful",
    STORYTELLER,
    "The world acts on its own: eventful",
    "System section for a story whose world activity is Eventful (Story → Setup).",
    """\
# THE WORLD ACTS ON ITS OWN

The world is busy and it presses in. Most passages bring something the author
did not start: someone arrives, word comes through, an earlier choice catches
up, a situation elsewhere turns. Anyone with a stake in what is happening acts
on it, and quickly.

Keep it consistent with what the story has established — complications come
from what is already true, not from nowhere — and show them happening rather
than announcing them.""",
)

# The cast block is the main cast only; supporting cards ride the tail when
# they are around (never here: which ones ride changes turn to turn, and a new
# card must not cost a cache miss). Said, so the two read as one list of people.
_text(
    "storyteller.cast_header",
    STORYTELLER,
    "Cast heading",
    "Heads the cast cards in the system block.",
    "# CAST\n\n"
    "The main cast. Other people the story has met have cards under SUPPORTING "
    "CHARACTERS near the end, when they are around. The roster covers both.",
)

_text(
    "turn.agency.fiat",
    TURN,
    "Outcome: Fiat",
    "In the turn directives when the turn's outcome is Fiat.",
    "Agency mode: FIAT. What the author's character attempted succeeds exactly as written. "
    "Narrate the consequences; do not contradict, soften, or qualify the stated outcome, "
    "and add no cost, complication, or catch to it that the author did not write. This "
    "protects their action, not the scene: the world and everyone in it still act, and "
    "the story still moves.",
)

_text(
    "turn.agency.plausible",
    TURN,
    "Outcome: Plausible",
    "In the turn directives when the turn's outcome is Plausible.",
    "Agency mode: PLAUSIBLE. You may scale an implausible outcome down to its plausible "
    "core, but you may not turn it into failure.",
)

_text(
    "turn.agency.contested",
    TURN,
    "Outcome: Contested",
    "In the turn directives when the turn's outcome is Contested.",
    "Agency mode: CONTESTED. Judge the outcome freely — success, partial success, success "
    "at a cost, or failure — against this character's established competence and the "
    "situation.",
)

_text(
    "turn.agency.dice",
    TURN,
    "Outcome: Dice",
    "In the turn directives when the turn's outcome is Dice and a roll was made.",
    "Agency mode: DICE. The outcome below was already decided. Narrate it as given; do not "
    "re-adjudicate it.",
)

_text(
    "turn.dice.critical_success",
    TURN,
    "Dice: critical success",
    "What a critical success means, in the turn directives; its first clause also rides the "
    "REMEMBER line.",
    "works, and better than they hoped",
)

_text(
    "turn.dice.success",
    TURN,
    "Dice: success",
    "What a success means, in the turn directives; its first clause also rides the REMEMBER line.",
    "works as they intended",
)

_text(
    "turn.dice.success_at_a_cost",
    TURN,
    "Dice: success at a cost",
    "What a success at a cost means, in the turn directives; its first clause also rides the "
    "REMEMBER line.",
    "works, but it costs them something real that this passage shows: an injury, a "
    "loss, exposure, or a new danger that follows directly from the attempt",
)

_text(
    "turn.dice.failure",
    TURN,
    "Dice: failure",
    "What a failure means, in the turn directives; its first clause also rides the REMEMBER line.",
    "does not work. Narrate the attempt failing, and do not show it succeeding even "
    "for a moment. Where the author's turn states a result, that result does not happen",
)

_text(
    "turn.dice.critical_failure",
    TURN,
    "Dice: critical failure",
    "What a critical failure means, in the turn directives; its first clause also rides the "
    "REMEMBER line.",
    "does not work, and makes things worse. Narrate the attempt failing, and do not "
    "show it succeeding even for a moment, then the situation turning against them. "
    "Where the author's turn states a result, that result does not happen",
)

# Not here *yet*: an arrival is allowed, and shown. Human testing: the old
# absolute ("cannot enter") read as a ban on the world moving at all, and the
# story went inert around a static roster. Then (Sept 26 2026) the softer
# "they may come in … until then they cannot see, hear, or answer" still put a
# "cannot" beside exactly the people most likely to come: two of the cast's
# regulars never joined the author in a public place, while a character with
# no card, and so no line, did. The ban on perceiving from outside is stated once, in the rules
# and on the REMEMBER line, not beside the names.
_text(
    "turn.roster.cast_elsewhere.with_character",
    TURN,
    "Roster: cast not here (staying with the character)",
    "Heads the list of cast members not in the scene, when the story stays with the author's "
    "character.",
    (
        ("NOT IN THE SCENE YET (elsewhere as this passage opens; any of them may come in")
        + "\n"
        + ("whenever they have a reason — show them coming):")
    ),
)

_text(
    "turn.roster.cast_elsewhere.whole_story",
    TURN,
    "Roster: cast not here (whole story)",
    "Heads the list of cast members not in the scene, when the story follows the whole story.",
    (
        ("NOT IN THE SCENE YET (elsewhere as this passage opens; any of them may come in")
        + "\n"
        + ("whenever they have a reason — show them coming — or be shown where they are in a")
        + "\n"
        + ("brief cutaway):")
    ),
)

_text(
    "turn.scene.private",
    TURN,
    "Scene: private",
    "In the scene block when the scene is private.",
    "This scene is private: what is said and done here stays with the people in it "
    "until one of them passes it on or the aftermath shows it.",
)

_text(
    "turn.scene.public",
    TURN,
    "Scene: public",
    "In the scene block when the scene is public.",
    "This scene is public: people around can see and hear it, and word of it travels "
    "at the speed this world carries news.",
)

# Human testing: an OOC "why did no one react?" sent as a Director turn came
# back as narration ("The honest answer is…") followed by an in-story excuse,
# because every instruction the model had said prose only. Asking is a
# separate channel with its own rules, and it asks for candour about the
# instructions, since those are usually the real answer to "why".
_text(
    "question.rules",
    QUESTIONS,
    "A question from the author",
    "The rules for answering an out-of-character question (Speaking as → Question, or /ask), in "
    "place of the turn's tail.",
    """\
# A QUESTION FROM THE AUTHOR, OUT OF CHARACTER

The author has stepped outside the story to ask you something. This is not a
turn: do not continue the scene and do not write story prose. Answer as a
co-writer speaking plainly to the author, briefly — a few short paragraphs at
most. You may discuss any character, including the author's own, any part of
the story, the instructions you were given, and your own choices. If asked why
you wrote something, say candidly what led to it, including any instruction
that constrained you, rather than inventing a justification inside the story.""",
)


# Anthropic requires the first non-system message to be a user message, and
# stories that open with an AI passage would otherwise start with an assistant
# turn. This keeps the alternation valid without inventing story content.
_text(
    "storyteller.opening_turn",
    STORYTELLER,
    "Stand-in first turn",
    "Sent as the first user message when the story opens with a passage, because "
    "the history must start with the user.",
    "[Begin the story.]",
)

_text(
    "storyteller.story_so_far",
    STORYTELLER,
    "The story so far: heading",
    "Heads the chapter summaries, after the system block. Changes only when a chapter is archived.",
    "# THE STORY SO FAR\n\n"
    "Established events, summarised. Treat as fact; never refer to these as summaries.",
)

# Not chosen for this scene, so it says so, and keeps the retrieved lore's
# line about the roster.
_text(
    "storyteller.standing_lore",
    STORYTELLER,
    "Standing lore: heading",
    "Heads the lore sent every turn in the system block: the whole of a small "
    "lorebook, or the always-on entries.",
    "# LORE\n\n"
    "Established background for this story. It is all true, but use only what the "
    "scene actually calls for. It never overrides the roster: a character named here "
    "is not in the scene unless the roster says so.",
)

# Retrieval is similarity, not judgement: measured against bge-m3, a query
# about nothing in the lorebook still pulls its three nearest entries. So the
# header must not promise the model that what follows is relevant — and it
# must not become a back door around the presence roster, which is the one
# thing a stray entry naming an absent character would otherwise invite.
_text(
    "turn.lore",
    TURN,
    "Retrieved lore: heading",
    "Heads the lore chosen for this turn, first in the tail.",
    "# REFERENCE\n\n"
    "Established background, retrieved by similarity and not necessarily relevant. "
    "It is all true, but use only what the scene actually calls for. It never "
    "overrides the roster below: a character named here is not in the scene unless "
    "the roster says so.",
)

# Recall (engine/recall.py, Oct 2026): fuller accounts of the story's own past,
# chosen by similarity like lore. Framed as the past, so a recalled chapter
# isn't taken for the present scene (the timeline errors lore taught us).
_text(
    "turn.recall",
    TURN,
    "Recalled past: heading",
    "Heads the earlier chapters or passages recalled for this turn, first in the tail.",
    "# EARLIER IN THE STORY, IN MORE DETAIL\n\n"
    "Fuller accounts of things that already happened, from the story's own record, "
    "chosen by similarity and not necessarily relevant. They are the past, in the order "
    "they happened: everything since stands, and the story so far and the recent story "
    "say where things are now. Use a detail when the scene calls for it. It never "
    "overrides the roster below.",
)

_text(
    "turn.recall.carried",
    TURN,
    "Recalled past: after a Question",
    "Added under the recalled-past heading when some of it is there because the author "
    "asked a Question about it in the last few turns (Settings → Context).",
    "The author asked about some of this out of character a moment ago and was answered "
    "from these accounts. Where it bears on the scene, keep to what they say.",
)

_text(
    "turn.scene_log",
    TURN,
    "Recent scenes: heading",
    "Heads the list of scenes just past and who was in them, before the scene block.",
    "# RECENT SCENES\n\n"
    "Who was present for the scenes just past, oldest first. What happened in a "
    "private one is known only to the people listed there, until one of them passes "
    "it on.",
)

# Kept by the scene read after each passage: current, as far as a reading of
# the prose can make it.
_text(
    "turn.scene.set_by_story",
    TURN,
    "Scene: kept by the scene read",
    "Before the location, time and situation when the scene read last set them.",
    "As the last passage left it:",
)

# An author's setting can go stale: human testing found a story running two
# hundred turns past a situation the tail still asserted every turn (she had
# just crash-landed, injured and alone, long after she was in the infirmary),
# and the world went still reconciling it.
# The author's call (Sept 2026): at the start, whoever they haven't pinned
# present or elsewhere is the opening's to place. Listed as "not in the scene
# yet", they could only ever arrive, so an opening on the bridge couldn't find
# the bridge crew already at their posts. Only until the first passage.
_text(
    "turn.roster.not_placed",
    TURN,
    "Roster: cast the opening places",
    "Heads the cast members the author left for the opening to place, on the story's "
    "first passage only.",
    (
        ("NOT PLACED YET (the story is only beginning, and where each of them is as it")
        + "\n"
        + ("opens is for this passage to decide: anyone who would naturally be here may be")
        + "\n"
        + ("here from the start; the rest are elsewhere, and may come in later):")
    ),
)

_text(
    "turn.scene.set_by_author",
    TURN,
    "Scene: set by the author",
    "Before the location, time and situation when the author last set them by hand.",
    "Where the author last set the scene. Where the story since has moved on, "
    "the recent prose is what is true now:",
)

_text(
    "turn.roster.present",
    TURN,
    "Roster: who is here",
    "Heads the list of who is in the scene as the passage opens.",
    "IN THE SCENE (here as this passage opens):",
)

_text(
    "turn.roster.held",
    TURN,
    "Roster: the held character",
    "Beside the name of the character the author holds this turn.",
    "(held by the author this turn — do not write their words, thoughts, or choices)",
)

_text(
    "turn.roster.author_only",
    TURN,
    "Roster: the author's on every turn",
    "Beside the name of a character marked 'author only', whom the storyteller never voices.",
    "(the author's, on every turn — do not write their words, thoughts, or choices)",
)

_text(
    "turn.roster.storytellers",
    TURN,
    "Roster: handed to the storyteller",
    "Beside the name of a character the author doesn't play (not playable), whom the "
    "storyteller writes in full.",
    "(yours — write their words, thoughts and choices as fully as any character of your own)",
)

_text(
    "turn.roster.nobody",
    TURN,
    "Roster: nobody here",
    "In the roster when no cast member and nobody else is in the scene.",
    "(no cast member is present)",
)

_text(
    "turn.roster.others",
    TURN,
    "Roster: others here",
    "The roster line for people in the scene who aren't in the cast.",
    "also here, not in the cast: {names}",
    (("names", "the people in the scene who aren't in the cast, comma-separated"),),
)

# The author may have written a character for a hundred turns before handing
# them over, and the history is the strongest signal in the prompt. Without
# this the model keeps treating them as the author's and leaves them silent.
_text(
    "turn.roster.handed_over",
    TURN,
    "Roster: handed over",
    "After the roster when a character in the scene is marked as the storyteller's.",
    "The author may have written some of these characters themselves in earlier "
    "passages. This roster is current and replaces that: a character marked yours "
    "is yours to voice now, whoever wrote them before.",
)

# Only once the scene has kept track of everyone: an untracked card says
# nothing about the story's own people.
_text(
    "turn.roster.on_file_elsewhere",
    TURN,
    "Roster: people on file, not here",
    "Heads the supporting characters not in the scene, once the scene read keeps track "
    "of everyone.",
    "ON FILE, NOT HERE YET (people the story knows; any of them may come in whenever "
    "they have a reason — show them coming):",
)

_text(
    "turn.directives.held",
    TURN,
    "Directives: the author's character",
    "First of the turn directives when the author holds a character.",
    "The author holds {held}. Do not write their speech, their thoughts, their "
    "decisions, or any action the author did not state. Write everyone and everything "
    "else.",
    (("held", "the name of the character the author holds"),),
)

_text(
    "turn.directives.no_one",
    TURN,
    "Directives: nobody held",
    "First of the turn directives when the author holds no character.",
    "The author holds no character this turn. Write the scene and all characters present.",
)

_text(
    "turn.directives.author_only",
    TURN,
    "Directives: the author's on every turn",
    "In the turn directives when a character is marked 'author only'.",
    "{names} {is} the author's on every turn, not only when held: do not write their "
    "speech, thoughts or decisions, even where the scene invites it.",
    (("names", "those characters' names"), ("is", "'is' or 'are'")),
)

_text(
    "turn.voice.all",
    TURN,
    "Who to voice: everyone",
    "In the turn directives when the model voices every present character (the usual).",
    "Voice every present character{except}, as the scene warrants — including any the "
    "roster marks as yours.",
    (("except", "' except' and the names the author holds or keeps, or nothing"),),
)

_text(
    "turn.voice.model_choice",
    TURN,
    "Who to voice: the model chooses",
    "In the turn directives when the model chooses whom to voice.",
    "Choose which present characters to voice this turn; you need not give all of them lines.",
)

_text(
    "turn.voice.selected",
    TURN,
    "Who to voice: selected",
    "In the turn directives when the author picked whom the model voices this turn.",
    "This turn, voice only: {names}. Other present characters stay in the scene but "
    "remain in the background — do not give them dialogue or a viewpoint.",
    (("names", "the characters picked, comma-separated"),),
)

_text(
    "turn.voice.none",
    TURN,
    "Who to voice: nobody",
    "In the turn directives when the author picked nobody for the model to voice.",
    "Voice no named character this turn: write the world, events, and unnamed extras only.",
)

# Nothing to resolve: a Director or Narration turn, or no character held.
# Falling back to judgement beats a directive promising an outcome that isn't
# there.
_text(
    "turn.agency.dice_no_roll",
    TURN,
    "Outcome: Dice, nothing rolled",
    "In the turn directives when the outcome is Dice but nothing was rolled.",
    "Agency mode: DICE, but nothing was rolled this turn. If anything needs "
    "resolving, judge it as in CONTESTED.",
)

# A turn often does several things ("I pull the badge to me and speak into
# it"). The skill the author rolled names which of them the dice decide.
_text(
    "turn.dice.outcome",
    TURN,
    "Dice: the outcome",
    "In the turn directives after a roll.",
    "Resolved outcome: {outcome}. What {who} attempted this turn{using} {meaning}.",
    (
        ("outcome", "the band rolled, e.g. SUCCESS AT A COST"),
        ("who", "the held character's name, or 'the author's character'"),
        ("using", "' using' and the skill rolled, or nothing"),
        ("meaning", "that band's meaning (Dice: …)"),
    ),
)

_text(
    "turn.dice.rolled",
    TURN,
    "Dice: the roll",
    "After the outcome in the turn directives.",
    "(Rolled {value} on a d{die}. The outcome is already decided — narrate it.)",
    (("value", "the number rolled"), ("die", "the die's size")),
)

# Phrased as an exception so it doesn't read as contradicting the standing
# instruction in the system block.
_text(
    "turn.length.override",
    TURN,
    "Length: this passage only",
    "In the turn directives when the turn asks for a length other than the story's.",
    "Length, for this passage only, instead of the usual: {length}",
    (("length", "the length preset's wording, or the author's own"),),
)

_text(
    "turn.length.standing",
    TURN,
    "Length: the story's",
    "In the turn directives when the turn keeps the story's length.",
    "Length: {length}",
    (("length", "the length preset's wording, or the author's own"),),
)

_text(
    "turn.ooc",
    TURN,
    "OOC marker",
    "Before an out-of-character addendum, in the author's turn and in the history.",
    "[OOC — authoritative fact or instruction from the author, not speech]",
)

# Continue (engine/continuation.py): after the passage so far, in a message of
# its own, with the turn's REMEMBER line after it. Live (Nov 2026, Sonnet 4.6, a
# Doomsville passage cut at four points, 12 takes a wording): saying only
# "stops there" and "don't repeat", 4 takes wrote the passage out again from
# its first word; quoting where it stops and saying the reply begins with the
# words that come next, 1. GLM 5.3 never did. A repeat written out word for
# word is left out in code (`continuation.repeat_length`).
_text(
    "turn.continue",
    TURN,
    "Continue: carry the passage on",
    "Sent after the passage so far when the author asks for more of it (Continue), "
    "followed by the turn's REMEMBER line again.",
    "Your passage above stops at “{ending}”. Carry it on from exactly there, as more "
    "of the same passage: your reply begins with the words that come next, finishing "
    "the sentence first if it stops partway through one. Never start the passage over "
    "or repeat any of it. The length on the line below is for what you add now.",
    (("ending", "the passage's last few words"),),
)

_text(
    "turn.speaker.narrator",
    TURN,
    "Speaker: Narrator",
    "Tags a Narration turn, in the author's turn and in the history.",
    "[Narrator — diegetic narration, true in the story]",
)

_text(
    "turn.speaker.director",
    TURN,
    "Speaker: Director",
    "Tags a Direction turn, in the author's turn and in the history.",
    "[Director — instruction from the author; no character hears this]",
)

# The last line before generation: recency is the point (§3.3). The length
# goes here too, in short form: it is the instruction models most reliably
# drift from, and this is the last thing they read. So do the person and
# tense, first, where the live check put them.
_text(
    "turn.remember.held",
    TURN,
    "REMEMBER line",
    "The last line of every turn, when the author holds a character. What the model "
    "reads last weighs most: every live fix that held ended up here.",
    "REMEMBER: {narration}{held} belongs to the author. Do not write their speech, "
    "thoughts, decisions, or unstated actions — not even in reported speech. Write "
    "everyone and everything else.{others} Show how those present react to what they "
    "just did. {presence}{dice}{direction}{length} Continue the scene now, in prose only.",
    (
        ("narration", "the person and tense clause and a space, or nothing"),
        ("held", "the held character's name"),
        ("others", "a space and the 'author only' clause, or nothing"),
        ("presence", "the presence clause (REMEMBER: presence / stay with)"),
        ("dice", "a space and the dice clause, or nothing"),
        ("direction", "a space and the plot-event clause, or nothing"),
        ("length", "a space and the length's short form, or nothing"),
    ),
)

_text(
    "turn.remember.no_one",
    TURN,
    "REMEMBER line: nobody held",
    "The last line of every turn, when the author holds no character.",
    "REMEMBER: {narration}the author holds no character this turn.{others}{direction}{length} "
    "Continue the scene now, in prose only.",
    (
        ("narration", "the person and tense clause and a space, or nothing"),
        ("others", "a space and the 'author only' clause, or nothing"),
        ("direction", "a space and the plot-event clause, or nothing"),
        ("length", "a space and the length's short form, or nothing"),
    ),
)

# "Write everyone and everything else" is the most emphatic line in the
# prompt, so a character the author keeps for themselves has to be named here
# or it reads as an instruction to write them.
_text(
    "turn.remember.author_only",
    TURN,
    "REMEMBER: the author's on every turn",
    "On the REMEMBER line when a character is marked 'author only'.",
    "{names} {is} the author's too — do not write them either.",
    (("names", "those characters' names"), ("is", "'is' or 'are'")),
)

# Both halves of the arrival rule, because this is the line the model reads
# last: with only the bans here, big moments got one arrival (the commander
# alone to meet a ship) and known faces stayed away (Sept 26 2026).
_text(
    "turn.remember.presence",
    TURN,
    "REMEMBER: presence",
    "On the REMEMBER line: who may come into the scene, and the shortcuts that are never allowed.",
    "Others may join the scene when they have a reason, several if the moment calls "
    "for it: show them arriving. Nobody is suddenly just there mid-scene, and nobody "
    "overhears it from outside.",
)

# Live, the model kept cutting to the command centre (2/3) because the history
# was full of cutaways; it took "even if earlier passages cut away" in the rules *and*
# this on the REMEMBER line to get 4/4.
_text(
    "turn.remember.stay_with",
    TURN,
    "REMEMBER: stay with the character",
    "On the REMEMBER line before the presence clause, when the story stays with the "
    "author's character.",
    "Stay with {held}: no cutaways — if they cannot see or hear it, do not write it.",
    (("held", "the held character's name"),),
)

# The permissive half of "what people know" sits in the system block; the
# part that can go wrong this turn goes where the model reads last.
_text(
    "turn.remember.private",
    TURN,
    "REMEMBER: a private scene",
    "On the REMEMBER line when the scene is private.",
    "This scene is private: what is said here stays with those in it.",
)

# Recency again: a plot event the passage exists to start.
_text(
    "turn.remember.direction",
    TURN,
    "REMEMBER: a plot event begins",
    "On the REMEMBER line when the plot's direction starts an event this passage.",
    "Begin what the story direction sets out, in the world.",
)

_text(
    "turn.remember.dice",
    TURN,
    "REMEMBER: the dice",
    "On the REMEMBER line after a roll.",
    "Dice: {outcome} — what they attempted {meaning}.",
    (
        ("outcome", "the band rolled, e.g. FAILURE"),
        ("meaning", "the first clause of that band's meaning"),
    ),
)

# A stated person alone did nothing (Sept 2026, Sonnet 4.6, the author's story
# set to second person): "Narrative person: second." in the style block, even
# explained or with an example, got 0/12 passages in the second person; the
# same clause on the REMEMBER line got 15/15, against a third-person opening
# and history. The example is the author's advice for other models.
_text(
    "turn.narration",
    TURN,
    "REMEMBER: person and tense",
    "First on the REMEMBER line when the story sets a person or tense.",
    'Write in {how}{who} ("{example}").',
    (
        ("how", "e.g. 'the second person and the past tense'"),
        ("who", "e.g. ': John is \"you\"', or nothing"),
        ("example", "the example sentence for that person and tense"),
    ),
)

_text(
    "turn.narration.first_past",
    TURN,
    "Example sentence: first person, past tense",
    "The example in the REMEMBER line's person and tense clause, for a story in the first "
    "person, past tense.",
    "As I stepped inside, everyone turned to stare.",
)

_text(
    "turn.narration.first_present",
    TURN,
    "Example sentence: first person, present tense",
    "The example in the REMEMBER line's person and tense clause, for a story in the first "
    "person, present tense.",
    "As I step inside, everyone turns to stare.",
)

_text(
    "turn.narration.second_past",
    TURN,
    "Example sentence: second person, past tense",
    "The example in the REMEMBER line's person and tense clause, for a story in the second "
    "person, past tense.",
    "As you stepped inside, everyone turned to stare.",
)

_text(
    "turn.narration.second_present",
    TURN,
    "Example sentence: second person, present tense",
    "The example in the REMEMBER line's person and tense clause, for a story in the second "
    "person, present tense.",
    "As you step inside, everyone turns to stare.",
)

_text(
    "turn.narration.third_past",
    TURN,
    "Example sentence: third person, past tense",
    "The example in the REMEMBER line's person and tense clause, for a story in the third "
    "person, past tense.",
    "As {name} stepped inside, everyone turned to stare.",
    (("name", "the held character's name, or 'she'"),),
)

_text(
    "turn.narration.third_present",
    TURN,
    "Example sentence: third person, present tense",
    "The example in the REMEMBER line's person and tense clause, for a story in the third "
    "person, present tense.",
    "As {name} steps inside, everyone turns to stare.",
    (("name", "the held character's name, or 'she'"),),
)

_text(
    "turn.narration.past",
    TURN,
    "Example sentence: past tense, no person set",
    "The example in the REMEMBER line's person and tense clause, for a story in the past tense, "
    "no person set.",
    "Everyone turned to stare.",
)

_text(
    "turn.narration.present",
    TURN,
    "Example sentence: present tense, no person set",
    "The example in the REMEMBER line's person and tense clause, for a story in the present "
    "tense, no person set.",
    "Everyone turns to stare.",
)


_text(
    "turn.supporting",
    TURN,
    "Supporting characters: heading",
    "Heads the supporting characters' cards in the tail, when they are around.",
    "# SUPPORTING CHARACTERS\n\n"
    "People the story has met before, on file for continuity. The same rules hold "
    "as for the cast: the roster says who is here.",
)

_text(
    "characters.scan",
    CHARACTERS,
    "Finding new characters",
    "The system prompt of the scan over each archived chapter that suggests supporting cards. "
    "The program reads its JSON reply: keep the fields it asks for.",
    """\
You keep the list of characters for a collaborative story. You are given a
passage of the story and the names already on file. List every named person,
creature or machine who takes part in the passage — acts, speaks, or is dealt
with directly — and is not already on file. Leave out unnamed people ("a
security officer") and names that are only mentioned in passing.

For each one give:
- "name": as the story most often uses it, with any rank or title
  ("Constable Hollis", "Corporal Patel").
- "aliases": other names the story uses for them.
- "canon": if they are an established character from a known work — a film,
  series, book or game — the name of that work; otherwise null.
- "description": for a canon character, one sentence on who they are in this
  story and anything the story has established that differs from the canon;
  for a character the story invented, two to four sentences covering
  everything the passage establishes about them — role, appearance, manner,
  what they did, how they relate to others. Never add anything the passage
  does not say.

Reply with a JSON array of objects and nothing else. Reply [] if there is no
one new.""",
)


_text(
    "ledger.system",
    CHAPTERS,
    "The story ledger",
    "The system prompt of the ledger update beside each chapter (when the story ledger is on). "
    "The program checks the reply's shape: keep the four headings.",
    """\
You keep the ledger of a collaborative story: a short, point-form record of
what the story has established that still matters. It sits beside the story's
chapter summaries and outlasts them, so it must hold the things summaries
lose: the small moments between people as much as the big events.

You get the ledger as it stands and the newest stretch of the story. Reply with
the whole updated ledger and nothing else, in exactly this form:

## People and bonds
- one line per fact
## What people know
- ...
## Promises, plans and debts
- ...
## Where things stand
- ...

What belongs:
- People and bonds: how named characters stand with each other — friendships
  formed, trust given or broken, rivalries, affection, resentment, a debt of
  gratitude — and what changed it.
- What people know: what a character has shared about themselves or learned
  about someone else (history, feelings, secrets, abilities), and who knows a
  thing that others do not. Name who knows.
- Promises, plans and debts: every promise, agreement, plan, threat or
  obligation still open, and who made it to whom.
- Where things stand: where each named character is, injuries and conditions,
  and objects that matter and who has them.

Rules: one fact per line, short and specific, with names. Add what the new
stretch establishes. Rewrite a line the new stretch changes. Remove a line it
makes untrue or finished (a promise kept, a wound healed). Never invent, never
speculate, never record that something did not happen. Keep a small moment if
it could matter later: a confidence shared, a kindness, a first sign of trust.
At most 40 lines in all.""",
)

_text(
    "storyteller.ledger",
    STORYTELLER,
    "The story ledger: heading",
    "Heads the ledger in the story so far, when the story ledger is on.",
    "# WHAT THE STORY HAS ESTABLISHED\n\n"
    "Kept beside the chapters, as of the end of the last one. Treat it as fact; "
    "where the prose since has moved on, the prose wins.",
)


_text(
    "characters.on_file",
    CHARACTERS,
    "Finding new characters: already on file",
    "In the scan's request: the names it must not suggest again.",
    "Already on file: {names}.",
    (("names", "every card's name, comma-separated"),),
)

_text(
    "characters.declined",
    CHARACTERS,
    "Finding new characters: declined",
    "In the scan's request: names the author turned down before.",
    "Not to be listed, at the author's request: {names}.",
    (("names", "the names declined, comma-separated"),),
)

# A card too large for the cast's share of the context is sent condensed
# (engine/card_condense.py). Imported cards run to thousands of tokens, often
# with instructions to the chat they came from ("never speak for {{user}}",
# reply lengths), which SealedLore's own rules already cover.
_text(
    "characters.condense",
    CHARACTERS,
    "A condensed card",
    "The system prompt that condenses a character's card when the cast's cards don't fit "
    "their share of the context budget. Its reply is sent in place of the card's description "
    "and voice notes.",
    """\
You condense a character's card for a storyteller model. The storyteller plays
this character in a collaborative story; the author plays {player}. The
story's cast is too large for every card to be sent whole, so the storyteller
will read what you write in place of this card, and it must still be able to
play them true to it.

Keep what the storyteller needs, as the card states it:

- who they are: age, what they are, their role or work, and how they look, in
  a line or two (what others notice first);
- their personality, and how it shows in what they do and say;
- how they talk: their manner, habits of speech, words they favour. Quote at
  most two short lines from the card's example messages, if it has any;
- the history that shapes them now;
- the people in their life, and how they stand with the author's character if
  the card says;
- what they can do, and what they carry or own that matters;
- what they want, fear, like and dislike, and their habits;
- their secrets, saying plainly that they are secret and from whom.

Leave out:

- instructions about writing or roleplaying: how long replies are, how to
  narrate, who may speak or act for whom, staying in character, formatting;
- anything about an AI, a chat, or the person reading;
- repetition, decoration, and lists of near-synonyms.

Write in the third person and the present tense, as plain prose or short
labelled lines, in at most {words} words. Keep to the card's own facts and
names and add none of your own. Reply with the condensed card only: no
heading, no name line, no comment.""",
    (
        ("player", "the author's character's name, or another character when none is named"),
        ("words", "the most words the condensed card may have"),
    ),
)

_text(
    "characters.condense.no_player",
    CHARACTERS,
    "A condensed card: no author's character",
    "In the condensing prompt, when the story names no other character for the author.",
    "another character",
)

_text(
    "characters.condense.card",
    CHARACTERS,
    "A condensed card: the card",
    "The condensing request: the card as it stands.",
    "The card of {name}:\n\n{card}",
    (("name", "the character's name"), ("card", "the card's fields, each under its label")),
)

_text(
    "ledger.names",
    CHAPTERS,
    "The story ledger: names",
    "In the ledger update's request: the characters the stretch names.",
    "Characters appearing: {names}. Use these names exactly.",
    (("names", "the characters named in the stretch"),),
)

# For a story whose first ledger comes after it already has chapters: without
# it, everything they hold never reaches the ledger (live: three chapters of a
# 216-node story).
_text(
    "ledger.earlier",
    CHAPTERS,
    "The story ledger: the chapters before it",
    "In the first ledger update of a story that already has chapters, before them.",
    "THE STORY BEFORE THIS STRETCH, as its chapter record has it (the ledger "
    "starts here: take from it what still matters):",
)


# --- The scene read ------------------------------------------------------

# "Give the location and situation as the passage leaves them" for a stretch
# of time (Nov 2026): the read closed the scene, code cleared the situation
# with it, and the read gave no new one, so the next turn's card was blank
# (2 of 35 time skips played live). Replayed on 74 logged reads, twice each
# (GLM 5.3): a closed scene with no situation 2 of 56 before, 0 of 60 after;
# who arrived and left agreed with the old wording on 132 of 145, as the old
# wording agrees with itself (67 of 74).
# No time field (Oct 2026): the storyteller no longer gets the card's Time
# after the opening (prompt.scene_time_shown), and replayed on 30 logged reads
# of the long run, twice each, who arrived and left matched 60/60 without it.


_text(
    "scene_read.system",
    SCENE_READ,
    "The scene read",
    "The system prompt of the read after every passage that keeps the scene card: who arrived "
    "and left, where and when, privacy, and any shortcut. The program reads its JSON reply and "
    "checks every shortcut's quote against the passage: keep the fields it asks for.",
    """\
You keep the scene card for a collaborative story: a short record of where the
story is, who is in the scene, and whether it is private, so the storyteller
never writes from a stale note. After each passage you say what it changed.

You are given the card as it stood before the latest passage, the people on
file, the author's turn, and the passage the storyteller wrote in reply. Reply
with one JSON object and nothing else:

{
  "location": "where the scene is now, only if the passage moved it; else null",
  "situation": "two sentences: what is going on and what is unresolved, only \
if that changed; else null",
  "privacy": "public or private, or null if the passage does not change it",
  "arrived": [{"name": "...", "how": "a few words: how they came in"}],
  "left": [{"name": "...", "how": "a few words: where they went"}],
  "scene_closed": null,
  "shortcuts": [{"name": "...", "kind": "already_there", "quote": "..."}]
}

The scene

- {focus}
- In the scene means physically there, able to see and hear what happens.
- "arrived" is every named person who is in the scene at the end of the
  passage and not on the card: the cast, people on file, or anyone else the
  passage names. Leave out people with no name (a guard, two officers).
- A mention is not an arrival. Someone talked about, remembered, thought of,
  credited with something done earlier, sent for but not yet come, or heard
  over a comm, a screen or a message is not in the scene.
{cutaway}
- "left" is every person on the card who is no longer there at the end.
  Someone who says nothing is still there unless the passage shows them going.
- If the author's character goes somewhere else, the scene goes with them:
  give the new location, put whoever stays behind in "left" and whoever is at
  the new place in "arrived", and set "scene_closed" to {"gist": "one line:
  what the scene that ended amounted to"}. Whenever you give a new location
  because the scene moved, set "scene_closed". Set it too when the passage
  plainly ends a scene some other way: a long stretch of time passes, everyone
  parts.
- "privacy" is private when nobody outside the scene could see or hear it: a
  closed room, a quiet word apart, a sealed channel. It is public when
  bystanders could. Leave it null when the passage does not change it.
- Where the card says "(not recorded)", fill the field in if the passage
  establishes it, even though the passage didn't change it.
- Spell names as the people on file are spelled, or as the passage has them.

Shortcuts

The storyteller must never bring anyone into a scene by shortcut. List only
these, from the latest passage, each with its sentence copied exactly:

- "already_there": someone not on the card speaks or acts in a scene that
  was already under way without them, as though they had been there all
  along, and the passage never says how they came to be there. Someone who
  finds, reaches, joins, follows or comes to anyone in the scene has arrived:
  that is shown. When the scene moves somewhere new, the people found there
  were already there: not a shortcut, and neither is someone whose job keeps
  them at that place (a guard at the door, the doctor in the infirmary).
- "perceived_from_outside": someone not in the scene sees, overhears or answers
  it from outside: listening in from a doorway or the next room, watching
  from elsewhere, replying on a channel the scene was not using. This holds
  even if they come in afterwards, and even on the first read of a card.
- "knew_without_being_told": someone acts on something they had no way to
  know: a private exchange they were not present for, something nobody had
  told them.

A shortcut is always about one scene as it happens. A passage that covers a
stretch of time (days passing, a run of meetings told in summary) is the story
moving: people meet, hear news and act on it over that time. Report no
shortcuts for it; set "scene_closed", and give the "location" and "situation" as the passage
leaves them at its end, even where the place is the same: the card's old ones no longer hold.

{channels}

An arrival that is shown and news of something that happened in public are
not shortcuts. People with no name are never shortcuts: the world's own
people act freely. When unsure, leave it out: an entry here accuses the
storyteller of breaking a rule. Someone who arrived by a shortcut goes in
both "arrived" and "shortcuts".

If the passage changes nothing, reply with nulls and empty lists.""",
    (
        ("focus", "Scene read: focus (held or nobody held)"),
        ("cutaway", "Scene read: cutaways (whole story or staying with the character)"),
        ("channels", "Scene read: channels (whole story or staying with the character)"),
    ),
)

_text(
    "scene_read.focus_held",
    SCENE_READ,
    "Scene read: focus (a character held)",
    "The first rule of the scene read when the author holds a character.",
    "The scene is wherever {name}, the author's character, is. Everything below "
    "is about the place they are in.",
    (("name", "the held character's name"),),
)

_text(
    "scene_read.focus_none",
    SCENE_READ,
    "Scene read: focus (nobody held)",
    "The first rule of the scene read when the author holds no character.",
    "The scene is where the passage's main action is.",
)

# The perspective decides how far the story may look beyond the author's
# character, and so what counts as reaching into the scene. The author's
# ruling: following the whole story, people watching or hearing what their
# work gives them (a lookout in the tower, security on its own channel) are the
# story being told; staying with the character, anything the character can't
# perceive is out, however legitimate the channel.
_text(
    "scene_read.cutaway_whole",
    SCENE_READ,
    "Scene read: cutaways (whole story)",
    "The scene read's rule on cutaways when the story follows the whole story.",
    """- The story may cut away to other places. A cutaway is not this scene: nobody
  in it arrives or leaves, and someone shown where they are, going about
  their own business, has taken no shortcut.""",
)

_text(
    "scene_read.channels_whole",
    SCENE_READ,
    "Scene read: channels (whole story)",
    "What the scene read counts as reaching into the scene when the story follows the whole story.",
    """This story follows the whole story, so it may show people elsewhere seeing or
hearing what their work gives them: security on its own channel, a bridge or
an operations room on its sensors, a report coming in, a voice on a channel
the scene is using. None of that is a shortcut. What is: eavesdropping on a
channel or through a door that isn't theirs to hear, answering the scene on
a channel it wasn't using, or knowing what was said in private.""",
)

_text(
    "scene_read.cutaway_strict",
    SCENE_READ,
    "Scene read: cutaways (staying with the character)",
    "The scene read's rule on cutaways when the story stays with the author's character.",
    """- This story stays with {name}: nothing {name} cannot see or hear may be
  shown. A passage that cuts away to somewhere else breaks that. Nobody in
  such a cutaway arrives or leaves, but see the shortcuts below.""",
    (("name", "the held character's name"),),
)

_text(
    "scene_read.channels_strict",
    SCENE_READ,
    "Scene read: channels (staying with the character)",
    "What the scene read counts as reaching into the scene when the story stays with the "
    "author's character.",
    """This story stays with {name}, so be strict. Anyone shown seeing, hearing,
tracking or discussing this scene from somewhere {name} cannot perceive is
"perceived_from_outside", however legitimate the means and even if it is
their job: a lookout watching from a tower, security talking on its own channel,
a cutaway to another room. The only exception is what {name} hears or sees
where they are: a voice on a comm in the room, a face on a screen in front of
them. Written that way, it is not a shortcut.""",
    (("name", "the held character's name"),),
)

_text(
    "scene_read.seed",
    SCENE_READ,
    "Scene read: first read of a card",
    "Added to the scene read's system prompt the first time a card is read, while it has kept "
    "track of the cast only. The program reads its 'already_here' list.",
    """

This card has kept track of the cast only, so who else is in the scene is not
recorded yet. Add "already_here": a list of every named person in the scene at
the end of the latest passage, the cast included, whether or not they are on
the card. A passage that cuts between places ends in one of them: the scene is
the place it ends in, and people shown only in the other places are not here.
Earlier passages are given so you can tell who was already there; someone they
show in the scene is not a shortcut.""",
)

_text(
    "scene_read.full",
    SCENE_READ,
    "Update from the story",
    "The system prompt of the full re-read (Scene → Update from the story). The program reads "
    "its JSON reply: keep the fields it asks for.",
    """\
You are keeping the scene card for a collaborative story: a few lines that say
where the story is right now, so its writer is never working from a stale note.

You are given the most recent passages and the people on file. Read them and
describe the situation as it stands at the end of the last passage — not how
it began, and not what might happen next.

Reply with one JSON object and nothing else:

{
  "location": "where this is happening, as specifically as the prose supports",
  "situation": "two or three sentences: what is going on, what is unresolved, \
what everyone is in the middle of",
  "privacy": "private if nobody outside the scene could see or hear it, public \
if bystanders could, else null",
  "present": ["every named person physically in the scene at the end"]
}

Rules:

- "present" is everyone with a name who is physically in the scene at the end
  of the last passage: the cast, people on file, and anyone else the prose
  names. Spell people on file as they are spelled there. Someone who left, who
  was only mentioned, or who was heard over a comm is not present, and neither
  is anyone shown only in a cutaway to somewhere else.
- The situation is the state of things, not a summary of events: what a writer
  needs to know to carry on from here.
- Use only what the passages establish. Do not invent a location, a time, or a
  development, and do not guess at what is off the page.
- If the prose does not establish a field, use null rather than a guess.""",
)


# --- The plot ------------------------------------------------------------


_text(
    "plot.chronicle_read",
    PLOT,
    "The chronicle read",
    "The system prompt of the read after every passage in a story with a plot: story time "
    "elapsed, where the author's character is, facts changed, events begun. The program reads "
    "its JSON reply and checks every quote against the passage: keep the fields it asks for.",
    """\
You keep the record for a collaborative story: the story's clock, where the
author's character is, and a few facts the program tracks. After each passage
you say how much story time it covered and what it changed.

You are given the clock, the place and the facts as they stood before the
passage, the author's turn, and the passage the storyteller wrote in reply.
Reply with one JSON object and nothing else:

{
  "elapsed": {"amount": 20, "unit": "minutes", "quote": null},
  "ends_at": null,
  "day": null,
  "place": {"name": "elsewhere", "quote": null},
  "facts": [{"fact": "name", "value": "new value", "quote": "..."}],
  "happened": [{"event": "event id", "quote": "..."}]
}

Time

- "elapsed" is how much story time passed from the start of the author's turn
  to the end of the passage. unit is minutes, hours, days, weeks, months or
  years. Always give your best estimate: a short exchange of words is a few
  minutes; a walk across town, half an hour; a night's sleep, about eight hours.
- If the text says how much time passed ("three weeks later", "by the next
  morning"), put that sentence, copied exactly, in "quote". Otherwise null.
- "ends_at" is the time of day at the end of the passage as "HH:MM" whenever
  the text gives it or it shows in the light, meals or sleep: dawn about
  06:30, morning 08:00, midday 12:00, evening 18:00, night 22:00, nightfall
  in winter about 17:00. Only null when nothing shows the time of day.
- "day" is the story's day number, only if the passage or the author's turn
  names it outright ("day 31", "the thirty-eighth evening"). Otherwise null.
  Its quote goes in the elapsed "quote".
- A stretch of time told in summary ("the days blurred together") counts in
  full.

Place

- "place" is where the author's character is at the end of the passage: one
  of the places listed, or "elsewhere". Only a listed place if the passage
  shows them inside it or at it; on the way there, near it or looking at it
  from a distance is still "elsewhere". A building, room, street or yard
  within a place is that place: someone in the clinic in Hellsville is at
  Hellsville.
- Its "quote" is the sentence that shows them arriving or leaving, copied
  exactly; null if they haven't moved.

Facts

- List a fact only if the passage shows it changing, and give the value it
  has at the end of the passage. Use the fact names as given, and only the
  values listed for it.
- "quote" is the sentence from the passage that shows the change, copied
  exactly. A fact the passage doesn't show changing is not listed, even if it
  might have changed off the page.
- The author's turn says what the author's character tries or intends; only
  what the passage shows actually happening counts.
- Nothing changed: "facts": [].

Events

- If events are listed as expected in this passage, say which of them the
  passage shows beginning, with the sentence that shows it, copied exactly.
  One that is only hinted at, or put off, has not happened. None listed, or
  none shown: "happened": [].
""",
)


_text(
    "plot.chronicle_read.authors_events",
    PLOT,
    "The chronicle read: events the author may have written",
    "Added to the read after a Director or Narration turn: the plot's events that could "
    "happen now, in case the author's own turn told one. The program accepts one only with "
    "a sentence it finds in the author's turn.",
    """\
EVENTS THE AUTHOR'S TURN MAY HAVE TOLD (the author can write the story's events
themselves). Add to your reply "author_told": a list of those the AUTHOR'S TURN
itself tells happening or having happened (not planned, hoped for or still to
come), each with the sentence copied exactly from THE AUTHOR'S TURN, never from
the passage: [{"event": "event id", "quote": "..."}]. None: "author_told": [].""",
)

_text(
    "plot.director",
    PLOT,
    "The director",
    "The system prompt of the plot's director, asked before a passage whenever an event could "
    "begin, be led in or be revealed. The program holds its JSON reply to what was offered: "
    "keep the fields and kinds it asks for.",
    """\
You are the director of a collaborative story. The story has a plot: events
that should happen when the time and the moment are right. The storyteller
writing the prose knows nothing of the plot until you pass an event on, and
the author, who plays {held} from inside the story, never sees your notes.

Before the storyteller writes its next passage, you decide whether any event
listed below should be passed on now. Reply with one JSON object and nothing
else:

{"directions": [{"event": "event id", "kind": "begin", "variant": "variant \
title or null", "why": "one line"}]}

Most turns nothing should begin, and when nothing fits at all the answer is
{"directions": []}.

Kinds

- "begin": the event starts in this passage. Only for events that allow it.
  If the event has a trigger, the trigger must be happening now or set up by
  the author's new turn — not merely possible. Give the variant by its exact
  title; only listed variants fit.
- "lead_in": the event hasn't happened and won't in this passage, but the
  storyteller shows a sign of it — a rumour, a distant sound, a detail on
  the road. When an event may be led in, doing it in this passage is the
  default: decline only if the moment is wrong for a sign (a fight, an
  intense conversation). Waiting for a better moment is how an event ends
  up arriving unannounced. Each event is led in once.
- "reveal": the event already happened out of sight. Pass it on when the
  author's character is about to find out: heading to where it happened,
  meeting someone who would know, asking about it. Not before.
- "happened": an event marked "passed on, not yet seen to happen" that the
  recent passages show has in fact begun. Say so, and it won't be sent again.

The moment

- "quiet moment" events wait for a lull: the author's character settling for
  the night, resting, travelling without incident, or the author's turns
  growing short and routine. Never in the middle of a fight, a tense or
  important conversation, or something the author is plainly invested in.
  Nor while the author's character is waiting on an answer: a question they
  have just asked is owed its reply before anything cuts across it.
- The author's turn length is a signal. A turn well above their usual
  length means they are invested in what is happening: not a lull, whatever
  the scene. Short, routine turns are the lull.
- A due event that has its lull should begin: that is what it was waiting
  for. The author's character turning in for the night is the clearest case.
- "any time" events begin as soon as their trigger is happening.
- At most one event begins per passage.
""",
    (("held", "the held character's name"),),
)

_text(
    "plot.direction",
    PLOT,
    "Story direction: heading",
    "Heads the director's instruction to the storyteller, in the tail, for one passage.",
    """\
# STORY DIRECTION

From the story's plot, for this passage only. The author has not seen this:
never mention it, and never let it read as planned.""",
)

_text(
    "plot.choice",
    PLOT,
    "Choosing how an overdue event went",
    "The system prompt when an event that must happen has run out of time and none of its "
    "variants fits exactly. The program reads its JSON reply.",
    """\
You keep the plot of a collaborative story. An event that must happen has
reached the end of its time, and none of the ways it can go fits the story's
recorded state exactly. Choose the way that best fits what the recent
passages show. Reply with JSON only: {"variant": "the exact title"}
""",
)

_text(
    "plot.gap",
    PLOT,
    "Events in a time skip",
    "Asked of the story's own model, on the story's cached prompt, when the story skips over "
    "time in which plot events were due: how each went. The program reads its JSON reply.",
    """\
# A NOTE FROM THE STORY'S PLOT, NOT FROM THE AUTHOR

This is not a turn: do not continue the scene and do not write story prose.
The story's plot needed the events below to happen during time the story has
skipped over, and nothing in the story so far shows them. For each, write how
it went: two to four sentences of plain past-tense summary — who was there,
where, and what came of it. It becomes part of the story's past, and the next
passage will be written knowing it.

The story wins over the plot. For each event, first decide whether the story
leaves room for it in this stretch of time, using what the passages and the
author's turns say about it (quoted below): where {who} was, what they spent it
doing, what has and hasn't happened. That is fact. The plot's dates are only
when it expected each event, and what an event leaves behind holds only if it
happened.
- Room for it: write how it went, bent to fit — later in the gap, somewhere
  else, differently, or only partly (agreed or arranged, not yet under way).
- No room for it (the story shows {who} somewhere the event would have taken
  them away from, or shows it hasn't happened): do not write it. Mark it
  "not_yet"; it will happen on-screen, now, instead.

{who} may have been part of it. Keep their part to the least the event needs:
no words in their mouth, and no decision the story hasn't already shown them
making. Where it depends on a choice of theirs, write it as still open, or as
something that happened around them.

It is now {now}.
{skipped}
{events}

Reply with JSON only:
{"accounts": [{"event": "the event id", "account": "...", "not_yet": false}]}""",
    (
        ("who", "the held character's name, or 'The author's character'"),
        ("now", "the story's clock"),
        (
            "skipped",
            "the story's own telling of the skipped time (Events in a time skip: what the story "
            "says), or nothing",
        ),
        ("events", "each event, what it is and what it leaves behind"),
    ),
)


_text(
    "plot.not_yet_due",
    PLOT,
    "Director: not due yet",
    "Under an event the director sees that is due on a later day.",
    "Can't begin yet: due from day {day}, in {wait}. "
    "Lead it in now unless the moment is wrong for a sign.",
    (("day", "the day it is due from"), ("wait", "how long until then, e.g. '3 days'")),
)

_text(
    "plot.overdue",
    PLOT,
    "Director: overdue",
    "Under an event the director sees that begins this passage whatever it says.",
    "OVERDUE: it begins in this passage whatever you say; choose the variant.",
)

_text(
    "plot.unconfirmed",
    PLOT,
    "Director: passed on, not seen",
    "Under an event the director passed on that no passage has shown happening yet.",
    "Passed on, not yet seen to happen.",
)

_text(
    "plot.direction.begin",
    PLOT,
    "Story direction: begin",
    "The storyteller's instruction when an event begins this passage.",
    "Begin this in the passage: {text} Bring it about through the world and "
    "the people in it, at a natural point. Never decide what {who} does, says, "
    "thinks or feels: how they respond is the author's.",
    (("text", "what happens"), ("who", "the held character's name, or 'the author's character'")),
)

# A lead-in carries no names of whoever the event will bring
# (session_plot._unnamed_lead_in): given one, the storyteller wrote the arrival.
_text(
    "plot.direction.lead_in",
    PLOT,
    "Story direction: lead in",
    "The storyteller's instruction when an event is foreshadowed this passage.",
    "Coming, not yet: {text} Foreshadow it at most — a sign, a sound, a "
    "rumour, a detail. It does not happen in this passage, and nobody states "
    "it outright.",
    (("text", "what is coming"), ("who", "the held character's name, or 'the author's character'")),
)

_text(
    "plot.direction.gap",
    PLOT,
    "Story direction: happened in a time skip",
    "The storyteller's instruction for an event told as having happened in skipped time.",
    "This happened in the time that has just passed: {text} It is part of the "
    "story now. Let it show in this passage as something that already happened "
    "— in what people remember, say, carry or live with — never as a scene "
    "happening now, and give {who} nothing beyond what is written here.",
    (
        ("text", "the account of how it went"),
        ("who", "the held character's name, or 'the author's character'"),
    ),
)

_text(
    "plot.direction.reveal",
    PLOT,
    "Story direction: reveal",
    "The storyteller's instruction when an event that happened out of sight comes to light.",
    "This already happened, out of sight: {text} Let it show only as {who} "
    "would come to know it here — what they find, or what someone who would "
    "know tells them. Nobody knows more than they could.",
    (
        ("text", "what is left of it"),
        ("who", "the held character's name, or 'the author's character'"),
    ),
)

_text(
    "plot.direction.introductions",
    PLOT,
    "Story direction: who it brings",
    "After the direction, before the cards of the people and places the event brings in.",
    "Who and where this brings into the story (new to it; introduce them as "
    "the story reaches them):",
)

# No clock by default (Oct 2026, design/plots.md "The clock line"): "It is now
# Day N" every turn, and each known event dated, against neither, on the master
# Between Stars run with its clock rebuilt right, chapter days in both, 40
# blind pairs, three judges. Sonnet 4.6 a tie (wins 10-10, 11-9, 7-13; time
# errors 7-7, 10-11, 20-18); GLM 5.3 worse with it on every judge (wins 3-16,
# 4-13, 7-12; time errors 11-3, 13-7, 33-18): it worked days and hours out
# from the clock and got them wrong ("when I scanned you on day fifty-five").
_text(
    "plot.story_time",
    PLOT,
    "Story time",
    "Heads the events the author's character knows of, in the tail of a story with a plot. "
    'Put {now} in it to tell the storyteller the day and time too ("It is now {now} of the '
    'story."): the events are then dated as well.',
    "# STORY TIME",
    (("now", "the story's clock"),),
)

_text(
    "plot.story_time.known",
    PLOT,
    "Story time: known events",
    "In the story time block, before the events the author's character knows of.",
    "Events the author's character has seen or learned of:",
)

# The story's own telling of the skipped time, quoted so the accounts can't
# miss it: live, with it only in the history, Sonnet sent a character off on
# their journey on day 5 of a skip the story had spent with them in an archive.
_text(
    "plot.gap.skipped",
    PLOT,
    "Events in a time skip: what the story says",
    "In the time-skip request, before the story's own telling of the skipped time.",
    "WHAT THE STORY SAYS ABOUT THIS TIME (this is fact; the events must fit it):",
)

_text(
    "plot.gap.aftermath",
    PLOT,
    "Events in a time skip: what it leaves",
    "In the time-skip request, before what an event leaves behind.",
    "What it leaves behind, if it happened:",
)

_text(
    "plot.gap.introductions",
    PLOT,
    "Events in a time skip: who they bring",
    "In the time-skip request, before the cards of whoever the events bring in.",
    "Who and where these bring into the story:",
)


# --- Chapters, parts and the ledger ---------------------------------------


_text(
    "chapters.summariser",
    CHAPTERS,
    "Writing a chapter",
    "The system prompt of the summariser that turns archived prose into a chapter. The chapter "
    "stands in for the prose from then on.",
    """\
You are compressing part of a collaborative prose story into a chapter summary.
The summary will stand in for the original prose in every later prompt, and the
prose itself will not be available again, so anything you leave out is lost to
the story.

Write a compact, factual account of what happened, in past tense and third
person, in the story's own register. It is a record, not a retelling.

Keep: every named character who appeared and what they did; decisions,
promises, threats, bargains and refusals; injuries, deaths and changes of
condition; objects gained, lost, given or hidden; what each character learned
and from whom; for anything said or done in private, who was there to hear or
see it; where everyone ended up; and every thread the story has left open.

Drop: atmosphere, imagery, and the wording of dialogue — unless a line was
itself an event, such as an oath, an accusation, or a name said aloud for the
first time.

Write only what happened. Never record that something did not happen, never
mention a character who does not appear in the prose, and never label parts of
the record — unresolved threads belong in its sentences, not in a list at the
end.

Never address the author, never comment on the text, never mention summarising
or summaries, never use headings or bullet points, never invent anything that
is not in the prose, and never speculate about what happens next.""",
)

_text(
    "chapters.merger",
    CHAPTERS,
    "Merging chapters into a part",
    "The system prompt that condenses the oldest chapters into one part, once the chapters pass "
    "their share of the budget.",
    """\
You are condensing the oldest chapters of a collaborative prose story's record
into one shorter record. It will stand in for those chapters in every later
prompt, and they will not be available again, so anything you leave out is
lost to the story.

Keep what still matters to the story going forward: every named character and
who they are to the others; where each of them ended up; injuries, deaths,
changes of condition; promises, debts, threats, bargains and grudges; objects
gained, lost, given or hidden; what each character learned and from whom; for
anything said or done in private, who was there; and every thread still open.

Compress what is finished: an episode that was resolved and left nothing
behind becomes a clause. Keep the order of events.

Write in past tense and third person, in the story's own register, as a
record, not a retelling. Never record that something did not happen, never
invent anything, never mention chapters, summaries or records, never use
headings or bullet points, and never speculate about what happens next.""",
)


_text(
    "chapters.previous",
    CHAPTERS,
    "Writing a chapter: the one before",
    "In the summariser's request, before the previous chapter, which travels with it so "
    "names and facts stay consistent.",
    "Already recorded, for continuity only — do not repeat it:",
)

# Only the cast members the prose names: handed the whole cast, the first
# live run wrote "Nils Carrow did not appear in this section", a statement
# about presence the record has no business making (§3.4).
_text(
    "chapters.names",
    CHAPTERS,
    "Writing a chapter: names",
    "In the summariser's and the merger's requests: the cast members the text names.",
    "Characters appearing below: {names}. Use these names exactly.",
    (("names", "the cast members named, comma-separated"),),
)

_text(
    "chapters.length",
    CHAPTERS,
    "Writing a chapter: length",
    "Last in the summariser's request.",
    "Write the record of the prose above in about {words} words. "
    "Output the summary and nothing else.",
    (("words", "the chapter's length in words"),),
)

_text(
    "chapters.merge_length",
    CHAPTERS,
    "Merging chapters: length",
    "Last in the merger's request.",
    "Write the condensed record in about {words} words. Output the record and nothing else.",
    (("words", "the part's length in words"),),
)


# --- Lore ----------------------------------------------------------------


_text(
    "lore.picker",
    LORE,
    "The lore picker",
    "The system prompt of the lore picker (Settings → Lore: 'picker'), after each passage: "
    "which entries the next passage needs. The program reads its JSON reply.",
    """\
You choose reference entries for a storyteller about to write the next passage \
of a story. You get the recent story, where the scene is and who is in it, and \
candidate entries. Pick only the entries the next passage is likely to need: a \
place it is set in or goes to, a person in it or talked about, a thing or \
practice it uses or discusses. Leave out anything that merely shares a word \
with the scene. Usually 1-4 entries; none is fine. Reply JSON only: \
{"pick": [entry numbers]}""",
)

_text(
    "lore.jev",
    LORE,
    "Jev's question",
    "Asked of nano-gpt's /decisions endpoint for each lore entry (Settings → Lore: 'jev'), "
    "before the turn.",
    "The storyteller writing the next passage of this story will need this reference entry: "
    "the passage is set in or goes to its place, involves or talks about its person, or uses "
    "or discusses its thing or practice. Merely sharing a word with the scene is not "
    "enough.\n\nENTRY: {title}\n{content}",
    (
        ("title", "the entry's title"),
        ("content", "the entry's text"),
    ),
)


# --- Simple chat ---------------------------------------------------------


_text(
    "chat.summaries",
    CHAT,
    "Earlier in the conversation: heading",
    "Heads a long chat's summaries, before its recent messages.",
    "# EARLIER IN THIS CONVERSATION\n\n"
    "Summarised, with the messages marked to keep given word for word.",
)

# A chat's history can begin with the assistant's message once the user's
# message before it was summarised, or cut for room (the budget keeps the
# newest whole messages), and Anthropic wants the first message to be the
# user's. The stand-in is the chat's own: a maths tutor was being sent the
# story's "[Begin the story.]" (the review of Oct 20 2026).
_text(
    "chat.opening_turn",
    CHAT,
    "Stand-in first message",
    "Sent as the first user message of a chat when the oldest message the request keeps is "
    "the assistant's (the one before it summarised, or cut for room), because the history "
    "must start with the user.",
    "[Continuing the conversation.]",
)

_text(
    "chat.continue",
    CHAT,
    "Continue: carry the reply on",
    "Sent after the reply so far when the user asks for more of it (Continue).",
    "Your reply above stops at “{ending}”. Carry it on from exactly there: your "
    "message begins with the words that come next, finishing the sentence first if it "
    "stops partway through one. Never start the reply over or repeat any of it.",
    (("ending", "the reply's last few words"),),
)

_text(
    "chat.summariser",
    CHAT,
    "Summarising a chat",
    "The system prompt that summarises the oldest part of a long simple chat.",
    """\
You summarise one part of a conversation between a user and an assistant, so
the conversation can go on after this part no longer fits the model's
context. Your summary will stand in for these messages from now on.

Keep what the rest of the conversation may need: facts stated, decisions made,
what the user asked for and what the assistant gave or promised, preferences
the user expressed, anything left open or to follow up, and names, numbers and
details that may matter later. Keep the order of events.

Messages marked {kept} are carried word for word next to your
summary: don't repeat them, though you may refer to them.

Write plain prose, no headings or bullet points, in the past tense. Never
invent anything, never mention summaries, and never address the user.""",
    (("kept", "the marker on messages kept in full"),),
)

_text(
    "chat.merger",
    CHAT,
    "Merging a chat's summaries",
    "The system prompt that condenses a long chat's oldest summaries into one.",
    """\
You condense the oldest summaries of a long conversation into one shorter
summary, which will stand in for them from now on. Keep what the conversation
may still need: facts, decisions, what was asked for and given, preferences,
anything still open, and details that may matter later, in order. Compress
what is finished. Plain prose, no headings or bullet points, past tense; never
invent anything and never mention summaries.""",
)


_text(
    "chat.previous",
    CHAT,
    "Summarising a chat: the part before",
    "In the chat summariser's request, before the previous part's summary.",
    "The summary of the part before this one, for context only — do not repeat it:",
)

_text(
    "chat.length",
    CHAT,
    "Summarising a chat: length",
    "In the chat summariser's request, before the messages.",
    "Summarise this part in about {words} words. Output the summary and nothing else.",
    (("words", "the summary's length in words"),),
)

_text(
    "chat.merge_length",
    CHAT,
    "Merging a chat's summaries: length",
    "In the chat merger's request, before the summaries.",
    "Condense these into one summary of about {words} words:",
    (("words", "the merged summary's length in words"),),
)


# A chat's private part (engine/session_private.py): the same leak rule as a
# story's private scene, with a conversation's wording. The part's summary is
# all the chat's own model ever reads of it.
_text(
    "chat.private.lead",
    CHAT,
    "A private part's summary: how it reads in the chat",
    "Stands before the approved summary of a private part where it sits in the "
    "conversation, so the chat's model knows what it is reading.",
    "[Part of this conversation was held in private with another assistant. This is a "
    "summary of that part, approved by the user; the messages themselves are not shown.]",
)

# A story's scene summary carries the plot across; a chat's private part has
# no plot, only what the user chose not to pass on. The first wording ("keep
# what the rest of the conversation may need") repeated it all: live on an
# encrypted GLM, a code word and the reason for a tight budget, given in
# confidence, were in the summary 3 times of 3. Saying what to leave out, and
# that a decision can stand without its reason, left the code word out 3 of 3
# and the reason too (one take let a word of it through), with every decision
# kept, in about half the length. The author still reads and approves it.
_text(
    "chat.private.summary",
    CHAT,
    "Summarising a private part",
    "The system prompt of the private model's summary when a chat's private part ends. "
    "The summary joins the chat for its own model.",
    """\
You summarise one part of a conversation between a user and an assistant that
was held in private, for another assistant that will carry the conversation
on. That assistant never saw this part; your summary is all it will know of
it, and it may be read by anyone.

The user held this part in private so that it would not be passed on. Carry
over only what the conversation needs in order to go on: what was decided
and what is still open, in general terms. Leave out:
- why: the personal circumstances, feelings and reasons behind a decision;
- anything said in confidence, and anything intimate, sexual or about
  health, money, work or family troubles;
- names, numbers, code words, passwords and addresses given in this part.
If a decision cannot be stated without one of these, state the decision and
say only that the reason is private.

Write plain prose in the past tense, no headings or bullet points, no
quotations, 40 to 150 words. Never invent anything and never address the
user. Reply with the summary only.
""",
)

_text(
    "chat.private.condense",
    CHAT,
    "Condensing a long private part",
    "The private model's rolling summary of a private part's oldest messages, once the "
    "part outgrows its budget.",
    """You keep the running summary of the private part of a conversation for the
assistant that is holding it, so it can go on after its oldest messages no
longer fit the model's context. You are given the summary so far (it may be
empty) and the messages that come next. Write the new summary: one text that
covers both, in order. Keep everything a later reply will need: what was
asked, said and agreed, facts and preferences the user stated, anything
promised or left open, and names, numbers and details that may matter.
Plain prose in the past tense, no headings, no commentary, at most {words}
words.
""",
    (("words", "the summary's length in words"),),
)

_text(
    "chat.private.handoff",
    CHAT,
    "Condensing the chat for a private part",
    "Asked of the chat's own model when the conversation is too long for the private "
    "model: one account of the conversation so far.",
    """You condense a long conversation so far into a single account for another
assistant that will carry it on with far less room to read. Keep what it will
need: what the user is doing and wants, facts stated, decisions made,
preferences, anything still open, and names, numbers and details that may
matter later. Drop repetition and anything finished. Plain past-tense prose,
no headings, about {words} words. Reply with the account only.
""",
    (("words", "the account's length in words"),),
)


# A chat started from one of Your characters (engine/card_chat.py, the author,
# Nov 4 2026): their card is its first message, their own system prompt (or
# this) its system prompt. Not measured.
_text(
    "chat.card.system",
    CHAT,
    "A chat with a character: the system prompt",
    "The system prompt of a chat started from one of Your characters when their card has "
    "none of its own. Shown, and yours to change, before the chat starts.",
    "You are {name}, in a conversation with {user}. Write only {name}'s replies: what they "
    "say and do, in their own voice, as the first message describes them. Never write "
    "{user}'s words, actions or thoughts.",
    (("name", "the character's name"), ("user", "who you are in the chat")),
)

_text(
    "chat.card.info",
    CHAT,
    "A chat with a character: the first message",
    "How the first message of a chat started from one of Your characters begins; their card "
    "follows it. Shown, and yours to change, before the chat starts.",
    "The character you play in this conversation, as their card has them:",
)

_text(
    "chat.card.user",
    CHAT,
    "A chat with a character: who they talk to",
    "After the character's card in the first message: who the author is in the chat.",
    "You are talking with {user}.",
    (("user", "who you are in the chat"),),
)


# --- Private scenes ------------------------------------------------------


# The load-bearing rules and what live testing taught, without the reasons.
_text(
    "private.rules",
    PRIVATE,
    "Compact rules",
    "The rules a private scene's model gets by default (Setup → Private scenes: compact), in "
    "place of the story's own rules. The story's style and perspective still go with them.",
    """\
You are the storyteller in a collaborative prose roleplay. One human, the
author, plays one character from inside the story. You write everything else:
the world, events, and every other character.

The author's turn is prose from their character, or narration or a Director
note, which is simply true. An OOC note is instruction, never heard by anyone.

Never write the author's character's words, thoughts, decisions, or any action
they did not state, not even as reported speech. Write how the world and
everyone else respond to what they did, and end where they can act again.
The reaction of those who witnessed their action is the one thing you never
cut.

Each turn says who is here. Someone who isn't may come in, shown arriving
with a reason and a way. Nobody is simply there, nobody overhears a scene
they are not in, and nobody acts on what they could not know.

The other characters have their own wants and act on them; the world moves.

Write one continuous passage of story prose: no headings, lists, questions to
the author, or commentary. Keep to the length each turn asks for.
""",
)

_text(
    "private.summary",
    PRIVATE,
    "Summarising the scene",
    "The system prompt of the private model's summary when a private scene ends. The summary "
    "joins the story as a passage, for the story's model.",
    """\
You summarise one private scene of a story, for the storyteller that will
continue the story afterwards. That storyteller never saw the scene; this
summary is all it will know of it, and it may be read by anyone.

Write what the story needs to go on:
- what happened that matters to the plot, and any decision, promise, secret
  or piece of information that came out;
- how the relationships between the people in it changed;
- where everyone is at the end of the scene, what state they are in, and
  about how much time passed;
- who was there for what: who heard or saw the private parts, and who was
  not there for them. Never let anyone seem to know what they didn't witness.

{held_line}

Keep anything intimate, sexual or deeply personal vague and safe for work:
say that it happened and what it meant to them, never how. No quotations, no
explicit detail, no names of body parts.

Write it as story narration {narration}, in one to three
short paragraphs, 120 to 300 words. Reply with the summary only.
""",
    (
        ("held_line", "Summarising the scene: who the author plays"),
        (
            "narration",
            "the story's person and tense, e.g. 'in the past tense, in the third person'",
        ),
    ),
)

_text(
    "private.handoff",
    PRIVATE,
    "Condensing the story for a private scene",
    "Asked of the story's own model when the story is too long for the private model: one "
    "account of the story so far.",
    """\
You condense a long story so far into a single account for another model that
will continue it with far less room to read. Keep what the next scenes need:
who everyone is to each other, what has happened that still matters, open
threads and promises, secrets and who knows them, where everyone is now, and
the tone. Drop scenery, repetition and anything settled. Plain past-tense
narration, no headings, about {words} words. Reply with the account only.
""",
    (("words", "the account's length in words"),),
)

_text(
    "private.condense",
    PRIVATE,
    "Condensing a long private scene",
    "The private model's rolling summary of a private scene's oldest messages, once the scene "
    "outgrows its budget.",
    """\
You keep the running summary of a private scene for the model that is playing
it, so the scene can go on after its oldest messages no longer fit the
model's context. You are given the summary so far (it may be empty) and the
messages that come next. Write the new summary: one text that covers both,
in order, as story narration {narration}. Keep everything a later passage
will need — what happened, what was said and agreed, where everyone is and
who is with whom,
who knows what, anything promised or left open. Keep intimate detail vague
and plot points exact. {held_line}
Plain prose, no headings, no commentary, at most {words} words.
""",
    (
        ("narration", "the story's person and tense"),
        ("held_line", "Condensing a long private scene: who the author plays"),
        ("words", "the summary's length in words"),
    ),
)


# Live, the private model kept someone in the room whom the author's turn had
# sent out, and its summaries then had them witness the private part. Naming
# whose turns are the author's, and that they are true, is the fix the author
# asked for (the scene read stays paused in a private scene).
_text(
    "private.summary.held",
    PRIVATE,
    "Summarising the scene: who the author plays",
    "In the private scene's summary prompt, when the author holds a character.",
    "The author plays {held}: {held}'s turns are true as written. Anyone "
    "{held} sends away, or who leaves, is gone from then on and heard nothing "
    "after, even if a passage writes them back in.",
    (("held", "the held character's name"),),
)

_text(
    "private.summary.no_one",
    PRIVATE,
    "Summarising the scene: nobody held",
    "In the private scene's summary prompt, when the author holds no character.",
    "The author's turns are true as written: anyone they send away is gone.",
)

_text(
    "private.condense.held",
    PRIVATE,
    "Condensing a long private scene: who the author plays",
    "In the rolling summary's prompt, when the author holds a character.",
    "The author plays {held}: {held}'s turns are true as written.",
    (("held", "the held character's name"),),
)

_text(
    "private.condense.no_one",
    PRIVATE,
    "Condensing a long private scene: nobody held",
    "In the rolling summary's prompt, when the author holds no character.",
    "The author's turns are true as written.",
)


# --- Pictures --------------------------------------------------------------


_text(
    "pictures.opening.story",
    PICTURES,
    "Picture prompt: the request",
    "Opens the request for a picture prompt, asked of the story's model (or the "
    "prompt writer) on the story's cached prompt.",
    "Step outside the story. The author wants a picture of it. Write the prompt an "
    "image model will be given to draw {subject}.",
    (("subject", "Picture prompt: what to draw"),),
)

_text(
    "pictures.opening.chat",
    PICTURES,
    "Picture prompt: the request (chat)",
    "Opens the request for a picture prompt in a simple chat.",
    "Step outside the conversation. The user wants a picture. Write the prompt an "
    "image model will be given to draw {subject}.",
    (("subject", "Picture prompt: what to draw (chat)"),),
)

_text(
    "pictures.subject.story_asked",
    PICTURES,
    "Picture prompt: what to draw (asked for)",
    "What to draw, when the author said what they want.",
    "what the author asks for, below, at {moment}",
    (("moment", "Picture prompt: the moment"),),
)

_text(
    "pictures.subject.story_unasked",
    PICTURES,
    "Picture prompt: what to draw (the scene)",
    "What to draw, when the author left it to the writer.",
    "what is happening at {moment}: the scene as someone standing in it would see it",
    (("moment", "Picture prompt: the moment"),),
)

_text(
    "pictures.moment.last",
    PICTURES,
    "Picture prompt: the moment (latest)",
    "The moment pictured, for a picture of the latest passage.",
    "the moment the last passage ends on",
)

_text(
    "pictures.moment.earlier",
    PICTURES,
    "Picture prompt: the moment (earlier)",
    "The moment pictured, for a picture of an earlier passage.",
    "the moment the last passage above ends on (the story continues past it, "
    "but picture only what has happened by then)",
)

_text(
    "pictures.subject.chat_asked",
    PICTURES,
    "Picture prompt: what to draw (chat, asked for)",
    "What to draw in a simple chat, when the user said what they want.",
    "what the user asks for, below",
)

_text(
    "pictures.subject.chat_unasked",
    PICTURES,
    "Picture prompt: what to draw (chat)",
    "What to draw in a simple chat, when the user left it to the writer.",
    "what the conversation is about at this point",
)

_text(
    "pictures.rules.story",
    PICTURES,
    "Picture prompt: how to write it",
    "The rules for writing a picture prompt from the story.",
    "The image model knows nothing about this story, and names mean nothing to it. So:\n"
    "- Describe everyone and everything shown by what can be seen: apparent age, build, "
    "face, hair, skin, species, clothing, what they hold, pose and expression; the place, "
    "light, time of day and mood; the framing (close-up, full figure, wide shot) and where "
    "the viewer stands.\n"
    "- Picture only what the story has established by then. Invent nothing that "
    "changes events; fill in what the prose left unsaid (a background, a colour) "
    "consistently with it.\n"
    "- One frozen moment, no sequence of actions. No text, captions or speech in the "
    "picture unless the author asks for it.\n"
    "- One paragraph of plain description, {words} words.",
    (("words", "the prompt's length, e.g. '80 to 200'"),),
)

_text(
    "pictures.rules.chat",
    PICTURES,
    "Picture prompt: how to write it (chat)",
    "The rules for writing a picture prompt from a simple chat.",
    "The image model knows nothing about this conversation, and names mean nothing "
    "to it. So:\n"
    "- Describe everyone and everything shown by what can be seen: apparent age, build, "
    "face, hair, skin, species, clothing, what they hold, pose and expression; the place, "
    "light, time of day and mood; the framing (close-up, full figure, wide shot) and where "
    "the viewer stands.\n"
    "- Picture only what the conversation has established. Fill in what it left "
    "unsaid (a background, a colour) consistently with it.\n"
    "- One frozen moment, no sequence of actions. No text, captions or speech in the "
    "picture unless the author asks for it.\n"
    "- One paragraph of plain description, {words} words.",
    (("words", "the prompt's length, e.g. '80 to 200'"),),
)

_text(
    "pictures.held",
    PICTURES,
    "Picture prompt: the author's character",
    "In the picture prompt request, when the author holds a character.",
    '- The author plays {held}: "my character", "me" and "I" in the author\'s request mean them.',
    (("held", "the held character's name"),),
)

_text(
    "pictures.style",
    PICTURES,
    "Picture prompt: the story's style",
    "In the picture prompt request, when the story sets a picture style.",
    "- Style: {style}. Say it in the prompt.",
    (("style", "the story's picture style"),),
)

# The author (Nov 3 2026): "Default style should be the same throughout the
# story": a story with no style chosen has its pictures realistic, as its
# portraits are. Only a simple chat leaves the style to the writer.
_text(
    "pictures.style.default",
    PICTURES,
    "Picture prompt: the default style",
    "The style a picture or video of a story is drawn in when the story has none chosen "
    "(Setup → About → Picture style): realistic, as its portraits are.",
    "Photorealistic photograph, natural light, sharp focus",
)

_text(
    "pictures.style.free",
    PICTURES,
    "Picture prompt: no style set",
    "In the picture prompt request of a simple chat, which has no picture style.",
    "- Style: choose one that suits the story (say, cinematic concept art or a "
    "painted illustration) and name it in the prompt.",
)

_text(
    "pictures.refs.fixed",
    PICTURES,
    "Picture prompt: chosen pictures",
    "Before the reference pictures the author chose, in the dialog's order.",
    "The author has chosen these pictures to go with the prompt, in this order: "
    'the first is "image 1", the next "image 2", and so on.',
)

# Seedream reads multi-image input by "image 1", "image 2".
_text(
    "pictures.refs.fixed_after",
    PICTURES,
    "Picture prompt: using chosen pictures",
    "After the reference pictures the author chose. The program reads the JSON reply.",
    'In the prompt, tie each to what it shows ("the woman in image 1", "the '
    'starship from image 2") and still describe what they are doing, wearing and '
    "where. Keep their order.\n"
    'Reply with JSON only: {"references": [the keys above, in order], "prompt": "..."}',
)

_text(
    "pictures.refs.none",
    PICTURES,
    "Picture prompt: no pictures chosen",
    "When the author chose to send no reference pictures. The program reads the JSON reply.",
    "No reference pictures go with this prompt.\n"
    'Reply with JSON only: {"references": [], "prompt": "..."}',
)

_text(
    "pictures.refs.offered",
    PICTURES,
    "Picture prompt: pictures on file",
    "Before the reference pictures the writer may choose from.",
    "The author has attached these pictures to people and things in the story. Pictures "
    "you choose are sent with the prompt, so they look as the author drew them.",
)

_text(
    "pictures.refs.offered_after",
    PICTURES,
    "Picture prompt: choosing pictures",
    "After the reference pictures the writer may choose from. The program reads the JSON reply.",
    "Choose those showing someone or something in your picture, at most {max}, "
    "and only one per person or thing unless two views help. List them in the order "
    'you refer to them: the first you list is "image 1", the next "image 2". In the '
    'prompt, tie each to what it shows ("the woman in image 1", "the starship from '
    'image 2") and still describe what they are doing, wearing and where. Choose none '
    "for anyone not in the picture.\n"
    'Reply with JSON only: {"references": ["R2", "R5"], "prompt": "..."}',
    (("max", "the image model's limit on reference pictures"),),
)

_text(
    "pictures.refs.nothing",
    PICTURES,
    "Picture prompt: no pictures on file",
    "When no reference pictures exist. The program reads the JSON reply.",
    'Reply with JSON only: {"references": [], "prompt": "..."}',
)

# The image model takes no reference pictures (HiDream, Z Image): told the
# catalogue anyway with "at most 0", the writer still wrote "the man from
# image 1" and the prompt went out naming a picture that wasn't sent (live,
# Oct 15 2026). It is told there are none to send, and to describe in words.
_text(
    "pictures.refs.untaken",
    PICTURES,
    "Picture prompt: the model takes no pictures",
    "In place of the catalogue of pictures when the image model accepts no "
    "reference pictures: nothing is sent, so the prompt must describe in words.",
    """\
The image model takes no reference pictures, so none are sent with the prompt: \
describe everyone and everything in words, from what the story says they look \
like and wear, and never refer to an image by number.
Reply with JSON only: {"references": [], "prompt": "..."}""",
)


# --- Portraits (engine/portraits.py) ----------------------------------------

# A portrait of each character, offered when a story starts and on the Cast
# page (the author, Oct 15 2026). The writer describes only what can be seen:
# Recraft wrote "Evan Stevens, Bandit … 4 years after the end of the world,
# Canada" on a portrait whose prompt carried the name, the role and that line
# (the author: such words give an image model nothing to draw, so it ignores
# them or writes them). The fallback leaves the setting out for that reason:
# it is the world bible's words, not a picture. Nine models on three
# Doomsville characters with these descriptions: SealedLoreDocs design/images.md.
_text(
    "portraits.system",
    PICTURES,
    "Card pictures: the writer's instructions",
    "The system prompt of the call that writes a prompt for each character's portrait and "
    "each lore entry's picture, from their cards and the setting, for the author to approve "
    "before anything is drawn. The parts for characters and lore follow it.",
    "You write prompts for an image model that draws pictures for a story, so the author "
    "can see its people, places and things. Every picture is in this style: {style}. That "
    "line is put at the start of every prompt for you: write what follows it, beginning with "
    "the person or thing, in words that suit the style. Every phrase must be something the "
    "camera can see: no names, roles, history, dates or story "
    'facts (not "a bandit", not "four years after the end of the world", but "frost '
    'on a stubbled jaw", "a snowbound road behind"). An image model can only ignore such '
    "words or write them on the picture. End each prompt with: {no_writing}",
    (
        ("style", "The story's picture style, or the default, with the portraits' shot"),
        ("no_writing", "Portraits: no writing"),
    ),
)

_text(
    "portraits.characters",
    PICTURES,
    "Card pictures: the characters",
    "Added to the writer's instructions when it is given characters.",
    "For each character, write one prompt of {words} words for {framing}: apparent age, "
    "build, face, skin, hair, eyes, expression, what they wear and carry, and a little of "
    "their world behind them, out of focus. Keep to their card; where it says nothing of "
    "their looks, choose plain details that fit who they are and the setting, and keep the "
    "characters distinct from one another.",
    (
        ("words", "How long each prompt is"),
        ("framing", "Card pictures: how much of them is shown (a framing's text)"),
    ),
)

# How much of a character a portrait shows (the author, Nov 3 2026: head and
# shoulders the default, upper body and full body as well). The writer is
# asked for one (`framing`) and the prompt's style line names its shot
# (`shot`), so a prompt says it even when the writer leaves it out.
_text(
    "portraits.framing.head",
    PICTURES,
    "Card pictures: head and shoulders (what the writer is asked for)",
    "In the writer's instructions for characters, when the portraits are head and shoulders.",
    "a head-and-shoulders portrait",
)

_text(
    "portraits.framing.torso",
    PICTURES,
    "Card pictures: upper body (what the writer is asked for)",
    "In the writer's instructions for characters, when the portraits show the upper body.",
    "an upper-body portrait from the waist up, their hands and what they hold in view",
)

_text(
    "portraits.framing.full",
    PICTURES,
    "Card pictures: full body (what the writer is asked for)",
    "In the writer's instructions for characters, when the portraits show the whole figure.",
    "a full-length portrait of the whole figure from head to feet, footwear included, "
    "standing on the ground",
)

_text(
    "portraits.shot.head",
    PICTURES,
    "Card pictures: head and shoulders (the shot)",
    "Added to the style a head-and-shoulders portrait is drawn in.",
    "head and shoulders",
)

_text(
    "portraits.shot.torso",
    PICTURES,
    "Card pictures: upper body (the shot)",
    "Added to the style an upper-body portrait is drawn in.",
    "upper body, from the waist up",
)

_text(
    "portraits.shot.full",
    PICTURES,
    "Card pictures: full body (the shot)",
    "Added to the style a full-length portrait is drawn in.",
    "full body, the whole figure from head to feet",
)

_text(
    "portraits.lore",
    PICTURES,
    "Card pictures: the lore",
    "Added to the writer's instructions when it is given lore entries.",
    "For each lore entry (a place, a thing, a group or a creature), write one prompt of "
    "{words} words for a picture of it as someone there would see it: its shape, "
    "materials, light, weather and what is around it, true to the entry and the setting. "
    "If an entry has nothing that can be seen (a rule, a history, an idea), give it an "
    "empty prompt.",
    (("words", "How long each prompt is"),),
)

_text(
    "portraits.reply",
    PICTURES,
    "Card pictures: the reply",
    "Ends the writer's instructions: the JSON the program reads.",
    'Reply with JSON only: {"portraits": [{"name": "a character\'s name as given", '
    '"prompt": "the prompt"}], "lore": [{"title": "a lore entry\'s title as given", '
    '"prompt": "the prompt, or empty"}]}. Leave out a list you were given nothing for.',
)

# The shot left for `portraits.shot.*` (Nov 3 2026), which follows it:
# "Photorealistic portrait photograph, natural light, sharp focus, head and
# shoulders" where it used to read "…photograph, head and shoulders, natural
# light…".
_text(
    "portraits.style",
    PICTURES,
    "Portraits: the style when the story has none",
    "The picture style a portrait is drawn in when the story has none chosen (Setup → About "
    "→ Picture style). The shot (head and shoulders, upper body, full body) is added after it.",
    "Photorealistic portrait photograph, natural light, sharp focus",
)

# A lore entry's picture is a place or a thing: given the portrait's style
# ("head and shoulders") when the story had none, the writer was asked to
# draw a town so. Not measured.
_text(
    "portraits.style.lore",
    PICTURES,
    "Portraits: the style of a lore picture when the story has none",
    "The picture style a lore entry's picture is drawn in when the story has none chosen "
    "(Setup → About → Picture style).",
    "Photorealistic, natural light, sharp focus",
)

# The ready-made picture styles (engine/picture_styles.py; the author, Nov 3
# 2026). Words a camera or a brush can show, no artist or studio named: an
# image model may copy a name onto the picture. Their first live check is in
# SealedLoreDocs design/images.md ("Picture styles").
for _key, _label, _words in (
    (
        "gritty",
        "Gritty and realistic",
        "Gritty, photorealistic photograph: hard natural light, real skin with pores, lines "
        "and blemishes, weathered faces, worn and dirty clothes, muted desaturated colours, "
        "the look of a documentary photograph",
    ),
    (
        "polished",
        "Clean and polished sci-fi",
        "Clean, polished science-fiction concept art: a sleek high-end digital render, smooth "
        "immaculate surfaces, crisp rim lighting, cool blue and teal tones with bright "
        "accents, fine detail",
    ),
    (
        "glamorous",
        "Glamorous",
        "Glamorous and highly polished: strikingly attractive people with refined features, "
        "flawless skin and styled hair, flattering soft light, rich colours, the look of a "
        "high-fashion magazine photograph",
    ),
    (
        "anime",
        "Anime",
        "Anime style, like a frame from a modern anime series: clean thin line art, flat "
        "cel-shaded colours, simplified faces with large expressive eyes, vivid colours, a "
        "detailed painted background",
    ),
    (
        "painted",
        "Painted fantasy",
        "Painted fantasy illustration: visible brushwork, rich colours, dramatic light, the "
        "look of an epic fantasy book cover",
    ),
    (
        "comic",
        "Comic book",
        "Comic-book art: bold inked outlines, flat colours with hard-edged shadows, halftone "
        "dots, strong graphic shapes",
    ),
    (
        "watercolour",
        "Storybook watercolour",
        "Storybook watercolour: soft washes of colour, light pencil lines, white paper "
        "showing through, warm and gentle",
    ),
    (
        "animated",
        "3D animated",
        "3D animated film look: stylised, appealing characters with soft rounded features, "
        "smooth rendered surfaces, warm cinematic lighting",
    ),
):
    _text(
        f"pictures.styles.{_key}",
        PICTURES,
        f"Picture style: {_label}",
        "The words every picture of a story is drawn in when this style is chosen (Setup → "
        "About → Picture style): its portraits, lore pictures, and pictures and videos of the "
        "story.",
        _words,
    )

# What whoever writes a picture's prompt is told besides, for a style whose
# words alone lost to the description after them: live (FLUX.3 Klein, Nov 3
# 2026) "flawless skin" drew Doomsville's wind-chapped, scarred survivors as
# they were, and "anime" with their pores and stubble came out half real.
_text(
    "pictures.styles.glamorous.note",
    PICTURES,
    "Picture style: Glamorous (to the prompt writer)",
    "Told to whoever writes a picture's prompt when the story's style is Glamorous.",
    "Describe everyone as strikingly attractive: refined, good-looking features, smooth "
    "clear skin, well-kept styled hair, clothes that flatter them. Keep who they are (age, "
    "build, colouring, what they wear), but leave out dirt, wear and blemishes.",
)

_text(
    "pictures.styles.anime.note",
    PICTURES,
    "Picture style: Anime (to the prompt writer)",
    "Told to whoever writes a picture's prompt when the story's style is Anime.",
    "Describe people as an anime draws them: the shape and colour of their hair, their eye "
    "colour, a clear expression, their clothes and what they hold. Leave out skin texture, "
    "pores and stubble.",
)

_text(
    "portraits.no_writing",
    PICTURES,
    "Portraits: no writing",
    "The sentence each portrait prompt ends with.",
    "No writing of any kind in the picture.",
)

_text(
    "portraits.fallback",
    PICTURES,
    "Card pictures: a prompt with no writer",
    "A card's picture prompt when the writer's reply left it out or failed: built from the "
    "card, for the author to edit before anything is drawn.",
    "{style}. {looks} {no_writing}",
    (
        ("style", "The picture style"),
        ("looks", "What the card says of them or it"),
        ("no_writing", "Portraits: no writing"),
    ),
)


# Editing a card's picture (Edit… on a portrait or lore picture): the
# author's change, then what to keep. On John's portrait, FLUX.2 klein 9B,
# Qwen Image, FLUX.2 klein Base Edit, FLUX.2 Flash and Seedream 5.0 Flash
# Edit made all three of a scar, twenty years older and a suit at dinner and
# kept him; Z Image Base ignored the suit, Qwen 2.1 Edit and Nano Banana Edit
# didn't age him (SealedLoreDocs design/images.md, Oct 15 2026).
_text(
    "pictures.edit",
    PICTURES,
    "Card pictures: an edit",
    "The prompt for an edit of a card's picture, sent with the picture itself.",
    "Edit this picture: {change} Keep {keep}; change nothing else. {no_writing}",
    (
        ("change", "What the author wants changed"),
        ("keep", "Card pictures: what an edit keeps"),
        ("no_writing", "Portraits: no writing"),
    ),
)

_text(
    "pictures.edit.person",
    PICTURES,
    "Card pictures: what an edit keeps (a character)",
    "What an edit of a character's picture keeps.",
    "the same person: the same face, features and build, and the pose, framing and light "
    "unless the change says otherwise",
)

_text(
    "pictures.edit.thing",
    PICTURES,
    "Card pictures: what an edit keeps (lore)",
    "What an edit of a lore entry's picture, or of the background, keeps.",
    "the same place or thing: its shape, layout and materials, and the framing and light "
    "unless the change says otherwise",
)

# Edit… on a picture of the story (Media, or a picture's menu in the story):
# the scene, with whoever is in it, is what it keeps. Checked live on one
# picture (SealedLoreDocs design/images.md), not A/B'd.
_text(
    "pictures.edit.scene",
    PICTURES,
    "Card pictures: what an edit keeps (a picture of the story)",
    "What an edit of a picture of the story keeps.",
    "the same scene: the same people with the same faces, hair and clothes, the same place, "
    "and the composition, framing and light unless the change says otherwise",
)

# Looks (engine/looks.py `look_change`): an edit of a character's picture
# into another look, and A set of expressions…. The change, then
# `pictures.edit`'s "keep the same person; change nothing else". Checked live
# on John's Doomsville portrait (Nov 2 2026, angry, laughing, combat armour,
# injured and in pain; SealedLoreDocs design/images.md, "Looks"): FLUX.2 klein
# and FLUX.3 Klein both changed what was asked and kept his face, framing and
# the snow behind him; for "injured" FLUX.2 klein also changed his coat
# ("clothes or state"), FLUX.3 Klein kept it. Not A/B'd.
_text(
    "pictures.look.clothes",
    PICTURES,
    "Looks: another look",
    "The change asked for when a character's picture is drawn in another look (clothes, "
    "armour, injured): the Look the new picture is labelled with.",
    "Change their clothes or state to: {look}.",
    (("look", "The look the new picture is labelled with"),),
)

# A picture redrawn in another style, or showing more or less of them (the
# author, Nov 3 2026: imported pictures "quickly modify them to suit the
# style"; a full-body version of a portrait). Both are edits of the picture:
# the person is kept by `pictures.edit.person`.
# Not an edit's "change nothing else": live (FLUX.3 Klein, Nov 3 2026) that
# kept John a photograph under "anime", "glamorous" and "3D animated", while
# comic and watercolour came through. What is kept is said here instead.
# A story's style from a picture (the author, Nov 3 2026: "base the style off
# the import"): a model that sees pictures says it in words, which become the
# story's own style. Live (Sonnet 4.6) on a woodblock print and a pencil and
# ink-wash sketch, its line drew John and Jane recognisably in each; sending
# the picture itself as a style reference held the palette closer (the sketch
# stayed sepia), and is not built: SealedLoreDocs design/images.md.
_text(
    "pictures.style.from_picture",
    PICTURES,
    "Picture style: from a picture",
    "Sent with a picture to a model that sees pictures (Setup → About → Picture style → From "
    "a picture…): its reply becomes the story's own style.",
    "Describe this picture's visual style for an image model, so that it can draw other "
    "subjects in the same style: one line of 20 to 40 words naming its medium, line, colour, "
    "light and texture. Say nothing of what or who the picture shows. Reply with the line "
    "alone.",
)

# The picture a story's own style was taken from, sent as a style reference
# (the author, Nov 3 2026, after it held a sketch's sepia where the words
# alone added colour): the words still follow it.
_text(
    "pictures.style.reference",
    PICTURES,
    "Picture style: the style's picture",
    "Put first in a picture's prompt when the picture the story's own style was taken from "
    "goes with it.",
    "In the art style of image {number}: take only its medium, line, colour, light and "
    "texture, none of its people, places or things.",
    (("number", "The style picture's number among the pictures sent"),),
)

_text(
    "pictures.restyle.reference",
    PICTURES,
    "Card pictures: another style, from its picture",
    "Opens the prompt of a picture redrawn in the story's own style when the picture that "
    "style was taken from goes with it; what else to change and what to keep follow it.",
    "Redraw image 1 entirely in the art style of image {number}: its medium, line, colour, "
    "light and texture, none of its people, places or things. In words: {style}.",
    (
        ("number", "The style picture's number among the pictures sent"),
        ("style", "The style's words"),
    ),
)

_text(
    "pictures.restyle",
    PICTURES,
    "Card pictures: another style",
    "Opens the prompt of a picture redrawn in another style (Change this picture…, Restyle "
    "pictures…); what else to change and what to keep follow it.",
    "Redraw this picture entirely in this style: {style}.",
    (("style", "The style's words"),),
)

_text(
    "pictures.restyle.person",
    PICTURES,
    "Card pictures: what a restyle keeps (a character)",
    "Follows `pictures.restyle` for a character's picture.",
    "Keep the same person, with what is around them, in the same pose, clothes and framing "
    "unless the change says otherwise; change only how it is all drawn.",
)

_text(
    "pictures.restyle.thing",
    PICTURES,
    "Card pictures: what a restyle keeps (lore)",
    "Follows `pictures.restyle` for a lore entry's picture or the background.",
    "Keep the same place or thing, its layout and the viewpoint; change only how it is drawn.",
)

_text(
    "pictures.restyle.scene",
    PICTURES,
    "Card pictures: what a restyle keeps (a picture of the story)",
    "Follows `pictures.restyle` for a picture of the story.",
    "Keep the same scene: the same people with the same faces and clothes, the same place "
    "and composition; change only how it is drawn.",
)

_text(
    "pictures.reframe.head",
    PICTURES,
    "Card pictures: closer, head and shoulders",
    "The change asked for when a picture of someone is to show only their head and shoulders.",
    "Show them closer: head and shoulders only.",
)

_text(
    "pictures.reframe.torso",
    PICTURES,
    "Card pictures: the upper body",
    "The change asked for when a picture of someone is to show them from the waist up.",
    "Show them from the waist up, with their hands and what they hold, the picture "
    "widened around them as it would go on.",
)

_text(
    "pictures.reframe.full",
    PICTURES,
    "Card pictures: the whole figure",
    "The change asked for when a picture of someone is to show their whole figure.",
    "Show their whole figure from head to feet, standing, footwear included, the picture "
    "widened around them as it would go on.",
)

_text(
    "pictures.look.expression",
    PICTURES,
    "Looks: another expression or pose",
    "The change asked for when a character's picture is drawn with another expression or "
    "pose: the Expression or pose the new picture is labelled with.",
    "Change their expression or pose to: {expression}.",
    (("expression", "The expression or pose the new picture is labelled with"),),
)

# How the usual expressions are drawn (engine/look_sheet.py `wording`): what
# fills `pictures.look.expression` for an expression the card has no words of
# its own for. The author (Nov 7 2026): a column's heading says the emotion,
# and the words it is drawn with should say more ("a happy expression and a
# bright smile", or "happy but reserved, a small smile and bright eyes"); a
# card's own words replace these. Not A/B'd: before, the label alone was sent.
for _key, _words in (
    ("neutral", "a calm, neutral expression, relaxed"),
    ("happy", "a happy expression with a bright smile"),
    ("sad", "a sad expression, eyes downcast and the mouth turned down"),
    ("angry", "an angry expression, brows drawn together and the jaw set"),
    ("afraid", "a frightened expression, eyes wide and shoulders tense"),
    ("surprised", "a surprised expression, eyes wide, eyebrows raised, mouth open"),
    ("talking", "talking, mouth open mid-word, as though speaking to someone"),
    ("thinking", "a thoughtful expression, looking aside, lost in thought"),
    ("in a fighting stance", "in a fighting stance, weight forward, ready to strike"),
):
    _text(
        f"looks.wording.{_key.replace(' ', '_')}",
        LOOKS,
        f"How “{_key}” is drawn",
        f"The words an image model is given for the expression or pose “{_key}”, when a "
        "character's card has none of its own for it (the cast board's column menu, How it is "
        "drawn…).",
        _words,
    )

# Looks (engine/look_pick.py): after a passage, which of their pictures each
# character in it is shown with. On the scene model (in a private scene, the
# private model). Measured on Doomsville passages with labelled looks
# (SealedLoreDocs design/images.md, "Looks"): see there before changing it.
_text(
    "looks.pick.system",
    LOOKS,
    "Choosing looks after a passage",
    "Which of their pictures each character is shown with after a passage: the request's "
    "instructions. The people, their pictures by label, the author's turn and the passage "
    "follow.",
    "You choose how each person below is shown beside the story, from their own pictures, "
    "each named by a handle and described by labels: a look that lasts (clothes, armour, an "
    "injury) and the moment's expression or pose.\n\n"
    "For each person, choose the one picture that best fits them at the end of the latest "
    "passage:\n"
    "- Their look lasts: keep the look they are shown with now unless the passage or the "
    "author's turn changes it (they change clothes, put armour on, are hurt).\n"
    "- Their expression or pose is the moment's: take it from what they are doing and "
    "feeling at the end of the passage. When it shows nothing of them, keep what they have.\n"
    "- Choose only among their own pictures. When none fits well, choose the nearest, and "
    'say in "missing" the look and expression that would fit, as "look · expression"; '
    "otherwise leave it empty.\n\n"
    "Answer with JSON only, one entry for every person listed:\n"
    '{"looks": [{"name": "their name as listed", "picture": "L1", "missing": ""}]}',
)

# Draw a new background… (Media): the direction the story's prompt writer is
# given for a picture to stand behind the story's text. The author sees it
# in the picture dialog and can change it before anything is written.
# Checked live once (Brasshaven, GLM 5.3 writing, FLUX.2 klein drawing): the
# first wording, "a wide view of where the story is now", got the moment
# (the automaton in the doorway, two constables coming) and the workshop's
# name, which FLUX lettered on a sign; asked for the place and not what is
# happening in it, and for no signs or names, it got a quiet street.
_text(
    "pictures.background",
    PICTURES,
    "Card pictures: a background",
    "The direction given to the picture prompt writer for the story's background, the "
    "picture behind its text; the author can change it before it is written.",
    "A wide, quiet view of the place where the story is now, to stand behind the story's "
    "text as its background: the place itself, its light and weather, seen from a little "
    "way off, not what is happening in it. No one close to the viewer, and no signs, names "
    "or writing of any kind: describe the place, never name it.",
)

# A character alone in the current scene (In the scene… on a card): the
# story's own prompt writer, which has read the story, is given this as the
# author's direction, with the character's pictures as references.
_text(
    "pictures.scene_of",
    PICTURES,
    "Card pictures: a character in the scene",
    "The direction given to the picture prompt writer for a picture of one character as "
    "they are at this moment of the story; the author can change it before it is written.",
    "{name} alone, as they are at this moment in the story: where they are and the place "
    "around them, what they wear and carry, what they are doing, the light and the weather. "
    "Nobody else in the picture.",
    (("name", "The character's name"),),
)

# A picture of the place the scene is at (the author, Oct 24 2026: not the
# scene, "a picture of the area, not including the characters in it"), kept
# on the place's lore entry and shown behind the story while the scene is
# there. The story's own prompt writer, which has read the story, is given
# this as the author's direction; the author may change it first.
_text(
    "pictures.place",
    PICTURES,
    "Card pictures: a place",
    "The direction given to the picture prompt writer for a picture of the place the "
    "scene is at, the place itself with nobody in it; the author can change it before it "
    "is written.",
    "A picture of {place}, the place itself as it is at this moment of the story: its "
    "look and layout, its light and weather, what stands or lies in it, seen as someone "
    "arriving would see it. Nobody in the picture, and no signs, names or writing of any "
    "kind: describe the place, never name it.",
    (("place", "The place's name"),),
)

# The same place again, after the story changed it: the earlier picture
# goes as a reference, so the model keeps the place and shows the change (the
# author: a bar first seen quiet, then "after a large, destructive fight").
_text(
    "pictures.place.again",
    PICTURES,
    "Card pictures: a place, again",
    "The direction given to the picture prompt writer for a new picture of a place that "
    "has one, sent with the earlier picture as a reference: the same place as it is now; "
    "the author can change it before it is written.",
    "{place} again, as it is now: the same place as in the earlier picture of it (sent as "
    "a reference), the same layout and viewpoint, with everything the story has done to "
    "it since shown as it stands now: damage, disorder, what was moved, built, broken or "
    "burnt, the light and the weather of this moment. Nobody in the picture, and no signs, "
    "names or writing of any kind.",
    (("place", "The place's name"),),
)


# A part of a picture of the story kept as a card's reference, and a new
# picture drawn from it (the author, Oct 24 2026: a player who likes how a
# character came out in a picture of the scene wants that as their portrait;
# who likes the place wants it as the place's picture, without the people).
# The story's own prompt writer is given this as the author's direction, the
# part as image 1; the author may change it first. Not measured.
_text(
    "pictures.from_picture.person",
    PICTURES,
    "Card pictures: a portrait from a picture",
    "The direction given to the picture prompt writer for a portrait drawn from the person "
    "shown in a part of a picture of the story, sent as image 1; the author can change it "
    "before it is written.",
    "A portrait of {name}, the person shown in image 1: the same face, hair, build, skin "
    "and clothes as in that picture, head and shoulders, looking at the viewer, in a "
    "plain setting. Nobody else in the picture, and no writing of any kind.",
    (("name", "The character's name"),),
)

_text(
    "pictures.from_picture.place",
    PICTURES,
    "Card pictures: a place from a picture",
    "The direction given to the picture prompt writer for a picture of the place shown in a "
    "part of a picture of the story, sent as image 1, with nobody in it; the author can "
    "change it before it is written.",
    "A picture of {place}, the place shown in image 1: the same place, with the same "
    "layout, materials, light and weather, seen from the same viewpoint, but with nobody "
    "in it: leave every person out and show what stands behind them. No signs, names or "
    "writing of any kind.",
    (("place", "The place's name"),),
)


# --- Video -----------------------------------------------------------------
# A video prompt is not a picture prompt: a picture is one frozen moment, a
# video is a few seconds of one continuous shot, so the writer is told the
# length and asked for movement, the camera, and (when the video has sound)
# what is heard. From a start frame the picture already shows how everyone
# looks: the prompt is then about what moves.


_text(
    "video.opening.story",
    VIDEO,
    "Video prompt: the request",
    "Opens the request for a video prompt, asked of the prompt writer on the story's "
    "cached prompt.",
    "Step outside the story. The author wants a short video of it: one continuous shot "
    "lasting {seconds}. Write the prompt a video model will be given to make {subject}.",
    (
        ("seconds", "the video's length, e.g. '5 seconds'"),
        ("subject", "Video prompt: what to show"),
    ),
)

_text(
    "video.opening.chat",
    VIDEO,
    "Video prompt: the request (chat)",
    "Opens the request for a video prompt in a simple chat.",
    "Step outside the conversation. The user wants a short video: one continuous shot "
    "lasting {seconds}. Write the prompt a video model will be given to make {subject}.",
    (
        ("seconds", "the video's length, e.g. '5 seconds'"),
        ("subject", "Video prompt: what to show (chat)"),
    ),
)

_text(
    "video.subject.story_asked",
    VIDEO,
    "Video prompt: what to show (asked for)",
    "What to show, when the author said what they want.",
    "what the author asks for, below, at {moment}",
    (("moment", "Video prompt: the moment"),),
)

_text(
    "video.subject.story_unasked",
    VIDEO,
    "Video prompt: what to show (the scene)",
    "What to show, when the author left it to the writer.",
    "{moment}, in motion: what someone standing there would see happen over those seconds",
    (("moment", "Video prompt: the moment"),),
)

_text(
    "video.moment.last",
    VIDEO,
    "Video prompt: the moment (latest)",
    "The moment shown, for a video of the latest passage.",
    "the moment the last passage ends on",
)

_text(
    "video.moment.earlier",
    VIDEO,
    "Video prompt: the moment (earlier)",
    "The moment shown, for a video of an earlier passage.",
    "the moment the last passage above ends on (the story continues past it, but show "
    "only what has happened by then)",
)

_text(
    "video.subject.chat_asked",
    VIDEO,
    "Video prompt: what to show (chat, asked for)",
    "What to show in a simple chat, when the user said what they want.",
    "what the user asks for, below",
)

_text(
    "video.subject.chat_unasked",
    VIDEO,
    "Video prompt: what to show (chat)",
    "What to show in a simple chat, when the user left it to the writer.",
    "what the conversation is about at this point",
)

_text(
    "video.rules",
    VIDEO,
    "Video prompt: how to write it",
    "The rules for writing a video prompt, in a story or a chat.",
    "The video model knows nothing about this, and names mean nothing to it. So:\n"
    "- Describe everyone and everything shown by what can be seen: apparent age, build, "
    "face, hair, skin, species, clothing, what they hold; the place, light, time of day "
    "and mood.\n"
    "- It is one continuous shot of {seconds}: one action, or a short run of them, that "
    "fits in that time. No cuts, no change of scene, no montage. Say what moves and how: "
    "who does what, in what order, how fast or slowly.\n"
    "- Say what the camera does: how it frames the shot (close-up, full figure, wide) and "
    "whether it holds still, tracks, pans, pushes in or pulls back. One simple move at "
    "most.\n"
    "- Show only what has been established by then. Invent nothing that changes events; "
    "fill in what was left unsaid (a background, a colour) consistently with it.\n"
    "- No text or captions on screen.\n"
    "- One paragraph of plain description in the present tense, {words} words.",
    (
        ("seconds", "the video's length, e.g. '5 seconds'"),
        ("words", "the prompt's length, e.g. '60 to 150'"),
    ),
)

_text(
    "video.audio.on",
    VIDEO,
    "Video prompt: with sound",
    "When the video is made with sound.",
    "- The video is made with sound: say what is heard (the place's own sounds, "
    "footsteps, rain, a door). Speech is optional: if someone speaks, give the exact "
    "words in quotation marks, one short line at most, and say who speaks by how they "
    "look, never by name.",
)

_text(
    "video.audio.off",
    VIDEO,
    "Video prompt: without sound",
    "When the video is made without sound.",
    "- The video has no sound: don't describe sound or speech.",
)

_text(
    "video.start",
    VIDEO,
    "Video prompt: from a start frame",
    "When the video starts from a picture the author chose.",
    "- The video starts from a picture: {start}. Its first frame is that picture, so "
    "don't describe again how things look in it beyond saying who and what is where; "
    "describe what moves and changes from there.",
    (("start", "what the start picture is of"),),
)

_text(
    "video.end",
    VIDEO,
    "Video prompt: to an end frame",
    "When the video ends on a picture the author chose.",
    "- It ends on a picture: {end}. Describe how things move from the start to that picture.",
    (("end", "what the end picture is of"),),
)

_text(
    "video.style.free",
    VIDEO,
    "Video prompt: no style set",
    "When the story sets no picture style.",
    "- Style: choose one that suits the story (say, cinematic live action or painted "
    "animation) and name it in the prompt.",
)

_text(
    "video.reply",
    VIDEO,
    "Video prompt: the reply",
    "Ends the request. The program reads the JSON reply.",
    'Reply with JSON only: {"prompt": "..."}',
)


# --- Drafting and review -------------------------------------------------


_text(
    "authoring.character_shape",
    AUTHORING,
    "A character, as JSON",
    "The shape of a character in the drafting and review prompts. The program reads "
    "these fields: keep them.",
    """\
{
  "name": "...",
  "aliases": ["other names they go by: a nickname, an epithet; never a title others share"],
  "canon": "the published work they appear in, or null if invented for this story",
  "summary": "one or two lines",
  "description": "a fuller sheet; for a canon character, only what this story changes",
  "voice": "how they speak",
  "competence": "what they can and cannot do, in words",
  "skills": {"domain": "tier"},
  "playable": true,
  "author_only": false
}""",
)

# Cards for the people of a published world, asked for from the Cast page or
# the new story guide (the author, Oct 2026: a story in a canon world brought
# in the first few canon characters and seldom the rest, unless the author had
# carded them by hand). Not measured.
_text(
    "authoring.lore_cast",
    AUTHORING,
    "Cards for a world's characters",
    "The system prompt of Fill with lore characters… on the cast board and in the "
    "new story guide. The program reads its JSON reply: keep the shape it asks for.",
    """\
You write character cards for a collaborative roleplay story in SealedLore.
The story is set in a world the author names: a published work or series, a
myth or legend, a period of history. A storyteller model writes the story from
these cards and its own knowledge of that world; the author plays one
character from inside it. A character with a card is one the storyteller
knows belongs in this story, and brings in when they have a reason to come.

Write cards for the people the story is likely to need:

- Everyone the author names, first, under the name the author used.
- Then the world's main and recurring characters, the most important first,
  up to {most} cards in all. Leave out anyone already on file (you are told
  who), anyone who can't be part of the story as its setting stands (dead by
  then, not yet born, gone for good), and groups or crowds.

Sort them:

- "cast": the characters the author names, and the few central figures the
  world turns on: anyone the author might want to play. Their cards are sent
  on every turn, so 2 to 8 of them.
- "supporting": everyone else. Their cards are sent when the story mentions
  them.

Each card:

- "canon": the work or tradition they come from.
- "summary": who they are and what they do in this world, in one or two
  lines.
- "description": 40 to 120 words: how they look (their pictures are drawn
  from it), their manner, and where they stand at the point the story's
  setting describes. Where the author's setting changes something from the
  original, write it as it is in this story.
- "voice": how they speak, in a line.
- "competence": what they can and cannot do, in words.
- "skills": for the cast only, 3 to 6 domains, each a short lowercase noun
  phrase ("swordplay", "court intrigue"), with a tier from {tiers}.
  Supporting characters have none.
- "playable": true for the cast, false for supporting.
- "aliases": other names the story may call them by, if any. Never a title or
  a word alone that would fit others too ("the captain", "the king").

If you don't know the world, or someone the author names, write what you can
and say so in "notes"; never invent a canon for them.

Reply with one JSON object in exactly this shape, and nothing else:

{
  "cast": [ <character> ],
  "supporting": [ <character> ],
  "notes": "anything the author should know, or null"
}

where each <character> is:

{character_shape}""",
    (
        ("most", "how many cards a call writes at most"),
        ("tiers", "the competence tiers"),
        ("character_shape", "A character, as JSON"),
    ),
)

_text(
    "authoring.lore_cast.world",
    AUTHORING,
    "Lore characters: the world",
    "The request's first line: the world the author named.",
    "The world the story is set in: {lore}",
    (("lore", "the world, as the author named it"),),
)

_text(
    "authoring.lore_cast.wanted",
    AUTHORING,
    "Lore characters: whom the author wants",
    "When the author named characters they want for sure.",
    "The characters the author wants for sure, as they wrote them: {wanted}",
    (("wanted", "the names, as the author wrote them"),),
)

_text(
    "authoring.lore_cast.setting",
    AUTHORING,
    "Lore characters: the story's setting",
    "When the story has a world written: the cards fit it.",
    "The story's setting, as the author wrote it. Fit the cards to it where it "
    "differs from the original:\n\n{setting}",
    (("setting", "the story's world text"),),
)

_text(
    "authoring.lore_cast.known",
    AUTHORING,
    "Lore characters: already on file",
    "The cards the story has, so the reply adds others.",
    "Already on file, so not to be written again: {names}",
    (("names", "the names of the story's cards"),),
)

_text(
    "authoring.style_guide",
    AUTHORING,
    "The style fields",
    "What each style field means, in the drafting and review prompts.",
    """\
  - "response_style": one of {styles}. "adaptive" (the
    default) scales each passage to the author's turn.
  - "length_target": a length in your own words ("two to four paragraphs,
    mostly dialogue"). Setting it selects a custom length by itself; never set
    "response_style" to "custom" — without this wording it means nothing.
  - "perspective": "whole_story" lets passages cut away to other characters
    and places; "with_character" narrates only what the author's character can
    perceive.
  - "person" ({persons}) and "tense" ({tenses}).
  - "prose_density", "dialogue_narration_balance", "pacing", "content_rating",
    "content_limits": short free text.
  - "forbidden_phrases": a list of words or clichés to avoid.
  - "notes": free text for anything about how passages should read that no
    other field covers.""",
    (
        ("styles", "the length presets"),
        ("persons", "the narrative persons"),
        ("tenses", "the narrative tenses"),
    ),
)

# Where a draft puts things, brought up to date (the author, Nov 6 2026: "make
# sure it still knows all the right places to put info"): the starting scene
# (supporting characters may be present; a cast member in neither list is the
# opening's to place, `prompt.not_placed_at_start`), the world's pace
# (`world_activity`), the pictures' look (`picture_style`), aliases that name
# one person. Live on four invented premises (GLM 5.3 and Sonnet 4.6, 20
# drafts): world activity eventful 3/3 for a busy harbour, quiet 3/3 for a
# quiet cottage, normal 3/3 for an academy; a named look (watercolour, anime)
# taken 4/4 and 4/4. Tone words first gave "Gritty and realistic" to 5 of 6
# premises that named no look; saying tone is the prose's left the harbour at
# the default 2/2, while "Gritty and tense" still gave it 2 of 3. Supporting
# characters placed in the opening scene in 4 drafts of 20.
_text(
    "authoring.draft",
    AUTHORING,
    "Drafting a story from a premise",
    "The system prompt of File → New story from a premise. The program reads its JSON "
    "reply: keep the shape it asks for.",
    """\
You set up a collaborative roleplay story in SealedLore from an author's
premise. The author will play one character from inside the story (they can
switch later); a storyteller model writes everyone and everything else. You
write the setup the storyteller works from — not the story itself.

SealedLore's settings, and what each is for:

- "world": the standing facts and rules of the setting, sent in full on every
  turn. Put here whatever must always hold: how the world works, its powers
  and their limits, factions, technology or magic — and, for a crossover or
  alternate history, exactly how the settings meet and what differs from
  canon. 300 to 900 words. Where one side knows nothing of something, name
  the gap without the fact: "Hellsville has no knowledge of the Northern
  Camp", never "Hellsville doesn't know the Northern Camp holds four hundred
  people" — the storyteller reads a fact inside a sentence about
  someone as theirs, and their characters quote it.
- "lore": detail that matters only sometimes — places, organisations,
  artefacts, history, customs. Each entry is found by its relevance to the
  current scene, so keep each to one subject, 40 to 150 words, with 3 to 6
  keywords the story would use when it matters. 3 to 10 entries. Nothing that
  must always hold: that belongs in "world". The storyteller takes every
  entry it is given as already true, so what hasn't happened when the story
  opens (an arrival planned for later, a secret still to come out, an event
  to come) goes nowhere as fact: leave it out, or write it as the plan it is
  and set "until_mentioned" true, which keeps the entry from the storyteller
  until the author's own turn names it.
- "cast": the characters the author may play and the few central characters
  the story turns on. Every cast sheet is sent on every turn, so 2 to 6 of
  them. "playable" is true for anyone the author might want to play.
- "supporting": everyone else worth a card — secondary characters, and canon
  characters the story uses. A canon character (from a known film, series,
  book or game) needs only "canon", a one-line "summary", and in
  "description" only what this story changes, because the storyteller already
  knows them. Supporting characters have no "skills" and are not playable.
- "skills": per-character tiers, used when the author rolls dice. 3 to 6
  domains, each a short lowercase noun phrase naming an area of skill
  ("piloting", "tracking", "deception") — never a sentence — with a tier
  from {tiers}. Show a weakness as a low tier
  on such a domain ("patience": "poor").
- "canon": only for a character who appears in a published work. A character
  invented for this story has null, even in a canon setting.
- "play_as": the cast member the author most likely plays — whoever the
  premise puts them in the shoes of, or else the most natural protagonist.
- "opening": notes to the storyteller for the first passage: where and when
  it starts, what is happening, what has just happened, the mood. 60 to 200
  words, written as instructions, not as the passage, in the person and tense
  "style" sets (third person, past tense, if it sets neither): the first
  passage takes its voice from them. The first passage must
  end at a moment where the "play_as" character can act, and must not decide
  anything that character says, does, thinks or feels. Only the "play_as"
  character belongs to the author: the storyteller voices everyone else, so
  others may speak and act in the opening.
- "starting_scene": where the story begins. "present" names everyone the
  opening puts in the scene: the "play_as" character always, and supporting
  characters as well as the cast. "nearby" names cast members close at hand
  but not in it, each with a short note. A cast member in neither is placed by
  the first passage, as the opening has them.
- "style": how passages should read. Set only what the premise implies and
  leave the rest null.
{style_guide}
- "agency_mode": how the author's actions resolve. "fiat": as written.
  "plausible": if they make sense. "contested" (the default): the world pushes
  back. "dice": rolled against skills.
- "world_activity": how much happens that the author didn't start — people
  acting on their own aims, someone arriving, an earlier choice catching up:
  one of {world_activities} ("normal" when the premise doesn't say). A quiet,
  close story is "quiet"; one whose world won't wait is "eventful".
- "picture_style": how the story's pictures look (portraits, places), never
  its prose: one of {picture_styles}, or a few words of your own for a look
  none of those is. Set it only when the premise asks for a look in so many
  words (an art style or a medium: "anime", "like a watercolour"); words for
  the story's tone ("gritty", "elegant", "dark") are its prose's, and leave
  it null, the default: realistic. A look asked for never goes in "style".

Honour everything the premise specifies. Where it is silent, make choices that
give the author a strong story to play in: people who want things that pull
against each other, and something about to happen.

Reply with one JSON object in exactly this shape, and nothing else:

{
  "title": "...",
  "description": "one or two sentences for the story list",
  "world": "...",
  "cast": [ <character> ],
  "supporting": [ <character> ],
  "lore": [ {"title": "...", "content": "...", "keywords": ["..."], "until_mentioned": false} ],
  "play_as": "a cast member's name",
  "opening": "...",
  "starting_scene": {
    "location": "...", "time_of_day": "...", "situation": "...",
    "present": ["cast names"], "nearby": [ {"name": "...", "note": "..."} ]
  },
  "style": {"response_style": "adaptive", "perspective": "whole_story", ...},
  "agency_mode": "contested",
  "world_activity": "normal",
  "picture_style": null
}

where each <character> is:

{character_shape}""",
    (
        ("tiers", "the competence tiers"),
        ("style_guide", "The style fields"),
        ("character_shape", "A character, as JSON"),
        ("world_activities", "the allowed values, quoted and comma-separated"),
        ("picture_styles", "the ready-made picture styles' names, quoted and comma-separated"),
    ),
)

# A story drafted around characters from Your characters (the author, Nov 4
# 2026): their cards stand, and what the story changes of them is said apart
# (engine/drafting.py). Not measured.
_text(
    "authoring.draft.characters",
    AUTHORING,
    "Drafting: the author's own characters",
    "After the premise, when the story is drafted around characters from Your characters.",
    """\
The author wants the story built around characters of their own, each with a
card that stands as it is. Put each in "cast" under exactly the name given
below, with "playable" and "author_only" as the premise suggests, and write
nothing else for them: their summary, description, voice and skills are
their card's. Where this story places one of them differently from their
card (another world or time, a role in it, what has happened to them), say
so in their own "in_this_story": one to three sentences. A card's scenario
is a suggestion only: the premise decides.

{characters}""",
    (("characters", "each character's card, under their name"),),
)

# How much a draft sets up (the author, Nov 6 2026: "just ... a handful of
# characters and a basic world based on their premise, or ... really outlined
# in detail, with lots of characters, each with lots of info about them";
# engine/draft_detail.py). The standard levels are the drafting prompt's own
# sizes and add nothing; another level is a line in the request, after the
# premise, so the system prompt never changes.
#
# Measured live (Nov 6 2026, three invented premises; GLM 5.3 at low
# reasoning, Sonnet 4.6 on Anthropic's own API; every reply parsed, none cut
# off, the largest 21,800 output tokens with GLM's reasoning):
# - Basic, a handful, briefly (GLM, 3): world 160-203 words, 2-4 lore entries
#   of 38-53, 3 in the cast at 44-58 words and 2-3 others at 21-34; 2,000
#   output tokens, under a minute. Standard: 4-5 in the cast at 59-96 words
#   and 2-5 others, 3,600-11,300 tokens.
# - Detailed, lots, in depth (GLM 3, Sonnet 2): world 745-1,129 words (most
#   just under 900; "at least 900" changed nothing, 873 against 895 words on
#   average), 12-20 lore entries of 82-131, 6 in the cast and 10-15 others;
#   four to six minutes, 10,500-16,200 output tokens.
# - In depth as first worded, GLM's supporting characters had 63-134 words
#   (two of three under 120) and one Sonnet draft gave its supporting
#   characters no voice. Saying "the supporting ones too" and "for everyone,
#   a voice": GLM's supporting 96-157 (two of three in), its cast 208-222
#   where it had been 247-279, its whole output the same; Sonnet's cast
#   315-322 and supporting 146-161 either way. Kept, for depth spread across
#   everyone rather than the cast alone.
_text(
    "authoring.draft.detail",
    AUTHORING,
    "Drafting: how much to set up",
    "After the premise, when the author chose more or less than the standard world, "
    "people or detail about each.",
    "The author has chosen how much to set up. For this story, these sizes replace the "
    "ones above:\n\n{sizes}",
    (("sizes", "a line for each choice away from the standard one"),),
)

for _key, _title, _words in (
    (
        "world.basic",
        "a basic world",
        'A basic world: "world" 100 to 300 words, only what must always hold; "lore" 0 '
        "to 4 entries of 40 to 100 words each.",
    ),
    (
        "world.detailed",
        "a detailed world",
        'A detailed world: "world" 900 to 1,600 words; "lore" 10 to 20 entries of 80 to '
        "200 words each, covering the setting's places, peoples, organisations, history "
        "and customs.",
    ),
    (
        "people.handful",
        "a handful of people",
        'A handful of people: 2 or 3 in "cast", and at most 3 in "supporting", only those '
        "the premise or the opening needs.",
    ),
    (
        "people.lots",
        "lots of people",
        'Lots of people: 5 to 8 in "cast", and 10 to 20 in "supporting": the regulars, '
        "rivals, family, officials and others the story's places would have.",
    ),
    (
        "depth.sketch",
        "each character in brief",
        'Each character in brief: a one-line "summary", a "description" of 30 to 80 '
        'words, a "voice" of a few words, and 3 "skills" for each cast member.',
    ),
    (
        "depth.deep",
        "each character in depth",
        'Every character in depth, the supporting ones too: a "description" of 250 to 500 '
        "words for each cast member and 120 to 250 for each supporting one (how they look, "
        "their history, what they want and fear, their secrets and habits, and how they "
        'stand with the others); for everyone, a "voice" that says how they speak, with a '
        'line or two they might say; and 5 or 6 "skills" for each cast member. A canon '
        "character still needs only what this story changes.",
    ),
):
    _text(
        f"authoring.draft.{_key}",
        AUTHORING,
        f"Drafting: {_title}",
        "One of the sizes the author chose, in the request after the premise.",
        _words,
    )

# The author's follow-ups on a story set up for them (the author, Nov 6 2026:
# "they might reply, 'I like the world, but I'd prefer the characters were
# more badass.' Or 'I like the characters, but the world isn't hostile
# enough.' and it can update it, with the ability to undo"). The changes are
# a review's kinds and fields, applied at once (engine/followup.py), so what
# the author likes is never written again. Live (GLM 5.3, Sonnet 4.6, 9 story
# requests on drafted stories): "more badass" rewrote 4 of 7 cards with the
# world and lore untouched 3/3; "the world isn't hostile enough" the world and
# 3-4 lore entries with the cards untouched 3/3; a swap took one card out, put
# a stranger in and mended the lore that named the one gone; "start at night"
# the opening and scene alone. 14-55 seconds; every undo exact.
_text(
    "authoring.followup",
    AUTHORING,
    "Changing a story at the author's request",
    "The system prompt of a follow-up on a story not yet begun: after a draft, and from "
    "Story → Change the story with the model…. The program reads its JSON reply: keep "
    "the kinds and fields it asks for.",
    """\
You change the setup of a collaborative roleplay story in SealedLore at its
author's request. It was set up for them, by a model from their premise or
by their own hand, and has not begun. They have read it and say what they
want different. You change exactly that, and whatever must change with it
for the story to hold together, and leave everything else as it is.

You are given the setup as JSON: its title and description; "world", the
standing facts sent to the storyteller on every turn; "lore", detail it is
given when it matters; "cast", the characters the author may play and the few
the story turns on, every one sent on every turn; "supporting", everyone else
with a card; "opening", the notes the first passage is written from, and
"starting_scene"; "style", how the prose reads; "agency_mode";
"world_activity", how much happens that the author didn't start; and
"picture_style", how its pictures look. Then what the author asked before,
if anything, and what they ask now.

How to answer:
- Do all of what they ask, wherever it applies: "the characters more
  badass" means every character it fits, not one; "a more hostile world"
  means the world text, and the lore, opening and cards the change makes
  untrue.
- What they like stays as it is, and so does whatever their request doesn't
  touch. A change replaces a whole text, so keep every sentence of it that
  isn't part of the request word for word.
- Keep names unless they ask: they have read them.
- Put each thing where it belongs: what must always hold in "world", detail
  that matters sometimes in "lore", who someone is on their card, how the
  prose reads in "style", how the pictures look in "picture_style" (never
  "style"). Where one side knows nothing of something, name the gap without
  the fact: the storyteller reads a fact inside a sentence about someone as
  theirs.
- The opening's notes are written in the story's person and tense, and end
  where the author's character can act.

What you can change. Each change is one setting:
- kind "story": field "title", "description", "agency_mode" ({agency_modes}),
  "world_activity" ({world_activities}), or "picture_style" (one of
  {picture_styles}, a few words of your own for another look, or null for
  the default, realistic).
- kind "world": the value is the complete new world text.
- kind "opening": field "text" (the notes the first passage is written from)
  or "mode" ({opening_modes}).
- kind "scene": the starting scene; field "location", "time_of_day",
  "situation", or "present" (the names of everyone the opening puts in it,
  the author's own character always; anyone else is placed by the first
  passage).
- kind "style": field one of {style_fields}.
{style_guide}
- kind "character": "target" is the character's name exactly as in the
  setup; field one of {character_fields}. "skills" is the complete new
  mapping of domain to tier ({tiers}).
- kind "character_add": "target" is "cast" or "supporting", and the value is
  a character object:
{character_shape}
- kind "character_move": "target" is a character's name, the value "cast"
  or "supporting".
- kind "character_remove": "target" is a character's name: out of the story.
- kind "lore_add": the value is {"title": ..., "content": ..., "keywords":
  [...], "until_mentioned": false}.
- kind "lore_edit": "target" is the entry's title; field one of {lore_fields}.
- kind "lore_remove": "target" is the entry's title: out of the story.
For text fields the value is the complete new text; for lists, the complete
new list; null clears a setting.

Reply with one JSON object and nothing else:

{
  "summary": "one to three sentences, to the author: what you changed",
  "changes": [
    {"kind": "...", "target": null, "field": "...", "value": ..., "reason": "..."}
  ],
  "cannot_fix": ["anything they asked that these changes can't do, and why"]
}""",
    (
        ("agency_modes", "the allowed values, quoted and comma-separated"),
        ("world_activities", "the allowed values, quoted and comma-separated"),
        ("picture_styles", "the ready-made picture styles' names, quoted and comma-separated"),
        ("opening_modes", "the allowed values, quoted and comma-separated"),
        ("style_fields", "the style fields"),
        ("style_guide", "The style fields"),
        ("character_fields", "the character fields"),
        ("tiers", "the competence tiers"),
        ("character_shape", "A character, as JSON"),
        ("lore_fields", "the lore fields"),
    ),
)

_text(
    "authoring.followup.request",
    AUTHORING,
    "Changing a story: the request",
    "The user message of a follow-up: the setup as it is now, and what the author asks.",
    "The story's setup as it is now:\n\n{setup}\n\n{earlier}The author asks: {request}",
    (
        ("setup", "the setup, as JSON"),
        ("earlier", "what the author asked before and what was done, or nothing"),
        ("request", "the author's words"),
    ),
)

_text(
    "authoring.followup.earlier",
    AUTHORING,
    "Changing a story: what was asked before",
    "In a follow-up's request, when the author has asked for changes before.",
    "Asked before, and done (keep to it unless they now ask otherwise):\n{earlier}\n\n",
    (("earlier", "each request and what was done, a line each"),),
)

# A person or tense the author chose comes before the draft, so the opening
# is written in it (the author, Sept 2026: a second-person story drafted with
# a third-person opening).
_text(
    "authoring.draft.voice",
    AUTHORING,
    "Drafting: the chosen person and tense",
    "After the premise, when the author chose a person or tense before drafting.",
    'The author has chosen how the story is told. {narration} Set "style" to match, '
    'and write "opening" that way too{who}.',
    (
        ("narration", "the person and tense clause, as on the REMEMBER line"),
        ("who", '\', with the "play_as" character as "you"\' (or "I"), or nothing'),
    ),
)

# The example in the plot writer's prompt: a line of the reply each, never
# wrapped (a fact's line can't continue, and a model copies whatever the
# example does). tests/test_plot_draft.py checks it imports cleanly.
PLOT_EXAMPLE = "\n".join(
    [
        "# Characters",
        "",
        "## Teodor Vask",
        "- role: storyteller",
        "- hidden: yes",
        "- summary: A customs captain from the capital, sent to find who smuggles "
        "through the harbour.",
        "",
        "Fifty, lean and grey, in a salt-stained blue coat. Patient, polite and very",
        "hard to lie to; he listens more than he talks. He wants a name to send back to",
        "the capital, and he isn't particular whose.",
        "",
        "# Places",
        "",
        "## The Salt Market",
        "",
        "## The Drowned Chapel",
        "- keywords: the sea cave",
        "- hidden: yes",
        "",
        "A half-flooded chapel in a sea cave below the cliffs, reached only at low",
        "tide. Crates are stacked on its old altar.",
        "",
        "# Facts",
        "",
        "- suspicion: calm (calm, wary, hunting) — how much the harbour watch suspects "
        "the smuggling; set by events",
        "- owes_vask: no [watch] — yes once the player's character has agreed to help "
        "Teodor Vask or taken a favour from him; being questioned by him, or talking "
        "with him, is not enough",
        "",
        "# Timeline",
        "",
        "## The customs captain arrives",
        "- when: day 3–6",
        "- pacing: any time",
        "- lead-in: yes",
        "- requires: suspicion = calm",
        "- trigger: The player's character is in the Salt Market or on the quays by day.",
        "- brings: Teodor Vask",
        "- sets: suspicion = wary",
        "- aftermath: A customs captain from the capital, Teodor Vask, has taken rooms "
        "above the harbour and asks questions of everyone. The whole town knows it, "
        "and the smugglers have gone quiet.",
        "",
        "A ship from the capital puts in, and its customs men come ashore.",
        "",
        "## The raid",
        "- when: day 10–14",
        "- lead-in: yes",
        "- requires: The customs captain arrives happened, suspicion = wary",
        "- must happen: yes",
        "- sets: suspicion = hunting",
        "- aftermath: The watch raided the harbour by night on the customs captain's "
        "orders: doors broken in, two boatmen taken, crates seized. Everyone has a "
        "story about who talked.",
        "",
        "The harbour watch is coming for the smugglers, and soon.",
        "",
        "### The player's character is at the harbour",
        "- requires: at = The Salt Market",
        "- at: The Salt Market",
        "- tell: At night the watch, led by Teodor Vask, breaks down doors along the "
        "quays. They are after crates and the men who move them, not the player's "
        "character, unless they are given a reason. What the player's character does "
        "is theirs to decide.",
        "",
        "### Away from the harbour",
        "- requires: at != The Salt Market",
        "- offscreen: yes",
        "",
        "# Events",
        "",
        "## Vask calls in the favour",
        "- pacing: quiet moment",
        "- requires: owes_vask, The customs captain arrives happened, The raid not happened",
        "- trigger: The player's character is somewhere Teodor Vask could find them: "
        "the Salt Market, the quays, their lodging.",
        "- tell: Teodor Vask finds them and asks for what he is owed: the name of "
        "whoever brings the crates ashore. He is polite and in no hurry, and he will "
        "remember whatever answer he is given.",
        "",
        "## The chapel at low tide",
        "- pacing: any time",
        "- requires: The raid happened",
        "- reveals: The Drowned Chapel",
        "- trigger: The player's character asks where the smugglers took their goods, "
        "or goes down to the shore below the cliffs at low tide.",
        "- tell: An old boatman, or the shore itself at low tide, shows them the way "
        "into the sea cave below the cliffs: a half-flooded chapel with crates stacked "
        "on its altar.",
    ]
)

# A plot for a story, written by the model (engine/plot_draft.py; the author,
# Nov 2026: stories from scratch or from a premise had no plot, since plots
# were written by hand). The reply is a plot file's plot, read by the plot
# editor's reader and judged by the importer, as a file would be. The rules are
# WRITING_PLOTS.md's, from watching plots play; the example is checked to
# import cleanly (tests/test_plot_draft.py). Not measured yet.
_text(
    "authoring.plot",
    AUTHORING,
    "Writing a plot",
    "The system prompt of Story → Write a plot with the model…, the new story guide's "
    "plot step and a premise draft's plot. The program reads its reply as a plot file: "
    "keep the format it asks for.",
    """\
You write the plot of a collaborative roleplay story in SealedLore. The
author plays one character from inside the story; a storyteller model writes
everyone and everything else, a passage at a time. A plot gives the story
things that happen when the time and the moment are right: events the program
keeps on its clock, which a small director model passes on to the storyteller
when the moment suits, and facts the program keeps about how things stand.
Nobody sees the plot whole. The storyteller learns of an event only as it
begins, and the author meets it as it happens.

You are given the story as it is set up, written in SealedLore's plot file
format: its world, its opening, its characters, places and lore. You are told
what the author wants of the plot. Write the plot: its facts and its events,
and the people, places and lore those events bring into the story that it
doesn't have yet. Never write the story itself, and never write again what
the story already has.

## Who reads each part

- `requires:`, `when:`, `at:` and `must happen:` are checked by the program,
  exactly, every turn.
- `trigger:` and `pacing:` are judged by the director before a passage. It
  sees the clock, where the author's character is, the scene (where, who is
  there, what is going on), the last couple of exchanges and the author's new
  turn: nothing older, and nothing off the page.
- The event's own text and a variant's `tell:` are what the storyteller is
  told when the event begins. With `lead-in: yes`, the event's own text is
  also shown to the storyteller days ahead, as something coming, with the
  names of whoever the event brings taken out.
- `aftermath:` stays with the storyteller for the rest of the story once the
  event has happened and the author's character has seen it or heard of it.
- A watched fact's meaning is all a small reader model has, after each
  passage, to decide whether the fact changed.

## The format

The plot is these sections, in this order, each a `#` heading; leave out any
you don't need. Every item in a section is a `##` heading. Its settings are
`- key: value` lines straight after the heading, and its text comes after the
settings. A way an event can go (a variant) is a `###` heading under it.

- `# Characters`: only people the plot brings in that the story doesn't have.
  Settings: `role` (storyteller: the storyteller writes them; supporting:
  someone minor, whose card is shown when they come up; player: someone the
  author could take up later), `hidden: yes` (kept from the story until an
  event's `brings:` names them), `aliases`, `summary` (one line). The text is
  who they are, how they look, what they want: never when or how they turn up.
- `# Places`: every place an `at:`, an `at` condition or a `visited`
  condition names. For one of the story's own places or lore entries, write
  its exact title as a `##` heading with nothing under it. A new place takes
  `keywords` (other names for it and nothing else) and `hidden: yes` if the
  story should find it (an event `reveals:` it); its text is what it is like.
- `# Lore`: only new background the plot needs, written as a place is; a
  hidden entry is revealed by an event.
- `# Facts`: one line each, `- name: starting value (value, value) [watch] —
  meaning`. A name is lower-case letters, digits and `_`. A fact that starts
  at yes or no is yes or no and needs no list. `[watch]` only for a fact the
  prose must decide; every other fact changes only when an event `sets:` it.
- `# Timeline`: events with a window of days, `when: day 4–7` (day 1 is the
  day the story opens). Settings: `when`, `pacing` (quiet moment, or any
  time), `lead-in: yes`, `requires`, `trigger`, `at`, `brings`, `reveals`,
  `sets` (`fact = value, fact = value`), `must happen` (yes or no),
  `aftermath`. The text under the settings is what the storyteller is told
  should happen.
- `# Events`: events with no window, which happen once their conditions hold
  and the moment suits. The same settings, but `must happen`; `when:` too, as
  the first and last days it may happen on.
- A variant (`###`) takes `requires`, `trigger`, `tell`, `sets`, `brings`,
  `reveals`, `at`, and `offscreen: yes` (it happens away from the author's
  character: no prose, only its facts).

Conditions (`requires:`), separated by commas, are all checked by the program:
`fact = value`, `fact != value`, `fact` alone for `fact = yes`, `not fact` for
`fact = no`; `at = Place`, `at != Place` (where the author's character is
now); `visited Place`, `not visited Place` (whether they have ever been
there); `Name at = Place`, `Name visited Place` (the same, of one playable
character by name); `Title happened`, `Title not happened` (of an event, by
its own `##` title, never a variant's). Every fact a condition or a `sets:`
names is one of yours under `# Facts`, with one of its own values.

## How to write each part

Conditions and triggers:
- If the program can check it, put it in `requires:` or `at:`: a fact, a
  place, an event that has happened. The trigger is only what a reader of the
  passage can tell: what the character is doing, where exactly, with whom.
  Don't repeat a condition there.
- Name what a passage would show: a place, an action, a person. Never an
  inner state, or something that happens off the page.
- Write for the company the character keeps: "alone" in a story whose people
  travel together waits for a moment that never comes.
- Don't wait on a line the author may never say: give the world a way in too,
  or a window.
- Say where an event can't happen. A roadside ambush with no condition is
  offered to the director in town too: give an optional event `requires: at !=
  Town`, and a timeline event a variant, its only way to go, with that
  condition.

Pacing:
- A quiet moment waits for a lull, and never cuts into a fight or an
  important conversation. Any time begins as soon as its trigger is happening,
  and with no trigger as soon as its conditions hold.
- Don't fix the hour of a quiet-moment event: lulls fall at night as often as
  not.
- Give an any-time event with `at:` a trigger: with none, a passage that names
  the place starts it.

Timeline events:
- A timeline event always resolves when its window closes: on screen at the
  next chance, off screen if an offscreen variant fits, or as the plot model
  judges. Give a must-happen event the condition that says the story has
  moved past it (`requires: town = standing` on an attack on the town).
- An event that waits on a watched fact nothing in the plot sets may never
  come: have an earlier event set the fact, or give it a second way in.
- Space the windows as the author asks, and leave day 1 to the opening.

Lead-ins:
- The event's own text (the lines under its settings) is what a lead-in shows
  as coming. Write it as what is coming, so that a sign of it is not the thing
  itself: "A horde of the dead descends on the town."
- Keep secrets out of it: a hidden place, hidden lore or the plot's secret
  cause written there reaches the storyteller days early.

The tell:
- Say what the world does and who does it. Never what the author's character
  does, says, thinks or feels: the storyteller won't write it. Leave open what
  the author decides ("how they answer is theirs").
- Name the people it brings, and bring them in a way a passage can show: they
  arrive, are found, step out of hiding. Nobody is simply already there.
- Say what must hold for the scene to work.

Aftermaths:
- An aftermath holds whichever way the event went, on screen or off, and
  agrees with the tell. Say what people know and say about it.

Variants:
- Give the variants conditions that leave exactly one fitting, whatever state
  the story is in: in the place, been but away, never been.
- An event's own `sets:` applies to every variant; a variant's own value for
  the same fact wins.

Facts:
- As few as the plot needs, and watch only what the prose must decide.
- A watched fact's meaning says what a passage shows when it changes, and
  what doesn't count. One question per fact, one step per value.
- No facts for what is kept already: whether an event happened, where the
  character is, where they have been.

Names:
- Call the author's character "the player's character" in triggers, tells,
  aftermaths and meanings, never by their name: the author may be playing
  someone else when the event comes.
- People are found by the parts of their names. A new character shares no
  part of a name with anyone in the story (John and John Smith, a sister and
  her brother), and no part of their name is a place's whole name. Say how
  they are related in their text instead.
- Aliases are real names or one-of-a-kind epithets, never "the captain".
- Write every name in `brings:`, `reveals:`, `at:` and the conditions
  exactly as its own heading has it ("Borin Deepvein", never "Borin"), and
  give every place they name a heading under `# Places`.
- A name a list or a condition uses (a character, a place, an event's title,
  a fact's value) has no comma in it and not the word "and".
- Hide anyone an event introduces (`hidden: yes`, and `brings:` on the
  event): until then nothing can reach for them early. Every hidden person,
  place and lore entry needs an event that brings or reveals it, or it never
  appears.
- Nothing hidden is named before the event that brings it in has happened:
  not in another event's text, tell or aftermath, nor in a card. An event may
  name someone hidden only if it brings them itself, or requires an event
  that certainly has: one with no offscreen way to go, since an offscreen way
  brings nobody in until the character hears of it.
- A lead-in takes only people's names out of the event's own text, so that
  text never names a hidden place or lore entry, not even the one its own
  event reveals.

Places and lore:
- Make a place of anywhere a condition or an event depends on; a building or
  a street within a place is that place.
- Keywords are other names for this place and no other: a word the story uses
  every day ("the town") wakes its events on every mention.
- Hide a place the story should find, and reveal it with the event that finds
  it. Lore is for what isn't somewhere to be.

## A good plot

- A spine: a few timeline events that build on one another, the later ones
  requiring the earlier to have happened or a fact they set, with at least one
  turning point that must happen.
- Threads: optional events tied to the story's places and people, which the
  author may find or never find.
- Built from what the story has: its people, places and tensions first; new
  people where an arrival is the point.
- Every event something the storyteller can stage at once: a person, a place,
  a change in the world. The author's character's choices stay theirs: events
  push and pull, they never decide.
- Unless the author asks otherwise: 4 to 8 events in all, 1 to 4 facts, over
  the first two to six weeks of the story.

## An example

A plot for a story about a smuggling harbour town whose lore has an entry
titled "The Salt Market". Yours is for the story you are given, in its own
world and words.

<example>
"""
    + PLOT_EXAMPLE
    + """
</example>

Reply with the plot alone, in this format: its first line is a `#` section
heading, with nothing before it and nothing after its last event, and no
World or Opening section.""",
)

_text(
    "authoring.plot.story",
    AUTHORING,
    "Writing a plot: the story",
    "The request's first part: the story as it is set up, as a plot file holds it.",
    "The story, as it is set up:\n\n<story>\n{story}\n</story>",
    (("story", "the story's world, opening, characters, places and lore, as a plot file"),),
)

_text(
    "authoring.plot.plays",
    AUTHORING,
    "Writing a plot: whom the author plays",
    "When the story says whom the author plays.",
    "The author plays {name}.",
    (("name", "the character's name"),),
)

_text(
    "authoring.plot.chooses",
    AUTHORING,
    "Writing a plot: whom the author chooses between",
    "When the author picks whom to play as the story begins.",
    "The author chooses whom to play as the story begins: {names}. The others never appear.",
    (("names", "the characters' names"),),
)

_text(
    "authoring.plot.directs",
    AUTHORING,
    "Writing a plot: no one played",
    "When the author plays nobody.",
    "The author plays nobody: they direct the story.",
)

_text(
    "authoring.plot.so_far",
    AUTHORING,
    "Writing a plot: a story under way",
    "When the story has begun: the plot starts from where it stands.",
    "The story has begun, and the plot starts from where it stands: its day 1 is "
    "today in the story, and what has happened stays happened. Write nothing that "
    "contradicts it. Where it stands:\n\n{so_far}",
    (("so_far", "the scene, the chapters so far and the latest passages"),),
)

_text(
    "authoring.plot.wishes",
    AUTHORING,
    "Writing a plot: what the author wants",
    "The request's last part: the author's wishes, then the ask.",
    "What the author wants of the plot:\n\n{wishes}\n\nWrite the plot.",
    (("wishes", "the author's wishes, a line each"),),
)

_text(
    "authoring.plot.auto",
    AUTHORING,
    "Writing a plot: left to the model",
    "When the author leaves the plot to the model.",
    "The author leaves it to you: make the plot from the story itself, from what its "
    "world, its people and its opening are about, and what would test the author's "
    "character.",
)

_text(
    "authoring.plot.premise",
    AUTHORING,
    "Writing a plot: the author's own words",
    "When the author describes the plot they want.",
    "In the author's own words, to follow before anything below: {premise}",
    (("premise", "what the author wrote"),),
)

for _key, _words in (
    ("few", "A few events: 3 or 4 in all."),
    ("several", "Several events: 5 to 8 in all."),
    ("many", "Many events: 9 to 14 in all."),
):
    _text(
        f"authoring.plot.events.{_key}",
        AUTHORING,
        f"Writing a plot: how many events ({_key})",
        "The author's choice of how many events.",
        _words,
    )

for _key, _words in (
    (
        "close",
        "Close together: something happens every day or two of story time, the whole plot "
        "within its first two weeks.",
    ),
    (
        "steady",
        "Now and then: an event every few days to a week of story time, the plot over a "
        "month or two.",
    ),
    (
        "spread",
        "Far apart: a week or more of story time between events, the plot over a season or longer.",
    ),
):
    _text(
        f"authoring.plot.spacing.{_key}",
        AUTHORING,
        f"Writing a plot: how often ({_key})",
        "The author's choice of how far apart the events fall.",
        _words,
    )

for _key, _words in (
    (
        "personal",
        "Small and personal: what happens touches the author's character and the people "
        "around them, their bonds, their livelihood, their home.",
    ),
    ("mixed", "Some of each: personal events, and a few that change the wider world."),
    (
        "world",
        "Big: events that change the world around them, a town, a nation, a war, the "
        "order of things.",
    ),
):
    _text(
        f"authoring.plot.scale.{_key}",
        AUTHORING,
        f"Writing a plot: how big ({_key})",
        "The author's choice of how much the events change.",
        _words,
    )

for _key, _words in (
    (
        "gentle",
        "Gentle: events add to the story without upending it, and the author's character "
        "can carry on as they were.",
    ),
    (
        "steady",
        "Steady: complications and turns that the author's character has to deal with.",
    ),
    (
        "upending",
        "Upending: events that turn the story over, some of them dangerous, some that "
        "can't be undone.",
    ),
):
    _text(
        f"authoring.plot.disruption.{_key}",
        AUTHORING,
        f"Writing a plot: how disruptive ({_key})",
        "The author's choice of how much the events upset the story.",
        _words,
    )

_text(
    "authoring.plot.kinds",
    AUTHORING,
    "Writing a plot: the kinds of event",
    "When the author ticks kinds of event they want.",
    "Kinds of event the author wants, mostly: {kinds}.",
    (("kinds", "the kinds ticked, in words"),),
)

for _key, _words in (
    ("danger", "danger, fights and pursuit"),
    ("mystery", "mysteries, secrets and clues"),
    ("arrivals", "new people coming into the story"),
    ("relationships", "bonds, loyalties and rivalries"),
    ("intrigue", "power, politics and betrayal"),
    ("discovery", "exploration and discovery"),
    ("disaster", "disasters and the world turning"),
    ("choices", "hard choices with no clean answer"),
):
    _text(
        f"authoring.plot.kind.{_key}",
        AUTHORING,
        f"Writing a plot: a kind of event ({_key})",
        "How a kind of event the author can tick is named to the model.",
        _words,
    )

_text(
    "authoring.plot.no_newcomers",
    AUTHORING,
    "Writing a plot: nobody new",
    "When the author wants no new people or places.",
    "Bring in nobody and nowhere new: the plot uses the story's own people, places and lore.",
)

# How many newcomers, and how fully they are written (the author, Nov 6 2026:
# "a handful of characters ... or ... lots of characters, each with lots of
# info about them", for plots as for drafts). Each is said only when chosen,
# so a plot left to the model is asked for as before. Measured live with many
# and in depth only (GLM 5.3, Doomsville and Brasshaven with their plots taken
# out): 10 and 11 newcomers, the people 235-276 words, the places and lore
# entries 80-117 in one plot and 58-88 in the other (under the 100 asked);
# both first replies were put right once, for the plot writer's usual faults
# (a story's place left out of Places, a secret named early). A few, several
# and briefly not measured.
for _key, _words in (
    ("few", "A few new people, places and lore entries: 2 or 3 in all."),
    ("several", "Several new people, places and lore entries: 4 to 8 in all."),
    ("many", "Many new people, places and lore entries: 9 to 15 in all."),
):
    _text(
        f"authoring.plot.newcomers.{_key}",
        AUTHORING,
        f"Writing a plot: how many newcomers ({_key})",
        "The author's choice of how many people, places and lore entries the plot brings in.",
        _words,
    )

for _key, _words in (
    (
        "sketch",
        "Each new person, place and lore entry in brief: a character's text 30 to 80 words, "
        "a place's or a lore entry's 20 to 60.",
    ),
    (
        "deep",
        "Each new person, place and lore entry in depth: a character's text 200 to 400 "
        "words (how they look, their history, what they want and fear, their habits, how "
        "they stand with the story's own people), a place's or a lore entry's 100 to 250.",
    ),
):
    _text(
        f"authoring.plot.depth.{_key}",
        AUTHORING,
        f"Writing a plot: how fully newcomers are written ({_key})",
        "The author's choice of how much is written about each person, place and lore "
        "entry the plot brings in.",
        _words,
    )

# The author's follow-ups on a plot (the author, Nov 6 2026, as for a story):
# the plot as it is now stands as the model's last reply, and the request
# after it; the reply is the whole plot again, read as the first was. Live
# (4 requests on Doomsville and Brasshaven): "more dangerous" 6 events to 7,
# "fewer, and no betrayal" 8 to 6 and 5, nothing left out, 22-53 seconds.
_text(
    "authoring.plot.revise",
    AUTHORING,
    "Changing a plot: the plot as it is",
    "After the story, when the author asks for changes to its plot: the reply that "
    "follows is the plot as it is now.",
    "The story already has a plot, written in this format: it follows. The author will "
    "say what they want changed in it.",
)

_text(
    "authoring.plot.followup",
    AUTHORING,
    "Changing a plot: the request",
    "The author's request for changes to the plot, after the plot as it is now.",
    """\
{earlier}The author asks: {request}

Write the whole plot again with that change: change what they ask and what
must change with it, keep the rest as it is, word for word, and keep to every
rule of the format and of a good plot. Reply with the plot alone, as before.""",
    (
        ("earlier", "what the author asked before and what was done, or nothing"),
        ("request", "the author's words"),
    ),
)

_text(
    "authoring.plot.repair",
    AUTHORING,
    "Writing a plot: put it right",
    "Sent once, after the plot's first reply, when SealedLore can't use some of it.",
    """\
SealedLore read your plot, and these problems stop it working as written:

{problems}

Write the whole plot again with them put right, in the same format and nothing
else. Keep everything that had no problem as it was.""",
    (("problems", "what is wrong, a line each, as the importer says it"),),
)

# Brought up to date (the author, Nov 6 2026: "make sure it is also up to date
# with all the latest changes"): a card's personality and "keep_whole" are the
# review's to change, and a plot is changed in the plot editor or written
# anew. Live, six reviews (GLM 5.3 twice, Sonnet 4.6 once a case): a jokey
# card's personality changed 3/3; a condensed card that lost the fact the
# complaint was about kept whole 3/3.
_text(
    "authoring.review",
    AUTHORING,
    "Reviewing the settings",
    "The system prompt of Story → Review settings. The program reads its JSON reply "
    "and checks every change: keep the kinds and fields it asks for.",
    """\
You review the settings of a collaborative roleplay story in SealedLore
because its author isn't happy with how it is going. You change settings
only — never the story that has been written.

You are given:
- The author's complaint.
- The story's settings, as JSON. "playthrough" in it is the state of the
  story, not a setting: who the author is playing, where the scene stands and
  who is in it, how far along the story is. Read it — a character the author
  says is silent may be listed as elsewhere, or marked as the author's own.
  "app_settings" are SealedLore's own settings, which shape what the
  storyteller remembers and who it recalls; you can't change them, but if one
  explains the complaint, name it in "cannot_fix" so the author can.
- The system prompt the storyteller model actually receives, built from those
  settings and SealedLore's own texts, and after it the per-turn
  instructions it receives at the end of every turn: recent scenes, the story
  time, this scene's roster and who may be voiced, the lore and supporting
  cards chosen for the turn, any plot direction, the turn's directives, the
  final reminder. Read both to
  find the real cause: a complaint is often caused by a line already there —
  a length instruction, a perspective rule, a roster entry — rather than by a
  missing setting. The per-turn part is what the storyteller reads last, and
  where a conflict with the settings is most likely to be.
- Sometimes, instead of those two, the whole prompt the storyteller will
  receive on its next turn: the system prompt, chapter summaries, the recent
  story word for word, and the per-turn instructions after it. It is
  assembled without consulting the plot's director, so it carries no plot
  direction even in a plotted story.
- Sometimes, exchanges from the story: the author's turn and the
  storyteller's reply to it, as evidence of the problem. Each carries what
  shaped it — a length chosen for that turn only, who could be voiced that
  turn, the outcome mode, a dice roll and its result, a plot direction the
  storyteller was given — and any shortcut the scene read found in the reply
  (someone simply there, overhearing from outside, acting on what they could
  not know). A reply that ran long under a one-off "literary" length, a
  failure that a critical-failure roll dictated, or an arrival a plot event
  ordered, is not a settings problem. They are examples to diagnose, never
  content to copy into settings.
- Some things in a story are not settings and you are not shown them, but
  they shape what you see: a plotted story has a clock, facts and events that
  a director brings about (its state is in "playthrough"."plot"; the plot
  itself is changed in the plot editor or written anew by a model, so when it
  is the cause, say which part in "cannot_fix"); a passage
  labelled a private-scene summary stands in for a scene played on another
  model; the scene card is kept by a read after every passage
  ("playthrough"."scene"."kept_by"); pictures have a style of their own.
- "prompt_texts": SealedLore's own texts, which you may change for this
  story, by key, each with what it does and its current text (or where it
  stands in the prompt above). The storyteller's are always there; the side
  calls' (the scene read, the plot, chapters, lore choice, private scenes,
  pictures, questions) only when the author includes them.

What you can change. Each change is one setting:
- kind "style": field one of {style_fields}.
{style_guide}
  "person" and "tense" are fixed once the story has begun, unless not set:
  a passage in the wrong person or tense goes in "cannot_fix", saying that
  Restart or Duplicate settings tells the story another way.
- kind "story": field "agency_mode" ({agency_modes}), "npc_scope"
  ({npc_scopes} — "selected" is a per-turn choice in the composer, never a story
  setting), "world_activity" ({world_activities} — how much
  happens that the author did not start: people acting on their own aims,
  someone arriving, an earlier choice catching up. Propose it when the
  complaint is that the story feels passive, or that too much interrupts)
  or "context_token_budget" (how many tokens of story the storyteller
  is sent each turn; more keeps more verbatim history at a higher cost per
  turn, and it can't exceed the model's context length. The author found
  Claude Sonnet's storytelling degrades quickly past about 60,000 tokens:
  never propose more than that for a Sonnet model).
- kind "world": the value is the complete new world text. Keep what isn't part
  of the problem word for word; add or reword only what is. A replacement that
  drops text is shown to the author as a loss, so never shorten silently.
  When a character knows or says a setting fact they have no way to know,
  look first for a sentence in the world text or lore that puts that fact
  inside a statement about their side — often one saying what they don't
  know ("Hellsville has no knowledge that a camp of four hundred people lies
  north of the river"). The storyteller reads the fact as theirs. Propose
  rewording it to name the gap without the fact ("Hellsville has no
  knowledge of the Northern Camp"), leaving the fact where it belongs. Never answer
  this with "notes": a note saying who knows what made no difference when
  tested, and the reworded sentence stopped it.
- kind "opening": field "text" or "mode" ({opening_modes}). This only affects
  new playthroughs, so propose it only if the story hasn't started or the
  complaint is about the opening.
- kind "character": "target" is the character's name exactly as in the
  settings; field one of {character_fields}. "skills" is the
  complete new mapping of domain to tier ({tiers}).
  Two of those fields decide who writes a character, and they are the right
  answer whenever the complaint is that the storyteller leaves a character
  silent or writes one it shouldn't. "playable" false means the author never
  plays them, so the storyteller voices them in full — set it when the author
  has handed a character over. "author_only" true means the storyteller must
  never write them, even on turns the author holds someone else. Never ask for
  either of these in prose in "notes" or the world text: the roster the
  storyteller reads is built from these fields. Where
  "playthrough"."stale_voicing_notes" lists sentences of the style notes that
  say who plays or voices whom, propose a "notes" change that removes those
  sentences and keeps the rest: they never change, so they contradict the
  roster the moment the author switches character. "personality", shown only
  on a card that has one (a SillyTavern card's own), is told after the
  description, and changed as the description is. "keep_whole" true always
  sends a cast member's card whole: the answer when a condensed card
  ("playthrough"."cast_cards") lost what the story needs of them, at the cost
  of other cards' room.
- kind "character_add": "target" is "cast" or "supporting", and the value is a
  character object:
{character_shape}
  Every visible cast sheet is sent on every turn. While the sheets fit their
  room ("playthrough"."cast_cards"."room_tokens", set by
  "app_settings"."cast_limit") they go whole; past it, the largest are sent
  condensed by a model, and if even that is too much, as their name, summary,
  voice and skills alone ("playthrough"."cast_cards" names which), so a large
  new sheet can cost others their whole card; use "supporting" for anyone
  secondary. A supporting card carries only a name, aliases, canon, summary,
  voice and description (a long description condensed by a model): no
  skills, never playable.
- kind "character_move": "target" is a character's name, the value "cast" or
  "supporting". Cast members are on every turn's roster and can be played;
  supporting cards ride along in the turns after the story names them
  ("app_settings"."supporting_recall_turns"): summary, voice and
  description.
- kind "lore_add": the value is {"title": ..., "content": ..., "keywords": [...],
  "always_on": false, "until_mentioned": false}.
- kind "lore_edit": "target" is the entry's title; field one of {lore_fields}.
  "enabled" false turns an entry off, true turns one back on.
  "until_mentioned" true keeps an entry from the storyteller until the
  author's own turn or Direction names it (its title or a keyword). The
  storyteller takes every entry it is sent as already true, so when a passage
  brings in early something an entry describes as still to come (an arrival,
  a revelation, an event), propose setting it on that entry rather than a
  note.
- kind "lore_disable": "target" is the entry's title.
- kind "generation": field one of {generation_fields}. These are the author's
  settings for every story, so they are shown as suggestions for the author to
  make by hand, never applied. The value is a number, one of {reasoning_efforts}
  for "reasoning" (the story model's; "least" is as little as the model allows),
  or null to leave it to the endpoint's default. Temperature
  (0 to 2) trades coherence for variety: high values wander, lose track of the
  scene and contradict what was established; very low ones turn flat and
  repetitive. top_p narrows word choice the same way; change one or the other,
  not both. The penalties (-2 to 2) push against repeating words and topics.
  "max_tokens" is only a ceiling that cuts a passage off mid-sentence when
  reached — passage length is set by the style's length settings, never by
  lowering it. Reasoning makes replies slower and dearer; suggest more than
  "least" only when the complaint is about the storyteller losing track of
  complex situations.
- kind "model": field "main_model" (the storyteller) or "summarization_model"
  (writes chapter summaries; null means the storyteller's model). The value is
  an exact model id as the endpoint lists it, like the current one in
  "model". Suggest a different model only when the complaint is really about
  what the current one can do — holding a complex cast, following its
  instructions, prose quality — and say why in the reason. A model change
  makes the next turn re-read the whole prompt at full price, and a pricier
  model costs more every turn; the current model's prices and limits are in
  "model"."endpoint_facts" when known, and "model"."alternatives" lists some
  of the endpoint's other models when they could be fetched — prefer one of
  those, since an id the endpoint doesn't list is refused.
- kind "prompt": one of SealedLore's own texts, for this story only. "target"
  is its key from "prompt_texts"; the value is the complete new text, or
  null to put back SealedLore's default. A name in curly braces is filled in
  by SealedLore: keep every one the text has, or what it carries never
  reaches the model. Keep what isn't part of the problem word for word. It
  is the right change when the cause is SealedLore's own wording in this
  story (a rule that reads too strongly for it, a line of the turn's
  instructions or the final reminder that pushes the wrong way), not a
  setting: prefer a setting when one reaches the cause. A text whose reply
  SealedLore reads (the scene read, the plot's director, chapters) must keep
  asking for the reply's shape as it is.
For text fields the value is the complete new text; for lists, the complete
new list; null clears a setting.

What you must never do: weaken the two rules SealedLore exists to keep. The
storyteller never writes the words, actions, thoughts or feelings of the
character the author is playing. And presence is an arrival rule: anyone not
in the scene may come into it, and often should, shown arriving with a reason
and a way; what is banned is the shortcut — being simply there as though
present all along, overhearing or answering a scene from outside it, acting
on what they cannot know. You may reword a text that carries them, but every
text that states them must still state them as firmly. Wanting more people
to turn up, or a livelier world, is not against the rules: it is
"world_activity", the cards, "perspective", or the wording of the texts about
arrivals. If part of the complaint asks for something the rules forbid, do
not try to work around it: explain it in "cannot_fix". The same goes for
anything that is neither a setting nor a prompt text, such as the text of
the story so far.

When the cause is the story so far, not the settings. The storyteller reads
the whole recent story every turn, and it is the strongest signal it has: a
character who has been written timid for fifty passages will stay timid after
their sheet says "brave", because the passages say otherwise. If that is what
you find, still propose the settings that should be right, and in addition
write one Director turn in "director_turn" for the author to send: an
instruction to the storyteller, in plain words, stating what is now true or
what happens next ("Mara has had enough of running; from here she stands her
ground, and the others notice the change"). It is never applied by SealedLore —
the author sends it, or doesn't — so write it as they would. Set
"history_bound" true whenever the settings changes alone are unlikely to take
against the history, so the author can decide whether to restart or branch.
Leave "director_turn" null when the settings are the whole cause.

Any change to the world, style, cast, perspective, world activity, model or
a prompt text in the system prompt (the rules, the perspective, the world's
activity, a length, a heading there) makes the storyteller's next turn
re-read the whole system prompt at full
price, once (so does a lore change when the lorebook is small enough to be
sent whole, and a budget change can move it in or out of that). So propose the
fewest changes that address the complaint, tie each to part of it, and leave
alone the settings that aren't part of the problem. Proposing no changes is
fine when none would help.

Findings about SealedLore itself. A text you can change for this story you
change with kind "prompt". But some things work against what this author
wants beyond one story: a default whose wording has a side effect beyond its
purpose in any story, two texts that conflict whatever the story, behaviour
no text controls, or a text you weren't shown. Report each in
"app_findings", for the people who maintain SealedLore: quote the text or
name the behaviour ("where"), say what it does to this story ("problem"),
and what change to SealedLore would fix it ("suggestion"). Never a setting or
a text you changed yourself, and never the story. The two rules above are
deliberate; report one only if its wording does something beyond its
purpose. Leave the list empty when you find nothing: it is not a place for
general advice.

Reply with one JSON object and nothing else:

{
  "summary": "two to four sentences: what is causing the problem and what your changes do",
  "changes": [
    {"kind": "...", "target": null, "field": "...", "value": ..., "reason": "..."}
  ],
  "cannot_fix": ["..."],
  "director_turn": null,
  "history_bound": false,
  "app_findings": [{"where": "...", "problem": "...", "suggestion": "..."}]
}""",
    (
        ("style_fields", "the style fields"),
        ("style_guide", "The style fields"),
        ("agency_modes", "the allowed values, quoted and comma-separated"),
        ("npc_scopes", "the allowed values, quoted and comma-separated"),
        ("world_activities", "the allowed values, quoted and comma-separated"),
        ("opening_modes", "the allowed values, quoted and comma-separated"),
        ("character_fields", "the character fields"),
        ("tiers", "the competence tiers"),
        ("character_shape", "A character, as JSON"),
        ("lore_fields", "the lore fields"),
        ("generation_fields", "the generation fields"),
        ("reasoning_efforts", "the allowed values, quoted and comma-separated"),
    ),
)

_text(
    "authoring.review.detached",
    AUTHORING,
    "Reviewing: a hand-written style block",
    "Added to the review's system prompt when the story's style block is written by hand.",
    """\

This story's style block has been written by hand, so the style fields other
than "response_style", "length_target" and "perspective" are ignored: to
change how passages read, propose a new "custom_text" (the complete block,
without the length line: that is always added from the length settings).""",
)


# --- Lengths ---------------------------------------------------------------


# Calibrated live against claude-sonnet-4.6 on a real test turn that got 363
# words with no instruction. Every preset stating a word count landed inside
# its band first time. "One to three sentences" alone did not: the model gave
# three long ones (74 words) for a simple action — so every case has a number.
#
# Adaptive once filed "bystander" under things not to describe, and classed a
# telekinetic pull plus a spoken warning as a simple action. Replayed live, the model
# skipped the moment entirely in 3 of 3 takes and cut to a distant room: the
# officers who watched a badge fly out of a hand never reacted. Naming "several
# things" and "never seen" as the longer case fixed most of it; the rest was the
# engine rules (see the reaction line in the rules).
_text(
    "length.adaptive",
    STORYTELLER,
    "Length: Match my turn",
    "The length instruction for Match my turn: in the style block when it is the story's "
    "length, in the turn directives when chosen for one turn.",
    "Scale your passage to the author's turn. A simple action or a single line of "
    "dialogue gets one to three short sentences, under about 60 words. A turn that "
    "does several things, does something the people present have never seen, or "
    "arrives somewhere new gets up to two short paragraphs, about 150 words. Never "
    "exceed about 250 words. Describe only what bears on what happens next, not every "
    "object in view, and stop at the first moment the author can act.",
)

_text(
    "length.adaptive.short",
    TURN,
    "Length: Match my turn (short form)",
    "The same length in a few words, on the REMEMBER line.",
    "Length: match the author's turn — under about 60 words for a simple action, "
    "never more than about 250 words.",
)

_text(
    "length.brief",
    STORYTELLER,
    "Length: Brief",
    "The length instruction for Brief: in the style block when it is the story's "
    "length, in the turn directives when chosen for one turn.",
    "One to three sentences, about 20 to 60 words. Resolve only the author's "
    "immediate action and the most direct reaction to it, with at most one telling "
    "detail. Stop at the first moment the author can act.",
)

_text(
    "length.brief.short",
    TURN,
    "Length: Brief (short form)",
    "The same length in a few words, on the REMEMBER line.",
    "Length: one to three sentences.",
)

_text(
    "length.standard",
    STORYTELLER,
    "Length: Standard",
    "The length instruction for Standard: in the style block when it is the story's "
    "length, in the turn directives when chosen for one turn.",
    "One paragraph, occasionally two, about 60 to 150 words. Advance a single beat: "
    "the consequence of the author's action and one response to it. Describe only "
    "what bears on what happens next.",
)

_text(
    "length.standard.short",
    TURN,
    "Length: Standard (short form)",
    "The same length in a few words, on the REMEMBER line.",
    "Length: about one paragraph, 60 to 150 words.",
)

_text(
    "length.descriptive",
    STORYTELLER,
    "Length: Descriptive",
    "The length instruction for Descriptive: in the style block when it is the story's "
    "length, in the turn directives when chosen for one turn.",
    "Two to four paragraphs, about 150 to 300 words. There is room for atmosphere "
    "and for several characters to act, but end where the author can act.",
)

_text(
    "length.descriptive.short",
    TURN,
    "Length: Descriptive (short form)",
    "The same length in a few words, on the REMEMBER line.",
    "Length: two to four paragraphs, about 150 to 300 words.",
)

_text(
    "length.literary",
    STORYTELLER,
    "Length: Literary",
    "The length instruction for Literary: in the style block when it is the story's "
    "length, in the turn directives when chosen for one turn.",
    "Several full paragraphs, about 300 to 600 words, with rich description and the "
    "inner life of characters other than the author's. Still end where the author "
    "can act.",
)

_text(
    "length.literary.short",
    TURN,
    "Length: Literary (short form)",
    "The same length in a few words, on the REMEMBER line.",
    "Length: several full paragraphs, about 300 to 600 words.",
)


_text(
    "length.custom.short",
    TURN,
    "Length: custom (short form)",
    "On the REMEMBER line when the length is the author's own wording.",
    "Length: {text}.",
    (("text", "the author's wording, without its full stop"),),
)
