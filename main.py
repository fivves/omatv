#!/usr/bin/env python3
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor

# GTK 4.18+ defaults to the GPU (ngl/vulkan) renderer, which hands a GtkGLArea's
# GL texture to the scene graph through a cross-API copy. On this stack (GTK
# 4.22 + mesa/radv) that path is both expensive and corrupt: GNOME/gtk#8133 is
# the CPU copy it costs without GSK_RENDERER=gl, and GNOME/gtk#6703 is the
# GLArea output texture being clobbered. Both showed up here as the video
# freezing, then returning vertically flipped and washed out (restart only).
# GTK's GL backend samples the GLArea texture natively, no copy, no clobber.
# setdefault, so an explicit GSK_RENDERER in the environment still wins.
os.environ.setdefault("GSK_RENDERER", "gl")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango, WebKit

import home
import mpv

APP_ID = "io.github.omarchy.omatv"
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_DIR = os.path.join(HERE, "chat")
CACHE = os.path.expanduser("~/.cache/omatv")
AVATAR_DIR = os.path.join(CACHE, "avatars")
EMOTE_DIR = os.path.join(CACHE, "emotes")
# Emote sets and badge maps change when you sub, unsub, or a channel changes its
# emotes, not hour to hour, so a window this wide costs nothing and saves every
# launch a pile of round trips. Keys carry the account/channel id, so switching
# Twitch accounts cannot serve one account's emotes to another.
EMOTE_TTL_S = 6 * 3600
# The bridge's fetch proxy is generic, so only these hosts (the third-party
# emote lists the chat pulls on startup) get cached; any other url stays live.
EMOTE_HOSTS = ("betterttv.net", "frankerfacez.com", "7tv.io")
SETTINGS_PATH = os.path.expanduser("~/.config/omatv/settings.json")
DEFAULT_CHAT_WIDTH = 340

# The chat page owns its own palette (chat.css :root). Its background follows
# the GTK theme instead of being hardcoded: this file is the palette the running
# Omarchy theme actually paints GTK apps with (gtk-theme-colors rewrites it on
# every theme change, and gtk.css imports it), and the two panel shades keep the
# +7/+13 lift the chat's panels always had over its own background.
CHAT_THEME_FILE = os.path.join(
    os.path.expanduser("~"), ".config", "gtk-4.0", "omarchy-theme.css")
CHAT_THEME_KEY = "window-bg-color"
CHAT_THEME_FALLBACK = "#131317"
CHAT_PANEL_LIFT = (7, 13)


def read_theme_palette(path=CHAT_THEME_FILE):
    """{name: '#rrggbb'} from the palette's :root block, {} if unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    return {name: val for name, val in
            re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", text)}


def lift_hex(color, delta):
    """#rrggbb with delta added per channel, clamped (bad input -> fallback)."""
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color or ""):
        return CHAT_THEME_FALLBACK
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(color[i:i + 2], 16) + delta)) for i in (1, 3, 5))


# Deferral for the single-click mute so the first half of a double-click
# doesn't mute before the fullscreen. Keep this short for a snappy mute;
# double-clicks slower than this will mute first.
SINGLE_CLICK_DELAY_MS = 200

# Every stream is loaded at SOURCE quality once; there is no quality switching
# anywhere. See home.STREAM_QUALITY.
#
# hwdec for tile players. auto-safe is forbidden on this hardware: ffmpeg's
# Vulkan hwaccel probe (auto-safe) segfaults. An explicit vaapi request does
# not enumerate Vulkan, so it is the candidate; the shipped value is whatever
# the 60s source-quality multi-tile run proved stable, else "no".
TILE_HWDEC = os.environ.get("OMATV_TILE_HWDEC", "vaapi")

# Responsive layout budget. The chat is an OVERLAY, so it never contributes to
# the content's minimum width; when the window cannot fit it and a minimum
# video pane it collapses to a toggleable pane that slides in from the right.
CHAT_MIN_W = 320          # WebKitWebView measure minimum (hard floor)
VIDEO_MIN_W = 360         # never shrink the watch video below this
CHAT_COLLAPSE_W = CHAT_MIN_W + VIDEO_MIN_W + 24   # narrower than this: dock the chat UNDER the video
# Stacked (vertical) chat. A window too narrow to hold the chat beside the video
# docks it under the video instead, spanning the full width: video on top, chat
# across the bottom. Previously that case just dropped the chat entirely.
VIDEO_MIN_H = 200          # never shrink the watch video below this
CHAT_STACK_FRACTION = 0.45  # share of the content height the docked chat takes
CHAT_STACK_MIN_H = 180      # below this a docked chat is not worth showing
CHAT_SIDE_MIN_W = 200       # narrowest the chat column goes to keep the video bar-free
STREAM_ASPECT = 16 / 9      # what the video area is really judged on
# A WebKitWebView reports its LAST ALLOCATION as its natural width. Once the
# stacked dock has stretched it to the window width, that number is what the
# side dock would size itself from, leaving the chat half the screen wide on top
# of the video. The chat therefore lives inside an Adw.Clamp (the one container
# that caps a child's natural width): the cap is the column width when the chat
# is a side column, and effectively unlimited when it spans the bottom.
CHAT_CLAMP_UNLIMITED = 100000
# Below this window width the header drops to a minimal button layout (Eddie,
# Oct 15 2026). WIDTH, not height, is what crowds the bar, so the rule keys on
# width alone. Measured crowding: at phone widths (~540) the title block, the
# chat-dock and fullscreen buttons, the font-style pair, the avatar+name chip
# and libadwaita's own window controls were all fighting over the same row.
HEADER_MINIMAL_W = 700
CONFIG_CANDIDATES = [
    os.environ.get("OMATV_TWITCH_JSON", ""),
    os.path.expanduser("~/.config/twitch.json"),
    os.path.expanduser("~/twitch.json"),
]

GL_DRAW_FRAMEBUFFER_BINDING = 0x8CA6

# Font styles, ported from gTunes' "Font Style" control (ui.font.mono there).
# The GTK side is a CSS class on the window; the chat WebView is a separate
# engine the class cannot reach, so it gets the same choice pushed as a variable.
FONT_UI = '"Inter", "Cantarell", system-ui, sans-serif'
FONT_MONO = '"JetBrainsMono Nerd Font", "JetBrains Mono", monospace'

APP_CSS = b"""
/* Font style (ported from gTunes). `.font-mono *` restyles every widget in the
   window; the chat page is handled separately in apply_chat_theme(). */
.font-mono * {
  font-family: "JetBrainsMono Nerd Font", "JetBrains Mono", monospace;
}
.font-style-toggle {
  padding: 2px 8px;
  min-width: 0;
  min-height: 0;
}
.avatar-letter {
  border-radius: 9999px;
  background: #3f3f46;
  color: #efeff1;
  font-weight: 700;
  font-size: 17px;
}
.small-avatar {
  font-size: 12px;
  border-radius: 9999px;
  background: linear-gradient(135deg, #a970ff, #7c3aed);
  color: white;
}
.avatar-img { border-radius: 9999px; }
.offline { opacity: 0.42; }
.live-dot {
  border-radius: 9999px;
  background: #22c55e;
  min-width: 11px;
  min-height: 11px;
  border: 2px solid #1a1a20;
}
.osd-pill {
  background: rgba(19,19,23,0.85);
  border-radius: 9999px;
  padding: 8px 16px;
  margin-top: 14px;
  color: #efeff1;
}
.dim-placeholder { opacity: 0.55; }
.subtitle { font-size: 12px; }
.game-pill {
  font-size: 10px;
  font-weight: 600;
  color: #a78bfa;
  background: rgba(167, 139, 250, 0.12);
  border: 1px solid rgba(167, 139, 250, 0.28);
  border-radius: 9999px;
  padding: 0 10px;
  min-height: 18px;
  margin-top: 1px;
}
.mute-badge {
  background: rgba(239, 68, 68, 0.92);
  color: white;
  border-radius: 9999px;
  padding: 6px 12px;
  font-weight: 700;
  font-size: 12px;
}
.chat-handle {
  background: rgba(255, 255, 255, 0.06);
}
.chat-handle:hover { background: rgba(255, 255, 255, 0.16); }
"""


def idle(fn, *args):
    GLib.idle_add(lambda: fn(*args) or False)


def load_config():
    for path in CONFIG_CANDIDATES:
        if path and os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    raise FileNotFoundError(
        "twitch.json not found (looked in ~/.config/twitch.json and ~/twitch.json)"
    )


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print("save settings failed:", e)


class TwitchAPI:
    def __init__(self, cfg):
        self.client_id = cfg["TWITCH_CLIENT_ID"]
        self.token = cfg["TWITCH_ACCESS_TOKEN"].removeprefix("oauth:")
        self.user_id = str(cfg.get("TWITCH_FOLLOWED_USER_ID") or "")
        self.login = ""
        self.scopes = []
        self.base = "https://api.twitch.tv/helix"

    def _get(self, path, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Client-ID": self.client_id,
                "Authorization": "Bearer " + self.token,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def get_user_id(self, login):
        """Resolve a channel login to its numeric user id via Helix."""
        d = self._get("/users", login=login)
        data = d.get("data", [])
        return data[0]["id"] if data else None

    def me(self):
        """Fetch the authenticated user's profile (avatar, display name)."""
        d = self._get("/users", id=self.user_id)
        data = d.get("data", [])
        if not data:
            return {}
        u = data[0]
        return {
            "id": u.get("id", ""),
            "login": u.get("login", ""),
            "display_name": u.get("display_name", ""),
            "avatar": u.get("profile_image_url", ""),
        }

    def validate(self):
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": "OAuth " + self.token},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        self.login = d.get("login", "")
        self.user_id = d.get("user_id") or self.user_id
        self.scopes = d.get("scopes", [])
        return d

    @property
    def can_chat(self):
        return "chat:edit" in self.scopes

    def followed(self):
        data = self._get("/channels/followed", user_id=self.user_id, first=100).get(
            "data", []
        )
        return [
            {
                "id": x["broadcaster_id"],
                "login": x["broadcaster_login"],
                "name": x["broadcaster_name"],
            }
            for x in data
        ]

    def users(self, ids):
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            q = "&".join("id=" + x for x in chunk)
            for u in self._get("/users?" + q).get("data", []):
                out[u["id"]] = u
        return out

    def streams(self, ids):
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            q = "&".join("user_id=" + x for x in chunk)
            for s in self._get("/streams?" + q).get("data", []):
                out[s["user_id"]] = s
        return out

    def fetch_badges(self, broadcaster_id):
        """Fetch global + channel badge sets via Helix. Returns a JSON string
        mapping set_id -> {version_id: image_url_2x}."""
        urls = ["https://api.twitch.tv/helix/chat/badges/global"]
        if broadcaster_id:
            urls.append(
                "https://api.twitch.tv/helix/chat/badges?broadcaster_id="
                + urllib.parse.quote(broadcaster_id)
            )

        def one(url):
            key = "url-" + cache_key_for_url(url)[0]

            def produce():
                req = urllib.request.Request(
                    url,
                    headers={
                        "Client-ID": self.client_id,
                        "Authorization": "Bearer " + self.token,
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.load(r).get("data", [])

            try:
                return cached(key, produce)[0]
            except Exception:
                return []

        out = {}
        for data in fetch_all(urls, one):
            for s in data:
                sid = s.get("set_id", "")
                if not sid:
                    continue
                versions = out.setdefault(sid, {})
                for v in s.get("versions", []):
                    vid = v.get("id", "")
                    url2x = v.get("image_url_2x") or v.get("image_url_1x")
                    if vid and url2x:
                        versions[vid] = url2x
        return json.dumps(out, ensure_ascii=False)

    def stream_info(self, login_or_id):
        """Fetch live stream info (title, game, viewers) for one channel.
        Accepts a login or a numeric id; returns a dict or None.
        NOTE: /streams uses user_login (NOT login) for the name param."""
        key = "user_id" if str(login_or_id).isdigit() else "user_login"
        d = self._get("/streams?" + key + "=" + urllib.parse.quote(str(login_or_id)))
        data = d.get("data", [])
        if not data:
            return None
        s = data[0]
        return {
            "title": s.get("title", ""),
            "game": s.get("game_name", ""),
            "viewers": s.get("viewer_count", 0),
            "started_at": s.get("started_at", ""),
        }

    def channel_info(self, login):
        """Resolve a login into the info dict the grid uses, followed or not.

        The grid builds these dicts in refresh_follows; paste-to-watch needs the
        same shape for a channel that is not in the followed list. Returns None
        when no such channel exists; `live` is False when it exists but is not
        streaming.
        """
        cid = self.get_user_id(login)
        if not cid:
            return None
        u = self.users([cid]).get(cid, {})
        s = self.streams([cid]).get(cid)
        return {
            "id": cid,
            "login": u.get("login") or login,
            "name": u.get("display_name") or login,
            "avatar": u.get("profile_image_url", ""),
            "live": bool(s),
            "viewers": s.get("viewer_count") if s else 0,
            "game": s.get("game_name") if s else "",
            "title": s.get("title") if s else "",
        }


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "omatv/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def download_avatar(user_id, url):
    os.makedirs(AVATAR_DIR, exist_ok=True)
    path = os.path.join(AVATAR_DIR, user_id + ".img")
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": "omatv/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    return path


def cache_key_for_url(url):
    """Cache key for a request url, or None when it must not be cached."""
    parts = urllib.parse.urlsplit(url)
    return hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json", (parts.hostname or "")


def emote_cache_key(url):
    """Cache key for the third-party emote-list urls, else None."""
    key, host = cache_key_for_url(url)
    if not any(host == h or host.endswith("." + h) for h in EMOTE_HOSTS):
        return None
    return "url-" + key


def cache_load(key):
    """(body, age_seconds) for a cached blob, or (None, None)."""
    try:
        with open(os.path.join(EMOTE_DIR, key), "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        return blob.get("body"), time.time() - float(blob.get("ts", 0))
    except Exception:
        return None, None


def cache_save(key, body):
    """Write atomically, so a kill mid-write cannot leave a half file behind."""
    try:
        os.makedirs(EMOTE_DIR, exist_ok=True)
        path = os.path.join(EMOTE_DIR, key)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "body": body}, fh)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def cached(key, produce, ttl=EMOTE_TTL_S):
    """(body, freshly_fetched) for a blob.

    Serves the cached copy while it is fresh, refreshes it when it is not, and
    keeps the stale copy when the refresh fails, since stale emote names still
    render and beat no emotes at all on a flaky connection."""
    body, age = cache_load(key)
    if body is not None and age is not None and age < ttl:
        return body, False
    try:
        fresh = produce()
    except Exception:
        if body is not None:
            return body, False
        raise
    cache_save(key, fresh)
    return fresh, True


def fetch_all(items, fn, workers=8):
    """Run fn over items concurrently, keeping input order.

    Each urllib urlopen opens its own connection, so every item pays a fresh DNS
    + TCP + TLS handshake. The twenty emote sets are independent, and fetching
    them one at a time was the whole of the chat's startup delay."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


def order_rail(rail, order):
    """Rearrange `rail`'s children into `order` (left to right).

    Gtk.Box has no "move child to index" call, so the sequence is rebuilt with
    reorder_child_after: walking left to right and anchoring each button to its
    predecessor lands the exact order. The widgets are never recreated, and a
    button that is already in place is not touched, so this is safe to run on
    every refresh.
    """
    prev = None
    for child in order:
        if child.get_parent() is rail:
            if rail.get_first_child() is child and prev is None:
                prev = child
                continue
            rail.reorder_child_after(child, prev)
        else:
            rail.append(child)
        prev = child


# First path segments of a twitch.tv URL that are never channel logins. A
# pasted link must not be opened as a channel because it happened to parse.
TWITCH_NON_CHANNEL = {
    "about", "activate", "bits", "channel", "collections", "creatorcamp",
    "directory", "download", "downloads", "drops", "friends", "following",
    "inventory", "jobs", "legal", "login", "messages", "notifications", "p",
    "privacy", "products", "purchases", "redeem", "search", "security",
    "settings", "signup", "store", "subscriptions", "terms", "turbo", "videos",
    "wallet",
}

LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")


def _clean_login(value):
    login = (value or "").strip().lower()
    if not login or login in TWITCH_NON_CHANNEL:
        return None
    if not LOGIN_RE.match(login):
        return None
    return login


def parse_twitch_login(text):
    """Pull a channel login out of pasted text, or None if it is not a link.

    Handles what the address bar or the share button gives you: twitch.tv/<login>
    with any of www./m. and a trailing slash, query string or fragment;
    player.twitch.tv/?channel=<login>; and /<login>/clip/<id> or /<login>/videos
    (a clip link still names its channel). twitch.tv/videos/<id> names no channel
    and is refused, as is anything that is not a twitch.tv URL.
    """
    if not text:
        return None
    raw = text.strip().strip("<>")
    if not raw or len(raw) > 400:
        return None
    # A pasted line often carries the message around the link; take the token
    # that holds the host rather than assuming the clipboard is only the URL.
    tokens = raw.split()
    raw = next((t for t in tokens if "twitch.tv" in t.lower()), None)
    if raw is None:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    host = (parts.netloc or "").lower().split(":")[0]
    for prefix in ("www.", "m.", "go."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    if host == "player.twitch.tv":
        q = urllib.parse.parse_qs(parts.query)
        return _clean_login((q.get("channel") or [""])[0])
    if host != "twitch.tv":
        return None
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return None
    return _clean_login(segs[0])


def parse_dropped_login(text):
    """Pull a channel login out of DROPPED text, or None if it is not one.

    A drag is looser than a paste: dragging a Discord message or a selected name
    hands over plain words, so a bare login ("shroud") is accepted here where
    the clipboard path insists on a link. Anything carrying a scheme, a path
    separator or whitespace is NOT a name, and that is what stops a dropped
    FILE ("file:///home/eddie/...") from being opened as a channel called
    "home". Real link forms are the other parser's job; this one only ever sees
    what that one refused.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 400:
        return None
    if "://" in raw or "/" in raw or "\\" in raw:
        return None
    if any(ch.isspace() for ch in raw):
        return None
    return _clean_login(raw)


class StreamerButton(Gtk.Button):
    def __init__(self, info):
        super().__init__()
        self.info = info
        self.add_css_class("flat")
        self.set_tooltip_text(info["name"] + "\nOffline")

        overlay = Gtk.Overlay()
        overlay.set_size_request(48, 48)

        self.avatar = Gtk.Label(label=(info["name"][:1] or "?").upper())
        self.avatar.add_css_class("avatar-letter")
        self.avatar.set_valign(Gtk.Align.CENTER)
        self.avatar.set_halign(Gtk.Align.CENTER)
        self.avatar.set_overflow(Gtk.Overflow.HIDDEN)
        self.avatar.set_size_request(44, 44)
        overlay.set_child(self.avatar)

        self.live_dot = Gtk.Box()
        self.live_dot.add_css_class("live-dot")
        self.live_dot.set_valign(Gtk.Align.END)
        self.live_dot.set_halign(Gtk.Align.END)
        self.live_dot.set_margin_bottom(2)
        self.live_dot.set_margin_end(2)
        # A live dot fades in/out rather than popping when a stream starts or
        # stops (same 300ms motion as everything else).
        self.live_dot_rev = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            transition_duration=home.MOTION_MS,
            child=self.live_dot,
            reveal_child=False,
        )
        self.live_dot_rev.set_valign(Gtk.Align.END)
        self.live_dot_rev.set_halign(Gtk.Align.END)
        overlay.add_overlay(self.live_dot_rev)

        self.set_child(overlay)
        # Set once a real avatar image replaces the letter placeholder; the
        # state class has to follow whichever widget is actually visible.
        self._avatar_pic = None
        self._apply_live(False)

    def set_avatar_texture(self, texture):
        pic = Gtk.Picture.new_for_paintable(texture)
        pic.set_content_fit(Gtk.ContentFit.COVER)
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        pic.set_size_request(44, 44)
        pic.add_css_class("avatar-img")
        self.avatar.set_visible(False)
        overlay = self.get_child()
        overlay.set_child(pic)
        # From here on the picture is what the user sees, so it is what carries
        # the live/offline state too (see _apply_live).
        self._avatar_pic = pic
        self._apply_live(self.info.get("live", False))

    def set_state(self, live, game):
        self.info["live"] = live
        self.info["game"] = game
        if self.live_dot_rev.get_reveal_child() != bool(live):
            self.live_dot_rev.set_reveal_child(bool(live))
        self._apply_live(live)
        self.set_tooltip_text(
            f"{self.info['name']}\n{game if live else 'Offline'}"
        )

    def _apply_live(self, live):
        # Apply the state class to the avatar the user actually SEES. Once a
        # real avatar downloads, set_avatar_texture hides the letter label and
        # installs a Gtk.Picture on top, so styling only the label left every
        # offline channel looking live: the rail never showed a channel going
        # offline (Eddie, Oct 11 2026).
        for child in (self.avatar, self._avatar_pic):
            if child is None:
                continue
            child.remove_css_class("offline")
            child.remove_css_class("live")
            child.add_css_class("live" if live else "offline")


class VideoArea(Gtk.GLArea):
    _seq = 0

    def __init__(self, app, tile=False):
        super().__init__()
        self.app = app
        # tile=True: a secondary muted player whose END_FILE / mute callbacks
        # must stay inert (they'd otherwise drive the window's one-player
        # channel-switch and mute-badge behaviour).
        self.tile = tile
        VideoArea._seq += 1
        # Stable identity for the debug log: every player created/stopped/
        # crossfaded is named so a log reader can follow one element.
        self.pid = ("T" if tile else "H") + str(VideoArea._seq)
        self.player = None
        self.render_ctx = None
        self._proc_fn = None
        self._init_params = None
        self._gl = None
        self._pending_url = None
        self.connect("realize", self.on_realize)
        self.connect("render", self.on_render)
        self.connect("unrealize", self.on_unrealize)

    def is_rendering(self):
        """True once mpv has a configured video output AND is past the first
        frame. Used to time the crossfade so it never covers a black frame."""
        p = self.player
        if p is None:
            return False
        try:
            if not p.vo_configured:
                return False
            return not p.core_idle
        except Exception:
            return False

    def on_realize(self, *a):
        self.make_current()
        try:
            egl = ctypes.CDLL("libEGL.so.1")
            egl.eglGetProcAddress.restype = ctypes.c_void_p
            egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
            egl_proc = egl.eglGetProcAddress
        except OSError:
            egl_proc = None
        try:
            glx = ctypes.CDLL("libGL.so.1")
            glx.glXGetProcAddress.restype = ctypes.c_void_p
            glx.glXGetProcAddress.argtypes = [ctypes.c_char_p]
            glx_proc = glx.glXGetProcAddress
        except OSError:
            glx = None
            glx_proc = None

        self._gl = glx

        def get_proc(_ctx, name):
            p = egl_proc(name) if egl_proc else 0
            if not p and glx_proc:
                p = glx_proc(name)
            return p or 0

        self._proc_fn = mpv.MpvGlGetProcAddressFn(get_proc)
        self._init_params = mpv.MpvOpenGLInitParams(self._proc_fn)

        self.player = mpv.MPV(
            vo="libmpv",
            gpu_api="opengl",
            # Tiles must never hardware-decode via auto-safe: ffmpeg's Vulkan
            # hwaccel probe segfaults on this machine's radv (core dump:
            # libavutil -> vkEnumerateInstanceExtensionProperties -> call
            # through a NULL pointer on a thread named "core"). auto-safe
            # ENUMERATES Vulkan; an explicit vaapi request does not. TILE_HWDEC
            # is read from OMATV_TILE_HWDEC so the choice can be A/B tested
            # without an edit; the shipped value is whatever the 60s
            # source-quality run proved stable (see report).
            hwdec=(TILE_HWDEC if self.tile else "auto-safe"),
            network_timeout="20",
            osd_level="0",
            # Low-latency live tuning: cap how much mpv buffers ahead.
            # Defaults (150MB demuxer readahead, hour-long cache window) add
            # several seconds of latency to a live stream.
            cache="no",
            demuxer_max_bytes="8MiB",
            demuxer_readahead_secs="2",
            cache_pause="no",
            cache_pause_initial="no",
            # ffmpeg's HLS demuxer starts 3 segments (-3) before the live edge
            # by default = ~6s of latency. -1 (the edge) stutters on cold start
            # because there's zero cushion; -2 keeps one segment of slack to
            # fetch ahead while staying near the edge.
            demuxer_lavf_o="live_start_index=-2",
            # A tile never makes sound: muted from construction.
            **({"mute": True} if self.tile else {}),
        )

        @self.player.event_callback(mpv.MpvEventID.END_FILE)
        def on_end(event):
            reason = getattr(event.data, "reason", None) if event.data else None
            reason_name = getattr(reason, "name", None) if reason is not None else None
            # Every player is a tile now; the window acts only if this one is
            # the current watch player (on_player_event checks that).
            idle(self.app.on_player_event, reason_name, self)

        self.render_ctx = mpv.MpvRenderContext(
            self.player,
            "opengl",
            opengl_init_params={"get_proc_address": self._proc_fn},
        )
        self._last_render = 0.0

        def want_render():
            # Tiles may skip a render tick while the grid is animating: the
            # video is moving anyway and the UI frame budget is what the eye
            # notices. mpv asks again on its next frame, so nothing is lost.
            if self.tile and home.TILE_RENDER_THROTTLE:
                page = getattr(self.app, "home_page", None)
                if page is not None and page.anim.busy():
                    now = time.monotonic()
                    if now - self._last_render < home.TILE_RENDER_SECS:
                        return
                    self._last_render = now
            idle(self.queue_render)

        self.render_ctx.update_cb = want_render

        # Mute badge follows mpv's real mute state; tiles never touch it.
        if not self.tile:
            self.player.observe_property(
                "mute", lambda _name, val: idle(self.app.on_mute_changed, bool(val))
            )

        # Perf truth in the log: which backend mpv actually picked for this
        # player (vaapi, or a silent fallback to software). Tile decode cost is
        # then a fact in the log rather than an assumption about TILE_HWDEC.
        self.player.observe_property(
            "hwdec-current",
            lambda _name, val: print(
                f"[omatv][hw] {self.pid} tile={self.tile} hwdec-current={val}",
                flush=True,
            ),
        )

        if self._pending_url:
            url = self._pending_url
            self._pending_url = None
            self.player.command("loadfile", url)

    def on_render(self, area, ctx):
        if not self.render_ctx:
            return True
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w <= 0 or h <= 0:
            return True
        fbo = ctypes.c_int(0)
        gl = self._gl
        if gl is not None:
            gl.glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, ctypes.byref(fbo))
        try:
            self.render_ctx.render(
                opengl_fbo={
                    "w": w,
                    "h": h,
                    "fbo": fbo.value,
                    "internal_format": 0,
                },
                flip_y=True,
            )
        except Exception as e:
            print("render error:", e)
        return True

    def play_url(self, url):
        if self.player:
            self.player.command("loadfile", url)
        else:
            self._pending_url = url

    def stop(self):
        self._pending_url = None
        if self.player:
            self.player.command("stop")

    def set_volume(self, v):
        if self.player:
            self.player.volume = max(0, min(130, v))

    def toggle_mute(self):
        if not self.player:
            return False
        self.player.mute = not self.player.mute
        return True

    def is_muted(self):
        return bool(self.player and self.player.mute)

    def on_unrealize(self, *a):
        if self.render_ctx:
            self.render_ctx.update_cb = None
            self.render_ctx.free()
            self.render_ctx = None
        if self.player:
            try:
                self.player.terminate()
            except Exception:
                pass
            self.player = None

    def shutdown(self):
        if self.render_ctx:
            self.render_ctx.update_cb = None
            self.render_ctx.free()
            self.render_ctx = None
        if self.player:
            try:
                self.player.terminate()
            except Exception:
                pass
            self.player = None


class OmatvWindow(Adw.ApplicationWindow):
    def __init__(self, app, api):
        super().__init__(application=app)
        self.api = api
        self.streamers = {}
        self.buttons = {}
        # Display order of the rail buttons (live first, offline pooled right).
        # Kept so a 60s refresh only reorders when the sequence really changed.
        self._rail_order = []
        self.current_channel = None
        self.current_display_name = None
        self.resolve_gen = 0
        self.send_times = []

        self.set_title("omatv")
        self.set_default_size(1440, 900)

        # Font style, restored from settings.json before the header is built (the
        # toggle's active state reads it). Applied as a class once the CSS
        # provider exists, below.
        self.font_mono = bool(load_settings().get("font_mono", False))

        self.toast_overlay = Adw.ToastOverlay()

        header = Adw.HeaderBar()
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title_box.set_halign(Gtk.Align.CENTER)
        title_label = Gtk.Label(label="omatv")
        title_label.add_css_class("title")
        title_label.set_halign(Gtk.Align.CENTER)
        # A long channel/title string must never be able to set the window's
        # minimum width: ellipsize so the header can always shrink.
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_max_width_chars(16)
        self.subtitle = Gtk.Label(label="pick a streamer to start watching")
        self.subtitle.add_css_class("subtitle")
        self.subtitle.add_css_class("dim-label")
        self.subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle.set_max_width_chars(52)
        self.subtitle.set_halign(Gtk.Align.CENTER)
        self.game_pill = Gtk.Label(label="")
        self.game_pill.add_css_class("game-pill")
        self.game_pill.set_halign(Gtk.Align.CENTER)
        # Long game names ("The Legend of Zelda: Ocarina of Time") must be
        # allowed to shrink, not force the window wider.
        self.game_pill.set_ellipsize(Pango.EllipsizeMode.END)
        self.game_pill.set_max_width_chars(18)
        self.game_pill.set_visible(False)
        subtitle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        subtitle_row.set_halign(Gtk.Align.CENTER)
        subtitle_row.append(self.subtitle)
        subtitle_row.append(self.game_pill)
        title_box.append(title_label)
        title_box.append(subtitle_row)
        header.set_title_widget(title_box)
        self.title_label = title_label

        # Single user button (avatar + name). When signed in it opens a menu
        # with Logout; when signed out it opens the sign-in dialog.
        self.user_menu = Gio.Menu()
        self.user_popover = Gtk.PopoverMenu.new_from_model(self.user_menu)
        self.user_btn = Gtk.MenuButton()
        self.user_btn.add_css_class("flat")
        self.user_btn.set_popover(self.user_popover)
        self.user_btn.set_menu_model(self.user_menu)
        self.user_btn.set_has_frame(False)
        user_chip = Gtk.Box(spacing=8)
        # Adw.Avatar — libadwaita's purpose-built round profile avatar.
        # Constructor enforces the size AND the circle; set_custom_image
        # swaps in the profile pic. (Gtk.Image ignores border-radius;
        # Gtk.Picture renders at natural size — both failed repeatedly.)
        self.user_avatar = Adw.Avatar.new(28, "?", False)
        self.user_avatar.set_show_initials(True)
        user_name = Gtk.Label(label="…")
        # A long username must not be able to set the window's minimum width.
        user_name.set_ellipsize(Pango.EllipsizeMode.END)
        user_name.set_max_width_chars(14)
        user_chip.append(self.user_avatar)
        user_chip.append(user_name)
        self.user_btn.set_child(user_chip)
        # Font style toggle: the same control gTunes has, a linked pair of icon
        # toggles ("Default" / "Monospace"), persisted by set_font_mono().
        font_btn_default = Gtk.ToggleButton()
        font_btn_default.set_icon_name("font-x-generic-symbolic")
        font_btn_default.set_tooltip_text("Default font")
        font_btn_default.set_active(not self.font_mono)
        font_btn_default.add_css_class("font-style-toggle")
        font_btn_mono = Gtk.ToggleButton()
        font_btn_mono.set_icon_name("utilities-terminal-symbolic")
        font_btn_mono.set_tooltip_text("Monospace font")
        font_btn_mono.set_active(self.font_mono)
        font_btn_mono.add_css_class("font-style-toggle")
        font_btn_mono.set_group(font_btn_default)
        font_btn_mono.connect(
            "toggled", lambda btn: self.set_font_mono(btn.get_active()))
        self.font_toggle_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.font_toggle_box.add_css_class("linked")
        self.font_toggle_box.set_valign(Gtk.Align.CENTER)
        self.font_toggle_box.append(font_btn_default)
        self.font_toggle_box.append(font_btn_mono)
        header.pack_end(self.font_toggle_box)
        header.pack_end(self.user_btn)
        self.user_name_lbl = user_name

        # Actions for the user menu (logout / sign-in). Attached to the
        # window's action group so menu items can trigger them.
        self._user_actions = Gio.SimpleActionGroup()
        act_logout = Gio.SimpleAction.new("logout", None)
        act_logout.connect("activate", lambda *_: self.logout())
        self._user_actions.insert(act_logout)
        act_signin = Gio.SimpleAction.new("signin", None)
        act_signin.connect("activate", lambda *_: self.show_login_dialog())
        self._user_actions.insert(act_signin)
        self.insert_action_group("win", self._user_actions)
        self._rebuild_user_menu()

        rail_scroll = Gtk.ScrolledWindow()
        rail_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        rail_scroll.set_max_content_height(64)
        # The rail carries one button per followed channel (87 of them here).
        # Its own minimum must not become the window's minimum.
        rail_scroll.set_min_content_width(1)
        self.rail = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            # No leading margin: the home button sits immediately left of this
            # scroller, so a 12px inside margin read as an oversized gap between
            # the home circle and the first channel. The channel pitch (6px of
            # box spacing plus each button's own 2px inset) is the gap the home
            # button has to match.
            margin_start=0,
            margin_end=12,
            margin_top=4,
            margin_bottom=8,
        )
        # Start-aligned: with the home button pinned outside the scroller the row
        # should begin immediately, never be re-centred.
        self.rail.set_halign(Gtk.Align.START)
        rail_scroll.set_child(self.rail)
        self.rail_scroll = rail_scroll

        # Home button pinned left of the scrollable channel rail.
        rail_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.home_btn = home.make_home_button(self)
        rail_bar.append(self.home_btn)
        rail_scroll.set_hexpand(True)
        rail_bar.append(rail_scroll)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(rail_bar)

        # ---- content surface ------------------------------------------------
        # A single Gtk.Fixed holds every tile's GLArea AND is where the tile
        # that becomes the watch video is grown. A realized GLArea cannot move
        # between containers without being unrealized (which frees its mpv
        # render context and terminates the player), so the home grid and the
        # watch video MUST share a parent — this surface. Because every stream
        # is loaded once at SOURCE quality, selecting a tile only MOVES it: no
        # new player, no new streamlink, no reload, on screen from frame one.
        self.surface = home.GridSurface()
        self.surface.set_valign(Gtk.Align.START)
        self.grid_scroller = Gtk.ScrolledWindow()
        self.grid_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.grid_scroller.set_child(self.surface)
        self.grid_scroller.set_hexpand(True)
        self.grid_scroller.set_vexpand(True)
        # A Gtk.Fixed reports its children's extent as its minimum, so a tile
        # grown to nearly the whole window would make the window's minimum that
        # wide and clip the right edge. Cap the propagated minimum here; the
        # scroller still allocates the surface the full viewport width, so the
        # grid just reflows to fit.
        #
        # GOTCHA, measured: min-content-width only caps the minimum while the
        # horizontal policy is NOT NEVER. With PolicyType.NEVER this cap was a
        # no-op, so a tile grown to the watch rect still floored the window at
        # ~1556px and clipped the right edge. Every grid_scroller policy below
        # must therefore stay AUTOMATIC horizontally - flipping one back to
        # NEVER silently re-breaks resizing.
        self.grid_scroller.set_min_content_width(1)
        self.grid_scroller.set_min_content_height(1)

        self.home_page = home.HomePage(self, VideoArea, self.surface, self.grid_scroller)

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(self.grid_scroller)
        content_overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.content_overlay = content_overlay

        # nobody-live state (themed status page, fades in/out)
        self.empty_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            transition_duration=home.MOTION_MS,
            child=self.home_page.empty,
        )
        content_overlay.add_overlay(self.empty_revealer)

        # Chrome that sits over the watch video. Its background is transparent
        # on purpose: the tile grown underneath must show through it.
        self.video_hud = Gtk.Fixed()
        self.video_hud.set_overflow(Gtk.Overflow.HIDDEN)
        self.watch_chrome = Gtk.Overlay()
        self.watch_chrome.set_child(Gtk.Box())
        self.status_revealer = Gtk.Revealer(
            valign=Gtk.Align.START,
            halign=Gtk.Align.CENTER,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
        )
        self.status_revealer.set_margin_top(14)
        status_pill = Gtk.Box(spacing=8)
        status_pill.add_css_class("osd-pill")
        self.spinner = Gtk.Spinner(spinning=False)
        self.status_label = Gtk.Label(label="")
        status_pill.append(self.spinner)
        status_pill.append(self.status_label)
        self.status_revealer.set_child(status_pill)
        self.watch_chrome.add_overlay(self.status_revealer)

        self.mute_badge = Gtk.Label(label="🔇 Muted")
        self.mute_badge.add_css_class("mute-badge")
        self.mute_badge.set_halign(Gtk.Align.END)
        self.mute_badge.set_valign(Gtk.Align.START)
        self.mute_badge.set_margin_top(14)
        self.mute_badge.set_margin_end(14)
        self.mute_badge.set_visible(False)
        self.watch_chrome.add_overlay(self.mute_badge)

        # Fullscreen + chat toggles live in the header so they are reachable
        # at any window size (the video chrome itself can be too small).
        self.fs_btn = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic")
        self.fs_btn.add_css_class("flat")
        self.fs_btn.set_tooltip_text("Fullscreen (double-click the video)")
        self.fs_btn.connect("clicked", lambda *a: self.toggle_fullscreen())
        header.pack_end(self.fs_btn)

        self.chat_btn = Gtk.ToggleButton.new()
        self.chat_btn.set_icon_name("sidebar-show-symbolic")
        self.chat_btn.add_css_class("flat")
        self.chat_btn.set_tooltip_text("Chat")
        self.chat_btn.connect("toggled", self._on_chat_toggle)
        # START side, at every size (Eddie, Oct 15 2026). The chat is a primary
        # action, and parking it in the end cluster made that cluster the thing
        # that ran out of room at phone widths. The start side holds only this
        # button, so the two ends stop competing for the same pixels.
        header.pack_start(self.chat_btn)

        self.video_hud.put(self.watch_chrome, 0, 0)
        self.watch_chrome.set_visible(False)
        content_overlay.add_overlay(self.video_hud)

        # --- why the hud is anchored top-left and sized, not can_target(False)
        # (1) A Gtk.Fixed picks ITSELF across its whole allocation and
        # Gtk.Overlay hands an overlay child the full area by default, so a
        # full-size video_hud sits ON TOP of the grid scroller and swallows
        # every tile click. Measured with pick(): a click at a home tile centre
        # returned `Fixed` (video_hud) instead of the tile, so the tile's
        # GestureClick never fired and clicking a stream did nothing.
        # (2) can_target(False) is NOT the fix: it makes the whole SUBTREE
        # unpickable. Measured: with it, a click on the watch video skipped
        # watch_chrome and reached the grown tile underneath, killing the
        # mute/fullscreen gestures.
        # So the hud is anchored top-left (natural size, not filled) and sized
        # to the video rect on the watch view / to nothing on home. Overlay
        # children do not contribute to the window minimum (verified: the
        # window's minimum stays 360px with the hud at 601x401).
        self.video_hud.set_halign(Gtk.Align.START)
        self.video_hud.set_valign(Gtk.Align.START)
        self.video_hud.set_size_request(0, 0)
        # The nobody-live page is never interactive, so it must never be able
        # to block a click that belongs to the grid.
        self.empty_revealer.set_can_target(False)

        # scroll + click over the watch video
        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES
        )
        scroll.connect("scroll", self.on_video_scroll)
        self.watch_chrome.add_controller(scroll)
        self._click_timer = None
        click = Gtk.GestureClick(button=0)
        click.connect("released", self.on_video_click)
        self.watch_chrome.add_controller(click)

        # ---- chat overlay (never contributes to the minimum width) -------
        self.webview = self.build_chat_view()
        self.chat_pref = True
        # Fixed width by design. Drag-to-resize was removed: the width was
        # persisted and could exceed what the window could fit, which made
        # effective_chat_width() return 0 and the chat vanish at its maximum.
        self.chat_width = DEFAULT_CHAT_WIDTH
        self.webview.set_size_request(CHAT_MIN_W, -1)

        # No drag handle: the chat width is fixed (see above).
        self.chat_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        # The clamp is what makes the side dock's width authoritative; see the
        # CHAT_CLAMP_UNLIMITED note at the top of the file.
        self.chat_clamp = Adw.Clamp(
            orientation=Gtk.Orientation.HORIZONTAL,
            maximum_size=CHAT_CLAMP_UNLIMITED,
            tightening_threshold=CHAT_CLAMP_UNLIMITED,
        )
        self.chat_clamp.set_child(self.webview)
        self.chat_box.append(self.chat_clamp)

        self.chat_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
            transition_duration=home.MOTION_MS,
            child=self.chat_box,
            reveal_child=False,
        )
        self.chat_revealer.set_halign(Gtk.Align.END)
        self.chat_revealer.set_valign(Gtk.Align.FILL)
        content_overlay.add_overlay(self.chat_revealer)

        self.toast_overlay.set_child(content_overlay)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(toolbar)
        root.append(self.toast_overlay)
        self.set_content(root)

        provider = Gtk.CssProvider()
        provider.load_from_data(APP_CSS, len(APP_CSS))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        if self.font_mono:
            self.add_css_class("font-mono")

        # ---- layout state ------------------------------------------------
        self.view = "home"
        # Where the chat is docked right now: "right", "bottom" or None. Drives
        # the revealer's alignment/transition, so it must be tracked, not just
        # derived: the alignment only has to change when the dock really moves.
        self._chat_mode = None
        # The stream's own aspect once mpv reports one; the stacked dock sizes
        # the video area from it so the picture fills the width with no bars.
        self.stream_aspect = STREAM_ASPECT
        self.watch_tile = None
        self.watch_channel = None
        self.watch_display = None
        self._chat_btn_guard = False
        self.ui_anim = home.Anim(self, self)
        self.home_page.set_active(True)

        # Real size changes drive the layout. The old 200ms Paned pin loop is
        # gone: there is no Paned to fight the WM for, and the chat is an
        # overlay whose width we clamp ourselves.
        #
        # GOTCHA, measured: a GTK4 widget has NO "width"/"height" GObject
        # properties (find_property("width") is None), so `notify::width` on
        # the scroller NEVER fires. The grid was therefore laid out once at
        # startup and never again: resizing the window left every tile at its
        # old cell, so below ~941px the tiles overflowed the content area and
        # the scroller grew a horizontal scrollbar. The scroller's horizontal
        # adjustment is what actually changes on a resize - its "changed" and
        # "notify::page-size" fire with the NEW scroller width already set.
        hadj = self.grid_scroller.get_hadjustment()
        hadj.connect("changed", self._on_content_resize)
        hadj.connect("notify::page-size", self._on_content_resize)
        self.grid_scroller.get_vadjustment().connect("changed", self._on_content_resize)

        self.connect("map", self.on_first_map)

        # Never leave streamlink processes behind. Each tile's streamlink is a
        # separate child that outlives the window: measured, closing omatv
        # normally left one `streamlink --twitch-low-latency` per live tile
        # running forever, each still pulling bandwidth (an earlier session
        # found ten of them). Kill them on window close AND on application
        # shutdown, because quit / Ctrl+Q destroys the window without a
        # close-request.
        self.connect("close-request", lambda *a: (self.cleanup_players(), False)[1])
        app.connect("shutdown", lambda *a: self.cleanup_players())

        action_quit = Gio.SimpleAction(name="quit")
        action_quit.connect("activate", lambda *a: app.quit())
        self.add_action(action_quit)
        app.set_accels_for_action("win.quit", ["<Ctrl>q"])

        # Hidden feature: paste a Twitch link on the home page to watch that
        # channel whether or not you follow it. This is a key CONTROLLER, not an
        # accelerator: an accel is dispatchable no matter which widget has
        # focus, so Ctrl+V registered that way would steal the paste out of the
        # chat composer. A controller runs in the bubble phase, so the focused
        # widget (the WebView) still gets Ctrl+V first, and on_paste_key only
        # consumes the event on the home page.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self.on_paste_key)
        self.add_controller(key_ctrl)

        # Drag a Twitch link (or a bare channel name) onto the window and it
        # opens, followed or not. Same parser, same guest tile, same watch path
        # as the paste above, so chat, badges and emotes all resolve. The
        # targets live on the WINDOW rather than on the home page: dropping onto
        # a stream that is already playing must switch to the dragged channel,
        # and that swap is exactly what _select already does for a tile click.
        # Ctrl+V deliberately stays home-only; a drop never touches the chat
        # composer, so it needs no such guard.
        self.install_drop_targets()

    # ---- drag a Twitch link or name in to watch it ------------------------
    def install_drop_targets(self):
        """Accept a dropped Twitch URL or channel name anywhere on the window.

        ONE target carrying BOTH gtypes, not one target per gtype. A browser
        link drag offers text/uri-list AND text/plain, so it matches both, and
        with two separate targets on the same widget GTK re-picks between them
        as the pointer moves: the accepted state flaps and the drop lands only
        if the release happens on a good flip. Measured that failure exactly:
        ONE successful drop out of 80 accepted drags, then nothing. GTK 4.24
        supports multiple gtypes on a single target (set_gtypes), which removes
        the ambiguity entirely.

        preload makes GTK fetch the value at drag-enter, which is what lets the
        log show the data BEFORE a drop happens.
        """
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        target.set_preload(True)
        target.connect("accept", self.on_drop_accept)
        target.connect("notify::value", self.on_drop_value)
        target.connect("drop", self.on_drop)
        self.add_controller(target)
        self.drop_target = target

    def on_drop_value(self, target, pspec):
        """Preloaded value arrived (drag-enter): log what is being carried.

        Informational only. Nothing is accepted or rejected here; ::drop is
        still where the decision is made, so a stray log cannot break the drag.
        """
        try:
            value = target.get_value()
        except Exception:
            return
        login = self._drop_login(value)
        print(
            f"[omatv][drop] value {type(value).__name__} -> login={login}",
            flush=True,
        )

    def on_drop_accept(self, target, drop):
        """Log what a drag is actually offering as it enters the window.

        SIGNATURE IS EXACTLY (target, drop): Gtk.DropTarget::accept carries no
        x/y (verified from GObject.signal_query), and a handler that asks for
        them raises TypeError on EVERY drag enter, which rejects the drag and
        the drop never fires. That is what "drag and drop doesn't work" looked
        like: the log showed this TypeError instead of a drop.

        A drag cannot be replayed after the fact and the source's choice of
        formats is what decides whether a drop lands, so record it.
        """
        try:
            formats = drop.get_formats().to_string()
        except Exception:
            formats = "?"
        print(f"[omatv][drop] enter formats={formats}", flush=True)
        # MUST return True (GtkDropTarget::accept docs): "If the drop is not
        # accepted, FALSE will be returned and the drop target will ignore the
        # drop." Returning False rejects the drag at entry, so ::drop NEVER
        # fires and the whole feature silently does nothing, which is exactly
        # what it looked like. If the value turns out not to be a channel,
        # on_drop returns False instead, which is the correct place to reject.
        return True

    @staticmethod
    def _drop_login(value):
        """Map a dropped value (file list or string) to a channel login."""
        candidates = []
        if isinstance(value, Gdk.FileList):
            for gfile in value.get_files():
                try:
                    candidates.append(gfile.get_uri() or "")
                except Exception:
                    pass
        elif isinstance(value, str):
            candidates.append(value)
        for raw in candidates:
            login = parse_twitch_login(raw) or parse_dropped_login(raw)
            if login:
                return login
        return None

    def on_drop(self, target, value, x, y):
        """Open whichever Twitch channel the dropped value names."""
        login = self._drop_login(value)
        if login:
            print(f"[omatv][drop] -> login={login}", flush=True)
            self.watch_pasted_channel(login)
            return True
        # A dropped file or arbitrary text is not ours: fall through rather than
        # opening some random channel off a dragged word.
        print(f"[omatv][drop] ignored, not a channel: {type(value).__name__}", flush=True)
        return False

    # ---- responsive layout ------------------------------------------------
    def _content_size(self):
        w = self.grid_scroller.get_width()
        h = self.grid_scroller.get_height()
        if w <= 0:
            w = self.get_width()
        if h <= 0:
            h = self.get_height()
        return w, h

    def chat_fits(self):
        w, _ = self._content_size()
        return w >= CHAT_COLLAPSE_W

    def effective_chat_width(self):
        """Width of the chat when it is docked beside the video (0 = no room)."""
        return self.side_split()[1]

    def video_aspect(self):
        """The aspect the video area has to match to show the picture with no
        bars. The stream's own aspect once mpv has decoded a frame, 16:9 until
        then (and for anything implausible)."""
        a = getattr(self, "stream_aspect", 0) or STREAM_ASPECT
        return a if 0.4 <= a <= 4.0 else STREAM_ASPECT

    def video_picture_area(self, box_w, box_h):
        """Area of the stream's picture letterboxed into a box of this size."""
        if box_w <= 0 or box_h <= 0:
            return 0.0
        aspect = self.video_aspect()
        pw = min(box_w, box_h * aspect)
        return pw * (pw / aspect)

    def _fraction_chat_height(self):
        """Legacy fallback share of the height, used when a bar-free stacked
        video would leave the chat less than its minimum."""
        _, h = self._content_size()
        maxh = h - VIDEO_MIN_H
        if maxh < CHAT_STACK_MIN_H:
            return 0
        return max(CHAT_STACK_MIN_H, min(int(round(h * CHAT_STACK_FRACTION)), maxh))

    def stack_split(self):
        """(video_h, chat_h, bar_free) for the dock under the video.

        The video takes exactly the height the stream needs for the full width,
        so the picture fills the area with no pillarbox bars, and the chat takes
        everything that is left (Eddie, Oct 11 2026). When that would leave the
        chat under CHAT_STACK_MIN_H there is no bar-free split to be had, so the
        old fraction is used and the flag says the picture will be barred.
        """
        w, h = self._content_size()
        vh = int(round(w / self.video_aspect()))
        ch = h - vh
        if ch >= CHAT_STACK_MIN_H:
            return (max(1, vh), ch, True)
        ch = self._fraction_chat_height()
        return (max(1, h - ch), ch, False)

    def effective_chat_height(self):
        """Height of the chat when it is docked under the video (0 = no room)."""
        return self.stack_split()[1]

    def side_split(self):
        """(video_w, chat_w, bar_free) for the dock beside the video.

        The mirror of stack_split, and the fix for the same complaint on the
        other axis (Eddie, Oct 12 2026): the video takes exactly the width the
        stream needs for its full height, so the picture fills its area with no
        bars, and the chat takes everything that is left. A fixed 340px column
        was the problem: 1896x900 of content with a 16:9 stream left a 1556x900
        box for a 1556x875 picture, i.e. 25px of black, and the mismatch gets
        far worse for a narrow or an ultrawide stream.

        When the leftover is under CHAT_SIDE_MIN_W there is no bar-free split
        with a chat worth reading (the WebView's own minimum is 0, measured, but
        a 150px chat is not usable), so the user's width is used and the flag
        says the picture will be barred.
        """
        w, h = self._content_size()
        vw = int(round(h * self.video_aspect()))
        cw = w - vw
        if cw >= CHAT_SIDE_MIN_W:
            return (max(1, vw), cw, True)
        maxw = max(0, w - VIDEO_MIN_W)
        if maxw < CHAT_MIN_W:
            return (max(1, w), 0, False)
        cw = max(CHAT_MIN_W, min(self.chat_width, maxw))
        return (max(1, w - cw), cw, False)

    def chat_mode(self):
        """Where the chat docks at the current window size, or None for no room.

        Whichever dock leaves the VIDEO the bigger picture wins, judged on the
        real picture rather than the box: a right-hand column in a tall window
        hands the stream a narrow strip and turns the rest of the height into
        letterbox bars. The stacked dock only competes when it can give a
        bar-free picture AND a usable chat; a stack that would have to clip the
        video does not beat a column.
        """
        w, h = self._content_size()
        cw = self.effective_chat_width()
        right_ok = cw > 0
        vh, ch, bar_free = self.stack_split()
        bottom_ok = ch > 0
        if bottom_ok and right_ok:
            if not bar_free:
                return "right"
            right_area = self.video_picture_area(w - cw, h)
            bottom_area = w * vh
            return "right" if right_area >= bottom_area else "bottom"
        if bottom_ok:
            return "bottom"
        if right_ok:
            return "right"
        return None

    def video_rect(self):
        w, h = self._content_size()
        return (0, 0, max(1, w), max(1, h))

    def _full_video_rect(self):
        """The rect a tile grows into on the watch view (surface coords).

        Clamped so the chat never takes space the window does not have and
        the video is never asked for more than the content area provides.
        """
        w, h = self._content_size()
        mode = self.chat_mode() if (self.chat_pref and self.view == "watch") else None
        if mode == "right":
            cw = self.effective_chat_width()
            return (0, 0, max(1, w - cw), max(1, h))
        if mode == "bottom":
            vh, _ch, _bar_free = self.stack_split()
            return (0, 0, max(1, w), max(1, vh))
        return (0, 0, max(1, w), max(1, h))

    def _on_content_resize(self, *a):
        # The adjustment signals also fire when only the surface's extent
        # changes (e.g. every frame of a grow tween). Only re-lay-out when the
        # content area's own size really changed, or the grow animation would
        # be restarted on every frame.
        size = self._content_size()
        if size == getattr(self, "_last_content_size", None):
            return
        self._last_content_size = size
        home.dbg(f"content resize {size[0]}x{size[1]}")
        self._apply_header_mode()
        self._apply_layout(snap=True)
        self.home_page._queue_reflow()

    def _apply_header_mode(self):
        """Drop the header to its minimal layout in a narrow window.

        In a phone-shaped window the bar runs out of room long before anything is
        actually wrong: the title block, the chat-dock and fullscreen buttons,
        the font-style pair, the avatar+name chip and libadwaita's own window
        controls all want the same row, so the stream title elides to an
        ellipsis and the controls end up shoulder to shoulder.

        Minimal keeps only actions and faces: the font-style pair goes because it
        is a set-once preference rather than an action; the username goes because
        the avatar already identifies the account and the button carries a
        "Logged in as ..." tooltip; and the stream-title line goes with them
        because at this width it rendered as nothing but "...", while the chat
        header already shows the full title.

        The chat toggle is deliberately NOT in this set: it lives on the header's
        start side at every size (see where it is packed), so it is never part of
        the crowded end cluster and there is no reason to take it away.

        Hiding a widget frees its space without rebuilding anything, so every
        pack keeps its position and widening the window restores the full bar
        exactly.
        """
        w = self._content_size()[0]
        minimal = w < HEADER_MINIMAL_W
        if minimal == getattr(self, "_header_minimal", None):
            return
        self._header_minimal = minimal
        self.font_toggle_box.set_visible(not minimal)
        self.user_name_lbl.set_visible(not minimal)
        self.subtitle.set_visible(not minimal)
        home.dbg(f"header: {'minimal' if minimal else 'full'} at window {w}px")

    def _on_chat_toggle(self, btn):
        if self._chat_btn_guard:
            return
        self.chat_pref = btn.get_active()
        self._apply_layout()

    def _apply_layout(self, snap=False):
        w, h = self._content_size()
        mode = self.chat_mode() if (self.chat_pref and self.view == "watch") else None
        if mode != self._chat_mode:
            # The dock changes side with the window shape, and the panel slides
            # in from the side it lives on, so alignment and transition move
            # together and BEFORE the revealer is asked to show anything.
            self._chat_mode = mode
            home.dbg(f"chat dock: {mode}")
            if mode == "bottom":
                self.chat_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
                self.chat_revealer.set_halign(Gtk.Align.FILL)
                # FILL, not END: the dock's height comes from margin_top below,
                # so it never depends on the chat's natural size.
                self.chat_revealer.set_valign(Gtk.Align.FILL)
                # BOTH, and it matters: the box is the revealer's child, so it
                # fills, but the WebView inside it keeps its own width request
                # unless it expands too (measured: a 560px-wide dock with a
                # 320px webview parked against the left edge).
                self.chat_box.set_hexpand(True)
                self.webview.set_hexpand(True)
                self.chat_clamp.set_hexpand(True)
            else:
                self.chat_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
                self.chat_revealer.set_halign(Gtk.Align.END)
                self.chat_revealer.set_valign(Gtk.Align.FILL)
                self.chat_box.set_hexpand(False)
                self.webview.set_hexpand(False)
                self.chat_clamp.set_hexpand(False)
        # Where the stacked dock starts, and how wide the column may be. Both
        # are set from the numbers the layout wants rather than from the chat's
        # own size request, because a WebKitWebView reports its LAST ALLOCATION
        # as its natural size in BOTH axes: measured, a chat that had been
        # 960px tall in fullscreen left a 900px-tall panel sitting over the
        # video after leaving it.
        top = max(0, h - self.effective_chat_height()) if mode == "bottom" else 0
        if self.chat_revealer.get_margin_top() != top:
            self.chat_revealer.set_margin_top(top)
        cap = (
            CHAT_CLAMP_UNLIMITED
            if mode == "bottom"
            else max(1, self.effective_chat_width() or self.chat_width)
        )
        if self.chat_clamp.get_maximum_size() != cap:
            if cap > self.chat_clamp.get_maximum_size():
                self.chat_clamp.set_maximum_size(cap)
                self.chat_clamp.set_tightening_threshold(cap)
            else:
                self.chat_clamp.set_tightening_threshold(cap)
                self.chat_clamp.set_maximum_size(cap)
        open_chat = mode is not None
        if self.chat_revealer.get_reveal_child() != open_chat:
            self.chat_revealer.set_reveal_child(open_chat)
        if mode == "right":
            # The chat's own minimum is 0 (measured), so a column narrower than
            # CHAT_MIN_W is fine when that is what keeps the video bar-free.
            self.webview.set_size_request(max(1, self.effective_chat_width()), -1)
        elif mode == "bottom":
            # Full width, held at exactly this height: the height request is
            # what keeps the dock at its share of the window.
            self.webview.set_size_request(CHAT_MIN_W, self.effective_chat_height())
        # keep the toggle's visual state in sync without recursing
        if self.chat_btn.get_active() != open_chat:
            self._chat_btn_guard = True
            self.chat_btn.set_active(open_chat)
            self._chat_btn_guard = False
        # the grown tile / chrome fill the video rect on the watch view
        vr = self._full_video_rect()
        if self.view == "watch":
            # The hud must cover EXACTLY the video rect: it carries the
            # mute/fullscreen gestures, and while it covers the grid it must
            # not cover a single pixel the grid needs for clicks.
            self.video_hud.set_size_request(vr[2], vr[3])
        else:
            self.video_hud.set_size_request(0, 0)
        if self.watch_tile is not None:
            self.watch_chrome.set_size_request(vr[2], vr[3])
            self.video_hud.move(self.watch_chrome, 0, 0)
            if self.view == "watch":
                # keep the grown video filling the (possibly new) video rect
                if snap:
                    # A content resize must place the video NOW, not animate
                    # it: the grow tween restarts on every resize event that
                    # Hyprland streams, so the tile lags the window edge and
                    # the player drifts away from the video area.
                    self.home_page.snap_tile(self.watch_tile, vr)
                else:
                    self.home_page.grow_tile(self.watch_tile, vr)

    def set_home_empty(self, empty):
        want = bool(empty) and self.view == "home"
        if self.empty_revealer.get_reveal_child() != want:
            self.empty_revealer.set_reveal_child(want)

    def is_watch_tile(self, tile):
        return tile is self.watch_tile

    def on_first_map(self, *a):
        self._apply_header_mode()
        self._apply_layout()

    def cleanup_players(self):
        """Kill every tile's streamlink and stop every player (idempotent).

        Wired to close-request and to the application's shutdown signal, so no
        streamlink child survives omatv's exit.
        """
        page = getattr(self, "home_page", None)
        if page is None:
            return False
        for tile in list(page.tiles.values()):
            tile.want = False
            try:
                page._kill_tile(tile)
            except Exception:
                pass
            try:
                if tile.va.player is not None:
                    tile.va.player.command("stop")
            except Exception:
                pass
        home.dbg("cleanup: all tile streamlinks killed, all players stopped")
        return False

    def chat_theme_vars(self):
        """The chat page's background variables, taken from the GTK theme.

        The theme's own window background becomes the chat background, and the
        two panel shades are lifted from it by the same amounts the chat's
        hardcoded panels used, so the layout keeps its hierarchy while the hue
        follows whatever theme is active.
        """
        base = read_theme_palette().get(CHAT_THEME_KEY) or CHAT_THEME_FALLBACK
        return {
            "--bg": base,
            "--panel": lift_hex(base, CHAT_PANEL_LIFT[0]),
            "--panel-2": lift_hex(base, CHAT_PANEL_LIFT[1]),
        }

    def set_font_mono(self, mono):
        """Switch the whole app between the UI font and a monospace one.

        Ported from gTunes' "Font Style" control: the GTK side is a CSS class on
        the window (`.font-mono *` restyles the descendants), and the chat, being
        a separate web engine, is pushed the matching variable. The choice
        persists in settings.json, next to where the chat width used to live.
        """
        mono = bool(mono)
        if mono == self.font_mono:
            return
        self.font_mono = mono
        if mono:
            self.add_css_class("font-mono")
        else:
            self.remove_css_class("font-mono")
        settings = load_settings()
        settings["font_mono"] = mono
        save_settings(settings)
        # Re-push the chat's variables so the WebView follows immediately.
        self.apply_chat_theme()
        print(f"font style: {'monospace' if mono else 'default'}", flush=True)

    def apply_chat_theme(self):
        """Push the current theme's surfaces into the chat page and its WebView.

        Runs at startup, again from the page's own "ready" handshake (before
        that the WebView has no document to set variables on), and whenever the
        palette file changes.
        """
        colors = self.chat_theme_vars()
        try:
            rgba = Gdk.RGBA()
            rgba.parse(colors["--bg"])
            self.webview.set_background_color(rgba)
        except Exception:
            pass
        if not getattr(self, "page_ready", False):
            return
        # Inline style on <html> beats the :root block in chat.css, so the page
        # needs no change of its own beyond keeping the variables in one place.
        js = "".join(
            "document.documentElement.style.setProperty("
            f"{json.dumps(k)}, {json.dumps(v)});"
            for k, v in colors.items()
        )
        # The chat is a separate web engine, so the window's `font-mono` class
        # cannot reach inside it: the same font choice rides along as a variable.
        js += (
            "document.documentElement.style.setProperty("
            f"{json.dumps('--chat-font')}, "
            f"{json.dumps(FONT_MONO if self.font_mono else FONT_UI)});"
        )
        self.webview.evaluate_javascript(js, -1, None, None, None)

    def watch_theme_palette(self):
        """Re-apply the chat colours whenever the theme rewrites its palette."""
        try:
            palette = Gio.File.new_for_path(CHAT_THEME_FILE)
            self._theme_monitor = palette.monitor_file(
                Gio.FileMonitorFlags.NONE, None)
            self._theme_monitor.connect("changed", self._on_theme_palette)
        except Exception as exc:
            self._theme_monitor = None
            print("theme watch unavailable:", exc, flush=True)

    def _on_theme_palette(self, *_a):
        # The rewrite can be a rename, so re-read on the next idle rather than
        # inside the monitor callback.
        GLib.idle_add(self._apply_chat_theme_idle)

    def _apply_chat_theme_idle(self):
        self.apply_chat_theme()
        return False

    def on_chat_decide_policy(self, view, decision, decision_type):
        """Keep the chat page from navigating anywhere but itself.

        Its only legitimate load is the local chat.html. Without this, anything
        that makes WebKit follow a URL (a dropped link landing on the WebView's
        own drop target, a stray anchor) replaces the page and the chat is gone
        until the watchdog notices 90s later.
        """
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        try:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
        except Exception:
            return False
        if uri.startswith("file://" + CHAT_DIR):
            return False
        decision.ignore()
        print(f"[omatv][chat] blocked navigation to {uri[:100]}", flush=True)
        # A link dropped over the CHAT panel is WebKit's drop target, not the
        # window's (WebKit registers its controllers at construction, before any
        # added later, and its reaction to a dropped URL is to navigate). Turn
        # that into the feature instead of swallowing it: open the channel and
        # keep the chat page where it is.
        login = parse_twitch_login(uri)
        if login:
            print(f"[omatv][drop] chat-surface drop -> login={login}", flush=True)
            self.watch_pasted_channel(login)
        return True

    def build_chat_view(self):
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("omatv")
        manager.connect("script-message-received::omatv", self.on_bridge_message)

        settings = WebKit.Settings(
            enable_javascript=True,
            enable_developer_extras=True,
            enable_write_console_messages_to_stdout=True,
        )
        # The default network session writes NOTHING to disk on this stack: its
        # cache directory sits at two files however many emote images load, so
        # every launch re-downloaded every emote from the CDN. An explicit
        # session with its own directories does cache them (measured: 10 images
        # -> 17 files / 67 KB), and the emote CDN sends max-age=14400, so they
        # come back off disk instead of the wire. The chat needs no cookies or
        # site storage (all of its API work goes through the bridge), so fresh
        # storage for this session costs nothing.
        self.network_session = WebKit.NetworkSession(
            data_directory=os.path.join(CACHE, "webkit-data"),
            cache_directory=os.path.join(CACHE, "webkit-cache"),
        )
        wv = WebKit.WebView(
            settings=settings,
            user_content_manager=manager,
            network_session=self.network_session,
        )
        # The chat page owns right-click: it shows its own reply/copy menu.
        # Returning True marks the event handled so WebKit doesn't also pop
        # its default "Back / Reload / Inspect Element" menu on top of it.
        wv.connect("context-menu", lambda view, ctx, event: True)
        # A dropped link over the CHAT surface is WebKit's own drop target, not
        # the window's (its controller is registered at construction, before any
        # we add), and its default reaction to a dropped URL is to NAVIGATE,
        # which would replace the chat page with a web page and leave the chat
        # dead until the watchdog rebuilt it. The chat never navigates by
        # design, so veto any load that is not its own file.
        wv.connect("decide-policy", self.on_chat_decide_policy)
        # The chat's background follows the GTK theme rather than a fixed hex:
        # set it before the page loads so the first paint is already right, and
        # watch the palette file so a theme switch repaints without a relaunch.
        rgba = Gdk.RGBA()
        rgba.parse(self.chat_theme_vars()["--bg"])
        wv.set_background_color(rgba)
        self.watch_theme_palette()

        html_path = os.path.join(CHAT_DIR, "chat.html")
        wv.load_uri("file://" + html_path)
        self.page_ready = False
        self._js_backlog = []
        self.pending_http = {}
        self.http_seq = 0
        return wv

    def push_js(self, payload):
        if not getattr(self, "page_ready", False):
            if not hasattr(self, "_js_backlog"):
                self._js_backlog = []
            self._js_backlog.append(payload)
            return
        js = "__push(" + json.dumps(payload, ensure_ascii=False) + ")"
        js = js.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        GLib.idle_add(
            lambda: (
                self.webview.evaluate_javascript(js, -1, None, None, None),
                False,
            )[1]
        )

    def on_bridge_message(self, manager, value):
        try:
            raw = value.to_string() if hasattr(value, "to_string") else str(value)
            msg = json.loads(raw)
        except Exception as e:
            print("bridge parse error:", e, raw[:200])
            return
        action = msg.get("action")

        if action == "ready":
            self.page_ready = True
            # First chance to reach the document: paint the chat with the
            # current theme's surfaces before anything else is pushed.
            self.apply_chat_theme()
            backlog = getattr(self, "_js_backlog", [])
            self._js_backlog = []
            self.push_js(
                {
                    "type": "init",
                    "token": self.api.token,
                    "nick": self.api.login,
                    "can_chat": self.api.can_chat,
                }
            )
            for item in backlog:
                self.push_js(item)

        elif action == "fetch":
            rid = msg["id"]
            url = msg["url"]
            print(f"[omBridge] fetch: {url[:60]}", flush=True)

            def work():
                key = emote_cache_key(url)
                try:
                    if key:
                        body, _fresh = cached(key, lambda: fetch_json(url))
                    else:
                        body = fetch_json(url)
                    ok, payload = True, body
                except Exception as e:
                    print(f"[omBridge] fetch ERROR: {e}", flush=True)
                    ok, payload = False, str(e)
                GLib.idle_add(self.resolve_http, rid, ok, payload)

            threading.Thread(target=work, daemon=True).start()

        elif action == "twitch_emotes":
            rid = msg["id"]
            broadcaster_id = msg.get("broadcaster_id", "")
            print(f"[omBridge] twitch_emotes: broadcaster={broadcaster_id}", flush=True)

            def emote_work():
                try:
                    payload = self.fetch_twitch_emotes(broadcaster_id)
                    ok = True
                except Exception as e:
                    print(f"[omBridge] twitch_emotes ERROR: {e}", flush=True)
                    ok, payload = False, str(e)
                GLib.idle_add(self.resolve_http, rid, ok, payload)

            threading.Thread(target=emote_work, daemon=True).start()

        elif action == "badges":
            rid = msg["id"]
            broadcaster_id = msg.get("broadcaster_id", "")
            print(f"[omBridge] badges: broadcaster={broadcaster_id}", flush=True)

            def badges_work():
                try:
                    payload = self.api.fetch_badges(broadcaster_id)
                    ok = True
                except Exception as e:
                    print(f"[omBridge] badges ERROR: {e}", flush=True)
                    ok, payload = False, str(e)
                GLib.idle_add(self.resolve_http, rid, ok, payload)

            threading.Thread(target=badges_work, daemon=True).start()

        elif action == "emote_sets":
            rid = msg["id"]
            set_ids = msg.get("set_ids", "")
            print(f"[omBridge] emote_sets request, {len(set_ids.split(','))} sets", flush=True)

            def sets_work():
                try:
                    payload = self.fetch_emote_sets(set_ids)
                    print(f"[omBridge] emote_sets result: {len(payload)} chars, areaHi={chr(97)+'reaHi' in payload}", flush=True)
                    ok = True
                except Exception as e:
                    print(f"[omBridge] emote_sets ERROR: {e}", flush=True)
                    ok, payload = False, str(e)
                GLib.idle_add(self.resolve_http, rid, ok, payload)

            threading.Thread(target=sets_work, daemon=True).start()

        elif action == "open_url":
            Gio.AppInfo.launch_default_for_uri(msg["url"], None)

        elif action == "notify":
            self.show_toast(msg.get("text", ""))

    def fetch_twitch_emotes(self, broadcaster_id):
        """Fetch global + channel + the viewer's own-channel emotes via Helix,
        as a JSON string. The viewer's own channel emotes are included so they
        can use their own subscriber emotes in any chat."""
        urls = [
            "https://api.twitch.tv/helix/chat/emotes/global",
        ]
        if broadcaster_id:
            urls.append(
                "https://api.twitch.tv/helix/chat/emotes?broadcaster_id="
                + urllib.parse.quote(broadcaster_id)
            )
        if self.api.user_id:
            urls.append(
                "https://api.twitch.tv/helix/chat/emotes?broadcaster_id="
                + urllib.parse.quote(self.api.user_id)
            )

        def fetch_url(url):
            req = urllib.request.Request(
                url,
                headers={
                    "Client-ID": self.api.client_id,
                    "Authorization": "Bearer " + self.api.token,
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r).get("data", [])
            out = []
            for e in data:
                eid = e.get("id", "")
                name = e.get("name", "")
                if eid and name:
                    out.append(
                        {
                            "name": name,
                            "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0",
                            "provider": "Twitch",
                        }
                    )
            return out

        def one(url):
            key = "url-" + cache_key_for_url(url)[0]
            try:
                return cached(key, lambda: fetch_url(url))[0]
            except Exception:
                # skip URL failures (e.g. empty broadcaster_id -> 400)
                return []

        emotes = []
        for chunk in fetch_all(urls, one):
            emotes.extend(chunk)
        return json.dumps(emotes, ensure_ascii=False)

    def fetch_emote_sets(self, set_ids):
        """Resolve a comma-separated list of emote-set IDs via Helix into a
        JSON string of emotes. These are the sets the user has access to
        (from the IRC emote-sets tag), including subscriber emotes from every
        channel they're subscribed to.

        NOTE: the /chat/emotes/set endpoint accepts only ONE emote_set_id per
        request (multiple IDs return 0 results), so fetch each set separately."""
        ids = [s.strip() for s in set_ids.split(",") if s.strip()]
        hits = []

        def one(sid):
            key = "set-" + sid + ".json"

            def produce():
                url = "https://api.twitch.tv/helix/chat/emotes/set?emote_set_id=" + urllib.parse.quote(sid)
                req = urllib.request.Request(
                    url,
                    headers={
                        "Client-ID": self.api.client_id,
                        "Authorization": "Bearer " + self.api.token,
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.load(r).get("data", [])
                out = []
                for e in data:
                    eid = e.get("id", "")
                    name = e.get("name", "")
                    if eid and name:
                        out.append(
                            {
                                "name": name,
                                "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0",
                                "provider": "Twitch",
                            }
                        )
                return out

            try:
                body, fresh = cached(key, produce)
                hits.append(fresh)
                return body
            except Exception:
                # skip sets that fail to resolve (bad UUIDs etc.)
                return []

        emotes = []
        for chunk in fetch_all(ids, one):
            emotes.extend(chunk)
        fetched = sum(1 for h in hits if h)
        print(
            f"[omBridge] emote_sets: {len(ids)} sets, {len(ids) - fetched} from cache, "
            f"{fetched} fetched",
            flush=True,
        )
        return json.dumps(emotes, ensure_ascii=False)

    def resolve_http(self, rid, ok, payload):
        self.pending_http.pop(rid, None)
        js = (
            "window.__resolveHttp("
            + json.dumps(rid)
            + ","
            + ("true" if ok else "false")
            + ","
            + json.dumps(payload, ensure_ascii=False)
            + ")"
        )
        js = js.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        try:
            self.webview.evaluate_javascript(js, -1, None, None, None)
        except Exception as e:
            print("resolve_http eval failed:", e)
        return False

    def show_toast(self, text, timeout=4):
        toast = Adw.Toast(title=text, timeout=timeout)
        self.toast_overlay.add_toast(toast)

    # ---- watch video: mute / volume / fullscreen --------------------------
    def watch_player(self):
        tile = self.watch_tile
        if tile is None or tile.va is None or tile.va.player is None:
            return None
        return tile.va.player

    def on_video_scroll(self, ctrl, dx, dy):
        player = self.watch_player()
        if player is None:
            return False
        try:
            vol = float(player.volume)
        except Exception:
            return False
        new_vol = vol - 5 if (dy + dx) > 0 else vol + 5
        try:
            player.volume = max(0, min(130, new_vol))
        except Exception:
            return False
        self.flash_status(f"Volume {int(new_vol)}%", transient=True)
        return True

    def on_video_click(self, gesture, n, x, y):
        b = gesture.get_current_button()
        # GTK emits ::released for every click, so toggling mute straight away
        # here also fires on the first half of a double-click. Defer the mute
        # by the double-click window; a second click cancels it, so a
        # double-click fullscreens without muting.
        if b == 1 and n == 1:
            self._cancel_click_timer()
            self._click_timer = GLib.timeout_add(
                SINGLE_CLICK_DELAY_MS, self._single_click_mute
            )
        elif b == 1 and n == 2:
            self._cancel_click_timer()
            self.toggle_fullscreen()
        elif b == 8:
            self._cancel_click_timer()
            self.toggle_fullscreen()

    def _cancel_click_timer(self):
        if self._click_timer:
            GLib.source_remove(self._click_timer)
            self._click_timer = None

    def _single_click_mute(self):
        """One-shot: a click that no second click followed. The persistent
        badge (top-right) reflects mpv's real mute state."""
        self._click_timer = None
        player = self.watch_player()
        if player is not None:
            try:
                player.mute = not player.mute
            except Exception:
                pass
        return False

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_mute_changed(self, muted):
        """Show/hide the persistent mute badge from mpv's real mute state."""
        self.mute_badge.set_visible(bool(muted) and self.watch_player() is not None)

    def on_stream_aspect(self, value):
        """mpv reports the stream's real aspect from the first decoded frame.

        The stacked dock sizes the video area from this so the picture fills the
        width with no pillarbox bars, so a change (a 4:3 stream, a 21:9 one)
        needs a fresh layout pass rather than waiting for a resize.
        """
        try:
            aspect = float(value)
        except (TypeError, ValueError):
            return
        if not (0.4 <= aspect <= 4.0):
            return
        if abs(aspect - self.stream_aspect) < 0.01:
            return
        home.dbg(f"stream aspect {aspect:.3f} (was {self.stream_aspect:.3f})")
        self.stream_aspect = aspect
        self._apply_layout(snap=True)
        self.home_page._queue_reflow()

    def _observe_watch_props(self):
        """Watch what the picture reports for itself: mute state, and the
        stream's aspect. Both only mean anything for the watch tile."""
        self._unobserve_watch_props()
        player = self.watch_player()
        if player is None:
            return

        def mute_cb(_name, val):
            GLib.idle_add(lambda: (self.on_mute_changed(bool(val)), False)[1])

        def aspect_cb(_name, val):
            GLib.idle_add(lambda: (self.on_stream_aspect(val), False)[1])

        try:
            self._mute_observer = player.observe_property("mute", mute_cb)
        except Exception as e:
            print("watch mute observe failed:", e, flush=True)
            self._mute_observer = None
        try:
            self._aspect_observer = player.observe_property(
                "video-params/aspect", aspect_cb
            )
        except Exception as e:
            print("watch aspect observe failed:", e, flush=True)
            self._aspect_observer = None

    def _unobserve_watch_props(self):
        for attr in ("_mute_observer", "_aspect_observer"):
            obs = getattr(self, attr, None)
            if obs is not None:
                try:
                    obs.unobserve()
                except Exception:
                    pass
            setattr(self, attr, None)

    def hide_mute_badge(self):
        self.mute_badge.set_visible(False)

    def flash_status(self, text, transient=False):
        self.status_label.set_text(text)
        self.spinner.set_spinning(False)
        self.spinner.set_visible(False)
        self.status_revealer.set_reveal_child(True)
        if transient:
            GLib.timeout_add_seconds(
                2, lambda: (self.maybe_hide_status(), False)[1]
            )

    def maybe_hide_status(self):
        if not self.spinner.get_spinning():
            self.status_revealer.set_reveal_child(False)
        return False

    def set_busy_status(self, text):
        self.status_label.set_text(text)
        self.spinner.set_visible(True)
        self.spinner.set_spinning(True)
        self.status_revealer.set_reveal_child(True)

    def hide_status(self):
        self.status_revealer.set_reveal_child(False)

    def _hide_status_on_video(self, tile):
        """Clear the status pill as soon as this tile's player has real video.

        Used when the top bar starts a tile that was not decoding, so the pill
        cannot linger after the picture is up, and cannot get stuck either.
        """
        player = tile.va.player
        if player is None:
            return

        def cb(_name, value):
            if value:
                GLib.idle_add(self.hide_status)

        try:
            player.observe_property("vo-configured", cb)
        except Exception as e:
            print("vo-configured observe failed:", e, flush=True)
        GLib.timeout_add_seconds(8, lambda: (self.hide_status(), False)[1])

    def update_header(self, display_name, title, game):
        """Set the header only when the text actually changed.

        The 60s poll calls this every minute; blindly calling set_text() there
        re-laid the header out and made it jitter. Now unchanged text is a
        no-op (and is logged as a skip).
        """
        changed = []
        if self.title_label.get_text() != display_name:
            self.title_label.set_text(display_name)
            changed.append("name")
        if title:
            self.subtitle.remove_css_class("dim-label")
            if self.subtitle.get_text() != title:
                self.subtitle.set_text(title)
                changed.append("title")
            if self.subtitle.get_tooltip_text() != title:
                self.subtitle.set_tooltip_text(title)
        else:
            self.subtitle.add_css_class("dim-label")
            if self.subtitle.get_text() != "pick a streamer to start watching":
                self.subtitle.set_text("pick a streamer to start watching")
                changed.append("title")
        if game:
            if self.game_pill.get_text() != game:
                self.game_pill.set_text(game)
                changed.append("game")
            if not self.game_pill.get_visible():
                self.game_pill.set_visible(True)
                changed.append("game-vis")
        else:
            if self.game_pill.get_visible():
                self.game_pill.set_visible(False)
                changed.append("game-vis")
            if self.game_pill.get_text():
                self.game_pill.set_text("")
        if changed:
            home.dbg(
                f"header set [{','.join(changed)}] name='{display_name}' "
                f"title='{(title or '')[:40]}' game='{game}'"
            )
        else:
            home.dbg("header skip (unchanged)")

    def poll_stream_info(self):
        """Periodic refresh of the header title/game while a stream is
        playing (streamers retitle / change games mid-stream). Returns True
        to keep the timer alive."""
        login = self.watch_channel if self.view == "watch" else None
        if not login:
            return False
        gen = self.resolve_gen
        display = self.watch_display or login

        def work():
            try:
                info = self.api.stream_info(login)
            except Exception:
                info = None
            if gen != self.resolve_gen:
                return
            if info:
                GLib.idle_add(
                    self.update_header, display, info.get("title", ""), info.get("game", "")
                )
                GLib.idle_add(
                    self.push_js,
                    {"type": "viewer_count", "viewers": info.get("viewers", 0)},
                )

        threading.Thread(target=work, daemon=True).start()
        return True

    # ---- view navigation --------------------------------------------------
    def select_tile(self, tile):
        """A grid tile was clicked: grow it into the watch video (same player)."""
        self._select(tile.login, tile.name, tile.cid, tile)

    def open_channel(self, login, name=None, channel_id=None):
        """Rail button / CLI: open a channel on the watch view."""
        login = login.lower()
        tile = self.home_page.find_tile(login=login, cid=channel_id)
        self._select(login, name or login, channel_id, tile)

    # back-compat for the CLI arg path
    play_channel = open_channel

    def _select(self, login, name, channel_id, tile):
        login = login.lower()
        if self.view == "watch" and self.watch_channel == login:
            return
        if tile is None and self.view == "watch":
            # Rail click on a channel with no grid tile means it is not live.
            # Do not tear down the stream that is playing for a dead one.
            home.dbg(f"select {login}: no tile (not live) — staying on {self.watch_channel}")
            self.show_toast(f"{name or login} is offline")
            return

        # (f) SAME FRAME: the previously watched channel leaves NOW. Its tile
        # is snapped back to its grid cell and its decoding stopped before the
        # new tile starts growing, so no frame of the old channel can render
        # on the watch page after the new selection.
        prev = self.watch_tile
        if prev is not None and prev is not tile:
            prev_login, prev_uid, prev_pid = prev.login, prev.uid, prev.va.pid
            self.home_page.set_tile_watch(prev, False)
            self.home_page.snap_home(prev)
            self.home_page.fade_tile(prev, 0.0)
            self.home_page._stop_tile(prev)
            home.dbg(
                f"select {login}: old watch player LEAVE channel={prev_login} "
                f"tile={prev_uid} player={prev_pid} (snap home + stop, same frame)"
            )
        self._unobserve_watch_props()

        self.view = "watch"
        self.watch_tile = tile
        self.watch_channel = login
        self.watch_display = name or login
        self.current_channel = login
        self.current_display_name = name or login
        self.resolve_gen += 1

        # The grid must IDLE while the watch view is up. Selecting a tile has to
        # put the PAGE in watch mode too, not just the window: otherwise the
        # viewport sync below (the scroll reset fires value-changed) sees
        # mode == "home" and restarts every other tile, so they keep decoding
        # behind the video. Measured: after a click, the non-watched tile still
        # had want=True + a live streamlink.
        self.home_page.set_mode("watch")
        self.home_page.set_active(False)

        running = sorted(
            (t.uid, t.va.pid, id(t.streamlink_proc))
            for t in self.home_page.tiles.values()
            if t.streamlink_proc is not None
        )
        home.dbg(
            f"select {login}: running tile players before/after grow "
            f"(uid,player,streamlink)={running} — select adds none"
        )

        # The watch view does not scroll; the grown tile lives at the top.
        # The VERTICAL policy must still NOT be NEVER: set_min_content_height
        # is a no-op under NEVER (the exact gotcha that broke the WIDTH once),
        # so with NEVER the scroller's minimum height becomes the whole hidden
        # grid's depth - measured 2836px at a 640px-wide window - and the watch
        # video area is then sized to that clipped, too-tall box, centring the
        # picture far below the window (black band on top, bottom cut off).
        # It stays AUTOMATIC; the watch view stays unscrollable because
        # set_mode() drops every non-watch tile out of the surface's layout.
        self.grid_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.grid_scroller.get_vadjustment().set_value(0)

        # chat follows the selected channel (same WebView, never rebuilt)
        self.push_js(
            {"type": "channel", "login": login, "name": name or login, "id": channel_id}
        )

        self.watch_chrome.set_visible(True)
        self.watch_chrome.set_opacity(0.0)
        vr = self._full_video_rect()
        self.watch_chrome.set_size_request(vr[2], vr[3])
        self.video_hud.set_size_request(vr[2], vr[3])
        self.video_hud.move(self.watch_chrome, 0, 0)

        if tile is not None:
            # RAIL SWITCH: choosing a channel from the top bar skips the home
            # screen, and home is what fades the tiles back in. Selecting any
            # channel faded every OTHER tile to 0.0 and stopped it, so growing
            # this tile as-is showed an invisible, dead video area - the black
            # screen when switching from the top bar. Restore both first.
            if tile.get_opacity() < 1.0:
                home.dbg(
                    f"select {login}: restore tile opacity {tile.get_opacity():.2f} -> 1.0 (rail switch)"
                )
                self.home_page.fade_tile(tile, 1.0)
            if tile.streamlink_proc is None or not tile.want:
                home.dbg(f"select {login}: tile not decoding (rail switch) -> starting it")
                self.flash_status(f"Tuning in {name or login}…")
                self.home_page._start_tile(tile)
                self._hide_status_on_video(tile)
            if tile.va.player is None:
                # Not realized yet (should not happen for a running tile).
                tile.va._pending_url = None
            # (h) NO new player, NO new streamlink: this running tile grows.
            home.dbg(
                f"select {login}: watch video child = tile {tile.uid} "
                f"player={tile.va.pid} (EXISTING player grown; no new player/streamlink)"
            )
            self.home_page.set_tile_watch(tile, True)
            self._observe_watch_props()
            self.home_page.grow_tile(tile, vr)
            for cid in list(self.home_page.order):
                t = self.home_page.tiles.get(cid)
                if t is None or t is tile:
                    continue
                self.home_page.fade_tile(t, 0.0)
                self.home_page._stop_tile(t)
            self.ui_anim.fade("chrome", self.watch_chrome, 0.0, 1.0, label="watch chrome")
        else:
            home.dbg(f"select {login}: watch video child = (no live tile yet)")
            self.watch_chrome.set_opacity(1.0)
            self.set_busy_status(f"Waiting for {name or login}…")

        self.update_header(name or login, "loading stream…", "")
        self._apply_layout()
        self._fetch_watch_info(login, name or login, channel_id)

    # ---- hidden: paste a Twitch link to watch it (followed or not) ---------
    def on_paste_key(self, ctrl, keyval, keycode, state):
        """Ctrl+V on the home page opens a Twitch link from the clipboard."""
        if keyval not in (Gdk.KEY_v, Gdk.KEY_V):
            return False
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        # Leave Ctrl+Shift+V (paste plain) and Ctrl+Alt+V alone.
        if state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        return self.paste_watch()

    def paste_watch(self):
        """Read the clipboard and open a Twitch link, if that is what it holds.

        Only fires on the home page, so this cannot interfere with pasting text
        into the chat. Returns True when the key event was consumed.
        """
        if self.view != "home":
            return False
        display = Gdk.Display.get_default()
        if display is None:
            return False

        def got(cb, res):
            try:
                text = cb.read_text_finish(res)
            except Exception:
                text = None
            login = parse_twitch_login(text)
            if login is None:
                # Deliberately not logging the clipboard: it may hold a secret.
                home.dbg(
                    f"paste-watch: clipboard is not a Twitch link "
                    f"(len={len(text or '')})"
                )
                return False
            home.dbg(f"paste-watch: link -> login={login}")
            self.watch_pasted_channel(login)
            return False

        display.get_clipboard().read_text_async(None, got)
        return True

    def watch_pasted_channel(self, login):
        """Resolve a pasted login through Helix and open it, followed or not."""
        login = (login or "").lower()
        if not login:
            return
        self.flash_status(f"Looking up {login}…")

        def work():
            try:
                info = self.api.channel_info(login)
            except Exception as exc:
                msg = f"Twitch API error: {exc}"
                GLib.idle_add(lambda m=msg: (self.show_toast(m), False)[1])
                return
            GLib.idle_add(lambda i=info: (self.open_pasted_channel(i, login), False)[1])

        threading.Thread(target=work, daemon=True).start()

    def open_pasted_channel(self, info, login):
        """Open a resolved channel through the normal watch path.

        A tile is what owns the streamlink child and the mpv player, so a
        channel you do not follow still needs one; add_pinned makes it and
        protects it from the 60s follows refresh. Selecting it is then the same
        path a click on a followed tile takes, so chat, badges and emotes all
        resolve off the channel id.
        """
        if info is None:
            self.show_toast(f"No channel called {login} on Twitch")
            return False
        if not info.get("live"):
            self.show_toast(f"{info.get('name') or login} is offline")
            return False
        if info.get("avatar"):
            try:
                info["local_avatar"] = download_avatar(info["id"], info["avatar"])
            except Exception:
                info["local_avatar"] = ""
        tile = self.home_page.find_tile(login=info["login"], cid=info["id"])
        if tile is None:
            tile = self.home_page.add_pinned(info)
        if tile is None:
            self.show_toast("Could not open that channel")
            return False
        home.dbg(
            f"paste-watch: open channel={info['login']} cid={info['id']} "
            f"tile={tile.uid}"
        )
        # A tile has no mpv player until its GLArea realizes, and _start_tile
        # refuses to run without one, so wait for that rather than selecting a
        # tile that cannot start. The grid realizes new tiles on the next
        # layout pass, so this is a frame or two, not a stall.
        self._select_when_ready(tile, 60)
        return False

    def _select_when_ready(self, tile, tries):
        if tile.va.player is not None:
            self.flash_status(f"Tuning in {tile.name}…")
            self.select_tile(tile)
            return False
        if tries <= 0:
            self.show_toast("Could not start that stream")
            return False
        GLib.timeout_add(
            50, lambda: (self._select_when_ready(tile, tries - 1), False)[1]
        )
        return False

    def _fetch_watch_info(self, login, display, channel_id):
        gen = self.resolve_gen

        def work():
            try:
                info = self.api.stream_info(login)
            except Exception:
                info = None
            if gen != self.resolve_gen:
                return
            if info:
                GLib.idle_add(
                    self.update_header, display, info.get("title", ""), info.get("game", "")
                )
                GLib.idle_add(
                    self.push_js,
                    {
                        "type": "stream_start",
                        "started_at": info.get("started_at", ""),
                        "viewers": info.get("viewers", 0),
                    },
                )
            else:
                GLib.idle_add(self.update_header, display, "", "")

        threading.Thread(target=work, daemon=True).start()

    def enter_watch(self):
        if self.watch_tile is not None:
            self.view = "watch"
            self.home_page.set_mode("watch")
            self.home_page.set_active(False)
            self.watch_chrome.set_visible(True)
            self.watch_chrome.set_opacity(1.0)
            # Keep the vertical policy AUTOMATIC too: see the long note in
            # _select - NEVER turns set_min_content_height(1) into a no-op and
            # floors the whole content area at the hidden grid's depth.
            self.grid_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self._apply_layout()
            self.home_page.grow_tile(self.watch_tile, self._full_video_rect())
        elif self.current_channel:
            self.open_channel(self.current_channel, self.current_display_name)

    def enter_home(self):
        if self.view == "home" and self.watch_tile is None:
            return
        home.dbg("nav watch->home")
        tile = self.watch_tile
        self.view = "home"
        self.watch_tile = None
        self.watch_channel = None
        self.watch_display = None
        self.current_channel = None
        self.current_display_name = None
        self.home_page.set_mode("home")
        self.home_page.set_active(True)
        self.grid_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.grid_scroller.get_vadjustment().set_value(0)
        self._unobserve_watch_props()
        self.watch_chrome.set_visible(False)
        self.hide_mute_badge()
        self.hide_status()
        self.update_header("omatv", "", "")
        self.push_js({"type": "stream_start", "started_at": "", "viewers": 0})
        if tile is not None:
            self.home_page.set_tile_watch(tile, False)
            if self.home_page.is_guest(tile.cid):
                # A pasted/dropped channel never belongs on the grid, so leaving
                # the watch view takes it off the screen. It is PARKED, not
                # destroyed: removing the widget that was just the full-size
                # watch video unrealizes its GLArea and frees the mpv render
                # context on GSK's own GL context, which blanked every surface in
                # the window (header, grid and chat) while the process kept
                # running. The parked widget is re-used by the next drop.
                self.home_page.park_guest(tile.cid)
                # Leftover guests from earlier drops were never on screen, so
                # erasing those is the ordinary eviction path.
                self.home_page.sweep_guests(keep=tile.cid)
            else:
                # shrink the live video back into its exact grid cell
                self.home_page.restore_tile(tile)
                # Channels you actually follow were never pinned, so this is a
                # no-op for them.
                self.home_page.unpin(tile.cid)
        for cid in self.home_page.order:
            t = self.home_page.tiles.get(cid)
            if t is not None:
                self.home_page.fade_tile(t, 1.0)
        self.set_home_empty(not self.home_page.grid_cids())
        self._apply_layout()
        self.home_page.restart_all()

    def on_player_event(self, reason, area):
        """mpv END_FILE for any tile; only the watch player acts on it."""
        if area is None or area is not (self.watch_tile.va if self.watch_tile else None):
            return False
        if reason == "ERROR":
            if self.watch_channel:
                self.show_toast(f"Playback error — {self.watch_channel} may be offline")
        elif reason == "EOF":
            self.show_toast(f"{self.watch_channel} went offline")
            self.enter_home()
        return False

    # ---- responsive diagnostic (OMATV_UI_SWEEP=1) -------------------------
    def _min_width(self, widget):
        try:
            return widget.get_preferred_size()[0].width
        except Exception:
            return -1

    def _debug_min_report(self):
        widgets = [
            ("window", self),
            ("content_root", self.get_content()),
            ("toolbar", self.get_content().get_first_child()),
            ("grid_scroller", self.grid_scroller),
            ("surface", self.surface),
            ("content_overlay", self.content_overlay),
            ("rail_scroll", self.rail_scroll),
            ("rail", self.rail),
            ("title_label", self.title_label),
            ("subtitle", self.subtitle),
            ("game_pill", self.game_pill),
            ("user_btn", self.user_btn),
            ("fs_btn", self.fs_btn),
            ("chat_btn", self.chat_btn),
            ("home_btn", self.home_btn),
            ("chat_box", self.chat_box),
            ("chat_revealer", self.chat_revealer),
            ("webview", self.webview),
        ]
        home.dbg("SWEEP === minimum widths (the window's minimum must be < any width) ===")
        for name, wdg in widgets:
            home.dbg(f"SWEEP min {name} = {self._min_width(wdg)}")
        worst = max(widgets, key=lambda kv: self._min_width(kv[1]))
        home.dbg(
            f"SWEEP widest-min widget = {worst[0]} ({self._min_width(worst[1])}px)"
        )

    def _debug_sweep(self, *_a):
        widths = [1896, 1400, 1100, 941, 800, 700, 600, 500, 400]
        # exercise the chat layout path (the thing that can clip)
        self.chat_pref = True
        self.view = "watch"
        self.watch_chrome.set_visible(True)
        self._apply_layout()
        self._debug_min_report()
        # The window is tiled by Hyprland, so a client resize request is
        # ignored (actual stayed 1896 in an earlier run). Simulate each content
        # width instead by pinning the scroller to exactly that width, and
        # report the window's real (width-independent) minimum alongside it —
        # that minimum is what decides clipping.
        self.grid_scroller.set_hexpand(False)

        def run(i=0):
            if i >= len(widths):
                self.grid_scroller.set_hexpand(True)
                self.grid_scroller.set_size_request(-1, -1)
                self._apply_layout()
                home.dbg("SWEEP done")
                return False
            W = widths[i]
            self.grid_scroller.set_size_request(W, -1)

            def report(W=W, i=i):
                wmin = self._min_width(self)
                cw = self._content_size()[0]
                fits = self.chat_fits()
                ecw = self.effective_chat_width()
                ech = self.effective_chat_height()
                open_chat = self.chat_pref and self.view == "watch" and self.chat_mode()
                cols, cells = self.home_page._compute_cells()
                tile_w = next(iter(cells.values()))[2] if cells else 0
                clipped = cw > W + 1
                home.dbg(
                    f"SWEEP width={W} content={cw} window_min={wmin} ok={wmin <= W} "
                    f"clipped={clipped} chat_fits={fits} "
                    f"chat_w={ecw if open_chat == 'right' else 0} "
                    f"chat_h={ech if open_chat == 'bottom' else 0} "
                    f"dock={open_chat or 'closed'} "
                    f"cols={cols} tile_w={tile_w}"
                )
                GLib.timeout_add(160, run, i + 1)
                return False

            GLib.timeout_add(220, report)
            return False

        GLib.timeout_add(400, run, 0)

    # ---- scripted nav driver (OMATV_UI_SCRIPT=1) --------------------------
    def _debug_script(self, *_a):
        page = self.home_page

        def pick_live(exclude=None):
            for cid in page.order:
                t = page.tiles.get(cid)
                if t is None or t is exclude:
                    continue
                if t.va.player is not None:
                    return t
            return None

        def cells():
            return {page.tiles[c].login: tuple(page.tiles[c].cell or ())
                    for c in page.order if c in page.tiles}

        self._dbg_A = None
        self._dbg_B = None

        def s0():
            home.dbg(f"SCRIPT cells@home0={cells()}")
            t = pick_live()
            if t is None:
                home.dbg("SCRIPT no live tile to select A")
                return False
            self._dbg_A = t
            home.dbg(f"SCRIPT select A={t.login} tile={t.uid} player={t.va.pid}")
            self.select_tile(t)
            return False

        def s1():
            home.dbg(f"SCRIPT cells@watchA={cells()}")
            home.dbg(f"SCRIPT watch_tile={self.watch_tile.login if self.watch_tile else None}")
            return False

        def s2():
            home.dbg("SCRIPT enter_home #1")
            self.enter_home()
            return False

        def s3():
            home.dbg(f"SCRIPT cells@home1={cells()} (must equal cells@home0)")
            b = pick_live(exclude=self._dbg_A)
            self._dbg_B = b
            home.dbg(f"SCRIPT select B={b.login if b else None} (different channel)")
            if b is not None:
                self.select_tile(b)
            return False

        def s4():
            home.dbg(f"SCRIPT cells@watchB={cells()}")
            home.dbg(
                "SCRIPT after selecting B: watch_tile="
                f"{self.watch_tile.login if self.watch_tile else None} "
                f"A_still_watch={self.watch_tile is self._dbg_A}"
            )
            return False

        def s5():
            home.dbg("SCRIPT enter_home #2")
            self.enter_home()
            return False

        def s4b():
            # Prove (e): call the 60s header refresh repeatedly while watching;
            # unchanged text must log as a skip, not a set. poll_stream_info
            # returns True (keep the 60s timer alive) so wrap it: a raw timeout
            # callback returning True would repeat forever.
            home.dbg("SCRIPT forcing repeated header refreshes (poll x4)")
            for i in range(4):
                GLib.timeout_add(
                    300 + i * 1300,
                    lambda: (self.poll_stream_info(), False)[1],
                )
            return False

        def s6():
            home.dbg("SCRIPT simulate the first followed stream leaving (drop head tile)")
            if page.order:
                cid = page.order[0]
                tile = page.tiles.pop(cid)
                page.order.remove(cid)
                home.dbg(
                    f"SCRIPT membership remove {tile.login} tile={tile.uid} (head) "
                    f"-> the remaining tiles must slide to fill (tween)"
                )
                page._reflow(motion=True, departing=[tile])
            return False

        def s7():
            home.dbg("SCRIPT done")
            return False

        for delay, fn in [
            (10000, s0), (12000, s1), (20000, s2), (32000, s3),
            (34000, s4), (36000, s4b), (44000, s5), (48000, s6), (56000, s7),
        ]:
            GLib.timeout_add(delay, fn)


    def show_login_dialog(self):
        """Popup asking for an access token + client ID (from
        twitchtokengenerator.com). Validates against Twitch, saves
        ~/.config/twitch.json, and re-inits the API + chat + follows."""
        dlg = Adw.MessageDialog.new(
            self,
            "Sign in to Twitch",
            (
                "Get a token at twitchtokengenerator.com — select the scopes:\n"
                "chat:read · chat:edit · user:read:follows · user:read:email\n"
                "Then paste the Access Token and Client ID below."
            ),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("signin", "Sign in")
        dlg.set_response_appearance("signin", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("signin")

        # Link to the token site
        link = Gtk.LinkButton.new_with_label(
            "https://twitchtokengenerator.com/", "Open twitchtokengenerator.com"
        )
        link.set_halign(Gtk.Align.START)

        tok_entry = Gtk.Entry()
        tok_entry.set_placeholder_text("Access Token")
        tok_entry.set_visibility(False)  # mask it, it's a secret
        tok_entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)

        cid_entry = Gtk.Entry()
        cid_entry.set_placeholder_text("Client ID")
        cid_entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(link)
        box.append(tok_entry)
        box.append(cid_entry)
        dlg.set_extra_child(box)

        def on_signin(_dlg, res):
            try:
                token = tok_entry.get_text().strip()
                cid = cid_entry.get_text().strip()
            except Exception:
                token = cid = ""
            if not token or not cid:
                self.show_toast("Need both an access token and a client ID")
                return
            self.apply_credentials(token, cid)

        dlg.connect("response", on_signin)
        dlg.present()

    def apply_credentials(self, token, client_id):
        """Validate the pasted token + client ID against Twitch, persist them,
        and re-init everything that depends on the API."""
        token = token.strip().removeprefix("oauth:")

        def work():
            try:
                # validate token + resolve user id/login
                req = urllib.request.Request(
                    "https://id.twitch.tv/oauth2/validate",
                    headers={"Authorization": "OAuth " + token},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    vd = json.load(r)
                login = vd.get("login", "")
                user_id = vd.get("user_id", "")
                scopes = vd.get("scopes", [])

                # write config (600 — it's a secret)
                cfg = {
                    "TWITCH_CLIENT_ID": client_id,
                    "TWITCH_ACCESS_TOKEN": token,
                    "TWITCH_FOLLOWED_USER_ID": user_id,
                }
                cfg_path = os.path.expanduser("~/.config/twitch.json")
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
                tmp = cfg_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cfg, f, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, cfg_path)

                # rebuild API + refresh UI on the main thread
                def apply():
                    self.api = TwitchAPI(cfg)
                    self.api.validate()  # fills login/scopes/can_chat
                    self.update_login_ui()
                    self.refresh_follows()
                    # re-init chat with the new token (reconnects IRC)
                    self.push_js(
                        {
                            "type": "init",
                            "token": self.api.token,
                            "nick": self.api.login,
                            "can_chat": self.api.can_chat,
                        }
                    )
                    if self.api.can_chat:
                        self.show_toast(f"Signed in as {self.api.login} — chat enabled")
                    else:
                        self.show_toast(
                            f"Signed in as {self.api.login} — token lacks chat:edit "
                            "(read-only chat)"
                        )
                    return False

                GLib.idle_add(apply)
            except Exception as e:
                print("login failed:", e, flush=True)
                GLib.idle_add(
                    lambda: (
                        self.show_toast(f"Sign-in failed: {e}"),
                        False,
                    )[1]
                )

        threading.Thread(target=work, daemon=True).start()

    def _rebuild_user_menu(self):
        """Set the menu items based on login state:
        signed in -> Logout; signed out -> Sign in…"""
        self.user_menu.remove_all()
        if self.api and self.api.token:
            self.user_menu.append("Logout", "win.logout")
        else:
            self.user_menu.append("Sign in…", "win.signin")

    def logout(self):
        """Sign out: drop the saved token, reset to anonymous, clear the
        follows rail and header, reconnect chat anonymously."""
        # Remove saved credentials so a restart starts logged out too.
        for path in CONFIG_CANDIDATES:
            if path and os.path.exists(path) and "twitch.json" in path:
                try:
                    os.remove(path)
                    print("removed", path)
                except Exception as e:
                    print("could not remove", path, e)

        self.api = TwitchAPI(
            {
                "TWITCH_CLIENT_ID": "",
                "TWITCH_ACCESS_TOKEN": "",
                "TWITCH_FOLLOWED_USER_ID": "",
            }
        )
        self.api.token = ""
        self.api.login = ""

        self.apply_streamers([])  # clears the follows rail
        self.update_login_ui()
        # Reconnect chat anonymously (no token -> justinfan read-only).
        self.push_js(
            {
                "type": "init",
                "token": "",
                "nick": "",
                "can_chat": False,
            }
        )
        self.show_toast("Signed out")

    def update_login_ui(self):
        """Refresh header user chip + avatar after a re-login."""
        user = self.api.login or "anonymous"
        self.user_name_lbl.set_text(user)
        self.user_avatar.set_text(user[:1].upper())
        self.user_avatar.set_custom_image(None)
        if self.api.can_chat:
            self.user_btn.set_tooltip_text(f"Logged in as {user}")
        else:
            self.user_btn.set_tooltip_text(
                f"Logged in as {user}\nRead-only chat: token lacks chat:edit"
            )
        self._rebuild_user_menu()

        def load_profile():
            if not self.api.token:
                return  # logged out
            try:
                me = self.api.me()
                avatar_url = me.get("avatar", "")
                if avatar_url:
                    path = download_avatar(me.get("id", "me"), avatar_url)
                    tex = Gdk.Texture.new_from_filename(path)
                    GLib.idle_add(
                        lambda: self.user_avatar.set_custom_image(tex) or False
                    )
            except Exception as e:
                print("profile avatar load failed:", e)

        threading.Thread(target=load_profile, daemon=True).start()

    def refresh_follows(self):
        if not self.api.token:
            return  # not signed in yet

        def work():
            try:
                api = self.api
                follows = api.followed()
                ids = [f["id"] for f in follows]
                users = api.users(ids)
                streams = api.streams(ids)
                infos = []
                for f in follows:
                    u = users.get(f["id"], {})
                    s = streams.get(f["id"])
                    infos.append(
                        {
                            "id": f["id"],
                            "login": f["login"],
                            "name": f["name"],
                            "avatar": u.get("profile_image_url", ""),
                            "live": bool(s),
                            "viewers": s.get("viewer_count") if s else 0,
                            "game": s.get("game_name") if s else "",
                            "title": s.get("title") if s else "",
                        }
                    )
                infos.sort(key=lambda i: ((not i["live"]), i["name"].lower()))

                for info in infos:
                    if info["avatar"]:
                        try:
                            info["local_avatar"] = download_avatar(info["id"], info["avatar"])
                        except Exception:
                            info["local_avatar"] = ""

                def apply():
                    self.apply_streamers(infos)
                    return False

                GLib.idle_add(apply)
            except Exception as e:
                print("refresh failed:", e)
                GLib.idle_add(lambda: (self.show_toast(f"Twitch API error: {e}"), False)[1])

        threading.Thread(target=work, daemon=True).start()

    def apply_streamers(self, infos):
        # The rail is updated IN PLACE: a channel button is created once and
        # kept for the app's life, so a 60s refresh never rebuilds (or pops)
        # the widgets. New buttons fade in; live dots/count use Revealers.
        existing = {i["id"] for i in infos}
        removed = []
        for bid in list(self.buttons):
            if bid not in existing:
                btn = self.buttons.pop(bid)
                parent = btn.get_parent()
                if parent is not None:
                    parent.remove(btn)
                removed.append(bid)

        added = []
        for info in infos:
            btn = self.buttons.get(info["id"])
            if btn is None:
                btn = StreamerButton(info)
                btn.connect(
                    "clicked",
                    lambda b: home.open_channel(
                        self, b.info["login"], b.info["name"], b.info["id"]
                    ),
                )
                btn.set_opacity(0.0)
                self.rail.append(btn)
                self.buttons[info["id"]] = btn
                added.append(btn)
                self.ui_anim.fade(
                    f"rail-{info['id']}", btn, 0.0, 1.0,
                    label=f"rail add {info['login']}",
                )
            else:
                btn.info.update(info)
            btn.set_state(info["live"], info["game"] or "Offline")
            local = info.get("local_avatar")
            if local and os.path.exists(local):
                try:
                    tex = Gdk.Texture.new_from_file(Gio.File.new_for_path(local))
                    btn.set_avatar_texture(tex)
                except Exception:
                    pass

        # Rail ORDER: live channels left, offline pooled right. refresh_follows
        # hands the list over already sorted live-first, so the rail follows it.
        # Before this, buttons were only ever APPENDED, which froze the order at
        # launch: a channel that went offline kept its slot forever and the top
        # bar never looked like it had refreshed (Eddie, Oct 11 2026).
        order = [self.buttons[i["id"]] for i in infos if i["id"] in self.buttons]
        moved = order != self._rail_order
        if moved:
            order_rail(self.rail, order)
            self._rail_order = order

        sig = hashlib.sha1(
            ",".join(str(id(b)) for b in self.buttons.values()).encode()
        ).hexdigest()[:12]
        home.dbg(
            f"rail apply: n={len(self.buttons)} sig={sig} "
            f"added={len(added)} removed={len(removed)} reordered={moved} "
            f"live={sum(1 for b in order if b.info.get('live'))}"
        )

        # Drive the home grid from the same 60s follows refresh.
        self.home_page.update(infos)

    def start_loops(self):
        def loop():
            while True:
                try:
                    self.refresh_follows()
                    self.poll_stream_info()
                except Exception as e:
                    # NEVER let one bad cycle kill the refresher: a dead loop
                    # froze the rail's live state until the app was restarted.
                    print("follows loop error:", e)
                time.sleep(60)

        threading.Thread(target=loop, daemon=True).start()


class OmatvApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.win = None
        self.api = None

    def do_activate(self):
        win = self.props.active_window
        if not win:
            # First-run / missing config: build an anonymous API so the window
            # still opens, then prompt to sign in. No crash on fresh installs.
            try:
                cfg = load_config()
                self.api = TwitchAPI(cfg)
                try:
                    self.api.validate()
                except Exception as e:
                    print("token validation failed:", e)
            except FileNotFoundError:
                print("no twitch.json found — first run, prompting sign-in")
                self.api = TwitchAPI(
                    {
                        "TWITCH_CLIENT_ID": "",
                        "TWITCH_ACCESS_TOKEN": "",
                        "TWITCH_FOLLOWED_USER_ID": "",
                    }
                )
                self.api.token = ""

            win = OmatvWindow(self, self.api)
            style = Adw.StyleManager.get_default()
            style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

            user = self.api.login or "anonymous"
            win.user_name_lbl.set_text(user)
            win.user_avatar.set_text(user[:1].upper())

            # Load the user's profile picture into the header avatar.
            def load_profile():
                if not self.api.token:
                    return  # first run / not signed in yet
                try:
                    me = self.api.me()
                    avatar_url = me.get("avatar", "")
                    if avatar_url:
                        path = download_avatar(me.get("id", "me"), avatar_url)
                        tex = Gdk.Texture.new_from_filename(path)
                        GLib.idle_add(
                            lambda: win.user_avatar.set_custom_image(tex) or False
                        )
                except Exception as e:
                    print("profile avatar load failed:", e)

            threading.Thread(target=load_profile, daemon=True).start()

            if self.api.can_chat:
                win.user_btn.set_tooltip_text(f"Logged in as {user}")
            else:
                win.user_btn.set_tooltip_text(
                    f"Logged in as {user}\nRead-only chat: twitch.json token lacks chat:edit"
                )

            win.present()
            win.start_loops()
            win.push_js({"type": "hello"})
            # First run — no token — prompt to sign in right away.
            if not self.api.token:
                GLib.timeout_add(600, lambda: (win.show_login_dialog(), False)[1])
            if len(sys.argv) > 1:
                GLib.timeout_add_seconds(
                    3,
                    lambda: (
                        win.play_channel(
                            sys.argv[1].split("/")[-1],
                            sys.argv[1].split("/")[-1],
                        ),
                        False,
                    )[1],
                )
            # Responsive diagnostic: open the first live tile, then sweep the
            # window through the widths the user asked to be checked.
            if os.environ.get("OMATV_UI_SWEEP") == "1":
                def _auto_select():
                    order = getattr(win.home_page, "order", [])
                    for cid in order:
                        t = win.home_page.tiles.get(cid)
                        if t is not None and t.va.player is not None:
                            win.select_tile(t)
                            home.dbg(f"SWEEP auto-selected {t.login} for the sweep")
                            break
                    return False
                GLib.timeout_add(7000, _auto_select)
                GLib.timeout_add(12000, win._debug_sweep)
            if os.environ.get("OMATV_UI_SCRIPT") == "1":
                GLib.timeout_add(2000, win._debug_script)
            # Drive the hidden paste-to-watch path with a login or URL, for
            # testing: OMATV_PASTE_TEST=somechannel python3 main.py
            if os.environ.get("OMATV_PASTE_TEST"):
                GLib.timeout_add_seconds(
                    6,
                    lambda: (
                        win.watch_pasted_channel(os.environ["OMATV_PASTE_TEST"]),
                        False,
                    )[1],
                )
        win.present()


if __name__ == "__main__":
    app = OmatvApp()
    try:
        app.run([])
    finally:
        pass
