---
title: Doomsville (with a map)
description: Four years after the dead rose, a lone survivor crosses a frozen Canadian winter, on a map of the country around Hellsville.
start: day 1, 15:30
agency: contested
world activity: normal
scarce: yes
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
- picture: pictures/john.jpg | portrait

A 30-year-old male survivor, ex-military, still very capable physically (strength
and hand-to-hand combat in particular). Stoic on the outside, dead on the inside.

## Jane
- role: player
- choose: start
- picture: pictures/jane.jpg | portrait

A 30-year-old female survivor, ex-military, still very capable physically
(endurance and speed; strong for her build). Stoic on the outside, dead on the
inside.

## Ben
- role: player
- hidden: yes
- picture: pictures/ben.jpg | portrait

A 50-year-old reclusive hermit who lives alone in the Hidden Caves. Bitter,
angry and resentful, but also highly resourceful and well supplied. Once met,
the author may take him up as a second character.

## Evan Stevens
- role: storyteller
- hidden: yes
- picture: pictures/evan.jpg | portrait

A 22-year-old bandit whose only interests are having the fanciest guns he can get
and how many people he can shoot with them.

## Marcus Webb
- role: storyteller
- hidden: yes
- picture: pictures/marcus.jpg | portrait

28, a former engineer, now the leader of Hellsville.

## Elena Ruiz
- role: storyteller
- hidden: yes
- picture: pictures/elena.jpg | portrait

25, a former doctor in Hellsville, still doing her best to treat the injured and
keep her community going.

# Places

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

# Lore

## The farmhouse
- keywords: farmhouse, the farm
- picture: pictures/farmhouse.jpg | from the road

The farmhouse where the story begins, five miles south of Hellsville: two storeys
of white paint gone to grey, one shutter hanging, snow drifted to the windowsills
and a sagging fence around the yard. From the road it looks abandoned.

# Map
- start: The farm

## The farm
- kind: site
- at: 0, 0
- size: 0.24
- way: The north road | north

An isolated farm in deep snow, five miles south of Hellsville: a house, a barn
and a frozen pond behind a sagging wooden fence.

## The farmhouse
- kind: building
- size: 0.025
- in: The farm
- at: -0.005, 0.022

Two storeys of white paint gone to grey, one shutter hanging. The porch is on
the north side, facing the road. Someone still lives here.

## The barn
- kind: building
- size: 0.04
- in: The farm
- at: 0.050, -0.012
- thing: Pickup truck | rusted, flat tyres, no fuel

Newer than the house, its roof still holding.

## The frozen pond
- kind: area
- in: The farm
- at: -0.066, -0.042
- size: 0.06

## The north road
- kind: route
- at: 0, 4
- known: yes
- way: The farm | south
- way: Hellsville | north, five miles in all

A grey crease between white fields, running five miles north from the farm to
Hellsville. Snow drifts across it where the fences have come down.

## The creek crossing
- kind: site
- at: 0.15, 2.6
- size: 0.12
- way: The north road | west, beside it

A low bridge over a frozen creek, half buried. Good cover; a bad place to be
caught.

## Hellsville
- kind: settlement
- at: 0, 8
- size: 0.9
- known: yes
- way: The north road | south
- way: The east road | east

A small walled town, lit at night: a water tower and a church steeple show
over its barricades from miles off.

## The south gate
- kind: site
- in: Hellsville
- at: -0.007, -0.223
- size: 0.03

A barricade of cars, tyres and scrap across the south road, with a gate the
watch opens for anyone they believe.

## Main street
- kind: route
- in: Hellsville
- at: -0.007, -0.054
- size: 0.03

## The diner
- kind: building
- in: Hellsville
- at: -0.059, -0.158

The old diner, now the town's meeting hall, lit by mismatched lanterns. Marcus
Webb holds court at the back table.

## The clinic
- kind: building
- in: Hellsville
- at: 0.043, 0.089

Elena Ruiz's clinic, in what was the hardware store.

## The church
- kind: building
- in: Hellsville
- at: -0.194, 0.157

Its weathered steeple is the tallest thing in town after the water tower.

## The water tower
- kind: site
- in: Hellsville
- at: 0.205, 0.176
- size: 0.05

## The general store
- kind: building
- in: Hellsville
- at: 0.043, -0.035
- thing: The town's stores | rationed, counted every evening

## The north gate
- kind: site
- in: Hellsville
- at: -0.007, 0.238
- size: 0.03

## The east road
- kind: route
- at: 2.2, 8.2
- way: Hellsville | west
- way: The hills | east

The road east out of Hellsville, toward the hills and whatever lies past them.

## The hills
- kind: area
- at: 4.8, 8.6
- size: 0.8
- way: Hellsville | west, about three miles
- way: The Hidden Caves | east, up the slope

Wooded limestone hills east of town. People from Hellsville don't go up there
much any more.

## The Hidden Caves
- kind: site
- at: 5.2, 9.1
- size: 0.2
- way: The hills | west, down the slope

A rocky hillside among the pines, with a narrow opening into the limestone that
Ben has made his home.

## The cave mouth
- kind: area
- in: The Hidden Caves
- at: -0.040, -0.046
- size: 0.02

A narrow opening in a rocky hillside among pines.

## The main chamber
- kind: room
- in: The Hidden Caves
- at: 0.025, 0.031
- size: 0.03
- thing: Ben's supplies | stacked, well kept

## The old highway
- kind: route
- at: -2, 5
- way: The north road | east

A four-lane highway west of the farm country, choked with dead cars. Bandits
work it.

## The gas station
- kind: site
- in: The old highway
- at: 0.1, -0.5
- size: 0.24

A burnt-out gas station beside the highway, picked over long ago.

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
