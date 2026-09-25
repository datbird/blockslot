// BlockSlot server UI. Plain JS, no build step and nothing from the internet.
// Every piece of text from the store goes in through textContent (see h()),
// never innerHTML, because game names and device names come from devices.
"use strict";

const state = { session: null, games: null, filter: "all", query: "", guideChecked: false };

// ------------------------------------------------------------------ helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVG = "http://www.w3.org/2000/svg";
function cube(kind) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 26 26");
  svg.setAttribute("class", "cube" + (kind ? " " + kind : ""));
  svg.setAttribute("aria-hidden", "true");
  for (const [cls, pts] of [["top", "13,2 23,8 13,14 3,8"], ["left", "3,8 13,14 13,25 3,19"],
                            ["right", "13,14 23,8 23,19 13,25"]]) {
    const p = document.createElementNS(SVG, "polygon");
    p.setAttribute("class", cls);
    p.setAttribute("points", pts);
    svg.append(p);
  }
  return svg;
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET" && state.session && state.session.csrf) {
    opts.headers["X-CSRF-Token"] = state.session.csrf;
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("The server did not answer. Check that it is running.");
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* an empty body */ }
  if (res.status === 401 && path !== "/api/login") {
    state.session = null;
    route();
    throw new Error(data.error || "Sign in first.");
  }
  if (!res.ok) throw new Error(data.error || ("The server answered " + res.status + "."));
  return data;
}

function toast(text, bad) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.className = "show" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = bad ? "bad" : ""; }, bad ? 6000 : 3000);
}

function ask(title, text, yes, danger) {
  const dlg = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-text").textContent = text;
  const btn = document.getElementById("confirm-yes");
  btn.textContent = yes;
  btn.className = "btn " + (danger ? "danger" : "primary");
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    dlg.showModal();
  });
}

function fmtTime(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric",
                                       hour: "numeric", minute: "2-digit" });
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(s)) return "";
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  if (s < 86400 * 60) return Math.round(s / 86400) + " days ago";
  return Math.round(s / 86400 / 30) + " months ago";
}

function fmtBytes(n) {
  if (n === null || n === undefined) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + units[i];
}

// "6:08 AM to 7:38 AM" on one day, full dates when it crossed midnight.
function playedRange(start, end) {
  if (!end) return "not recorded";
  if (!start) return "until " + fmtTime(end);
  const a = new Date(start), b = new Date(end);
  if (a.toDateString() !== b.toDateString()) return fmtTime(start) + " to " + fmtTime(end);
  const t = (d) => d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return t(a) + " to " + t(b);
}

function playTime(start, end) {
  if (!start || !end) return null;
  const mins = Math.round((new Date(end) - new Date(start)) / 60000);
  if (!(mins >= 0)) return null;
  if (mins < 60) return mins + " min";
  return Math.floor(mins / 60) + " h " + (mins % 60) + " min";
}

function busy(btn, fn) {
  return async (ev) => {
    if (ev) ev.preventDefault();
    btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  };
}

function field(label, input, cls) {
  return h("label", { class: "field" + (cls ? " " + cls : "") }, h("span", {}, label), input);
}

function errorPanel(message) {
  return h("div", { class: "error-box" }, message);
}

// ------------------------------------------------------------------ shell

const PAGES = [
  ["games", "Games"], ["devices", "Devices"], ["settings", "Settings"],
  ["storage", "Storage"], ["account", "Account"],
];

function wordmark() {
  return h("span", { class: "wordmark" }, "Block", h("span", {}, "Slot"));
}

function shell(active, content) {
  const nav = h("nav", { class: "nav", "aria-label": "Sections" },
    PAGES.map(([id, name]) => h("a", { href: "#/" + id, class: id === active ? "on" : "",
                                       "aria-current": id === active ? "page" : null },
                                h("span", { class: "dot" }), name)));
  const out = h("button", { class: "btn ghost small", type: "button" }, "Sign out");
  out.addEventListener("click", busy(out, async () => {
    await api("POST", "/api/logout");
    state.session = null;
    route();
  }));
  return h("div", { class: "shell" },
    h("aside", { class: "rail" },
      h("a", { class: "brand", href: "#/games" }, h("img", { src: "/static/icon.svg", alt: "" }), wordmark()),
      nav,
      h("div", { class: "rail-foot" },
        h("a", { class: "guide-link" + (active === "start" ? " on" : ""), href: "#/start",
                 "aria-current": active === "start" ? "page" : null }, cube("small"), "Getting started"),
        h("div", { class: "who muted", title: state.session.user }, state.session.user), out)),
    h("main", { class: "main", id: "main" }, content));
}

function pageHead(label, title, extra) {
  return h("div", { class: "page-head" },
    h("div", {}, h("div", { class: "label orange" }, label), h("h1", {}, title)), extra || null);
}

function mount(node) {
  const root = document.getElementById("root");
  root.replaceChildren(node);
}

// replaceChildren would write a null as the text "null".
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}

// "Skip for now" holds for this tab until the next sign-in. Only a
// convenience: storage can be missing, and then the guide simply opens again.
const SKIP_KEY = "blockslot-guide-skipped";
function guideSkipped(set) {
  try {
    if (set === true) sessionStorage.setItem(SKIP_KEY, "1");
    else if (set === false) sessionStorage.removeItem(SKIP_KEY);
    else return sessionStorage.getItem(SKIP_KEY) === "1";
  } catch (e) { /* no storage here */ }
  return false;
}

// ------------------------------------------------------------------ sign in

function gate(firstRun, cfAccess) {
  const user = h("input", { type: "text", autocomplete: "username", required: true, autofocus: true });
  const pass = h("input", { type: "password", required: true,
                            autocomplete: firstRun ? "new-password" : "current-password" });
  const pass2 = firstRun ? h("input", { type: "password", required: true, autocomplete: "new-password" }) : null;
  const err = h("div", {});
  const btn = h("button", { class: "btn primary", type: "submit" },
                firstRun ? "Create the admin account" : "Sign in");
  const form = h("form", {},
    firstRun ? h("div", { class: "gate-intro" },
      h("p", {}, "BlockSlot keeps game saves in step between your PCs and your Steam Deck. This server is where the saves live."),
      h("p", { class: "hint" }, "Start with the admin account. It can manage everything here. Setting up devices comes next.")) : null,
    field("Username", user), field("Password", pass),
    firstRun ? field("Password again", pass2) : null, err, btn,
    !firstRun && cfAccess ? h("p", { class: "hint" }, "Signed in to Cloudflare Access? Open this page through its Cloudflare address to skip this.") : null);
  form.addEventListener("submit", busy(btn, async () => {
    err.replaceChildren();
    if (firstRun && pass.value !== pass2.value) {
      err.replaceChildren(errorPanel("The two passwords are different."));
      return;
    }
    try {
      const data = await api("POST", firstRun ? "/api/setup" : "/api/login",
                             { username: user.value, password: pass.value });
      state.session = data;
      state.guideChecked = false;
      guideSkipped(false);
      location.hash = firstRun ? "#/start" : "#/games";
      route();
    } catch (e) {
      err.replaceChildren(errorPanel(e.message));
    }
  }));
  mount(h("div", { class: "gate" }, h("div", { class: "gate-card" },
    h("div", { class: "gate-brand" }, h("img", { src: "/static/icon.svg", alt: "" }), wordmark(),
      firstRun ? h("div", { class: "gate-steps" }, h("div", { class: "label orange" }, "Getting started, step 1 of 5"),
                   stepCubes(0)) : h("div", { class: "label orange" }, "Save server")),
    h("div", { class: "panel" }, form))));
}

// ------------------------------------------------------------------ games

async function gamesPage(refresh) {
  const list = h("div", {}, h("p", { class: "muted" }, "Reading the store..."));
  const search = h("input", { type: "search", placeholder: "Search games", value: state.query,
                              "aria-label": "Search games" });
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Show" },
    [["all", "All"], ["games", "Games"], ["library", "Library games"], ["two", "Two saves"]].map(([id, name]) => {
      const b = h("button", { type: "button", class: state.filter === id ? "on" : "" }, name);
      b.addEventListener("click", () => {
        state.filter = id;
        seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        draw();
      });
      return b;
    }));
  const reload = h("button", { class: "btn small ghost", type: "button" }, "Reload");
  reload.addEventListener("click", () => gamesPage(true));
  search.addEventListener("input", () => { state.query = search.value; draw(); });
  mount(shell("games", [pageHead("Save browser", "Games", reload),
                        h("div", { class: "toolbar" }, search, seg), list]));

  function draw() {
    if (!state.games) return;
    const q = state.query.trim().toLowerCase();
    const rows = state.games.filter((g) => {
      if (state.filter === "games" && g.library) return false;
      if (state.filter === "library" && !g.library) return false;
      if (state.filter === "two" && g.heads < 2) return false;
      if (!q) return true;
      return (g.title + " " + (g.label || "") + " " + (g.library || "")).toLowerCase().includes(q);
    });
    if (!state.games.length) {
      list.replaceChildren(h("div", { class: "panel empty welcome" }, cube("big"),
        h("h2", {}, "No saves on the store yet"),
        h("p", {}, "A game shows here after a device plays it and closes it. Add a device first, then play something."),
        h("div", { class: "row center" },
          h("a", { class: "btn primary", href: "#/devices" }, "Add a device"),
          h("a", { class: "btn", href: "#/start" }, "Open the getting started guide"))));
      return;
    }
    const shown = rows.slice(0, 400);
    list.replaceChildren(
      h("p", { class: "count" }, rows.length === state.games.length ? state.games.length + " games"
        : rows.length + " of " + state.games.length + " games"),
      rows.length ? h("ul", { class: "game-list" }, shown.map((g) => h("li", {},
        h("a", { class: "game-row", href: "#/game/" + encodeURIComponent(g.key) },
          cube(g.heads > 1 ? "warm" : (g.library ? "lib" : "")),
          h("div", {},
            h("div", { class: "title" }, g.title),
            h("div", { class: "meta" },
              g.label ? h("span", { class: "chip teal" }, g.label) : null,
              g.library ? h("span", { class: "muted small" }, g.library) : null,
              g.heads > 1 ? h("span", { class: "chip orange" }, "Two saves") : null,
              g.uploading ? h("span", { class: "chip" }, "Uploading") : null)),
          h("div", { class: "when" }, h("span", { class: "mono" }, ago(g.when)),
            h("span", { class: "muted" }, "on " + who(g.device))))))) :
        h("div", { class: "panel empty" }, h("p", {}, "No game matches that.")),
      rows.length > shown.length ? h("p", { class: "count" }, "Showing the newest 400. Search to find the rest.") : "");
  }

  try {
    const names = loadDeviceNames(refresh);
    if (!state.games || refresh) state.games = (await api("GET", "/api/games" + (refresh ? "?refresh=1" : ""))).games;
    await names;
    draw();
  } catch (e) {
    list.replaceChildren(errorPanel(e.message));
  }
}

// Device display names from the Devices page. Snapshots carry only the
// device id; a person knows "Steam Deck", not "steam-deck".
const deviceNames = { server: "BlockSlot server" };
let namesLoaded = false;

async function loadDeviceNames(force) {
  if (namesLoaded && !force) return;
  try {
    const d = await api("GET", "/api/devices");
    for (const x of d.devices) deviceNames[x.device] = x.name || x.device;
    namesLoaded = true;
  } catch (e) { /* the ids still show */ }
}

function who(id) { return deviceNames[id] || id; }

async function gamePage(key) {
  const body = h("div", {}, h("p", { class: "muted" }, "Reading the history..."));
  mount(shell("games", [h("a", { class: "back", href: "#/games" }, "All games"), body]));
  let g;
  try {
    const names = loadDeviceNames();
    g = await api("GET", "/api/games/" + encodeURIComponent(key) + "?refresh=1");
    await names;
  } catch (e) {
    body.replaceChildren(errorPanel(e.message));
    return;
  }
  const byId = Object.fromEntries(g.history.map((s) => [s.id, s]));
  const onlyHead = g.heads.length === 1 ? g.heads[0] : null;

  async function doRestore(snap, settle) {
    const s = byId[snap];
    const ok = await ask(settle ? "Keep this save?" : "Restore this save?",
      settle ? "The save from " + who(s.device) + " (" + fmtTime(s.played_end || s.created) + ") becomes the one save. " +
               "Each device restores it at its next launch. The other save stays in the history."
             : "The save from " + who(s.device) + " (" + fmtTime(s.played_end || s.created) + ") becomes the current save. " +
               "Each device restores it at its next launch. Nothing is deleted.",
      settle ? "Keep this save" : "Restore this save");
    if (!ok) return;
    try {
      await api("POST", "/api/games/" + encodeURIComponent(key) + (settle ? "/settle" : "/restore"), { snapshot: snap });
      state.games = null;
      toast(settle ? "Settled. Devices pick it up at their next launch." : "Restored. Devices pick it up at their next launch.");
      gamePage(key);
    } catch (e) { toast(e.message, true); }
  }

  const fork = g.heads.length > 1 ? h("div", { class: "panel fork" },
    h("div", { class: "panel-head" }, h("h2", {}, "Two saves"), h("span", { class: "chip orange" }, g.heads.length + " saves")),
    h("p", { class: "muted" }, "More than one device played this game without the other's save. Pick the one to keep. The other stays in the history."),
    h("div", { class: "choices" }, g.heads.map((id) => {
      const s = byId[id];
      const b = h("button", { class: "btn warn small", type: "button" }, "Keep " + who(s.device) + "'s save");
      b.addEventListener("click", () => doRestore(id, true));
      return h("div", { class: "choice" },
        h("div", { class: "choice-device" }, who(s.device)),
        h("div", { class: "mono small" }, fmtTime(s.played_end || s.created)),
        playTime(s.played_start, s.played_end) ? h("div", { class: "muted small" }, "Played " + playTime(s.played_start, s.played_end)) : null,
        b);
    }))) : null;

  const chain = h("ol", { class: "chain" }, g.history.map((s) => {
    const actions = h("div", { class: "actions" });
    if (s.id !== onlyHead) {
      const b = h("button", { class: "btn small", type: "button" }, "Restore this save");
      b.addEventListener("click", () => doRestore(s.id, false));
      actions.append(b);
    }
    actions.append(h("a", { class: "btn small ghost", download: "",
      href: "/api/games/" + encodeURIComponent(key) + "/snapshots/" + encodeURIComponent(s.id) + "/zip" }, "Download as zip"));
    const played = playTime(s.played_start, s.played_end);
    const from = s.restored_from && byId[s.restored_from];
    return h("li", {},
      h("div", { class: "node" }, cube(s.head ? "warm" : "")),
      h("div", { class: "slot" + (s.head ? " head" : "") },
        h("div", { class: "slot-top" },
          h("div", {}, h("div", { class: "row" }, h("span", { class: "device" }, who(s.device)),
                         s.head ? h("span", { class: "chip orange" }, g.heads.length > 1 ? "Undecided" : "Current") : null),
            h("div", { class: "mono small muted" }, fmtTime(s.created))),
          actions),
        h("div", { class: "facts" },
          h("div", {}, h("div", { class: "label" }, "Played"),
            h("div", { class: "mono" }, playedRange(s.played_start, s.played_end))),
          h("div", {}, h("div", { class: "label" }, "Play time"), h("div", { class: "mono" }, played || "unknown")),
          h("div", {}, h("div", { class: "label" }, "Size"),
            h("div", { class: "mono" }, fmtBytes(s.bytes) + ", " + s.files + (s.files === 1 ? " file" : " files")))),
        s.restored_from ? h("p", { class: "note" }, "Restored here from the save of " +
          (from ? who(from.device) + " at " + fmtTime(from.created) : s.restored_from) + ".") :
        (s.merge ? h("p", { class: "note" }, "Settled two saves into this one.") : null),
        s.imported ? h("p", { class: "note" }, "Imported from the Syncthing folder.") : null));
  }));

  body.replaceChildren(
    pageHead(g.library ? "Library game in " + g.library : "Game", g.title,
      g.label ? h("span", { class: "chip teal" }, g.label) : null),
    h("div", { class: "stack" }, fork,
      h("div", { class: "panel-head" }, h("h2", {}, "History"),
        h("span", { class: "muted small" }, g.history.length + (g.history.length === 1 ? " save" : " saves"))),
      chain));
}

// ------------------------------------------------------------------ settings

function libraryCard(name, lib, onRemove) {
  const kind = lib.one_game ? "one" : "library";
  const nameIn = h("input", { type: "text", value: name, placeholder: "RetroFrontend" });
  const kindSel = h("select", {},
    h("option", { value: "library" }, "A library of many games"),
    h("option", { value: "one" }, "One game"));
  kindSel.value = kind;
  const oneGame = h("input", { type: "text", value: lib.one_game || "", placeholder: "The game's name" });
  const system = h("input", { type: "text", value: lib.system || "", placeholder: "Optional, such as ps4" });
  const label = h("input", { type: "text", value: lib.label || "", placeholder: "Optional, found from the system" });
  const exts = h("input", { type: "text", placeholder: "srm, sav, mcr  or  *",
    value: lib.extensions === "*" ? "*" : (lib.extensions || []).join(", ") });
  const aliases = h("textarea", { rows: 3, placeholder: "gc gamecube\nsg-1000 sg1000" },
    (lib.system_aliases || []).map((g) => g.join(" ")).join("\n"));
  const always = h("input", { type: "text", value: (lib.always_dirs || []).join(", "), placeholder: "mame, mame-sa" });
  const oneField = field("Game name", oneGame);
  const sync = () => { oneField.hidden = kindSel.value !== "one"; };
  kindSel.addEventListener("change", sync);
  sync();
  const remove = h("button", { class: "btn danger small", type: "button" }, "Remove");
  const card = h("div", { class: "lib-card" },
    h("div", { class: "panel-head" }, h("h3", {}, name || "New library"), remove),
    h("div", { class: "grid-3" },
      field("Name", nameIn), field("Holds", kindSel), oneField,
      field("System", system), field("Emulator label", label),
      field("Save file extensions", exts),
      field("Same system, different names (one group per line)", aliases, "span-2"),
      field("Folders where every file is a save", always)));
  remove.addEventListener("click", () => { card.remove(); onRemove && onRemove(); });
  card.collect = () => {
    const n = nameIn.value.trim();
    if (!n) return null;
    const out = {};
    if (kindSel.value === "one" && oneGame.value.trim()) out.one_game = oneGame.value.trim();
    if (system.value.trim()) out.system = system.value.trim();
    if (label.value.trim()) out.label = label.value.trim();
    const e = exts.value.trim();
    if (e === "*") out.extensions = "*";
    else if (e) out.extensions = e.split(/[\s,]+/).filter(Boolean);
    const groups = aliases.value.split("\n").map((l) => l.split(/[\s,]+/).filter(Boolean)).filter((g) => g.length > 1);
    if (groups.length) out.system_aliases = groups;
    const a = always.value.split(/[\s,]+/).filter(Boolean);
    if (a.length) out.always_dirs = a;
    return [n, out];
  };
  return card;
}

async function settingsPage() {
  const body = h("div", {}, h("p", { class: "muted" }, "Reading the settings..."));
  mount(shell("settings", [pageHead("Shared by every device", "Settings"), body]));
  let data;
  try { data = await api("GET", "/api/settings/shared"); } catch (e) { body.replaceChildren(errorPanel(e.message)); return; }
  const s = data.settings;

  const cards = h("div", {});
  for (const [name, lib] of Object.entries(s.libraries || {})) cards.append(libraryCard(name, lib));
  const add = h("button", { class: "btn small", type: "button" }, "Add a library");
  add.addEventListener("click", () => cards.append(libraryCard("", {})));

  const perDevice = h("input", { type: "number", min: 1, max: 1000, value: s.retention.per_device });
  const days = h("input", { type: "number", min: 0, max: 3650, value: s.retention.days });
  const save = h("button", { class: "btn primary", type: "button" }, "Save settings");
  save.addEventListener("click", busy(save, async () => {
    const libraries = {};
    for (const c of cards.children) {
      const got = c.collect();
      if (got) libraries[got[0]] = got[1];
    }
    await api("PUT", "/api/settings/shared", { libraries,
      retention: { per_device: parseInt(perDevice.value, 10), days: parseInt(days.value, 10) } });
    toast("Settings saved. Devices read them within 5 minutes.");
    settingsPage();
  }));

  let importPanel = null;
  if (!data.exists) {
    const text = h("textarea", { rows: 8, placeholder: "{\"trees\": { ... }}" });
    const go = h("button", { class: "btn primary", type: "button" }, "Import");
    go.addEventListener("click", busy(go, async () => {
      const got = await api("POST", "/api/settings/import", { text: text.value });
      toast("Imported " + Object.keys(got.settings.libraries).length + " libraries and the folders of " +
            got.devices.length + " devices.");
      settingsPage();
    }));
    importPanel = h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Start from a device's settings")),
      h("p", { class: "muted" }, "Paste the trees section of a device's savepick.json. Its libraries land here, and each device's folders on the Devices page."),
      h("div", {}, text), h("div", { class: "row-end" }, go));
  }

  body.replaceChildren(h("div", { class: "stack" },
    importPanel,
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Libraries and emulator games"), add),
      h("p", { class: "hint" }, "A library is an emulator's saves folder, split into one save per game. " +
        "One game is a folder that holds a single game's save, such as Bloodborne in shadPS4. " +
        "Each device sets where the folder is on the Devices page."),
      h("div", { class: "stack" }, cards)),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Keeping old saves")),
      h("p", { class: "hint" }, "The newest saves of each device and every save newer than the days below are kept. The current save always is."),
      h("div", { class: "grid-2" }, field("Saves kept per device, per game", perDevice), field("Keep every save newer than (days)", days))),
    h("div", { class: "row-end" }, save)));
}

// ------------------------------------------------------------------ devices

async function devicesPage() {
  const body = h("div", {}, h("p", { class: "muted" }, "Reading devices..."));
  mount(shell("devices", [pageHead("Keys and folders", "Devices"), body]));
  let data, shared;
  try {
    [data, shared] = await Promise.all([api("GET", "/api/devices"), api("GET", "/api/settings/shared")]);
  } catch (e) { body.replaceChildren(errorPanel(e.message)); return; }
  const libraries = Object.keys(shared.settings.libraries || {});

  const nameIn = h("input", { type: "text", placeholder: "deck, laptop, desktop" });
  const addBtn = h("button", { class: "btn primary", type: "submit" }, "Add device");
  const codeOut = h("div", { class: "code-out" });
  const addForm = h("form", { class: "row" }, h("div", { class: "field" }, nameIn), addBtn);
  addForm.addEventListener("submit", busy(addBtn, async () => {
    const got = await api("POST", "/api/devices", { name: nameIn.value });
    codeOut.replaceChildren(deviceResult(got, nameIn.value.trim() || got.device));
    nameIn.value = "";
    refreshList();
  }));

  const list = h("div", { class: "stack device-list" });
  function refreshList() {
    namesLoaded = false;
    api("GET", "/api/devices").then((d) => { data = d; drawList(); }).catch((e) => toast(e.message, true));
  }
  function drawList() {
    if (!data.devices.length) {
      list.replaceChildren(h("div", { class: "panel empty" }, h("p", {}, "No devices yet. Add one above.")));
      return;
    }
    list.replaceChildren(...data.devices.map((d) => {
      const edit = h("button", { class: "btn small", type: "button" }, "Folders");
      const remove = d.has_key ? h("button", { class: "btn small danger", type: "button" }, "Remove") : null;
      const pairBtn = d.has_key ? h("button", { class: "btn small ghost", type: "button",
        title: d.pair_pending ? null : "Works while the last pairing code is still waiting" }, "New pairing code") : null;
      if (pairBtn) pairBtn.addEventListener("click", busy(pairBtn, async () => {
        const got = await api("POST", "/api/devices/" + encodeURIComponent(d.device) + "/pair-code", {});
        codeOut.replaceChildren(deviceResult(got, d.name));
        codeOut.scrollIntoView({ block: "nearest" });
      }));
      const editor = h("div", { hidden: true });
      edit.addEventListener("click", () => {
        editor.hidden = !editor.hidden;
        if (editor.hidden) return;
        const name = h("input", { type: "text", value: d.name });
        const roots = {};
        const names = Array.from(new Set(libraries.concat(Object.keys(d.roots || {}))));
        const save = h("button", { class: "btn primary small", type: "button" }, "Save");
        save.addEventListener("click", busy(save, async () => {
          const out = {};
          for (const [lib, input] of Object.entries(roots)) if (input.value.trim()) out[lib] = input.value.trim();
          await api("PUT", "/api/devices/" + encodeURIComponent(d.device) + "/settings", { name: name.value, roots: out });
          toast("Saved. " + d.name + " reads it within 5 minutes.");
          refreshList();
        }));
        editor.replaceChildren(h("div", { class: "stack" },
          h("div", { class: "grid-2" }, field("Display name", name),
            ...names.map((lib) => { roots[lib] = h("input", { type: "text", value: (d.roots || {})[lib] || "",
                                                              placeholder: "Not on this device" });
                                    return field(lib + " folder", roots[lib]); })),
          names.length ? null : h("p", { class: "hint" }, "No libraries yet. Add them on the Settings page."),
          h("p", { class: "hint" }, "The device's own app can set these too. The newest change wins."),
          h("div", { class: "row-end" }, save)));
      });
      if (remove) remove.addEventListener("click", async () => {
        const ok = await ask("Remove " + d.name + "?", "Its key stops working at once" +
          (d.cloudflare ? ", and its Cloudflare token is deleted" : "") + ". Its saves stay on the store.", "Remove", true);
        if (!ok) return;
        try { await api("DELETE", "/api/devices/" + encodeURIComponent(d.device)); toast("Removed " + d.name + "."); refreshList(); }
        catch (e) { toast(e.message, true); }
      });
      return h("div", { class: "panel" },
        h("div", { class: "device-row" }, cube(d.has_key ? "" : "lib"),
          h("div", {}, h("div", { class: "row" }, h("strong", {}, d.name),
                         d.name !== d.device ? h("span", { class: "mono small muted" }, d.device) : null),
            h("div", { class: "row small muted" },
              h("span", {}, d.last_save ? "Last save " + ago(d.last_save) : "No saves yet"),
              d.has_key ? h("span", { class: "chip teal" }, "Key made here") : h("span", { class: "chip" }, "Key made elsewhere"),
              d.cloudflare ? h("span", { class: "chip teal" }, "Cloudflare token") : null)),
          h("div", { class: "row" }, pairBtn, edit, remove)),
        editor);
    }));
  }
  drawList();

  const endpoint = h("input", { type: "text", value: data.public_endpoint_set ? data.public_endpoint : "",
                                placeholder: data.public_endpoint });
  const saveEndpoint = h("button", { class: "btn small", type: "button" }, "Save address");
  saveEndpoint.addEventListener("click", busy(saveEndpoint, async () => {
    await api("PUT", "/api/endpoint", { endpoint: endpoint.value });
    toast("Saved. New setup codes use it.");
  }));

  const cf = data.cloudflare;
  const cfToken = h("input", { type: "password", autocomplete: "off",
                               placeholder: cf.api_token_set ? "Saved. Type a new one to replace it." : "Cloudflare API token" });
  const cfAccount = h("input", { type: "text", value: cf.account_id });
  const cfApp = h("input", { type: "text", value: cf.app_id });
  const cfSave = h("button", { class: "btn small", type: "button" }, "Save Cloudflare settings");
  cfSave.addEventListener("click", busy(cfSave, async () => {
    const body = { account_id: cfAccount.value, app_id: cfApp.value };
    if (cfToken.value) body.api_token = cfToken.value;
    await api("PUT", "/api/cloudflare", body);
    cfToken.value = "";
    toast("Saved. New devices get a Cloudflare token too.");
  }));

  body.replaceChildren(h("div", { class: "stack" },
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Add a device")),
      h("p", { class: "hint" }, "Each device gets its own key, so a lost one is one removal."),
      h("div", {}, addForm), codeOut),
    list,
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Address devices use")),
      h("p", { class: "hint" }, "Where devices reach the store. Leave it empty for this server's own address on port 3900."),
      h("div", { class: "row" }, h("div", { class: "field" }, endpoint), saveEndpoint)),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Cloudflare service tokens"), h("span", { class: "muted small" }, "Optional")),
      h("p", { class: "hint" }, "When the store sits behind a Cloudflare Access app, each new device also gets a service token, added to that app's Service Auth policy."),
      h("div", { class: "grid-3" }, field("API token", cfToken), field("Account ID", cfAccount), field("Access app ID", cfApp)),
      h("div", { class: "row-end" }, cfSave))));
}

// ------------------------------------------------------------------ storage

async function storagePage() {
  const body = h("div", {}, h("p", { class: "muted" }, "Measuring the store..."));
  mount(shell("storage", [pageHead("Bucket blockslot", "Storage"), body]));
  let s;
  try { s = await api("GET", "/api/storage"); } catch (e) { body.replaceChildren(errorPanel(e.message)); return; }
  const b = s.bucket || {};
  const stat = (label, value, sub) => h("div", { class: "stat" }, h("div", { class: "label" }, label),
                                        h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);

  const result = h("div", {});
  const preview = h("button", { class: "btn", type: "button" }, "Preview clean-up");
  const run = h("button", { class: "btn warn", type: "button", disabled: true }, "Clean now");
  const show = (r, done) => result.replaceChildren(
    r.skipped ? h("p", { class: "muted" }, "A device is uploading right now, so nothing was touched. Try again in an hour.")
              : h("p", {}, (done ? "Removed " : "Would remove ") + r.removed_snapshots + " old saves and " +
                          r.removed_blobs + " files nothing uses any more."),
    r.log && r.log.length ? h("div", { class: "log" }, r.log.join("\n")) : null);
  preview.addEventListener("click", busy(preview, async () => {
    const r = await api("POST", "/api/storage/clean", { dry_run: true });
    show(r, false);
    run.disabled = r.skipped;
  }));
  run.addEventListener("click", busy(run, async () => {
    const r = await api("POST", "/api/storage/clean", { dry_run: false });
    show(r, true);
    run.disabled = true;
    toast("Clean-up finished.");
  }));

  const keeper = h("input", { type: "checkbox", checked: s.keeper });
  keeper.addEventListener("change", async () => {
    try {
      await api("PUT", "/api/storage/keeper", { keeper: keeper.checked });
      toast(keeper.checked ? "This server now cleans the store once a day." : "This server no longer cleans the store.");
    } catch (e) { keeper.checked = !keeper.checked; toast(e.message, true); }
  });

  body.replaceChildren(h("div", { class: "stack" },
    h("div", { class: "stats" },
      stat("Stored", b.error ? "unknown" : fmtBytes(b.bytes), b.error ? b.error : null),
      stat("Objects", b.error || b.objects === undefined ? "unknown" : String(b.objects)),
      stat("Games", String(s.games), s.snapshots + " saves"),
      stat("Newest upload", s.newest_upload ? ago(s.newest_upload) : "none", s.newest_upload ? fmtTime(s.newest_upload) : null)),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Clean-up")),
      h("p", { class: "muted" }, "Keeps the current save of every game, the newest " + s.retention.per_device +
        " saves of each device, and every save from the last " + s.retention.days + " days. Change these on the Settings page."),
      h("div", { class: "row-end" }, preview, run), result),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Keeper")),
      h("label", { class: "switch" }, keeper, h("span", {}, "This server cleans the store once a day")),
      h("p", { class: "hint" }, "Only one keeper per store. Turn it off on any device that had the job. " +
        (s.last_clean ? "Last clean-up " + fmtTime(s.last_clean) + "." : "No clean-up has run here yet.")))));
}

// ------------------------------------------------------------------ account

async function accountPage() {
  const body = h("div", {}, h("p", { class: "muted" }, "Loading..."));
  mount(shell("account", [pageHead("Signed in as " + state.session.user, "Account"), body]));
  let users, access;
  try {
    [users, access] = await Promise.all([api("GET", "/api/users"), api("GET", "/api/access")]);
  } catch (e) { body.replaceChildren(errorPanel(e.message)); return; }

  let passwordPanel;
  if (state.session.via === "password") {
    const cur = h("input", { type: "password", autocomplete: "current-password" });
    const nw = h("input", { type: "password", autocomplete: "new-password" });
    const nw2 = h("input", { type: "password", autocomplete: "new-password" });
    const go = h("button", { class: "btn primary small", type: "button" }, "Change password");
    go.addEventListener("click", busy(go, async () => {
      if (nw.value !== nw2.value) throw new Error("The two new passwords are different.");
      await api("POST", "/api/account/password", { current: cur.value, new: nw.value });
      cur.value = nw.value = nw2.value = "";
      toast("Password changed. Other sessions of this account are signed out.");
    }));
    passwordPanel = h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Your password")),
      h("div", { class: "grid-3" }, field("Current", cur), field("New", nw), field("New again", nw2)),
      h("div", { class: "row-end" }, go));
  } else {
    passwordPanel = h("div", { class: "panel" }, h("p", { class: "muted" },
      "You signed in through Cloudflare Access, so there is no password to change here."));
  }

  const userList = h("div", {});
  function drawUsers(list) {
    userList.replaceChildren(...list.map((u) => {
      const reset = h("button", { class: "btn small ghost", type: "button" }, "Set password");
      reset.addEventListener("click", async () => {
        const pw = window.prompt("New password for " + u.username + " (at least 8 characters)");
        if (!pw) return;
        try { await api("PUT", "/api/users/" + encodeURIComponent(u.username) + "/password", { password: pw }); toast("Password set."); }
        catch (e) { toast(e.message, true); }
      });
      const del = h("button", { class: "btn small danger", type: "button" }, "Remove");
      del.addEventListener("click", async () => {
        if (!(await ask("Remove " + u.username + "?", "They are signed out at once.", "Remove", true))) return;
        try { drawUsers((await api("DELETE", "/api/users/" + encodeURIComponent(u.username))).users); }
        catch (e) { toast(e.message, true); }
      });
      return h("div", { class: "user-row" },
        h("div", {}, h("strong", {}, u.username), u.username === users.me ? h("span", { class: "muted small" }, "  (you)") : null,
          h("div", { class: "muted small" }, "Added " + fmtTime(u.created))),
        h("div", { class: "row" }, reset, del));
    }));
  }
  drawUsers(users.users);
  const nu = h("input", { type: "text", autocomplete: "off" });
  const np = h("input", { type: "password", autocomplete: "new-password" });
  const addU = h("button", { class: "btn small", type: "button" }, "Add user");
  addU.addEventListener("click", busy(addU, async () => {
    drawUsers((await api("POST", "/api/users", { username: nu.value, password: np.value })).users);
    nu.value = np.value = "";
    toast("User added.");
  }));

  const team = h("input", { type: "text", value: access.team_domain, placeholder: "yourteam.cloudflareaccess.com" });
  const aud = h("input", { type: "text", value: access.aud, placeholder: "Application audience (AUD) tag" });
  const emails = h("textarea", { rows: 3, placeholder: "you@example.com" }, access.emails.join("\n"));
  const saveAccess = h("button", { class: "btn small", type: "button" }, "Save sign-in settings");
  saveAccess.addEventListener("click", busy(saveAccess, async () => {
    await api("PUT", "/api/access", { team_domain: team.value, aud: aud.value, emails: emails.value });
    toast("Saved.");
  }));

  body.replaceChildren(h("div", { class: "stack" }, passwordPanel,
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Users")),
      h("p", { class: "hint" }, "Everyone here can manage everything."),
      userList,
      h("div", { class: "grid-3" }, field("Username", nu), field("Password", np),
        h("div", { class: "field" }, h("span", {}, "\u00a0"), addU))),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Sign in with Cloudflare Access"),
        h("span", { class: "muted small" }, "Optional")),
      h("p", { class: "hint" }, "Put this page behind a Cloudflare Access app, then fill these in. " +
        "An allowed email that Access has already checked is signed in without a password."),
      h("div", { class: "grid-2" }, field("Team domain", team), field("Audience tag", aud),
        field("Allowed emails, one per line", emails, "span-2")),
      h("div", { class: "row-end" }, saveAccess))));
}

// ------------------------------------------------------------------ a new device

const RELEASES = "https://github.com/datbird/blockslot/releases/latest";
const DECKY_SOURCE = "https://github.com/datbird/blockslot-decky#install-from-source";

function copyButton(text, label) {
  const b = h("button", { class: "btn small", type: "button" }, label || "Copy");
  b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied."); }
    catch (e) { toast("Select the text and copy it by hand.", true); }
  });
  return b;
}

// The pairing code counts down to its expiry, and stops when it leaves the page.
function countdown(expires) {
  const el = h("span", { class: "mono" });
  const end = new Date(expires).getTime();
  const tick = () => {
    if (!el.isConnected && el.dataset.started) { clearInterval(timer); return; }
    el.dataset.started = "1";
    const left = Math.round((end - Date.now()) / 1000);
    if (!(left > 0)) {
      el.textContent = "Expired. Make a new code on the Devices page.";
      el.classList.add("expired");
      clearInterval(timer);
      return;
    }
    el.textContent = "Works for " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0");
  };
  const timer = setInterval(tick, 1000);
  tick();
  return el;
}

// What a device needs after it is added: the pairing code to type, the long
// setup code to paste, and what to do on each kind of device.
function deviceResult(got, name) {
  const origin = location.origin;
  return h("div", { class: "stack device-result" },
    h("div", { class: "pair" },
      h("div", { class: "pair-main" },
        h("div", { class: "label" }, "Pairing code for " + (name || got.device)),
        h("div", { class: "pair-code", "aria-label": "Pairing code " + got.pair_code.split("").join(" ") }, got.pair_code),
        h("div", { class: "small muted" }, countdown(got.pair_expires), ". Used once.")),
      h("div", { class: "pair-side" },
        h("div", { class: "label" }, "Address to enter"),
        h("div", { class: "pair-addr mono" }, origin),
        h("p", { class: "hint" }, "On the device, enter this address and the code. It fetches its key from here."),
        got.cloudflare ? h("span", { class: "chip teal" }, "Includes a Cloudflare token") : null)),
    h("div", { class: "platforms" },
      h("div", { class: "platform" },
        h("h3", {}, "Windows"),
        h("ol", { class: "steps-list" },
          h("li", {}, "Download BlockSlot for Windows from the ",
            h("a", { href: RELEASES, target: "_blank", rel: "noopener" }, "releases page"),
            ". Unzip it and run BlockSlot.exe."),
          h("li", {}, "Open Store, enter the address and the pairing code, and press Pair with the server."))),
      h("div", { class: "platform" },
        h("h3", {}, "Steam Deck"),
        h("ol", { class: "steps-list" },
          h("li", {}, "Install the BlockSlot plugin for Decky Loader. Until it is in the Decky store, install the decky zip from the ",
            h("a", { href: RELEASES, target: "_blank", rel: "noopener" }, "releases page"),
            " (Decky, Settings, Developer, Install Plugin from URL), or ",
            h("a", { href: DECKY_SOURCE, target: "_blank", rel: "noopener" }, "build it from source"), "."),
          h("li", {}, "Open BlockSlot from the Quick Access menu, choose Open BlockSlot, then Server."),
          h("li", {}, "Enter the address and the pairing code, and choose Pair with the server.")))),
    h("details", { class: "setup-code" },
      h("summary", {}, "Setup code, to paste instead"),
      h("p", { class: "hint" }, "One line with the same settings, for copy and paste. On Windows: Store, Paste setup code, then Save and test. " +
        "It holds the device's secret key and is shown only now."),
      h("div", { class: "code-box" }, got.setup_code),
      h("div", { class: "row" }, copyButton(got.setup_code, "Copy setup code"))));
}

// ------------------------------------------------------------------ getting started

const STEPS = [
  ["account", "Admin account"], ["address", "Device address"], ["emulators", "Emulator games"],
  ["device", "First device"], ["done", "Done"],
];

function stepCubes(current) {
  return h("div", { class: "step-cubes", "aria-hidden": "true" },
    STEPS.map((_s, i) => cube(i < current ? "" : i === current ? "warm" : "idle")));
}

async function saveStep(step, done) {
  const body = { step };
  if (done !== undefined) body.done = done;
  try { state.onboarding = await api("PUT", "/api/onboarding", body); }
  catch (e) { toast(e.message, true); }
}

function leaveGuide() {
  state.guideChecked = true;
  guideSkipped(true);
  location.hash = "#/games";
}

async function guidePage(wanted) {
  const body = h("div", {}, h("p", { class: "muted" }, "Loading..."));
  const skip = h("button", { class: "btn ghost small", type: "button" }, "Skip for now");
  skip.addEventListener("click", leaveGuide);
  mount(shell("start", [pageHead("Getting started", "Set up BlockSlot", skip), body]));
  let ob;
  try { ob = state.onboarding = await api("GET", "/api/onboarding"); }
  catch (e) { body.replaceChildren(errorPanel(e.message)); return; }
  state.guideChecked = true;
  let step = STEPS.some(([id]) => id === wanted) ? wanted : ob.step;
  if (step === "account") step = "address";
  const index = STEPS.findIndex(([id]) => id === step);
  if (wanted && wanted !== ob.step && step !== "done") saveStep(step);

  const steps = h("ol", { class: "guide-steps" }, STEPS.map(([id, name], i) => {
    const status = i < index ? "Done" : i === index ? "Now" : (id === "emulators" ? "Optional" : "");
    const inner = [cube(i < index ? "" : i === index ? "warm" : "idle"),
      h("span", { class: "step-name" }, name), status ? h("span", { class: "step-status" }, status) : null];
    const cls = i < index ? "past" : i === index ? "now" : "later";
    if (id === "account") return h("li", { class: cls }, h("div", { class: "step-link" }, inner));
    return h("li", { class: cls }, h("a", { class: "step-link", href: "#/start/" + id,
      "aria-current": i === index ? "step" : null }, inner));
  }));

  const next = STEPS[index + 1] && STEPS[index + 1][0];
  const prev = index > 1 ? STEPS[index - 1][0] : null;
  const go = async (to) => { await saveStep(to, to === "done" ? true : undefined); location.hash = "#/start/" + to; };
  function footer(primary) {
    const back = prev ? h("a", { class: "btn ghost", href: "#/start/" + prev }, "Back") : null;
    let skipStep = null;
    if (next) {
      skipStep = h("button", { class: "btn ghost", type: "button" }, "Skip this step");
      skipStep.addEventListener("click", busy(skipStep, () => go(next)));
    }
    return h("div", { class: "step-foot" }, back, h("span", { class: "grow" }), skipStep, primary);
  }

  const panel = h("div", { class: "panel step-panel" });
  body.replaceChildren(h("div", { class: "guide" }, steps, panel));

  if (step === "address") {
    const input = h("input", { type: "text", value: ob.public_endpoint, "aria-label": "Address devices use",
                               spellcheck: "false" });
    const local = /^(localhost|127\.|\[?::1\]?$)/.test(location.hostname);
    const save = h("button", { class: "btn primary", type: "button" }, "Save and continue");
    save.addEventListener("click", busy(save, async () => {
      await api("PUT", "/api/endpoint", { endpoint: input.value });
      await go(next);
    }));
    fill(panel,
      h("h2", {}, "Where devices reach this server"),
      h("p", { class: "lead" }, "Devices send their saves to this address. It goes into each device's setup, so set it before adding one."),
      field(ob.public_endpoint_set ? "Address devices use" : "Address devices use, guessed from this page", input),
      local ? h("div", { class: "note-box" }, "This page is open on localhost, so the guess only works on this machine. " +
        "Put the server's network address here, such as http://192.168.1.20:3900.") : null,
      h("p", { class: "hint" }, "Keep the guess when devices are on the same network. Change it when they come in another way, " +
        "such as through a reverse proxy or a Cloudflare hostname: https://saves.example.com."),
      footer(save));
    return;
  }

  if (step === "emulators") {
    let shared = null;
    try { shared = await api("GET", "/api/settings/shared"); } catch (e) { toast(e.message, true); }
    const libs = shared ? Object.keys(shared.settings.libraries || {}) : [];
    const work = h("div", {});
    const choose = (id) => {
      for (const b of choices.querySelectorAll("button")) b.classList.toggle("on", b.dataset.id === id);
      if (id === "skip") return go(next);
      if (id === "add") {
        const card = libraryCard("", { extensions: ["srm", "sav"] });
        const save = h("button", { class: "btn primary", type: "button" }, "Save and continue");
        save.addEventListener("click", busy(save, async () => {
          const got = card.collect();
          if (!got) throw new Error("Give it a name, such as RetroArch saves.");
          const cur = (await api("GET", "/api/settings/shared")).settings;
          const libraries = Object.assign({}, cur.libraries || {}, { [got[0]]: got[1] });
          await api("PUT", "/api/settings/shared", { libraries, retention: cur.retention });
          toast("Added " + got[0] + ". Set its folder on each device on the Devices page.");
          await go(next);
        }));
        work.replaceChildren(h("div", { class: "stack" }, card,
          h("p", { class: "hint" }, "Each device sets where this folder is on its disk, on the Devices page or in its own app."),
          h("div", { class: "row-end" }, save)));
      } else if (id === "import") {
        if (shared && shared.exists) {
          work.replaceChildren(h("p", { class: "hint" }, "The shared settings exist already, so there is nothing to import. " +
            "Change them on the Settings page."));
          return;
        }
        const text = h("textarea", { rows: 7, placeholder: "{\"trees\": { ... }}", "aria-label": "savepick.json" });
        const imp = h("button", { class: "btn primary", type: "button" }, "Import and continue");
        imp.addEventListener("click", busy(imp, async () => {
          const got = await api("POST", "/api/settings/import", { text: text.value });
          toast("Imported " + Object.keys(got.settings.libraries).length + " libraries and the folders of " +
                got.devices.length + " devices.");
          await go(next);
        }));
        work.replaceChildren(h("div", { class: "stack" },
          h("p", { class: "hint" }, "Paste a device's savepick.json, or its trees section. Its libraries land in Settings, " +
            "and each device's folders on the Devices page."),
          text, h("div", { class: "row-end" }, imp)));
      }
    };
    const option = (id, title, text) => {
      const b = h("button", { type: "button", class: "option", "data-id": id },
        h("span", { class: "option-title" }, title), h("span", { class: "option-text" }, text));
      b.addEventListener("click", () => choose(id));
      return b;
    };
    const choices = h("div", { class: "options" },
      option("add", "Add one now", "An emulator's saves folder, or one game in an emulator."),
      option("import", "Import from a device", "Paste the settings of a device that already has them."),
      option("skip", "Nothing to add", "Only Steam and PC games. Go on to the device."));
    fill(panel,
      h("h2", {}, "Emulator games"),
      h("p", { class: "lead" }, "Steam and PC games need nothing here. BlockSlot finds their saves on its own."),
      h("p", { class: "hint" }, "Add something only for an emulator's saves folder, such as RetroArch or RetroBat, " +
        "or for one game that runs in an emulator, such as a PS4 game in shadPS4."),
      libs.length ? h("p", { class: "hint" }, "Already set: " + libs.join(", ") + ".") : null,
      choices, work, footer(null));
    return;
  }

  if (step === "device") {
    const nameIn = h("input", { type: "text", placeholder: "Steam Deck, Laptop, Desktop", "aria-label": "Device name" });
    const add = h("button", { class: "btn primary", type: "submit" }, "Add device");
    const out = h("div", {});
    const cont = h("button", { class: "btn", type: "button" }, "Continue");
    cont.addEventListener("click", busy(cont, () => go(next)));
    const form = h("form", { class: "row" }, h("div", { class: "field" }, nameIn), add);
    form.addEventListener("submit", busy(add, async () => {
      const name = nameIn.value.trim();
      const got = await api("POST", "/api/devices", { name });
      namesLoaded = false;
      out.replaceChildren(deviceResult(got, name || got.device));
      nameIn.value = "";
      nameIn.placeholder = "Another device";
      add.textContent = "Add another";
      cont.className = "btn primary";
    }));
    fill(panel,
      h("h2", {}, "Add your first device"),
      h("p", { class: "lead" }, "Name it for the machine, such as Steam Deck or Laptop. It gets its own key, " +
        "so a lost device is one removal on the Devices page."),
      form, out, footer(cont));
    return;
  }

  // done
  if (!ob.done) saveStep("done", true);
  fill(panel,
    h("h2", {}, "You are set up"),
    h("p", { class: "lead" }, "From here the devices do the work."),
    h("ul", { class: "done-list" },
      h("li", {}, cube(""), h("span", {}, "When a game closes, the device uploads its save.")),
      h("li", {}, cube(""), h("span", {}, "Before a game starts, the device restores the newest save from any device.")),
      h("li", {}, cube("warm"), h("span", {}, "If two devices played from the same save, the game shows Two saves and you pick one.")),
      h("li", {}, cube("idle"), h("span", {}, "The Games page fills in after the first game is played and closed."))),
    h("p", { class: "hint" }, "This guide stays under Getting started in the sidebar."),
    h("div", { class: "step-foot" },
      h("a", { class: "btn ghost", href: "#/settings" }, "Settings"),
      h("a", { class: "btn ghost", href: "#/devices" }, "Devices"),
      h("span", { class: "grow" }),
      h("a", { class: "btn primary", href: "#/games" }, "Go to Games")));
}

// ------------------------------------------------------------------ routing

async function route() {
  if (!state.session) {
    let s;
    try {
      s = await (await fetch("/api/session", { credentials: "same-origin" })).json();
    } catch (e) {
      mount(h("div", { class: "gate" }, errorPanel("The server did not answer. Reload the page to try again.")));
      return;
    }
    if (!s.user) return gate(s.first_run, s.cloudflare_access);
    state.session = s;
  }
  const parts = (location.hash || "#/games").slice(2).split("/");
  const page = parts[0];
  if (page === "start") return guidePage(parts[1]);
  // Once per page load, a guide that is not finished opens instead of Games.
  if (!state.guideChecked && !guideSkipped() && (page === "games" || page === "")) {
    state.guideChecked = true;
    try {
      state.onboarding = await api("GET", "/api/onboarding");
      if (!state.onboarding.done) { location.hash = "#/start"; return; }
    } catch (e) { /* the page still works without it */ }
  }
  if (page === "game" && parts[1]) return gamePage(decodeURIComponent(parts[1]));
  if (page === "devices") return devicesPage();
  if (page === "settings") return settingsPage();
  if (page === "storage") return storagePage();
  if (page === "account") return accountPage();
  return gamesPage(false);
}

window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", route);
