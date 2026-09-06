"use strict";

const $ = (id) => document.getElementById(id);
const msgsEl = $("msgs");
const chanNameEl = $("chan-name");
const viewersEl = $("viewers");
const connEl = $("conn");
const noticeEl = $("notice");
const input = $("msg-input");
const sendBtn = $("send-btn");
const emoteBtn = $("emote-btn");
const emotePop = $("emote-pop");
const emoteGrid = $("emote-grid");
const emoteSearch = $("emote-search");
const resumeBtn = $("resume");

let state = {
  token: null,
  nick: null,
  canChat: false,
  ws: null,
  connected: false,
  startedAt: null,
  uptimeTimer: null,
  joinedChannel: null,
  wantedChannel: null,
  customEmotes: new Map(),
  allEmoteList: [],
  badges: {},
  emotesReady: false,
  myLoginLower: null,
  myEmoteSets: null,
  myBadges: "", // our own badges (e.g. "subscriber/6,premium/1") from USERSTATE
  mySetEmotes: [],
  knownUsers: new Map(),  // login(lower) -> {login, display, color}
  stick: true,
  reconnectTimer: null,
};

function bridge(msg) {
  try {
    window.webkit.messageHandlers.omatv.postMessage(JSON.stringify(msg));
  } catch (e) {
    console.error("bridge failed", e);
  }
}

const httpPending = new Map();
let httpSeq = 0;

window.__push = function (payload) {
  switch (payload.type) {
    case "init":
      state.token = payload.token || null;
      state.nick = payload.nick || null;
      state.canChat = !!payload.can_chat && !!payload.token;
      state.myLoginLower = (payload.nick || "").toLowerCase();
      updateConnUI();
      updateComposer();
      if (state.wantedChannel) joinChannel(state.wantedChannel);
      break;
    case "channel":
      clearUptime();
      clearViewers();
      joinChannel(payload);
      break;
    case "stream_start":
      if (payload?.started_at) {
        setUptime(payload.started_at);
        if (payload?.viewers != null) setViewers(payload.viewers);
      } else {
        clearUptime();
        clearViewers();
      }
      break;
    case "viewer_count":
      if (payload?.viewers != null) setViewers(payload.viewers);
      break;
  }
};
window.__pushQueue.forEach((p) => window.__push(p));
window.__pushQueue = [];

function httpGet(url) {
  return new Promise((resolve, reject) => {
    const id = "h" + ++httpSeq;
    httpPending.set(id, { resolve, reject });
    bridge({ action: "fetch", id, url });
    setTimeout(() => {
      if (httpPending.has(id)) {
        httpPending.delete(id);
        reject(new Error("timeout " + url));
      }
    }, 20000);
  });
}

window.__resolveHttp = function (id, ok, payload) {
  const p = httpPending.get(id);
  if (!p) return;
  httpPending.delete(id);
  if (ok) p.resolve(JSON.parse(payload));
  else p.reject(new Error(payload));
};

function httpGetEmotes(broadcasterId) {
  return new Promise((resolve, reject) => {
    const id = "e" + ++httpSeq;
    httpPending.set(id, { resolve, reject });
    bridge({ action: "twitch_emotes", id, broadcaster_id: broadcasterId || "" });
    setTimeout(() => {
      if (httpPending.has(id)) {
        httpPending.delete(id);
        reject(new Error("timeout twitch_emotes"));
      }
    }, 20000);
  });
}

function httpGetEmoteSets(setIds) {
  return new Promise((resolve, reject) => {
    const id = "s" + ++httpSeq;
    httpPending.set(id, { resolve, reject });
    bridge({ action: "emote_sets", id, set_ids: setIds || "" });
    setTimeout(() => {
      if (httpPending.has(id)) {
        httpPending.delete(id);
        reject(new Error("timeout emote_sets"));
      }
    }, 20000);
  });
}

function httpGetBadges(broadcasterId) {
  return new Promise((resolve, reject) => {
    const id = "b" + ++httpSeq;
    httpPending.set(id, { resolve, reject });
    bridge({ action: "badges", id, broadcaster_id: broadcasterId || "" });
    setTimeout(() => {
      if (httpPending.has(id)) {
        httpPending.delete(id);
        reject(new Error("timeout badges"));
      }
    }, 20000);
  });
}

/* ------------------------------ emotes ------------------------------ */

function stvUrl(host) {
  const files = host.files || [];
  let f =
    files.find((x) => x.name === "2x.webp") ||
    files.find((x) => x.name.startsWith("2x") && x.name.endsWith(".webp")) ||
    files.find((x) => x.name.startsWith("2x")) ||
    files[0];
  return f ? "https:" + host.url + "/" + f.name : null;
}

async function loadEmotes(channelId) {
  const jobs = [
    ["stv-global", httpGet("https://7tv.io/v3/emote-sets/global").catch(() => null)],
    ["bttv-global", httpGet("https://api.betterttv.net/3/cached/emotes/global").catch(() => null)],
    ["ffz-global", httpGet("https://api.frankerfacez.com/v1/set/global").catch(() => null)],
  ];
  if (channelId) {
    jobs.push(["stv-chan", httpGet(`https://7tv.io/v3/users/twitch/${channelId}`).catch(() => null)]);
    jobs.push(["bttv-chan", httpGet(`https://api.betterttv.net/3/cached/users/twitch/${channelId}`).catch(() => null)]);
    jobs.push(["ffz-room", httpGet(`https://api.frankerfacez.com/v1/room/id/${channelId}`).catch(() => null)]);
  }
  // Native Twitch emotes via the Python bridge (needs the API token).
  jobs.push(["twitch", httpGetEmotes(channelId)]);
  // Badge images (global + channel) so chat shows real Twitch badges.
  jobs.push(["badges", httpGetBadges(channelId).catch(() => null)]);

  const results = await Promise.all(jobs.map(([_, p]) => p));
  const [stvG, bttvG, ffzG, stvC, bttvC, ffzR, twitchE, badges] = results;

  state.badges = badges || {};

  const map = new Map();

  const addStv = (emotes) =>
    (emotes || []).forEach((e) => {
      if (!e?.data?.host) return;
      const url = stvUrl(e.data.host);
      if (url)
        map.set(e.name, {
          url,
          zeroWidth: !!(e.data.flags & 1),
          provider: "7TV",
        });
    });

  const addBttv = (list) =>
    (list || []).forEach((e) => {
      if (e?.code && e?.id)
        map.set(e.code, {
          url: `https://cdn.betterttv.net/emote/${e.id}/2x`,
          provider: "BTTV",
        });
    });

  const addFfzSets = (sets) =>
    Object.values(sets || {}).forEach((set) =>
      (set.emoticons || []).forEach((e) => {
        if (!e?.name) return;
        const u = e.urls["4"] || e.urls["2"] || e.urls["1"];
        if (u)
          map.set(e.name, {
            url: u.startsWith("//") ? "https:" + u : u,
            provider: "FFZ",
          });
      })
    );

  addStv(stvG?.emotes);
  addStv(stvC?.emote_set?.emotes);
  addBttv(bttvG);
  if (bttvC) {
    addBttv(bttvC.channelEmotes);
    addBttv(bttvC.sharedEmotes);
  }
  addFfzSets(ffzG?.sets);
  addFfzSets(ffzR?.sets);
  (twitchE || []).forEach((e) => {
    if (e?.name && e?.url) map.set(e.name, { url: e.url, provider: "Twitch" });
  });
  (state.mySetEmotes || []).forEach((e) => {
    if (e?.name && e?.url) map.set(e.name, { url: e.url, provider: "Twitch" });
  });

  state.customEmotes = map;
  state.allEmoteList = [...map.entries()].sort((a, b) =>
    a[0].localeCompare(b[0])
  );
  renderEmoteGrid("");
  emoteLoadDone("channel");
}

// Load the emotes from the user's emote-set list (from the IRC emote-sets
// tag) — this covers subscriber emotes from every channel they're subbed to.
async function loadMyEmoteSets(setIds) {
  try {
    const emotes = await httpGetEmoteSets(setIds);
    state.mySetEmotes = emotes || [];
  } catch (e) {
    state.mySetEmotes = state.mySetEmotes || [];
  }
  // merge into the current map without waiting for a channel reload
  (state.mySetEmotes || []).forEach((e) => {
    if (e?.name && e?.url)
      state.customEmotes.set(e.name, { url: e.url, provider: "Twitch" });
  });
  state.allEmoteList = [...state.customEmotes.entries()].sort((a, b) =>
    a[0].localeCompare(b[0])
  );
  renderEmoteGrid("");
  emoteLoadDone("sets");
}

// Tracks both emote load paths (channel emotes + subscriber sets) so we can
// show ONE "all emotes loaded" line once everything is in.
let _emoteLoadBits = 0;
let _pendingMsgs = [];
function flushEmoteReady() {
  state.emotesReady = true;
  const n = state.customEmotes.size;
  addSystem(`All emotes loaded (${n} across Twitch, 7TV, BTTV, FFZ)`, "");
  // flush buffered chat messages now that emotes/badges are ready
  const pending = _pendingMsgs.splice(0, _pendingMsgs.length);
  pending.forEach(([nick, tags, text]) => addMessage(nick, tags, text));
  _emoteLoadBits = 0; // reset so a channel switch re-announces
}
function emoteLoadDone(bit) {
  const flag = bit === "sets" ? 1 : 2;
  if (_emoteLoadBits & flag) return; // already counted
  _emoteLoadBits |= flag;
  if (_emoteLoadBits === 3) flushEmoteReady();
}

/* -------------------------------- irc -------------------------------- */

function connectIRC() {
  clearTimeout(state.reconnectTimer);
  if (
    state.ws &&
    (state.ws.readyState === WebSocket.CONNECTING ||
      state.ws.readyState === WebSocket.OPEN)
  ) {
    return Promise.resolve();
  }
  const anon = !state.token || state.authFailures >= 2;
  return new Promise((resolve, reject) => {
    try {
      const ws = new WebSocket(
        "wss://irc-ws.chat.twitch.tv:443",
        "irc"
      );
      state.ws = ws;
      state.anonMode = anon;
      ws.onopen = () => {
        if (!anon) {
          ws.send("PASS oauth:" + state.token);
          ws.send("NICK " + state.nick);
        } else {
          ws.send("PASS SCHMOOPIIE");
          ws.send("NICK justinfan" + Math.floor(10000 + Math.random() * 90000));
        }
        ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership");
      };
      ws.onmessage = (ev) => {
        ev.data.split("\r\n").forEach((line) => {
          if (line) handleLine(line, resolve);
        });
      };
      ws.onclose = () => {
        state.connected = false;
        updateConnUI();
        scheduleReconnect();
      };
      ws.onerror = () => {};
    } catch (e) {
      reject(e);
    }
  });
}

function scheduleReconnect() {
  if (!state.wantedChannel) return;
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = setTimeout(async () => {
    setConn("connecting…", "connecting");
    try {
      await connectIRC();
    } catch (e) {}
  }, 3000);
}

function handleLine(line, welcomeResolve) {
  let tags = {};
  let rest = line;
  if (rest.startsWith("@")) {
    const sp = rest.indexOf(" ");
    tags = parseTags(rest.slice(1, sp));
    rest = rest.slice(sp + 1);
  }
  if (rest.startsWith("PING")) {
    state.ws.send("PONG" + rest.slice(4));
    return;
  }

  let prefix = "";
  if (rest.startsWith(":")) {
    const sp = rest.indexOf(" ");
    prefix = rest.slice(1, sp);
    rest = rest.slice(sp + 1);
  }
  const cmd = rest.split(" ")[0];
  const params = rest.slice(cmd.length).trimStart();

  // Capture the user's OWN emote-set list from the GLOBALUSERSTATE message
  // (sent on connect for authenticated users) OR the USERSTATE message (sent
  // when joining a channel — carries the same emote-sets tag). These sets
  // include subscriber emotes from every channel the user is subscribed to,
  // which the Helix chat/emotes endpoints don't fully expose. Only
  // GLOBALUSERSTATE/USERSTATE for our own nick carry OUR sets — other users'
  // PRIVMSG tags show THEIR sets and must not be captured.
  if (cmd === "GLOBALUSERSTATE" || cmd === "USERSTATE") {
    // Our own badges (subscriber/6, premium/1, ...) — used to render them on
    // our own optimistic send copy, since Twitch doesn't echo our PRIVMSG.
    if (tags.badges) state.myBadges = tags.badges;
    const esTag = tags["emote-sets"];
    if (esTag && esTag !== state.myEmoteSets) {
      state.myEmoteSets = esTag;
      loadMyEmoteSets(esTag);
    }
  }

  if (cmd === "001") {
    state.connected = true;
    updateConnUI();
    updateComposer();
    if (state.wantedChannel) {
      state.ws.send("JOIN #" + state.wantedChannel.login);
    }
    if (welcomeResolve) welcomeResolve();
    return;
  }
  if (cmd === "CAP") return;
  if (cmd === "RECONNECT") {
    state.ws.close();
    return;
  }
  if (cmd === "JOIN") {
    const login = (prefix.split("!")[0] || "").toLowerCase();
    if (login) knowUser(login, tags["display-name"] || login, tags.color);
    if (params.toLowerCase().startsWith("#" + (state.wantedChannel?.login || "\u0000"))) {
      state.joinedChannel = state.wantedChannel;
      const canSend = state.canChat && !state.anonMode;
      setConn(canSend ? `connected · ${state.nick}` : "read-only chat", canSend ? "connected" : "readonly");
    }
    return;
  }
  if (cmd === "PART") {
    const login = (prefix.split("!")[0] || "").toLowerCase();
    if (login) state.knownUsers.delete(login);
    return;
  }
  if (cmd === "353") { // NAMES list — "353 user = #chan :user1 user2 ..."
    const names = params.split(":").slice(1).join(":");
    names.split(/\s+/).forEach((n) => {
      if (!n) return;
      const clean = n.startsWith("@") || n.startsWith("+") ? n.slice(1) : n;
      const login = clean.toLowerCase();
      if (login && !state.knownUsers.has(login)) {
        state.knownUsers.set(login, { login, display: clean, color: null });
      }
    });
    return;
  }
  if (cmd === "366") { // end of NAMES
    return;
  }
  if (cmd === "NOTICE") {
    const body = (params.split(":", 2)[1] || params).toLowerCase();
    if (body.includes("login unsuccessful") && !state.anonMode) {
      state.authFailures = (state.authFailures || 0) + 1;
      state.ws.close();
      if (state.authFailures >= 2) {
        setConn("token rejected · read-only", "readonly");
        noticeEl.textContent =
          "twitch.json token was rejected for chat — reconnecting anonymously (read-only). Refresh the token in twitch.json to chat.";
        noticeEl.className = "warn";
        updateComposer();
        state.connected = false;
        scheduleReconnect();
        return;
      }
    }
    addSystem(params.split(":", 2)[1] || params, "err");
    return;
  }
  if (cmd === "USERNOTICE") {
    if (tags["system-msg"])
      addSystem(unescapeTags(tags["system-msg"]), tags["msg-id"] === "sub" || tags["msg-id"] === "resub" ? "sub" : "");
    return;
  }
  if (cmd === "CLEARCHAT") {
    const target = params.split(" ")[1];
    if (!target) {
      msgsEl.querySelectorAll(".msg").forEach((n) => n.remove());
    } else {
      const login = target.toLowerCase();
      msgsEl.querySelectorAll(`.msg[data-login="${login}"]`).forEach((n) => n.remove());
    }
    return;
  }
  if (cmd === "PRIVMSG") {
    const chan = params.split(" ")[0];
    const textStart = params.indexOf(":");
    const text = textStart >= 0 ? params.slice(textStart + 1) : "";
    const nick = prefix.split("!")[0];
    knowUser(nick, tags["display-name"] || nick, tags.color);
    addMessage(nick, tags, text);
  }
}

function parseTags(s) {
  const out = {};
  s.split(";").forEach((pair) => {
    const eq = pair.indexOf("=");
    if (eq < 0) return;
    out[pair.slice(0, eq)] = pair.slice(eq + 1);
  });
  return out;
}

function unescapeTags(v) {
  let out = "";
  for (let i = 0; i < v.length; i++) {
    if (v[i] === "\\" && i + 1 < v.length) {
      const c = v[i + 1];
      out += c === "s" ? " " : c === ":" ? ";" : c === "r" ? "\r" : c === "n" ? "\n" : "\\" + c;
      i++;
    } else {
      out += v[i];
    }
  }
  return out;
}

// Remember a chatter so @-tab-complete can find them. keyed by lowercase
// login; keeps the display name + color so the popup can render nicely.
function knowUser(login, display, color) {
  if (!login) return;
  const l = login.toLowerCase();
  const existing = state.knownUsers.get(l);
  if (!existing) {
    state.knownUsers.set(l, { login: l, display: display || login, color: color || null });
  } else {
    if (display && existing.display !== display) existing.display = display;
    if (color && existing.color !== color) existing.color = color;
  }
}

/* ----------------------------- rendering ----------------------------- */

const BADGES = {
  broadcaster: "HOST",
  moderator: "MOD",
  vip: "VIP",
  subscriber: "SUB",
  founder: "FOUNDER",
  premium: "PRIME",
  staff: "STAFF",
  global_mod: "GMOD",
  admin: "ADMIN",
  artist: "ARTIST",
};

function readableColor(c) {
  if (!c || !/^#[0-9a-f]{6}$/i.test(c)) return null;
  let r = parseInt(c.slice(1, 3), 16),
    g = parseInt(c.slice(3, 5), 16),
    b = parseInt(c.slice(5, 7), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  if (lum < 0.28) {
    r = Math.round(r * 0.5 + 128);
    g = Math.round(g * 0.5 + 128);
    b = Math.round(b * 0.5 + 128);
  }
  return `rgb(${r},${g},${b})`;
}

const FALLBACK_COLORS = ["#ff7bd1","#7bd3ff","#9ef29e","#ffd479","#c39bff","#ff9d7b","#7bffe0"];

function userColor(tags, nick) {
  let c = readableColor(tags.color);
  if (c) return c;
  let h = 0;
  for (const ch of nick) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return FALLBACK_COLORS[h % FALLBACK_COLORS.length];
}

function makeEmoteImg(emote) {
  const img = document.createElement("img");
  img.className = "emote";
  img.src = emote.url;
  img.alt = img.title = emote._name || "";
  return img;
}

function renderTextChunk(frag, chunk, mentionMe) {
  const parts = chunk.split(/(\s+)/);
  for (const part of parts) {
    if (!part) continue;
    if (/^\s+$/.test(part)) {
      frag.append(part);
      continue;
    }
    const emote = state.customEmotes.get(part);
    if (emote) {
      frag.append(makeEmoteImg(Object.assign({ _name: part }, emote)));
      continue;
    }
    if (/^https?:\/\/[^\s]+$/i.test(part)) {
      const a = document.createElement("a");
      a.className = "link";
      a.href = "#";
      a.textContent = part.length > 48 ? part.slice(0, 45) + "…" : part;
      a.title = part;
      a.onclick = (ev) => {
        ev.preventDefault();
        bridge({ action: "open_url", url: part });
      };
      frag.append(a);
      continue;
    }
    frag.append(part);
  }
}

function addMessage(nick, tags, rawText) {
  // Don't render chat until emotes + badges are loaded, so messages show
  // their real emotes/badges the moment they appear. Buffer them instead.
  if (!state.emotesReady) {
    _pendingMsgs.push([nick, tags, rawText]);
    // cap the buffer so a slow load doesn't accumulate forever
    if (_pendingMsgs.length > 200) _pendingMsgs.shift();
    return;
  }
  let text = rawText;
  let isAction = false;
  if (text.startsWith("\u0001ACTION ")) {
    isAction = true;
    text = text.slice(8).replace(/\u0001$/, "");
  }

  const row = document.createElement("div");
  row.className = "msg";
  row.dataset.login = (tags.login || nick).toLowerCase();

  if (tags["msg-id"] === "highlighted-message") row.classList.add("highlighted");
  if (tags["first-msg"] === "1") row.classList.add("first");

  const mentioned =
    state.myLoginLower &&
    text.toLowerCase().includes("@" + state.myLoginLower);
  if (mentioned) row.classList.add("mentioned");

  (tags.badges || "").split(",").forEach((b) => {
    const [name, version] = b.split("/");
    const url = state.badges?.[name]?.[version];
    if (url) {
      const img = document.createElement("img");
      img.className = "badge-img";
      img.src = url;
      img.alt = name;
      img.title = name.replace(/-/g, " ");
      row.append(img);
    } else if (BADGES[name]) {
      const el = document.createElement("span");
      el.className = "badge " + name;
      el.textContent = BADGES[name];
      row.append(el);
    }
  });

  const uname = document.createElement("span");
  uname.className = "user";
  uname.textContent = tags["display-name"] || nick;
  uname.style.color = userColor(tags, nick);
  row.append(uname);

  const sep = document.createElement("span");
  sep.className = "sep";
  sep.textContent = ":";
  row.append(sep);

  if (isAction) {
    row.classList.add("action");
    row.style.color = userColor(tags, nick);
  }

  const cps = Array.from(text);
  const ranges = [];
  (tags.emotes || "").split("/").forEach((group) => {
    if (!group) return;
    const ci = group.indexOf(":");
    if (ci < 0) return;
    const id = group.slice(0, ci);
    group.slice(ci + 1).split(",").forEach((rng) => {
      const dash = rng.indexOf("-");
      if (dash < 0) return;
      ranges.push({
        start: parseInt(rng.slice(0, dash), 10),
        end: parseInt(rng.slice(dash + 1), 10),
        id,
      });
    });
  });
  ranges.sort((a, b) => b.start - a.start);
  for (const r of ranges) {
    if (r.end >= cps.length) continue;
    cps.splice(r.start, r.end - r.start + 1, {
      native: `https://static-cdn.jtvnw.net/emoticons/v2/${r.id}/default/dark/2.0`,
      name: text,
    });
  }

  const frag = document.createDocumentFragment();
  let buf = "";
  const flushBuf = () => {
    if (buf) {
      renderTextChunk(frag, buf, mentioned);
      buf = "";
    }
  };
  for (const piece of cps) {
    if (piece && typeof piece === "object" && piece.native) {
      flushBuf();
      const img = document.createElement("img");
      img.className = "emote";
      img.src = piece.native;
      img.title = "(Twitch emote)";
      frag.append(img);
    } else {
      buf += piece;
    }
  }
  flushBuf();

  row.append(frag);

  appendMsg(row);
}

function addSystem(text, kind) {
  const row = document.createElement("div");
  row.className = "msg system-msg";
  row.textContent = text;
  if (kind === "err") row.style.color = "var(--red)";
  if (kind === "sub") row.classList.add("highlighted");
  appendMsg(row);
}

function appendMsg(row) {
  const atBottom = isNearBottom();
  msgsEl.append(row);
  trimOld();
  if (atBottom || state.stick) {
    requestAnimationFrame(scrollToBottom);
  } else {
    resumeBtn.classList.remove("hidden");
  }
}

function isNearBottom() {
  return (
    msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 60
  );
}

function scrollToBottom() {
  msgsEl.scrollTop = msgsEl.scrollHeight;
  state.stick = true;
  resumeBtn.classList.add("hidden");
}

function trimOld() {
  while (msgsEl.children.length > 500) msgsEl.firstElementChild.remove();
}

msgsEl.addEventListener("scroll", () => {
  if (isNearBottom()) {
    state.stick = true;
    resumeBtn.classList.add("hidden");
  } else if (msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight > 120) {
    state.stick = false;
  }
});
resumeBtn.addEventListener("click", scrollToBottom);

/* ------------------------------ composer ----------------------------- */

const SEND_WINDOW = 30000;
const SEND_MAX = 18;
let sentTimes = [];

function updateConnUI() {
  if (!state.connected && state.wantedChannel) {
    setConn("connecting…", "connecting");
  }
}

function setConn(text, cls) {
  // While a stream is live, the uptime pill owns this slot — don't let
  // connection-status text (connected · nick) clobber it.
  if (state.startedAt && (cls === "connected" || cls === "readonly")) return;
  connEl.textContent = text;
  connEl.className = "conn " + cls;
}

// ---- uptime pill (replaces "connected · nick" while a stream is live) ----
function formatUptime(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m ${sec}s`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}
function tickUptime() {
  if (!state.startedAt) return;
  const up = Date.now() - state.startedAt;
  if (up < 0) return; // clock skew; wait
  const text = "live · " + formatUptime(up);
  if (connEl.textContent !== text) {
    connEl.textContent = text;
    connEl.className = "conn connected";
  }
}
function setUptime(startedAtIso) {
  state.startedAt = startedAtIso ? new Date(startedAtIso).getTime() : Date.now();
  if (state.uptimeTimer) clearInterval(state.uptimeTimer);
  tickUptime();
  state.uptimeTimer = setInterval(tickUptime, 1000); // live tick — repaint only on change
}
function clearUptime() {
  if (state.uptimeTimer) clearInterval(state.uptimeTimer);
  state.uptimeTimer = null;
  state.startedAt = null;
}

// ---- viewers pill (center of the header, live only) ----
function formatViewers(n) {
  if (n == null) return "";
  return n.toLocaleString("en-US");
}
function setViewers(n) {
  const text = formatViewers(n) + " watching";
  if (viewersEl.textContent !== text) {
    viewersEl.textContent = text;
    viewersEl.classList.add("live-viewers");
    viewersEl.classList.remove("hidden");
  }
}
function clearViewers() {
  viewersEl.textContent = "";
  viewersEl.classList.remove("live-viewers");
  viewersEl.classList.add("hidden");
}

function updateComposer() {
  const readonly = !state.canChat || state.anonMode;
  if (!readonly) {
    input.contentEditable = "true";
    input.removeAttribute("data-disabled");
    sendBtn.disabled = false;
    input.dataset.placeholder = "Send a message";
    noticeEl.textContent = "";
    noticeEl.className = "";
  } else {
    input.contentEditable = "false";
    input.setAttribute("data-disabled", "1");
    sendBtn.disabled = true;
    if (state.connected || state.wantedChannel) {
      noticeEl.textContent =
        "Read-only chat — refresh your twitch.json token (needs chat:edit scope) to send messages";
      noticeEl.className = "warn";
    }
  }
}

$("composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = getEditorText().trim();
  if (!text || !state.ws || !state.joinedChannel) return;

  const now = Date.now();
  sentTimes = sentTimes.filter((t) => now - t < SEND_WINDOW);
  if (sentTimes.length >= SEND_MAX) {
    noticeEl.textContent = "Slow down — rate limit reached";
    noticeEl.className = "err";
    return;
  }
  sentTimes.push(now);

  state.ws.send(`PRIVMSG #${state.joinedChannel.login} :${text.slice(0, 480)}`);

  const mine = document.createElement("div");
  mine.className = "msg own-msg";
  // Render OUR badges (captured from USERSTATE) on the optimistic copy —
  // Twitch doesn't echo our own PRIVMSG back, so this is the only place they
  // can appear.
  (state.myBadges || "").split(",").forEach((b) => {
    const [name, version] = b.split("/");
    const url = state.badges?.[name]?.[version];
    if (url) {
      const img = document.createElement("img");
      img.className = "badge-img";
      img.src = url;
      img.alt = name;
      img.title = name.replace(/-/g, " ");
      mine.append(img);
    } else if (BADGES[name]) {
      const el = document.createElement("span");
      el.className = "badge " + name;
      el.textContent = BADGES[name];
      mine.append(el);
    }
  });
  const un = document.createElement("span");
  un.className = "user";
  un.textContent = state.nick;
  un.style.color = "var(--accent)";
  mine.append(un);
  const sep = document.createElement("span");
  sep.className = "sep";
  sep.textContent = ":";
  mine.append(sep);
  const bodySpan = document.createElement("span");
  const frag = document.createDocumentFragment();
  renderTextChunk(frag, text, false);
  bodySpan.append(frag);
  mine.append(bodySpan);
  appendMsg(mine);

  input.innerHTML = "";
  input.focus();
});

input.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey && !tabState) {
    ev.preventDefault();
    $("composer").requestSubmit();
  }
});

document.addEventListener("click", (ev) => {
  if (!emotePop.contains(ev.target) && ev.target !== emoteBtn) {
    emotePop.classList.add("hidden");
  }
  if (!tabPop.contains(ev.target)) {
    closeTabPop();
  }
});

emoteBtn.addEventListener("click", () => {
  emotePop.classList.toggle("hidden");
  if (!emotePop.classList.contains("hidden")) {
    emoteSearch.value = "";
    renderEmoteGrid("");
    emoteSearch.focus();
  }
});

emoteSearch.addEventListener("input", () => renderEmoteGrid(emoteSearch.value));

function renderEmoteGrid(query) {
  emoteGrid.innerHTML = "";
  const q = query.trim().toLowerCase();
  const list = q
    ? state.allEmoteList.filter(([name]) => name.toLowerCase().includes(q))
    : state.allEmoteList;
  list.slice(0, 300).forEach(([name, e]) => {
    const cell = document.createElement("div");
    cell.className = "emote-cell";
    cell.title = `${name} · ${e.provider}`;
    const img = document.createElement("img");
    img.src = e.url;
    img.loading = "lazy";
    cell.append(img);
    cell.addEventListener("click", () => insertAtCursor(name + " "));
    emoteGrid.append(cell);
  });
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "system-msg";
    empty.style.gridColumn = "1/-1";
    empty.style.textAlign = "center";
    empty.textContent = "no emotes found";
    emoteGrid.append(empty);
  }
}

function insertAtCursor(textToInsert) {
  // Insert plain text at the caret in the contenteditable, then render emotes.
  insertEditorText(textToInsert);
}

/* --------------------------- tab completion -------------------------- */

const tabPop = $("tab-pop");
const tabList = $("tab-list");
let tabState = null; // { startNode, startOffset, word, matches, selected }

/* ------------------------ contenteditable editor ---------------------- */

// Extract plain text: emote <img> become their emote name (data-name),
// other elements become their text content.
function getEditorText() {
  let out = "";
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      out += node.textContent;
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.classList && node.classList.contains("emote") && node.dataset.name) {
        out += node.dataset.name;
      } else {
        node.childNodes.forEach(walk);
      }
      if (node.tagName === "DIV" || node.tagName === "BR") out += "\n";
    }
  };
  input.childNodes.forEach(walk);
  return out;
}

// Get { node, offset } for the caret, in text-offset terms relative to a
// "virtual string" built by getEditorText. Returns null if caret is in an
// emote <img> (treat as after it).
function getCaretTextOffset() {
  const sel = window.getSelection();
  if (!sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  // Build virtual text + a parallel list of {node, len} segments
  const segs = [];
  let virtual = "";
  const build = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      segs.push({ node, len: node.textContent.length, kind: "text" });
      virtual += node.textContent;
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.classList && node.classList.contains("emote") && node.dataset.name) {
        segs.push({ node, len: node.dataset.name.length, kind: "emote" });
        virtual += node.dataset.name;
      } else if (node.tagName === "BR") {
        segs.push({ node, len: 1, kind: "br" });
        virtual += "\n";
      } else {
        node.childNodes.forEach(build);
      }
    }
  };
  input.childNodes.forEach(build);

  // Find offset of range start within the virtual string
  let offset = 0;
  const startNode = range.startContainer;
  const startOff = range.startOffset;
  for (const s of segs) {
    if (s.node === startNode) {
      if (s.kind === "text") {
        offset += Math.min(startOff, s.len);
      } else {
        // caret at an emote node boundary
        offset += startOff > 0 ? s.len : 0;
      }
      break;
    }
    offset += s.len;
  }
  return { offset, virtual };
}

// Set caret to a given offset in the virtual text string.
function setCaretTextOffset(target) {
  const segs = [];
  const build = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      segs.push({ node, len: node.textContent.length, kind: "text" });
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.classList && node.classList.contains("emote") && node.dataset.name) {
        segs.push({ node, len: node.dataset.name.length, kind: "emote" });
      } else if (node.tagName === "BR") {
        segs.push({ node, len: 1, kind: "br" });
      } else {
        node.childNodes.forEach(build);
      }
    }
  };
  input.childNodes.forEach(build);

  let remaining = target;
  for (const s of segs) {
    if (remaining <= s.len) {
      if (s.kind === "text") {
        const r = document.createRange();
        r.setStart(s.node, remaining);
        r.collapse(true);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      } else {
        // put caret before the emote node
        const r = document.createRange();
        r.setStartBefore(s.node);
        r.collapse(true);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      }
      return;
    }
    remaining -= s.len;
  }
  // end of content
  const r = document.createRange();
  r.selectNodeContents(input);
  r.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
}

// Render emote words in the contenteditable as <img>. Preserves caret by
// snapshotting the caret offset, rebuilding, and restoring.
// A word only becomes an emote image once it's COMPLETE: the caret must be
// past the end of the token (i.e. the user typed a space / moved on). If the
// caret is inside or at the end of the token, it's still being typed — keep
// it as text so "Kapp" doesn't flash to an image while typing "Kappa".
function renderEmotesInEditor() {
  const caret = getCaretTextOffset();
  const virtual = caret ? caret.virtual : getEditorText();
  const caretOffset = caret ? caret.offset : virtual.length;

  // Tokenize the virtual text: split into words and non-words, tracking
  // each token's span so we know if the caret is still on it.
  const frag = document.createDocumentFragment();
  const tokens = virtual.split(/(\s+)/);
  let pos = 0;
  for (const tok of tokens) {
    if (!tok) {
      continue;
    }
    const start = pos;
    const end = pos + tok.length;
    pos = end;

    if (/^\s+$/.test(tok)) {
      frag.append(document.createTextNode(tok));
      continue;
    }

    const beingTyped = caretOffset > start && caretOffset <= end;
    const e = !beingTyped ? state.customEmotes.get(tok) : null;
    if (e) {
      const img = document.createElement("img");
      img.className = "emote";
      img.src = e.url;
      img.alt = tok;
      img.dataset.name = tok;
      img.title = `${tok} · ${e.provider}`;
      frag.append(img);
    } else {
      frag.append(document.createTextNode(tok));
    }
  }
  input.innerHTML = "";
  input.append(frag);
  if (caret) setCaretTextOffset(caret.offset);
}

// Insert text at the caret (plain text), then re-render emotes.
function insertEditorText(text) {
  const caret = getCaretTextOffset();
  const virtual = caret ? caret.virtual : getEditorText();
  const pos = caret ? caret.offset : virtual.length;
  const newVirtual = virtual.slice(0, pos) + text + virtual.slice(pos);
  // set content
  const frag = document.createDocumentFragment();
  frag.append(document.createTextNode(newVirtual));
  input.innerHTML = "";
  input.append(frag);
  setCaretTextOffset(pos + text.length);
  renderEmotesInEditor();
  input.focus();
}

// Get the word before the caret (in virtual-text terms), returning the
// caret offset at word start so we can replace exactly that word.
function getWordBeforeCursor() {
  const caret = getCaretTextOffset();
  if (!caret) return null;
  const { offset, virtual } = caret;
  const before = virtual.slice(0, offset);
  // Match a word, optionally preceded by @ (for user mentions). Bare "@"
  // matches too (zero word chars) so tab shows the full roster.
  const m = before.match(/(^|\s)(@[\w:]*|[\w:]+)$/);
  if (!m) return null;
  return { start: offset - m[2].length, word: m[2] };
}

function findEmoteMatches(word) {
  if (!word) return [];
  const w = word.toLowerCase();
  const out = [];
  for (const [name, e] of state.customEmotes) {
    if (name.toLowerCase().startsWith(w)) {
      out.push({ name, e });
    }
  }
  // Sort: exact-ish first (shortest name), then alpha
  out.sort((a, b) => {
    const ad = Math.abs(a.name.length - word.length);
    const bd = Math.abs(b.name.length - word.length);
    if (ad !== bd) return ad - bd;
    return a.name.localeCompare(b.name);
  });
  return out;
}

function findUserMatches(partial) {
  const p = partial.toLowerCase();
  const out = [];
  for (const [login, u] of state.knownUsers) {
    // Match against both login and display name (case-insensitive prefix).
    if (
      login.startsWith(p) ||
      (u.display && u.display.toLowerCase().startsWith(p))
    ) {
      out.push({ name: "@" + u.login, user: u });
    }
  }
  out.sort((a, b) => {
    const ad = Math.abs(a.user.login.length - partial.length);
    const bd = Math.abs(b.user.login.length - partial.length);
    if (ad !== bd) return ad - bd;
    return a.user.login.localeCompare(b.user.login);
  });
  return out;
}

// Tab-completion dispatcher: "@..." → users, otherwise → emotes.
function getTabMatches(word) {
  if (word.startsWith("@")) return findUserMatches(word.slice(1));
  return findEmoteMatches(word);
}

function openTabPop(matches, start, word) {
  if (!matches.length) {
    closeTabPop();
    return;
  }
  tabState = { start, word, matches, selected: 0 };
  tabList.innerHTML = "";
  matches.slice(0, 8).forEach((m, i) => {
    const item = document.createElement("div");
    item.className = "tab-item" + (i === 0 ? " selected" : "");
    if (m.user) {
      // User mention: @login badge + display name
      const badge = document.createElement("span");
      badge.className = "tab-user-badge";
      badge.textContent = "@";
      const name = document.createElement("span");
      name.className = "tab-name";
      name.textContent = m.user.login;
      if (m.user.color) name.style.color = m.user.color;
      const prov = document.createElement("span");
      prov.className = "tab-provider";
      prov.textContent = m.user.display && m.user.display !== m.user.login ? m.user.display : "user";
      item.append(badge, name, prov);
    } else {
      const img = document.createElement("img");
      img.src = m.e.url;
      img.loading = "lazy";
      img.alt = m.name;
      const name = document.createElement("span");
      name.className = "tab-name";
      name.textContent = m.name;
      const prov = document.createElement("span");
      prov.className = "tab-provider";
      prov.textContent = m.e.provider;
      item.append(img, name, prov);
    }
    item.addEventListener("click", () => {
      applyTabCompletion(m);
    });
    tabList.append(item);
  });
  tabPop.classList.remove("hidden");
}

function closeTabPop() {
  tabState = null;
  tabPop.classList.add("hidden");
  tabList.innerHTML = "";
}

function moveTabSelection(delta) {
  if (!tabState || !tabState.matches.length) return;
  const n = Math.min(tabState.matches.length, 8);
  tabState.selected = (tabState.selected + delta + n) % n;
  [...tabList.children].forEach((el, i) => {
    el.classList.toggle("selected", i === tabState.selected);
  });
  const sel = tabList.children[tabState.selected];
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

function applyTabCompletion(m) {
  if (!tabState) return;
  const { start } = tabState;
  // Replace the word [start, caret) with the emote name + space, via
  // virtual-text surgery, then re-render.
  const caret = getCaretTextOffset();
  const virtual = caret ? caret.virtual : getEditorText();
  const end = caret ? caret.offset : virtual.length;
  const newVirtual = virtual.slice(0, start) + m.name + " " + virtual.slice(end);
  input.innerHTML = "";
  input.append(document.createTextNode(newVirtual));
  setCaretTextOffset(start + m.name.length + 1);
  renderEmotesInEditor();
  input.focus();
  closeTabPop();
}

input.addEventListener("keydown", (ev) => {
  if (ev.key === "Tab") {
    const wb = getWordBeforeCursor();
    if (wb && (tabState || getTabMatches(wb.word).length)) {
      ev.preventDefault();
      if (tabState && tabState.word === wb.word) {
        // Cycle through the open list
        moveTabSelection(ev.shiftKey ? -1 : 1);
      } else {
        const matches = getTabMatches(wb.word);
        openTabPop(matches, wb.start, wb.word);
      }
    }
  } else if (ev.key === "Enter") {
    if (tabState && !ev.shiftKey) {
      const m = tabState.matches[tabState.selected];
      if (m) {
        ev.preventDefault();
        applyTabCompletion(m);
        return;
      }
    }
  } else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    const wb = getWordBeforeCursor();
    if (tabState) {
      ev.preventDefault();
      moveTabSelection(ev.key === "ArrowDown" ? 1 : -1);
    } else if (wb && getTabMatches(wb.word).length) {
      // Arrow keys also open the list, like FFZ
      ev.preventDefault();
      const matches = getTabMatches(wb.word);
      openTabPop(matches, wb.start, wb.word);
    }
  } else if (ev.key === "Escape") {
    closeTabPop();
  } else if (tabState && ev.key.length === 1) {
    // typing changes the word — re-evaluate or close
    const wb = getWordBeforeCursor();
    if (!wb || wb.word !== tabState.word) {
      closeTabPop();
    }
  }
});

input.addEventListener("input", () => {
  // Auto-open @-mention completion while typing (Discord-style) — no Tab needed.
  const wb = getWordBeforeCursor();
  if (wb && wb.word.startsWith("@")) {
    const matches = getTabMatches(wb.word);
    if (matches.length) {
      openTabPop(matches, wb.start, wb.word);
    } else {
      closeTabPop();
    }
  } else {
    closeTabPop();
  }
  renderEmotesInEditor();
});

/* ------------------------------- channel ----------------------------- */

async function joinChannel(info) {
  state.wantedChannel = info;
  chanNameEl.textContent = info.name || info.login;
  msgsEl.innerHTML = "";
  _pendingMsgs = [];
  state.emotesReady = false;
  _emoteLoadBits = 0;
  // Subscriber emote sets are global (same for every channel) and only
  // arrive once via USERSTATE — if already loaded, count that bit now so a
  // channel switch doesn't deadlock the ready flag.
  if (state.mySetEmotes.length > 0) _emoteLoadBits |= 1;

  if (info.id !== state.loadedEmoteChannel) {
    state.loadedEmoteChannel = info.id;
    loadEmotes(info.id);
  } else if (state.customEmotes.size > 0) {
    // emotes already cached for this channel — count the channel bit and
    // flush immediately if sets are also cached
    _emoteLoadBits |= 2;
    if (_emoteLoadBits === 3) flushEmoteReady();
  }

  if (!state.connected) {
    setConn("connecting…", "connecting");
    try {
      await connectIRC();
    } catch (e) {
      setConn("connection failed", "error");
      scheduleReconnect();
    }
  } else if (state.joinedChannel && state.joinedChannel.login !== info.login) {
    state.ws.send("PART #" + state.joinedChannel.login);
    state.joinedChannel = info;
    state.ws.send("JOIN #" + info.login);
  } else if (!state.joinedChannel) {
    state.ws.send("JOIN #" + info.login);
  }
  updateComposer();
}

bridge({ action: "ready" });
