---
title: Doomsville
description: Four years after the dead rose, a lone survivor crosses a frozen Canadian winter.
start: day 1, 15:30
agency: contested
world activity: normal
---

# World

The world of Doomsville takes place four years after a zombie uprising. 99.9% of
the world's population died in a matter of days when the virus struck. Those who
weren't killed are naturally immune to the airborne pathogen. However, even they
can be turned by a zombie's bite, in just a matter of days. Worse, anyone who
dies of any cause will rise as a zombie unless their brain is destroyed.

There are no governments left, no military, essentially no society at all. There
may be only some tens of thousands of living humans in all of North America. Some
are in small communities trying to get by. Others are violent and cruel bandits.
Others are roving nomads. Many who still live barely seem human themselves, from
the toll the last four years has taken on them.

# Opening
- location: A seemingly abandoned farmhouse, five miles south of Hellsville
- time: Late afternoon, deep winter
- situation: Freezing wind, failing light. The farmhouse looks empty.

The player's character approaches a seemingly abandoned farmhouse in the freezing
Canadian winter.

# Characters

## John
- role: player
- choose: start

A 30-year-old male survivor, ex-military, still very capable physically (strength
and hand-to-hand combat in particular). Stoic on the outside, dead on the inside.

## Jane
- role: player
- choose: start

A 30-year-old female survivor, ex-military, still very capable physically
(endurance and speed; strong for her build). Stoic on the outside, dead on the
inside.

## Ben
- role: player
- hidden: yes

A 50-year-old reclusive hermit who lives alone in the Hidden Caves. Bitter,
angry and resentful, but also highly resourceful and well supplied. Once met,
the author may take him up as a second character.

## Evan Stevens
- role: storyteller
- hidden: yes

A 22-year-old bandit whose only interests are having the fanciest guns he can get
and how many people he can shoot with them.

## Marcus Webb
- role: storyteller
- hidden: yes

28, a former engineer, now the leader of Hellsville.

## Elena Ruiz
- role: storyteller
- hidden: yes

25, a former doctor in Hellsville, still doing her best to treat the injured and
keep her community going.

# Places

## Hellsville
- keywords: the town

A small town five miles north of the farmhouse where the story begins. It is well
known in the region: most people the player's character meets know it exists.
Whether they say so, or tell them where it is, is another matter.

## The Hidden Caves
- keywords: caves, the caves
- hidden: yes

A series of caves in the hills near Hellsville. There is not much there.

# Facts

- hellsville: standing (standing, under attack, saved, fallen) [watch] — the state of the town. Saved only if an attack on it is beaten off; fallen once the dead overrun it
- elena: in hellsville (in hellsville, fleeing east, with the player, dead) — where Elena Ruiz is and what became of her
- has_companions: no [watch] — yes while at least one other living person (not an animal) travels or lives with the player's character, as a party or a place in a community; no if they are alone again

# Timeline

## The fall of Hellsville
- when: day 30–50
- lead-in: yes
- requires: hellsville = standing
- must happen: yes
- aftermath: Hellsville was overrun by the dead. The town is burned out and the dead still walk its streets. Elena Ruiz got out and went east. Within a few weeks most people in the region have heard, and anyone asked about the town will say so.

A horde of the dead descends on Hellsville.

### The player's character is in Hellsville
- requires: at = Hellsville
- at: Hellsville
- brings: Marcus Webb, Elena Ruiz
- tell: The attack comes while they are in the town. The horde breaks through the outer defences; Marcus Webb tries to organise a defence and Elena Ruiz tends the wounded. How the player's character reacts is theirs to decide — the town can be held if they and the townspeople fight well, or it can fall.
- sets: hellsville = under attack

### The player's character has been, but is away
- requires: visited Hellsville, at != Hellsville
- brings: Elena Ruiz
- tell: Elena Ruiz, fleeing east from Hellsville, crosses the player's character's path, frightened and exhausted. She tells them the town is under attack. If they go back at once they can still help hold it; if they don't, it falls.
- sets: hellsville = under attack, elena = fleeing east

### The player's character has never been
- requires: not visited Hellsville
- offscreen: yes
- sets: hellsville = fallen, elena = fleeing east

## Alone too long
- when: day 100–110
- pacing: any time
- requires: has_companions = no
- trigger: The player's character goes to sleep.
- tell: While they sleep alone, one of the dead finds them. They do not wake in time. Write the attack and its outcome from the world's side: the character dies, and this is the end of the story.

# Events

## First bandit meeting
- when: day 2–60
- pacing: any time
- brings: Evan Stevens
- trigger: The player's character is travelling the roads or scavenging away from shelter, alone or nearly so.
- tell: A small band of bandits, three or four of them, confronts the player's character on the road. Evan Stevens, the youngest, is the one with the fanciest rifle and the eagerness to use it. What they want, and how it goes, depends on how the player's character handles them.

## Welcome to Hellsville
- at: Hellsville
- pacing: any time
- requires: hellsville = standing
- brings: Marcus Webb, Elena Ruiz
- trigger: The player's character arrives at Hellsville, or is in Hellsville and hasn't yet met the man who runs it.
- tell: Marcus Webb, who runs the town, comes out to greet them. While he talks, an armed man working with him circles behind the player's character with a gun, in case they try anything. The player's character notices him only if the author has them explicitly scan the area, keep watch, or otherwise act with particular caution; otherwise do not mention him until he makes himself known. Elena Ruiz, the town's doctor, is somewhere about.

## The caves
- pacing: any time
- requires: visited Hellsville
- reveals: The Hidden Caves
- trigger: The player's character actively searches or explores the country around Hellsville, or presses someone with pointed questions about the area.
- tell: They find, or are told about, a series of caves in the hills near Hellsville.

## Ben in the caves
- at: The Hidden Caves
- pacing: any time
- requires: the caves happened, at = The Hidden Caves
- brings: Ben
- trigger: The player's character is in the Hidden Caves and chooses to explore them rather than leave.
- tell: Deep in the caves they come upon Ben: a bitter, angry hermit, well supplied and hostile to intruders.
