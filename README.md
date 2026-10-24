# SealedLore

A Linux and Windows application for AI-driven roleplay with many characters
in an open world, with a number of particularly useful features.

![The start page: six sample stories, each with its picture and a line on what it is, and below them the ways to start a story of your own](docs/screenshots/start-page.webp)

*Beta.* This application has been tested extensively both through
automation and manually by the developer. It is however still in early
public testing, so expect bugs, and there are no guarantees that everything
works as intended. Bug reports and pull requests are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md). Free software under the AGPL (see
[Licence](#licence)).

### Beta Features Status

Simple Chat is complete and should be a bug free experience. While simple by
design, it offers features useful to basic roleplay chat. Auto summarization,
branching, easy creation of pictures and videos, swapping mid chat to private 
AI models with TEE and E2EE models with Tinfoil, and in memory only chat modes.
All those are working properly and I don't expect many changes to this mode.

Starting a full story from scratch or from a premise is also fully function
and works well. Most updates to this mode will improve improved prompting,
tweaks to features to get better responses and so on. Nothing is broken,
but improvements will come regularly.

Stories with a human authored plot. This also works quite well at the moment,
most improvements here will be related to better integrating the plots into
the story. Making it more natural, more consistent, etc. As above, nothing
is broken, but there are definitely improvements to be made here.

Stories on a map are withdrawn for now. The map of the earlier 2.0 betas is
being redesigned as a mode of its own, with rules of its own; a story that was
played on one goes on as a story without it.

### New in version 2

- **Pictures of places** (new in 2.0.0b7). A picture of each place the
  story goes to, with nobody in it, kept on the place's lore entry and shown
  behind the story's text while the scene is there. A button above the story
  draws one for the place you are at, or a new one from the last as the place
  is now (the bar after the brawl). The samples come with a picture of each
  of their places. See [Pictures of places](#pictures-of-places) below.
- **Visual Stories** (new in 2.0.0b7, early). A separate way to play: a
  world on its map, the visuals centre stage, the model for the people you
  meet and the plot, the game's own rules for everything else. The start
  page lists them under the samples, Doomsville first: walk its map, talk to
  its people, and try anything the rules don't cover (the keeper rules on it
  and makes the outcome real on the board). See [Visual
  Stories](docs/manual.md#visual-stories) in the manual.
- **A picture kept as a card's own** (new in 2.0.0b7). Right-click any
  picture and choose **Use as a character's picture…** or **Use as a place's
  picture…**: frame the one person, or the place without the people, and keep
  it as their portrait or the place's picture, or have a portrait or a
  picture of the place drawn from the part you framed.
- **Every character with a face.** A portrait for each character, drawn from
  their card when you start, and the People page beside the story shows who
  is here by their faces.
- **New sample stories to start from**, each with a plot, a background and a
  picture of everyone and of every place: The Ashen Reach (grimdark science
  fiction), The Silverwood (fantasy), Brasshaven (steampunk) and The Ninth
  Furnace (a hellscape).
- **A friendlier window.** A start page that shows every way to begin, the
  pages beside the story titled with what they are for, Settings in groups,
  plain names throughout, and room to read.
- **Anthropic's own API**, and several endpoints at once, each with its own
  key.
- **Image models chosen once.** Pick a model, and SealedLore sends its
  editing version whenever a picture goes with the prompt.
- **Play from your phone** (new in 2.0.0b2). Turn on File → Phone access,
  and a phone's Tor Browser can play the stories you share, wherever the
  phone is, through SealedLore's own tor: nothing to set up on your router,
  no port opened. Pair it by scanning a code; its bookmark is its login.
  Text stories and chats, on Linux, for now. See
  [Playing from your phone](docs/manual.md#playing-from-your-phone).
- **Plots that play better** (new in 2.0.0b3). A guide to wording a plot,
  [Writing a plot that plays well](SampleStories/WRITING_PLOTS.md) (also under
  Help), the sample plots reworded where it mattered, and the plot director
  asked far less often.
- **Characters from the start, and a known world's cast** (new in
  2.0.0b4). Write a story from scratch makes your characters on the Cast page
  itself, portraits and all, in as much or as little detail as you like. For
  a story set in a world that already exists, **Fill with lore characters…**
  writes cards for its people, the ones you name first.
- **The map is a view, and it is painted** (2.1.0b1, for testing). Nothing on
  the map moves by itself any more: click where to go, a building to go in,
  a door, a person or a thing, and the turn goes in the box for you to send;
  the move is made when it goes, and your figure walks there. Each area you
  stand in is painted once as a storybook illustration from the map itself,
  with the people and things standing on it where the map has them. The
  Region map is a board of those paintings, laid out by direction rather
  than to scale. Doomsville with a map and Brasshaven come with every area
  designed and painted.
- **A distrusted root out of the bundle** (2.0.0b5). The AppImage no longer
  trusts emSign ECC Root CA - G3, the eMudhra root whose intermediates were
  found to have recoverable keys in October 2026, and Private Mode's client
  is on Tinfoil's current SDK, checked against NanoGPT's own.
- **Three small fixes from play** (2.0.0b6). The quick setup offers a
  passage length of your own; its other way in is called **Advanced setup…**;
  and a past turn's OOC note is edited with the turn, and goes with a new
  branch from it.

### Pictures of places

A story can keep a picture of each place it goes to: the place itself, as it
is now, with nobody in it. **Picture of this place…** above the story (and
under Story) has the storyteller's prompt writer describe the place the scene
is at, you check the prompt, and the picture is drawn (FLUX.3 Klein by
default, about a cent); it goes on the place's lore entry, made for it if
there is none, and stands behind the story's text while the story is there.
Come back later and the picture is back. When the place has changed (a bar
after a fight, a house after the fire), **New picture of this place…** draws
it again from the last picture, so it is the same place with the change
shown. Every picture stays on the entry and in Media. Settings → Images →
Place pictures turns the background off; the pictures stay.

### Privacy

The "Sealed" in the name refers both to the plots (see below) and to how the
application handles your chats. It fully supports NanoGPT's end-to-end
encrypted private models, as well as their standard TEE models and local
servers. But that alone isn't that special.

What makes it more useful is that you can switch from your usual model to a
private one seamlessly, mid-story, and nothing from the private part reaches
your usual model except a summary you approve.

Let's say you're using Claude Sonnet for your roleplay, as it is one of the
best models for prose. Then your story goes somewhere Sonnet may not handle,
or somewhere you simply want to keep private. At that point you can start a
private scene on an end-to-end encrypted model like GLM 5.3. The private
model picks up the story where it is (if the story is too long for it, your
usual model can first condense it into a summary), and you carry on with the
private model. When you are done, you end the private scene: the private
model summarises it, keeping anything intimate or sensitive vague and safe
for work, and shows you the summary. You can drop it, change it or approve
it, and the approved summary is added to your main story. You then continue
with the smarter model, and all it knows of that part are the key plot
points, without the material that might cause refusals.

A simple chat can do the same: the Private button holds the next part of the
conversation on your private model, and the chat's own model reads only the
summary you approve.

### Author-driven plots

The "Sealed" in the name also refers to plots written by a person (or, I
suppose, an AI) and kept hidden from the player as they play through them.
Events can trigger under specific circumstances, or at specific times in the
story.

So rather than an AI simply injecting random events to spice up a story, an
actual human can write a plot outline that reacts to what you do. A
secondary AI model follows the story to track when triggers are met: for
example, being at a set place at a set time, or how you respond to a certain
person. Things can also happen off screen: if you fail to meet an objective
by a certain date, a town can be overrun in the background, or a character
you would have met may be dead instead, and consequences may follow. A time
skip or an event you write yourself in a Direction turn moves the plot along
with it.

The plot file editor (File → Plot file editor…) lets you write your own
plots and share them, with reference pictures for their characters, places
and lore if you like (kept in a folder beside the file).

### Auto-fix your story

If the AI isn't responding the way you'd like, but you can't work out which
setting needs changing, Story → Review settings with the model… has an AI
model (I recommend Opus 5.5) look over the story's settings, the model
settings and the prompts the storyteller receives. Based on your complaint, it recommends
how to fix it, and you accept or reject each recommendation.

### Character and scene consistency

It's always annoying when characters suddenly appear in a scene out of
nowhere: "listening in", "standing nearby", "listening on the radio". In a
story this is guarded against by the Scene page, which tracks who is where,
and by prompts that testing has shown do a good job of avoiding it without
breaking the story's flow. And the model never speaks or acts for your
character.

### Pictures and video

Every character can have a portrait, and every lore entry a picture, drawn
from what their cards say: the Start dialog offers them when a story begins,
and you see and can change every prompt first. A portrait can then be drawn
again, changed (older, a scar, other clothes) or shown in the scene as the
story stands, keeping the same face. FLUX.3 Klein draws them by default for
about a cent each; Seedream 5.0 Pro draws the best.

You can also have a picture of the current scene: the AI that has read the
story writes the prompt, using those portraits and any other reference
pictures you give people, places and objects. You see the whole request
before anything is sent. I recommend Seedream 5.0 Pro for this, which is the
default. Every picture and video the story has made is on its Media page.

It can make short videos of the story too (Seedance 2.5 by default), played
in place in the story, from a prompt written for video and, if you like,
starting or ending on one of your pictures. Video costs far more than
pictures, so it is off until you give it an API key of its own (Settings →
Video), and every video shows its price and waits for you to accept it.
Pictures and video can each use a different service from chat (NanoGPT,
OpenRouter, WaveSpeed, or anything OpenAI-compatible).

### Simple chat

You can also have a basic chat without the storytelling features, but still
with summarising, end-to-end encrypted models, image generation, and so on.

### API usage fees

I suggest setting a spending limit on your NanoGPT (or other provider's) API
key. With every feature on, SealedLore calls several models, sometimes
several per turn, so it isn't meant to be a cheaper way to roleplay, but a
better one. That said, most of it can be turned off, and you still keep many
of the features.

Have fun!

## What else it does

- **Playing**: you write as a character you can switch at any time, as
  narration, or as direction to the model; you can also ask it questions out
  of character.
- **People by their faces**: beside the story, who is here now,
  the rest of the cast and the supporting characters, each with their
  portrait; someone the story brings in without a card gets one in a click.
- **The world and the cast**: a world bible, cast sheets with skills,
  supporting characters the story introduces, and a lorebook whose entries
  can wait, unseen by any model, until you mention them, so what hasn't
  happened yet doesn't happen early.
- **Outcomes and dice**: fiat, plausible, contested, or d100 rolls against
  the character's skills.
- **Length and style**: length presets with measured word counts, a per-turn
  override, and a style block you can edit by hand.
- **Long stories**: old prose summarised into chapters in the background and
  merged into parts as the story grows, with a prompt laid out for prompt
  caching. Edit any chapter or rebuild it in the background; ask a Question
  about something long merged and its chapters come back, for the Question
  and the next few turns.
- **Takes and branches**: regenerate any passage in place, or rewrite any
  earlier message as a named branch; a diagram shows them all.
- **Drafting**: a whole story drafted from a premise.
- **Costs**: per passage, per session and per story.
- **Routes**: on NanoGPT, choose which host serves each model (the fastest,
  the cheapest, or one you pick), and see whether the choice was followed.
  The storyteller's host can be changed beside its model, mid-story or
  mid-chat.
- **Quick replies**: models are asked to reason as little as they allow by
  default (Settings → Generation, for every story). A model that NanoGPT
  lists as reasoning by default is asked for less from its first request;
  one caught thinking at length anyway is noted under the passage and asked
  for less from then on. When a model gets slow or its writing gets worse, the two checks under
  Help → Model trouble → The model takes too long before it writes… and
  Help → Model trouble → The model's writing has got worse… see whether its
  host is to blame, compare other hosts, and let you switch host or mark a
  bad one.
- **Pictures and videos** of the story in its Media tab, the videos played in
  place, and a line above the story that always says where it is kept and
  which model and route it is on.
- **Chats kept in memory only**, pictures and all, that leave nothing on
  disk unless you export them.

## Installing

### Linux: the AppImage

Download `SealedLore-<version>-x86_64.AppImage` from the
[releases page](https://github.com/DigitalArchon/SealedLore/releases), then:

```bash
chmod +x SealedLore-*-x86_64.AppImage
./SealedLore-*-x86_64.AppImage
```

It needs x86_64 and glibc 2.34 or newer (Ubuntu 22.04, Mint 21, Debian 12,
Fedora 35 and later), with nothing else to install. (Playing videos in the
app uses the PulseAudio client library, which nearly every desktop has.) To
check the download:

```bash
gh attestation verify SealedLore-<version>-x86_64.AppImage --repo DigitalArchon/SealedLore
```

The build is reproducible, so you can also rebuild a release yourself and
compare checksums: see [Building and checking the AppImage](docs/building.md).

The AppImage holds every Python file both as source and compiled, and Python
runs the compiled one. So reading the source inside it tells you what runs
only if the compiled files are what that source compiles to, which
`packaging/appimage/verify_bytecode.sh` checks, offline and without a
rebuild: see [Checking the compiled Python](docs/building.md#checking-the-compiled-python).

### Linux: from source

Python 3.11 or newer. On Debian, Ubuntu and Mint, X11 also needs
`sudo apt-get install libxcb-cursor0`.

```bash
git clone https://github.com/DigitalArchon/SealedLore && cd SealedLore
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
sealedlore-gui
```

To add it to your desktop's menu, see
[Desktop entry](docs/manual.md#desktop-entry-optional).

### Windows (from source)

Windows 10 or 11. There is no installer yet: open **Windows PowerShell** from
the Start menu and paste these lines one at a time. Close and reopen
PowerShell after the first two, so it finds what they installed.

```powershell
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
```

```powershell
git clone https://github.com/DigitalArchon/SealedLore
cd SealedLore
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\pythonw -m sealedlore.gui.app
```

The manual's [Windows section](docs/manual.md#windows-from-source) goes
through this step by step. It covers installing without winget or Git,
making a desktop shortcut, updating, and what to do if something goes wrong.
Linux is the version SealedLore is built and tested on day to day, and
Windows offers weaker privacy on the machine itself (see
[Your data and privacy](#your-data-and-privacy)).

## First five minutes

1. **Point it at an endpoint.** File → Settings → Endpoints takes a base URL
   and an API key. Any endpoint that speaks the OpenAI `/chat/completions`
   protocol works; the defaults assume [NanoGPT](https://nano-gpt.com)
   (`https://nano-gpt.com/api/v1`), and [OpenRouter](https://openrouter.ai)
   (`https://openrouter.ai/api/v1`) or a local server on `localhost` work the
   same way. Anthropic's own API works directly too, with an Anthropic key
   and `https://api.anthropic.com` as the base URL (Settings → Endpoints →
   Speaks reads it from the address). Several endpoints can be set up at
   once, each with its own key, and the model picker's Provider column
   shows which endpoint each model is from. The key is kept in the system
   keychain (GNOME Keyring or
   KWallet on Linux, Credential Manager on Windows), or in plain text in the
   data folder on a computer without one; Settings says which under each key
   field. It is used for nothing else. NanoGPT is the recommended endpoint: it also serves the
   end-to-end encrypted models that private scenes and chats use. Make an
   account at [nano-gpt.com](https://nano-gpt.com/), or through the
   developer's [referral link](https://nano-gpt.com/r/mackztyf). It applies to
   what you use directly on the NanoGPT website (image and video generation,
   text chat and so on): you get a 5% discount there, and the developer gets
   credit worth 10% of it. It doesn't discount what SealedLore itself uses
   through your API key. Until an endpoint, key and story model are set, the
   window's start screen walks through these steps.

2. **Check the story model** on the Models page. It starts as Claude Sonnet
   4.6, which with Opus 5.5 measured best as a storyteller; GLM 5.3 is a good
   cheap one. Browse… lists what the endpoint offers. On NanoGPT the small
   calls made on every turn (the scene, the plot, picking lore) start on
   cheaper models that measured well for them; **Use recommended models**
   fills them in on a setup you already have, and **Test speed…** shows how
   quickly each model you have chosen answers. A field left blank falls back
   to the story model. For a model NanoGPT runs on several hosts, **Route…**
   beside its field can pick the fastest or cheapest host, or one host,
   billed pay-as-you-go (see the manual's Models section).

3. **Start from a sample.** The start page shows the sample stories, each
   with its picture and a line on what it is (File → New… shows them too):
   Doomsville (a survivor at a farmhouse, with a plot), or one of four other
   worlds, each with a plot, a background, and a picture of every character
   and place: The Ashen Reach (grimdark
   science fiction on a dying mining world), The Silverwood (elves, forests,
   caves and a city on a waterfall), Brasshaven (a steampunk city of
   airships and clockwork) and The Ninth Furnace (a hellscape of cultists
   and rivers of lava). Setup opens first; Save, then choose who you are
   playing and the opening is written. Or choose **Draft a story from an
   idea** and describe a story in a sentence or two, or **Write a story from
   scratch**, which walks through it a step at a time, characters and their
   portraits included. Set in a world that already exists? **Fill with lore
   characters…** writes cards for its people.

4. **Play.** Type what your character does or says and press Enter. The
   storyteller answers. Beside the story, **People** shows who is here now,
   by their faces,
   with a portrait drawn for anyone who has none in a click. *Playing* is
   the character you write, *Speaking as* is who a turn's text lands as
   (your character, Narration, Direction to the storyteller, or a Question
   to the model out of character), and **More ▸** has the rest. View →
   Prompt page (advanced) shows exactly what the storyteller was sent.

To look around with no endpoint at all, start it with `--mock`
(`sealedlore-gui --mock`, or the AppImage with `--mock`) and a scripted
storyteller plays instead.

The [manual](docs/manual.md) covers the rest: every part of the window, and
what each feature does.

## Your data and privacy

There is no telemetry, no update check and no crash reporting. Every endpoint
must be `https://` (plain `http://` is accepted only for `localhost`). Phone
access, below, answers phones over `http://` on an onion address, which tor
encrypts the whole way.

- **The chat endpoint** gets every prompt: the world, cast, lore, the recent
  story and your turn, and the side calls made from the story (chapter
  summaries, scene and plot reads, reviews, picture prompts).
- **The embeddings endpoint**, off by default, gets the lore entries, the
  chapters merged into parts (and the archived passages, if recall is set to
  search them), a query built from the last few passages, and your Question
  or turn when recall searches for it.
- **Pictures** go to the picture endpoint (Settings → Images; by default the
  chat endpoint): the prompt you approved and the reference pictures you
  chose.
- **Videos** go to the video endpoint (Settings → Video, off until it has a
  key): the prompt you approved and the start and end pictures you chose.
- **A private scene** goes to the private model alone. Checking a TEE model
  sends the enclave's GPU evidence to NVIDIA and fetches Intel's public
  certificates; nothing from your story goes to either. A `private/` model is
  end-to-end encrypted, so NanoGPT relays only ciphertext.
- **Phone access**, off unless you turn it on, runs SealedLore's own tor,
  which publishes your computer's onion address on the Tor network. Only
  phones you paired get anything, and only from the stories you share. What a
  phone shows may stay on the phone (Tor Browser and Android keep screenshots,
  previews and what the keyboard learns), private scenes and memory-only
  chats included.
- **On disk**, settings and stories live in a data folder only your user
  can read. API keys go to the system keychain, or into that folder's
  `config.json` in plain text where there is no keychain or you choose it. Each story keeps every
  request and reply in its `api_log.jsonl`, so a story backup carries every
  prompt sent for it. An "in memory" private scene keeps its messages off
  the disk (only a content-free record of each call is kept, for the cost);
  an "in memory" chat, on any model, writes nothing at all.
  On Linux the app tells the system to keep no core dump after a crash;
  `crash.log` records only where in the program it happened, never your
  stories.
- **Swap and hibernation** belong to the operating system, on Linux and
  Windows alike: it may write the app's memory, a memory-only scene or chat
  included, to swap or a hibernation file (`pagefile.sys` and `hiberfil.sys`
  on Windows). Disk encryption covers them. On Linux, if privacy is your
  main concern, turn swap off while you use SealedLore with
  `sudo swapoff -a`, and back on afterwards with `sudo swapon -a`. Turning
  it off first moves whatever is already in swap back into memory, which
  needs enough free RAM. Don't hibernate meanwhile.

**On Windows** the network side is the same, but the machine gives less
protection:

- a crash may leave a memory dump with Windows Error Reporting;
- other programs you run can read the app's memory;
- OneDrive may upload exports saved in synced folders.

The app does keep private windows out of screen capture and Recall.

The full account, including what the TEE and end-to-end encryption checks
prove and what they don't, is in the manual:
[Where your data goes](docs/manual.md#where-your-data-goes) and
[Private scenes](docs/manual.md#private-scenes).

## More

- [The manual](docs/manual.md): installing in detail, the window, every
  feature, the terminal client.
- [The plot file format](SampleStories/PLOT_FORMAT.md), with
  [Doomsville](SampleStories/Doomsville.md) as a worked example, and
  [Writing a plot that plays well](SampleStories/WRITING_PLOTS.md), on how
  to word one.
- [Building and checking the AppImage](docs/building.md).
- [Contributing](CONTRIBUTING.md): setting up, running the tests, and how
  the project works.

## Licence

SealedLore is free software under the **GNU Affero General Public License,
version 3 or later** ([LICENSE](LICENSE)). You may use, study, change and
share it. If you distribute it, changed or not, or let people use a changed
version over a network, you must offer them its source under the same
licence. This is a summary: the licence itself is what applies.

Digital Archon, SealedLore's author, can also license SealedLore's own code
on other terms by separate written agreement, for example for use in
software that isn't released under the AGPL. To ask, contact Digital Archon
through <https://www.digitalarchon.com.au>.
Such an agreement covers SealedLore's code only. The third-party software it
uses keeps its own licence, and anyone who ships that software must still
follow its terms. Contributions are accepted under the terms in
[CONTRIBUTING.md](CONTRIBUTING.md), which allow both.

**Third-party software.** SealedLore's dependencies keep their own licences,
all of them compatible with the AGPL. Qt for Python (PySide6) is under the
LGPL-3.0; httpx, pydantic, tiktoken, numpy, tinfoil and the packages they use
are under permissive licences (MIT, BSD, Apache-2.0, and MPL-2.0 for
certifi). The AppImage lists everything it bundles, with versions, sources
and licence texts, in `usr/share/licenses/sealedlore/THIRD-PARTY-NOTICES.md`
(template: [packaging/appimage/THIRD-PARTY-NOTICES.md](packaging/appimage/THIRD-PARTY-NOTICES.md)).
