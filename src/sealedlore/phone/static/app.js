// SealedLore on a phone: the open story, played from Tor Browser.
//
// Served by the desktop over its onion address (sealedlore/phone/). The link
// this page is opened by is the login: "#k=<token>" after pairing. The part
// after "#" never leaves the phone except in the Authorization header of
// the page's own requests. Every bit of story text goes in as text
// (textContent), never as HTML: what a model writes is not trusted.
"use strict";

(() => {
  const RECONNECT_SECONDS = [1, 2, 4, 8, 15, 30];
  const PAIR_POLL_MS = 2000;
  const PAGE = 40;

  const S = {
    token: "",
    state: null,
    composer: null,
    messages: [],
    earlier: false,
    branches: [],
    version: -1,
    writing: null, // { card, text } while a passage streams
    continuing: null, // { id, words, text, label, labelText } while a passage is carried on
    revising: null, // { id, words, text, label, labelText } while a passage is written again with a change
    noting: null, // { id, mode, form, field, go } while a few words are written under a passage
    faceUrls: new Map(), // a character's picture, fetched once: id -> object URL
    stageEl: null, // the stage, kept across the composer's rebuilds
    asking: null, // this page's word for the suggestions it asked for, until they come
    suggestions: null, // { options, private }: Suggest…'s offers, until one is taken or they are put away
    suggestEl: null, // their list, kept across the composer's rebuilds
    reconnects: 0,
    ended: false, // revoked or forgotten: nothing more to do
    acknowledged: new Set(), // private kinds the author chose to see, this page session
    declined: new Set(),
    choices: { speaker: null, length: null, outcome: null, skill: null, difficulty: null },
    more: false,
    sending: false,
  };

  // --- small helpers -------------------------------------------------------------

  function el(tag, options, children) {
    const node = document.createElement(tag);
    const o = options || {};
    if (o.cls) node.className = o.cls;
    if (o.text !== undefined && o.text !== null) node.textContent = String(o.text);
    if (o.attrs) for (const [k, v] of Object.entries(o.attrs)) node.setAttribute(k, v);
    if (o.on) for (const [k, v] of Object.entries(o.on)) node.addEventListener(k, v);
    for (const child of children || []) if (child) node.appendChild(child);
    return node;
  }

  function button(text, onClick, cls) {
    return el("button", { text, cls, attrs: { type: "button" }, on: { click: onClick } });
  }

  function $(id) {
    return document.getElementById(id);
  }

  function auth() {
    return { Authorization: "Bearer " + S.token };
  }

  let toastTimer = null;
  function toast(text, error) {
    let node = $("toast");
    if (!node) {
      node = el("div", { attrs: { id: "toast", role: "status" } });
      node.addEventListener("click", () => node.remove());
      document.body.appendChild(node);
    }
    node.textContent = text;
    node.className = error ? "error" : "";
    placeToast();
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.remove(), error ? 8000 : 5000);
  }

  // Over the story, clear of the composer however tall it stands: with More
  // open or suggestions listed, a word about a job covered the box to type in.
  function placeToast() {
    const node = $("toast");
    const form = $("composer");
    if (node && form && form.offsetHeight) node.style.bottom = form.offsetHeight + 12 + "px";
  }

  async function call(method, path, body) {
    const options = { method, headers: auth(), cache: "no-store" };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let data = {};
    if ((response.headers.get("Content-Type") || "").startsWith("application/json")) {
      try {
        data = await response.json();
      } catch (_) {
        data = {};
      }
    }
    if (response.status === 401) {
      notAllowed();
      throw new Error("not paired");
    }
    return { status: response.status, ok: response.ok, data };
  }

  function messageOf(answer) {
    return (answer.data && answer.data.error) || "The desktop didn't answer as expected.";
  }

  // --- pages with no story -----------------------------------------------------------

  function page(children) {
    document.body.classList.remove("story-view");
    const app = $("app");
    app.replaceChildren(el("div", { cls: "page-message" }, children));
  }

  function notAllowed() {
    if (S.ended) return;
    S.ended = true;
    stopStream();
    page([
      el("h1", { text: "This phone isn't allowed any more" }),
      el("p", {
        text:
          "It was removed on the desktop, or never paired. To pair it again, open " +
          "File → Phone devices… → Pair a phone… on the desktop and scan the code.",
      }),
    ]);
  }

  function noKey() {
    page([
      el("h1", { text: "SealedLore" }),
      el("p", {
        text:
          "Open your SealedLore bookmark: it is how this phone logs in. To pair a phone, " +
          "open File → Phone devices… → Pair a phone… on the desktop.",
      }),
    ]);
  }

  // --- pairing -----------------------------------------------------------------------

  function pairPage(code) {
    const name = el("input", {
      attrs: { type: "text", maxlength: "40", value: "My phone", "aria-label": "This phone's name" },
    });
    const status = el("div");
    const connect = button("Connect", () => claim(code, name.value, connect, status), "primary");
    page([
      el("h1", { text: "Pair this phone with SealedLore" }),
      el("p", { text: "Give this phone a name the desktop will show, then connect." }),
      el("div", { cls: "row" }, [name, connect]),
      status,
    ]);
  }

  async function claim(code, name, connect, status) {
    connect.disabled = true;
    status.replaceChildren(el("p", { text: "Asking the desktop…" }));
    let answer;
    try {
      const response = await fetch("/api/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, name }),
        cache: "no-store",
      });
      answer = { status: response.status, data: await response.json() };
    } catch (e) {
      connect.disabled = false;
      status.replaceChildren(el("p", { cls: "box warn", text: "The desktop couldn't be reached. Try again." }));
      return;
    }
    if (answer.status !== 202) {
      const used = answer.status === 410 ? " If this phone is paired already, open your bookmark instead." : "";
      status.replaceChildren(el("p", { cls: "box warn", text: messageOf(answer) + used }));
      connect.disabled = answer.status === 410;
      return;
    }
    status.replaceChildren(
      el("p", { text: "SealedLore on the desktop now asks whether to allow this phone. Check that it shows:" }),
      el("div", { cls: "check", text: answer.data.check }),
      el("p", { cls: "placeholder", text: "Waiting for the desktop…" })
    );
    waitForAnswer(answer.data.claim, status);
  }

  async function waitForAnswer(claimId, status) {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, PAIR_POLL_MS));
      let data;
      try {
        const response = await fetch("/api/pair/" + encodeURIComponent(claimId), { cache: "no-store" });
        data = await response.json();
      } catch (e) {
        continue; // the connection dropped: ask again
      }
      if (data.state === "waiting") continue;
      if (data.state === "approved" && data.token) return paired(data.token);
      const why =
        data.state === "refused"
          ? "The desktop said no."
          : "That took too long. Show the code on the desktop again and scan it again.";
      status.replaceChildren(el("p", { cls: "box warn", text: why }));
      return;
    }
  }

  function paired(token) {
    S.token = token;
    // The login link: replaced, not added, so Back doesn't lead to the spent code.
    location.replace("#k=" + token);
    page([
      el("h1", { text: "Paired" }),
      el("div", {
        cls: "box ok",
        text:
          "Bookmark this page now (⋮ menu → ☆). The bookmark is how this phone logs in: " +
          "Tor Browser forgets this page when it closes, and the address can't be found again " +
          "without the bookmark.",
      }),
      el("div", { cls: "row" }, [button("I've bookmarked it", () => start(), "primary")]),
    ]);
  }

  // --- the story ----------------------------------------------------------------------

  function layout() {
    document.body.classList.add("story-view");
    const menu = button("☰", openMenu);
    menu.setAttribute("aria-label", "Stories, branches and this phone");
    $("app").replaceChildren(
      el("header", { attrs: { id: "top" } }, [el("span", { cls: "title", attrs: { id: "title" } }), menu]),
      el("div", { attrs: { id: "status", role: "status" } }),
      el("div", { attrs: { id: "banner" } }),
      el("main", { attrs: { id: "messages" } }),
      el("form", { attrs: { id: "composer" } })
    );
    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      send();
    });
  }

  async function start() {
    S.ended = false;
    layout();
    await refreshAll();
    openStream();
  }

  async function refreshAll() {
    S.asking = null;
    try {
      const answer = await call("GET", "/api/state");
      S.state = answer.data;
      if (S.state.shared) {
        await Promise.all([loadComposer(), loadLatest()]);
      } else {
        S.messages = [];
        S.composer = null;
        S.suggestions = null;
      }
      render();
    } catch (e) {
      if (!S.ended) setStatus("The desktop can't be reached. Is SealedLore running with Phone access on?");
    }
  }

  async function loadComposer() {
    const answer = await call("GET", "/api/composer");
    if (!answer.ok) return;
    const previous = S.composer;
    S.composer = answer.data;
    if (!previous || S.choices.speaker === null) {
      S.choices.speaker = S.composer.speaker || null;
      S.choices.length = S.composer.length || null;
      S.choices.outcome = S.composer.outcome || null;
    }
    const offered = (list, value) => (list || []).some((o) => o.value === value);
    if (!offered(S.composer.speakers, S.choices.speaker)) S.choices.speaker = S.composer.speaker || null;
    if (S.choices.length && !offered(S.composer.lengths, S.choices.length)) S.choices.length = null;
    if (S.choices.outcome && !offered(S.composer.outcomes, S.choices.outcome)) S.choices.outcome = null;
  }

  async function loadLatest() {
    const [transcript, branches] = await Promise.all([
      call("GET", "/api/transcript?limit=" + PAGE),
      call("GET", "/api/branches"),
    ]);
    if (transcript.ok) {
      S.messages = transcript.data.messages;
      S.earlier = transcript.data.earlier;
      S.version = transcript.data.version;
    }
    if (branches.ok) S.branches = branches.data.branches || [];
    S.writing = null;
    S.continuing = null;
    S.revising = null;
  }

  async function loadEarlier() {
    if (!S.messages.length) return;
    const answer = await call(
      "GET",
      "/api/transcript?limit=" + PAGE + "&before=" + encodeURIComponent(S.messages[0].id)
    );
    if (!answer.ok) return;
    const scroller = document.scrollingElement;
    const fromBottom = scroller.scrollHeight - scroller.scrollTop;
    S.messages = answer.data.messages.concat(S.messages);
    S.earlier = answer.data.earlier;
    renderMessages();
    scroller.scrollTop = scroller.scrollHeight - fromBottom;
  }

  function setStatus(text) {
    const node = $("status");
    if (node) node.textContent = text || "";
  }

  function render() {
    if (!$("messages")) layout();
    const state = S.state || {};
    $("title").textContent = state.story ? state.story.title : "SealedLore";
    renderStatus();
    renderBanner();
    if (!state.shared) {
      renderNoStory();
      return;
    }
    renderMessages();
    renderComposer();
  }

  function renderStatus() {
    const state = S.state || {};
    let text = "";
    if (state.desktop_waiting) text = "The desktop is waiting for an answer.";
    else if (state.queued) text = "A turn is waiting to go.";
    else if (state.busy && !state.background) text = "Writing…";
    else if (state.busy) text = "Reading the passage…";
    setStatus(text);
    if ($("composer") && S.composer) syncSendButton();
  }

  const PRIVATE_WORDS = {
    scene: "a private scene",
    memory: "kept in memory only",
    tee: "a chat on an attested private model",
  };
  const PRIVATE_PLACEHOLDER = {
    scene: "Written in a private scene.",
    memory: "Kept in memory only.",
    tee: "Written on an attested private model.",
  };
  const PRIVATE_PICTURE = {
    scene: "A picture made in a private scene.",
    memory: "A picture from a scene kept in memory only.",
    tee: "A picture from a chat on an attested private model.",
  };
  const PRIVATE_SUGGESTIONS = {
    scene: "Suggestions from a private scene.",
    memory: "Suggestions kept in memory only.",
    tee: "Suggestions from an attested private model.",
  };
  const PRIVATE_QUESTION = {
    scene: "A Question asked in a private scene.",
    memory: "A Question kept in memory only.",
    tee: "A Question asked on an attested private model.",
  };

  function privatePlaceholder(words, kind) {
    return button(words[kind] + " Tap to see it.", () => askToShow(kind), "placeholder");
  }

  function renderBanner() {
    const banner = $("banner");
    if (!banner) return;
    const kind = S.state && S.state.private;
    if (!kind) {
      banner.replaceChildren();
      return;
    }
    banner.replaceChildren(
      el("div", {
        cls: "box warn",
        text:
          "This is " + PRIVATE_WORDS[kind] + ". What this page shows is on this phone now, " +
          "and SealedLore can't say what Tor Browser or Android keep of it.",
      })
    );
  }

  async function renderNoStory() {
    const box = $("messages");
    $("composer").replaceChildren();
    composerKey = "";
    S.suggestions = null;
    const state = S.state || {};
    const why = state.open_elsewhere
      ? "The story open on the desktop isn't shared with this phone."
      : "No story is open on the desktop.";
    box.replaceChildren(el("p", { cls: "box", text: why }), el("p", { text: "Open a shared story:" }));
    const list = await storiesList();
    box.appendChild(list);
  }

  async function storiesList() {
    const list = el("div", { cls: "list" });
    let answer;
    try {
      answer = await call("GET", "/api/stories");
    } catch (e) {
      return list;
    }
    const stories = (answer.data && answer.data.stories) || [];
    if (!stories.length) {
      list.appendChild(
        el("p", {
          cls: "placeholder",
          text: "Nothing is shared yet. Choose stories on the desktop: File → Phone devices….",
        })
      );
    }
    for (const story of stories) {
      const label = story.title + " · " + story.messages + " message" + (story.messages === 1 ? "" : "s");
      const b = button(label, () => openStory(story.id), story.open ? "current" : "");
      list.appendChild(b);
    }
    return list;
  }

  async function openStory(id) {
    closeOverlay();
    const answer = await call("POST", "/api/stories/" + encodeURIComponent(id) + "/open", {});
    if (!answer.ok) toast(messageOf(answer), true);
    else toast("Opening it on the desktop…");
  }

  // --- messages -----------------------------------------------------------------------

  function hidden(kind) {
    return kind && !S.acknowledged.has(kind);
  }

  function messageCard(message, isLast) {
    const kind = message.private;
    const classes = ["message", message.kind];
    if (kind) classes.push("private");
    if (message.archived) classes.push("archived");
    const card = el("article", { cls: classes.join(" "), attrs: { "data-id": message.id } });
    if (message.branch_start) card.appendChild(el("div", { cls: "branch-start", text: "Branch “" + message.branch_start + "” starts here" }));
    let label = message.label;
    if (message.roll) label += " · " + message.roll;
    if (message.private_summary) label = "Private scene · summary";
    else if (kind) label += " · " + (kind === "tee" ? "private model" : "private");
    const labelNode = el("div", { cls: "label", text: label });
    card.appendChild(labelNode);
    if (hidden(kind)) {
      card.appendChild(
        button(PRIVATE_PLACEHOLDER[kind] + " Tap to see it.", () => askToShow(kind), "placeholder")
      );
      return card;
    }
    if (message.faces && message.faces.length) {
      card.appendChild(el("div", { cls: "faces" }, message.faces.map((f) => faceImage(f.picture, f.name))));
    }
    const text = el("div", { cls: "text", text: message.text });
    card.appendChild(text);
    const going = S.continuing;
    if (going && going.id === message.id) {
      // Carried on: what comes is added to the end of this message.
      card.classList.add("writing");
      Object.assign(going, { text, base: message.text, label: labelNode, labelText: label });
      showContinuing();
    }
    const again = S.revising;
    if (again && again.id === message.id) {
      // Written again with a change: what comes takes this passage's place,
      // and until the first of it comes the passage shows as it was.
      card.classList.add("writing");
      const labelText = label + " · being written again";
      Object.assign(again, { text, base: message.text, label: labelNode, labelText });
      labelNode.textContent = labelText;
      showRevising();
    }
    if (message.ooc) card.appendChild(el("div", { cls: "ooc", text: "Out of character: " + message.ooc }));
    // A picture or a Question has its own tag: one made in a private scene
    // before its first message hangs on a public passage, and is held back
    // as a private message is (not even fetched) until it is acknowledged.
    if (message.pictures && message.pictures.length) {
      const strip = el("div", { cls: "pictures" });
      for (const picture of message.pictures) {
        strip.appendChild(
          hidden(picture.private) ? privatePlaceholder(PRIVATE_PICTURE, picture.private) : thumbnail(picture.id)
        );
      }
      card.appendChild(strip);
    }
    for (const aside of message.asides || []) {
      if (hidden(aside.private)) {
        card.appendChild(privatePlaceholder(PRIVATE_QUESTION, aside.private));
        continue;
      }
      card.appendChild(
        el("details", {}, [
          el("summary", { text: "Question: " + aside.question }),
          el("div", { cls: "text", text: aside.answer || "(no answer)" }),
        ])
      );
    }
    const actions = el("div", { cls: "actions" });
    if (message.take) {
      const [position, count] = message.take;
      actions.appendChild(button("‹", () => switchTake(message.id, -1)));
      actions.appendChild(el("span", { cls: "take", text: position + " / " + count }));
      actions.appendChild(button("›", () => switchTake(message.id, 1)));
      actions.children[0].disabled = position <= 1;
      actions.children[2].disabled = position >= count;
    }
    if (message.can_regenerate && isLast) {
      actions.appendChild(button("Regenerate", () => regenerate(message.id)));
      actions.appendChild(button("Regenerate with a note…", () => openNote(message.id, "retake")));
    }
    if (message.can_continue) actions.appendChild(button("Continue", () => continuePassage(message.id)));
    if (message.can_revise) {
      const chat = S.composer && S.composer.chat;
      actions.appendChild(
        button(chat ? "Change this reply…" : "Change this passage…", () => openNote(message.id, "change"))
      );
    }
    if (message.can_reroll) actions.appendChild(button("Roll again", () => reroll(message.id)));
    if (actions.children.length) card.appendChild(actions);
    const noting = S.noting;
    if (noting && noting.id === message.id && message[NOTE_WORDS[noting.mode].flag]) {
      // The same form as before the list was built again: what is typed stays.
      card.appendChild(noting.form);
      noting.placed = true;
    }
    return card;
  }

  function renderMessages() {
    const box = $("messages");
    if (!box) return;
    const atBottom = nearBottom();
    const nodes = [];
    if (S.earlier) {
      nodes.push(button("Earlier messages", loadEarlier));
      nodes[0].id = "earlier";
    }
    if (!S.messages.length) {
      nodes.push(el("p", { cls: "placeholder", text: "Nothing written yet. Write the first turn below." }));
    }
    const lastPassage = [...S.messages].reverse().find((m) => m.kind === "assistant");
    if (S.continuing) S.continuing.text = null; // found again below, unless it is hidden
    if (S.revising) S.revising.text = null;
    const noting = S.noting;
    const typing = noting !== null && document.activeElement === noting.field;
    if (noting) noting.placed = false;
    S.messages.forEach((message) => nodes.push(messageCard(message, message === lastPassage)));
    if (S.writing) nodes.push(S.writing.card);
    box.replaceChildren(...nodes);
    // The passage the words were for has gone from the story as it stands.
    if (noting && !noting.placed && S.noting === noting) S.noting = null;
    else if (typing) noting.field.focus();
    if (atBottom) scrollToEnd();
    offerPrivate();
  }

  function nearBottom() {
    const scroller = document.scrollingElement;
    return scroller.scrollHeight - scroller.scrollTop - window.innerHeight < 200;
  }

  function scrollToEnd() {
    window.scrollTo(0, document.scrollingElement.scrollHeight);
  }

  // A character as they look (the faces over a passage, the stage): fetched
  // once for the page's life, since a picture's id never changes what it is.
  function faceImage(id, name) {
    const image = el("img", { attrs: { alt: name, title: name } });
    const known = S.faceUrls.get(id);
    if (known) {
      image.src = known;
      return image;
    }
    fetch("/api/pictures/" + encodeURIComponent(id) + "?thumb=1", { headers: auth(), cache: "no-store" })
      .then((r) => (r.ok ? r.blob() : null))
      .then((blob) => {
        if (!blob) return;
        const url = S.faceUrls.get(id) || URL.createObjectURL(blob);
        S.faceUrls.set(id, url);
        image.src = url;
      })
      .catch(() => {});
    return image;
  }

  // The stage: who is in the scene now, as they look, at the top of the box.
  function renderStage() {
    if (!S.stageEl) S.stageEl = el("div", { attrs: { id: "stage" } });
    const state = S.state || {};
    const people = state.stage || [];
    const kind = state.private;
    if (!people.length || hidden(kind)) {
      S.stageEl.replaceChildren();
      return S.stageEl;
    }
    S.stageEl.replaceChildren(
      ...people.map((p) => el("figure", {}, [faceImage(p.picture, p.name), el("figcaption", { text: p.name })]))
    );
    return S.stageEl;
  }

  function thumbnail(id) {
    const image = el("img", { attrs: { alt: "A picture from the story" } });
    fetch("/api/pictures/" + encodeURIComponent(id) + "?thumb=1", { headers: auth(), cache: "no-store" })
      .then((r) => (r.ok ? r.blob() : null))
      .then((blob) => {
        if (blob) image.src = URL.createObjectURL(blob);
      })
      .catch(() => {});
    image.addEventListener("click", () => wholePicture(id));
    return image;
  }

  async function wholePicture(id) {
    const response = await fetch("/api/pictures/" + encodeURIComponent(id), { headers: auth(), cache: "no-store" });
    if (!response.ok) return;
    const url = URL.createObjectURL(await response.blob());
    overlay([el("img", { cls: "whole", attrs: { src: url, alt: "A picture from the story" } }), button("Close", closeOverlay)]);
  }

  // --- private content ------------------------------------------------------------------

  const PRIVATE_NOTICE = {
    memory:
      "This is kept in memory only. SealedLore keeps it out of every file on your computer, " +
      "but it can't promise the same for this phone.",
    scene: "This is a private scene. Only the private model sees it; this phone now will too.",
    tee: "This chat runs on an attested private model. The model is attested; this phone is not.",
  };

  function privateKinds() {
    const kinds = new Set();
    for (const message of S.messages) {
      if (message.private) kinds.add(message.private);
      for (const each of [...(message.pictures || []), ...(message.asides || [])]) {
        if (each.private) kinds.add(each.private);
      }
    }
    if (S.suggestions && S.suggestions.private) kinds.add(S.suggestions.private);
    if (S.state && S.state.private) kinds.add(S.state.private);
    return kinds;
  }

  function offerPrivate() {
    if ($("overlay")) return;
    for (const kind of privateKinds()) {
      if (!S.acknowledged.has(kind) && !S.declined.has(kind)) {
        askToShow(kind);
        return;
      }
    }
  }

  function askToShow(kind) {
    overlay([
      el("h1", { text: "Before this is shown" }),
      el("p", { text: PRIVATE_NOTICE[kind] }),
      el("p", {
        text:
          "Tor Browser and Android may keep what this page shows: the open tab, screenshots " +
          "and the recent-apps preview, what your keyboard learns as you type, and backups.",
      }),
      el("div", { cls: "row" }, [
        button("Show it", () => {
          S.acknowledged.add(kind);
          closeOverlay();
          renderMessages();
          renderSuggestions();
        }, "primary"),
        button("Not now", () => {
          S.declined.add(kind);
          closeOverlay();
        }),
      ]),
    ]);
  }

  // --- the passage being written ---------------------------------------------------------

  function writingCard(label) {
    const text = el("div", { cls: "text" });
    const card = el("article", { cls: "message assistant writing" }, [el("div", { cls: "label", text: label }), text]);
    S.writing = { card, text, label: card.firstChild, labelText: label, words: "" };
    const box = $("messages");
    if (box) {
      const atBottom = nearBottom();
      box.appendChild(card);
      if (atBottom) scrollToEnd();
    }
  }

  function showWriting() {
    const w = S.writing;
    if (!w) return;
    const kind = S.state && S.state.private;
    w.text.textContent = hidden(kind) ? "Private text is being written. Tap the placeholder above to see it." : w.words;
  }

  function startContinuing(id) {
    S.continuing = { id, words: "", text: null, label: null, labelText: "" };
    renderMessages();
  }

  function showContinuing() {
    const c = S.continuing;
    if (!c || !c.text) return;
    c.text.textContent = c.base + c.words;
  }

  // Change this passage…: the passage is written again where it stands, not
  // in a new card. When the job settles the list is loaded again, and shows
  // the new take in its place (or the passage as it was, if none came).
  function startRevising(id) {
    S.revising = { id, words: "", text: null, label: null, labelText: "" };
    renderMessages();
  }

  function showRevising() {
    const r = S.revising;
    if (!r || !r.text) return;
    r.text.textContent = r.words || r.base;
  }

  // --- a few words for one passage ----------------------------------------------------------

  // The form that opens under a passage. `flag` is the entry's own word on
  // whether the desktop offers it there; the words are a story's and a chat's.
  const NOTE_WORDS = {
    retake: {
      flag: "can_regenerate",
      asks: "What should the new take do differently?",
      hint: [
        "Your note goes to the storyteller with this take only, as an instruction no character " +
          "hears. Left blank, this is a plain Regenerate.",
        "Your note goes to the model with this reply only, apart from your message. Left " +
          "blank, this is a plain Regenerate.",
      ],
      go: "Regenerate",
      blank: true,
    },
    change: {
      flag: "can_revise",
      asks: "What should change?",
      hint: [
        "The storyteller writes this passage again with your change made, keeping the rest in " +
          "the same words wherever it can. The result is a new take in this one's place; this " +
          "one is kept (‹ ›).",
        "The model writes this reply again with your change made, keeping the rest in the same " +
          "words wherever it can. The result is a new take in this one's place; this one is " +
          "kept (‹ ›).",
      ],
      go: "Change",
      blank: false,
    },
  };

  function openNote(id, mode) {
    const open = S.noting;
    if (open && open.id === id && open.mode === mode) {
      open.field.focus();
      return;
    }
    if (open && open.field.value.trim() && !window.confirm("Discard what you wrote?")) return;
    const words = NOTE_WORDS[mode];
    const chat = S.composer && S.composer.chat ? 1 : 0;
    const field = el("textarea", { attrs: { rows: "2", maxlength: "2000", "aria-label": words.asks } });
    const go = button(words.go, sendNote, "primary");
    const form = el("div", { cls: "note" }, [
      el("div", { cls: "asks", text: words.asks }),
      field,
      el("div", { cls: "hint", text: words.hint[chat] }),
      el("div", { cls: "row" }, [go, button("Cancel", cancelNote)]),
    ]);
    S.noting = { id, mode, form, field, go, sending: false, placed: false };
    field.addEventListener("input", syncNote);
    renderMessages();
    syncNote();
    if (S.noting) {
      revealNote();
      field.focus();
    }
  }

  // The composer stands over the bottom of the page: the form is scrolled
  // clear of it, so its buttons can be reached.
  function revealNote() {
    const n = S.noting;
    if (!n) return;
    const composer = $("composer");
    const limit = window.innerHeight - (composer ? composer.offsetHeight : 0) - 8;
    const bottom = n.form.getBoundingClientRect().bottom;
    if (bottom > limit) window.scrollBy(0, bottom - limit);
  }

  function syncNote() {
    const n = S.noting;
    if (!n) return;
    n.go.disabled = n.sending || (!NOTE_WORDS[n.mode].blank && !n.field.value.trim());
  }

  function cancelNote() {
    const n = S.noting;
    if (!n) return;
    if (n.field.value.trim() && !window.confirm("Discard what you wrote?")) return;
    closeNote();
  }

  function closeNote() {
    S.noting = null;
    renderMessages();
  }

  // The words go with the request and are kept nowhere on this page: sent,
  // the form is gone; refused, it stays as typed with the desktop's reason.
  async function sendNote() {
    const n = S.noting;
    if (!n || n.sending) return;
    const words = n.field.value.trim();
    if (!words && !NOTE_WORDS[n.mode].blank) return;
    n.sending = true;
    syncNote();
    let answer;
    try {
      if (n.mode === "change") {
        answer = await call("POST", "/api/revise", { node_id: n.id, change: words });
      } else {
        const body = retakeBody(n.id);
        if (words) body.note = words;
        answer = await call("POST", "/api/regenerate", body);
      }
    } catch (e) {
      if (!S.ended) toast("The desktop couldn't be reached. What you wrote is still here.", true);
      return;
    } finally {
      n.sending = false;
      syncNote();
    }
    if (!answer.ok) toast(messageOf(answer), true);
    else if (S.noting === n) closeNote();
  }

  // --- the composer ------------------------------------------------------------------------

  function select(options, value, defaultLabel, onChange) {
    const node = el("select");
    if (defaultLabel !== undefined) node.appendChild(el("option", { text: defaultLabel, attrs: { value: "" } }));
    for (const option of options || []) node.appendChild(el("option", { text: option.label, attrs: { value: option.value } }));
    node.value = value || "";
    node.addEventListener("change", () => onChange(node.value || null));
    return node;
  }

  function labelled(caption, control) {
    return el("label", {}, [el("span", { text: caption }), control]);
  }

  function rolls() {
    const c = S.composer;
    if (!c || c.chat) return false;
    const outcome = S.choices.outcome || c.story_outcome;
    return outcome === "dice" && c.held && S.choices.speaker === c.held;
  }

  let composerKey = "";

  function renderComposer() {
    const form = $("composer");
    const c = S.composer;
    if (!form || !c) return;
    // Built again only when what it offers changes: rebuilt after every
    // passage, it closed the phone's keyboard mid-sentence.
    const key = JSON.stringify([c, S.choices, S.more]);
    if (key === composerKey && form.querySelector("textarea")) {
      syncSendButton();
      return;
    }
    composerKey = key;
    const draft = form.querySelector("textarea");
    const focused = draft !== null && document.activeElement === draft;
    const text = el("textarea", {
      attrs: { id: "turn", rows: "3", maxlength: "20000", placeholder: c.chat ? "Write a message…" : "Write your turn…" },
    });
    text.value = draft ? draft.value : "";
    text.addEventListener("input", syncSendButton);
    const rows = [text];
    const controls = el("div", { cls: "row" });
    if (!c.chat) {
      const asking = S.choices.speaker === c.question;
      controls.appendChild(
        labelled(
          "Speaking as",
          select(c.speakers, S.choices.speaker, undefined, (v) => {
            S.choices.speaker = v;
            renderComposer();
          })
        )
      );
      if (!asking) {
        controls.appendChild(
          labelled("Length", select(c.lengths, S.choices.length, c.length_default, (v) => (S.choices.length = v)))
        );
        controls.appendChild(
          button(S.more ? "Fewer ▾" : "More ▸", () => {
            S.more = !S.more;
            renderComposer();
          })
        );
      }
    }
    // Suggest…: in a chat beside Send; in a story behind More, as on the desktop.
    if (c.chat && c.can_suggest) controls.appendChild(suggestButton());
    controls.appendChild(el("button", { cls: "send primary", text: "Send", attrs: { type: "submit", id: "send" } }));
    rows.push(controls);
    if (!c.chat && S.more && S.choices.speaker !== c.question) {
      const more = el("div", { cls: "row" });
      more.appendChild(
        labelled(
          "Outcome",
          select(c.outcomes, S.choices.outcome, c.outcome_default, (v) => {
            S.choices.outcome = v;
            renderComposer();
          })
        )
      );
      if (rolls()) {
        more.appendChild(labelled("Roll", select(c.skills, S.choices.skill, "General", (v) => (S.choices.skill = v))));
        more.appendChild(
          labelled(
            "Difficulty",
            select(c.difficulties, S.choices.difficulty || c.difficulty, undefined, (v) => (S.choices.difficulty = v))
          )
        );
      }
      if (c.can_suggest) more.appendChild(suggestButton());
      rows.push(more);
    }
    form.replaceChildren(renderStage(), renderSuggestions(), ...rows);
    if (focused) text.focus();
    syncSendButton();
    fitComposerSpace();
  }

  // The composer stands over the bottom of the page: the story keeps that
  // much room below its last message.
  function fitComposerSpace() {
    placeToast();
    const form = $("composer");
    const box = $("messages");
    if (form && box) box.style.paddingBottom = form.offsetHeight + 24 + "px";
    // A note being typed stays clear of the composer as it grows, or as the
    // phone's keyboard makes the page shorter.
    if (S.noting && document.activeElement === S.noting.field) revealNote();
  }

  function syncSendButton() {
    const send = $("send");
    const text = $("turn");
    if (!send || !text) return;
    const state = S.state || {};
    const writing = state.busy && !state.background;
    send.textContent = writing ? "Stop" : state.busy ? "Queue" : "Send";
    send.className = "send " + (writing ? "danger" : "primary");
    send.disabled = S.sending || (!writing && (!text.value.trim() || state.queued || state.desktop_waiting));
    const more = $("suggest");
    if (more) {
      more.textContent = S.asking ? "Asking…" : "Suggest…";
      more.disabled = S.asking !== null || Boolean(state.busy || state.queued || state.desktop_waiting);
    }
  }

  // --- Suggest… ------------------------------------------------------------------------------

  // A few things the author might write next, asked of the desktop and
  // offered above the box. They come to this page alone, over its events,
  // marked with the word it asked by; touching one only puts it in the box.
  function suggestButton() {
    const node = button("Suggest…", suggest);
    node.id = "suggest";
    return node;
  }

  function askWord() {
    const parts = new Uint32Array(2);
    crypto.getRandomValues(parts);
    return "ask-" + parts[0].toString(16) + parts[1].toString(16);
  }

  async function suggest() {
    if (S.asking) return;
    const ask = askWord();
    const failed = (words) => {
      if (S.asking === ask) S.asking = null;
      syncSendButton();
      if (words) toast(words, true);
    };
    S.asking = ask;
    syncSendButton();
    let answer;
    try {
      answer = await call("POST", "/api/suggest", { ask });
    } catch (e) {
      failed(S.ended ? "" : "The desktop couldn't be reached.");
      return;
    }
    if (!answer.ok) failed(messageOf(answer));
  }

  function renderSuggestions() {
    if (!S.suggestEl) S.suggestEl = el("div", { attrs: { id: "suggestions" } });
    const node = S.suggestEl;
    const offered = S.suggestions;
    if (!offered) {
      node.replaceChildren();
    } else if (hidden(offered.private)) {
      // Asked in a private scene, they are its text: held back as its messages are.
      node.replaceChildren(
        button(
          PRIVATE_SUGGESTIONS[offered.private] + " Tap to see them.",
          () => askToShow(offered.private),
          "placeholder"
        )
      );
    } else {
      const chat = S.composer && S.composer.chat;
      const away = button("✕", dismissSuggestions);
      away.setAttribute("aria-label", "Put the suggestions away");
      node.replaceChildren(
        el("div", { cls: "head" }, [
          el("span", {
            text: chat
              ? "Touch one to put it in the box, to edit and send:"
              : "Touch one to put it in the box, to edit and send as your own:",
          }),
          away,
        ]),
        el(
          "div",
          { cls: "list" },
          offered.options.map((option) => button(option, () => takeSuggestion(option)))
        )
      );
    }
    fitComposerSpace();
    return node;
  }

  function dismissSuggestions() {
    S.suggestions = null;
    renderSuggestions();
  }

  // Into the box, to change and send by hand: nothing here sends it.
  function takeSuggestion(words) {
    const c = S.composer;
    if (!c || !$("turn")) return;
    if (
      $("turn").value.trim() &&
      !window.confirm("The box already holds something you wrote. Replace it with this suggestion?")
    ) {
      return;
    }
    S.suggestions = null;
    // Spoken as the character played, whose turn it suggests.
    const held = !c.chat && c.held && (c.speakers || []).some((o) => o.value === c.held);
    if (held && S.choices.speaker !== c.held) {
      S.choices.speaker = c.held;
      renderComposer();
    }
    const box = $("turn");
    box.value = words;
    renderSuggestions();
    syncSendButton();
    box.focus();
  }

  async function send() {
    const state = S.state || {};
    if (state.busy && !state.background) {
      await call("POST", "/api/stop", {});
      return;
    }
    const text = $("turn");
    const words = text.value.trim();
    if (!words || !S.composer) return;
    const c = S.composer;
    const body = { text: words, speaker: c.chat ? c.speaker : S.choices.speaker };
    if (!c.chat && S.choices.speaker !== c.question) {
      if (S.choices.length) body.length = S.choices.length;
      if (S.choices.outcome) body.outcome = S.choices.outcome;
      if (rolls()) {
        if (S.choices.skill) body.skill = S.choices.skill;
        body.difficulty = S.choices.difficulty || c.difficulty;
      }
    }
    S.sending = true;
    syncSendButton();
    try {
      const answer = await call("POST", "/api/turn", body);
      if (answer.status === 202) {
        text.value = "";
        if (answer.data.queued) toast("Your turn goes once the desktop finishes reading the passage.");
      } else {
        toast(messageOf(answer), true);
      }
    } catch (e) {
      if (!S.ended) toast("The desktop couldn't be reached. Your turn is still in the box.", true);
    } finally {
      S.sending = false;
      syncSendButton();
    }
  }

  // Another take of a passage, shaped by the composer's Length and Outcome.
  function retakeBody(nodeId) {
    const body = { node_id: nodeId };
    if (S.choices.length) body.length = S.choices.length;
    if (S.choices.outcome) body.outcome = S.choices.outcome;
    return body;
  }

  async function regenerate(nodeId) {
    const answer = await call("POST", "/api/regenerate", retakeBody(nodeId));
    if (!answer.ok) toast(messageOf(answer), true);
  }

  async function continuePassage(nodeId) {
    const body = { node_id: nodeId };
    if (S.choices.length) body.length = S.choices.length;
    const answer = await call("POST", "/api/continue", body);
    if (!answer.ok) toast(messageOf(answer), true);
  }

  async function reroll(nodeId) {
    const answer = await call("POST", "/api/reroll", { node_id: nodeId });
    if (!answer.ok) toast(messageOf(answer), true);
  }

  async function switchTake(nodeId, step) {
    const answer = await call("POST", "/api/take", { node_id: nodeId, step });
    if (!answer.ok) toast(messageOf(answer), true);
  }

  // --- the menu -------------------------------------------------------------------------------

  function overlay(children) {
    closeOverlay();
    document.body.appendChild(
      el("div", { cls: "overlay", attrs: { id: "overlay" } }, [el("div", { cls: "sheet" }, children)])
    );
  }

  function closeOverlay() {
    const node = $("overlay");
    if (node) node.remove();
  }

  async function openMenu() {
    const parts = [el("h1", { text: "Stories" }), await storiesList()];
    if (S.state && S.state.shared && S.branches.length > 1) {
      parts.push(el("h1", { text: "Branches" }));
      const list = el("div", { cls: "list" });
      for (const branch of S.branches) {
        const name = "  ".repeat(branch.depth) + branch.name + " · " + branch.messages + " messages";
        list.appendChild(button(name, () => switchBranch(branch.start_id), branch.current ? "current" : ""));
      }
      parts.push(list);
    }
    parts.push(el("h1", { text: "This phone" }));
    parts.push(
      el("div", { cls: "row" }, [
        button("Forget this phone", forget, "danger"),
        button("Close", closeOverlay, "primary"),
      ])
    );
    // The version and where its source code is (the AGPL's offer to anyone
    // using it over a network), as the footer has them.
    const about = $("footer").cloneNode(true);
    about.removeAttribute("id");
    about.className = "about";
    parts.push(about);
    overlay(parts);
  }

  async function switchBranch(startId) {
    closeOverlay();
    const answer = await call("POST", "/api/branch", { start_id: startId });
    if (!answer.ok) toast(messageOf(answer), true);
  }

  async function forget() {
    if (!window.confirm("Forget this phone? It has to be paired again to play.")) return;
    await call("POST", "/api/forget", {});
    S.ended = true;
    stopStream();
    page([
      el("h1", { text: "This phone is forgotten" }),
      el("p", { text: "Delete the bookmark too: it no longer logs in." }),
    ]);
  }

  // --- the event stream ---------------------------------------------------------------------

  let streamController = null;

  function stopStream() {
    if (streamController) streamController.abort();
    streamController = null;
  }

  async function openStream() {
    stopStream();
    const controller = new AbortController();
    streamController = controller;
    try {
      const response = await fetch("/api/events", { headers: auth(), cache: "no-store", signal: controller.signal });
      if (response.status === 401) return notAllowed();
      if (!response.ok) throw new Error("status " + response.status);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (S.reconnects) {
          S.reconnects = 0;
          refreshAll();
        }
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          handleBlock(block);
        }
      }
    } catch (e) {
      if (controller.signal.aborted) return;
    }
    if (S.ended || controller !== streamController) return;
    const wait = RECONNECT_SECONDS[Math.min(S.reconnects, RECONNECT_SECONDS.length - 1)];
    S.reconnects += 1;
    setStatus("Reconnecting to the desktop…");
    setTimeout(openStream, wait * 1000);
  }

  function handleBlock(block) {
    let name = "";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) name = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (!name) return; // a heartbeat
    let value = {};
    try {
      value = data ? JSON.parse(data) : {};
    } catch (e) {
      return;
    }
    onEvent(name, value);
  }

  function onEvent(name, data) {
    switch (name) {
      case "hello":
      case "state": {
        const before = S.state || {};
        S.state = data;
        const storyBefore = before.story ? before.story.id : null;
        const storyNow = data.story ? data.story.id : null;
        if (storyBefore !== storyNow || before.shared !== data.shared) refreshAll();
        else {
          renderStatus();
          renderBanner();
          renderStage();
          fitComposerSpace();
          offerPrivate();
        }
        break;
      }
      case "changed":
        if (data.version !== S.version) {
          Promise.all([loadComposer(), loadLatest()]).then(render).catch(() => {});
        }
        break;
      case "turn":
        // The story moves on: what was offered was for the moment before.
        if (data.kind !== "question" && S.suggestions) dismissSuggestions();
        S.messages.push({
          id: "pending-" + Date.now(),
          kind: data.kind === "question" ? "director" : data.kind,
          label: data.label + (data.by === "phone" ? " · from a phone" : ""),
          text: data.text,
          private: S.state ? S.state.private : null,
        });
        renderMessages();
        break;
      case "job_started":
        if (data.continues) startContinuing(data.continues);
        else if (data.revises) startRevising(data.revises);
        else if (data.streaming) writingCard(data.question ? "Answer" : S.composer && S.composer.chat ? "Assistant" : "Story");
        break;
      case "thinking":
        if (S.writing && !S.writing.words) S.writing.label.textContent = "Thinking…";
        else if (S.continuing && !S.continuing.words && S.continuing.label) S.continuing.label.textContent = "Thinking…";
        else if (S.revising && !S.revising.words && S.revising.label) S.revising.label.textContent = "Thinking…";
        break;
      case "text":
        if (S.continuing) {
          S.continuing.words += data.text;
          if (S.continuing.label) S.continuing.label.textContent = S.continuing.labelText;
          showContinuing();
          if (nearBottom()) scrollToEnd();
          break;
        }
        if (S.revising) {
          S.revising.words += data.text;
          if (S.revising.label) S.revising.label.textContent = S.revising.labelText;
          showRevising();
          break;
        }
        if (!S.writing) writingCard("Story");
        S.writing.words += data.text;
        S.writing.label.textContent = S.writing.labelText;
        showWriting();
        if (nearBottom()) scrollToEnd();
        break;
      case "suggestions":
        // This page's own asking only: another page of this phone has its own word.
        if (!S.asking || data.ask !== S.asking) break;
        S.asking = null;
        S.suggestions = (data.options || []).length
          ? { options: data.options, private: data.private || null }
          : null;
        renderSuggestions();
        syncSendButton();
        offerPrivate();
        break;
      case "notice":
        toast(data.text);
        break;
      case "failed":
        toast("It failed: " + data.error, true);
        break;
      case "queued_returned": {
        const text = $("turn");
        if (text && !text.value.trim()) text.value = data.text;
        toast("Your queued turn didn't go: it's back in the box to send again.");
        syncSendButton();
        break;
      }
      case "revoked":
      case "forgotten":
        notAllowed();
        break;
      case "stopped":
        S.asking = null;
        syncSendButton();
        setStatus("Phone access was turned off on the desktop.");
        break;
      default:
        break;
    }
  }

  // --- start -----------------------------------------------------------------------------------

  window.addEventListener("resize", () => fitComposerSpace());

  function boot() {
    const hash = location.hash;
    if (hash.startsWith("#pair=")) return pairPage(decodeURIComponent(hash.slice(6)));
    if (hash.startsWith("#k=")) {
      S.token = decodeURIComponent(hash.slice(3));
      return start();
    }
    return noKey();
  }

  boot();
})();
