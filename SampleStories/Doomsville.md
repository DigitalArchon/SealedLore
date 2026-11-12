---
title: Doomsville
description: Four years after the dead rose, a lone survivor crosses a frozen Canadian winter.
start: day 1, 15:30
agency: contested
world activity: normal
picture style: gritty and realistic
background: pictures/doomsville-background.jpg
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
- picture: pictures/john.jpg | portrait | look: winter coat | expression: neutral
- picture: pictures/john-happy.jpg | look: winter coat | expression: happy
- picture: pictures/john-sad.jpg | look: winter coat | expression: sad
- picture: pictures/john-angry.jpg | look: winter coat | expression: angry
- picture: pictures/john-afraid.jpg | look: winter coat | expression: afraid
- picture: pictures/john-armour.jpg | look: combat armour | expression: neutral
- picture: pictures/john-indoors.jpg | look: indoor clothes | expression: neutral

A 30-year-old male survivor, ex-military, still very capable physically (strength
and hand-to-hand combat in particular). Stoic on the outside, dead on the inside.

## Jane
- role: player
- choose: start
- picture: pictures/jane.jpg | portrait | look: winter coat | expression: neutral
- picture: pictures/jane-happy.jpg | look: winter coat | expression: happy
- picture: pictures/jane-sad.jpg | look: winter coat | expression: sad
- picture: pictures/jane-angry.jpg | look: winter coat | expression: angry
- picture: pictures/jane-afraid.jpg | look: winter coat | expression: afraid
- picture: pictures/jane-armour.jpg | look: combat armour | expression: neutral
- picture: pictures/jane-indoors.jpg | look: indoor clothes | expression: neutral

A 30-year-old female survivor, ex-military, still very capable physically
(endurance and speed; strong for her build). Stoic on the outside, dead on the
inside.

## Ben
- role: player
- hidden: yes
- picture: pictures/ben.jpg | portrait | look: fur coat | expression: grim
- picture: pictures/ben-happy.jpg | look: fur coat | expression: happy

A 50-year-old reclusive hermit who lives alone in the Hidden Caves. Bitter,
angry and resentful, but also highly resourceful and well supplied. Once met,
the author may take him up as a second character.

## Evan Stevens
- role: storyteller
- hidden: yes
- picture: pictures/evan.jpg | portrait | look: winter coat | expression: grinning
- picture: pictures/evan-angry.jpg | look: winter coat | expression: angry

A 22-year-old bandit whose only interests are having the fanciest guns he can get
and how many people he can shoot with them.

## Marcus Webb
- role: storyteller
- hidden: yes
- picture: pictures/marcus.jpg | portrait | look: winter coat | expression: neutral
- picture: pictures/marcus-angry.jpg | look: winter coat | expression: angry
- picture: pictures/marcus-happy.jpg | look: winter coat | expression: happy

28, a former engineer, now the leader of Hellsville.

## Elena Ruiz
- role: storyteller
- hidden: yes
- picture: pictures/elena.jpg | portrait | look: work clothes | expression: neutral
- picture: pictures/elena-happy.jpg | look: work clothes | expression: happy
- picture: pictures/elena-sad.jpg | look: work clothes | expression: sad

25, a former doctor in Hellsville, still doing her best to treat the injured and
keep her community going.

# Places

## The farmhouse
- keywords: farmhouse, the farm
- picture: pictures/farmhouse.jpg | from the road

The farmhouse where the story begins, five miles south of Hellsville: two storeys
of white paint gone to grey, one shutter hanging, snow drifted to the windowsills
and a sagging fence around the yard. From the road it looks abandoned.

## Hellsville
- keywords: the town
- picture: pictures/hellsville.jpg | main street

A small town five miles north of the farmhouse where the story begins. It is well
known in the region: most people the player's character meets know it exists.
Whether they say so, or tell them where it is, is another matter.

## The Hidden Caves
- keywords: caves, the caves
- hidden: yes
- picture: pictures/hidden-caves.jpg | the cave mouth

A series of caves in the hills near Hellsville. There is not much there.

# Facts

- hellsville: standing (standing, under attack, saved, fallen) [watch] — the state of the town. Saved only if an attack on it is beaten off; fallen once the dead overrun it
- elena: in hellsville (in hellsville, fleeing east, with the player, dead) — where Elena Ruiz is and what became of her
- has_companions: no [watch] — yes once another living person (not an animal) has joined the player's character to travel on with them or to live alongside them, as a party or a place in a community; meeting someone, talking with them, sharing a meal or being shown the way is not enough; no if they are alone again

# Timeline

## The fall of Hellsville
- when: day 30–50
- lead-in: yes
- requires: hellsville = standing
- must happen: yes
- aftermath: A horde of the dead came down on Hellsville. If the town fell, it is burned out, the dead still walk its streets, and Elena Ruiz got out and went east; if it was held, the scars of the fight are everywhere and its people are burying their dead. Within a few weeks most people in the region have heard what happened, and anyone asked about the town will say so.

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

## Smoke on the road
- when: day 1–5
- at: The farmhouse
- pacing: quiet moment
- trigger: The player's character is at the farmhouse with a fire lit or a light showing, and nothing else is going on.
- tell: Someone on the road north has seen the smoke: a woman pulling a sled of salvage, on her way to Hellsville to trade. She stops at the fence and calls out rather than come any closer. She wants a night out of the wind and will pay for it with news or a tin of something, and she will go on up the road if she is turned away. Asked, she says what she knows of the town: it takes in people who work, and it is run by one man. Whether she is let in, sent on or robbed is the player's character's to decide.

## The dead at the door
- when: day 2–12
- at: The farmhouse
- pacing: any time
- trigger: It is night at the farmhouse, and the player's character is indoors, resting or asleep.
- tell: Two of the dead, drawn by the light or the smell of smoke, find the farmhouse in the dark. The first the player's character knows of it is a weight against the door or a face at a window. They can be fought, waited out or slipped past, and the noise of any of it carries.

## First bandit meeting
- when: day 2–60
- pacing: any time
- brings: Evan Stevens
- trigger: The player's character is travelling the roads or scavenging away from shelter, alone or nearly so.
- tell: A small band of bandits, three or four of them, confronts the player's character on the road. Evan Stevens, the youngest, is the one with the fanciest rifle and the eagerness to use it. What they want, and how it goes, depends on how the player's character handles them.

### Away from Hellsville
- requires: at != Hellsville

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
