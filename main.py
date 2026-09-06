#!/usr/bin/env python3
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, WebKit

import mpv

APP_ID = "io.github.omarchy.omatv"
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_DIR = os.path.join(HERE, "chat")
CACHE = os.path.expanduser("~/.cache/omatv")
AVATAR_DIR = os.path.join(CACHE, "avatars")
SETTINGS_PATH = os.path.expanduser("~/.config/omatv/settings.json")
DEFAULT_CHAT_WIDTH = 340
CONFIG_CANDIDATES = [
    os.environ.get("OMATV_TWITCH_JSON", ""),
    os.path.expanduser("~/.config/twitch.json"),
    os.path.expanduser("~/twitch.json"),
]

GL_DRAW_FRAMEBUFFER_BINDING = 0x8CA6

APP_CSS = b"""
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
        out = {}
        urls = ["https://api.twitch.tv/helix/chat/badges/global"]
        if broadcaster_id:
            urls.append(
                "https://api.twitch.tv/helix/chat/badges?broadcaster_id="
                + urllib.parse.quote(broadcaster_id)
            )
        for url in urls:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Client-ID": self.client_id,
                        "Authorization": "Bearer " + self.token,
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.load(r).get("data", [])
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
            except Exception:
                continue
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
        self.live_dot.set_visible(False)
        overlay.add_overlay(self.live_dot)

        self.set_child(overlay)
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

    def set_state(self, live, game):
        self.info["live"] = live
        self.info["game"] = game
        self.live_dot.set_visible(live)
        self._apply_live(live)
        self.set_tooltip_text(
            f"{self.info['name']}\n{game if live else 'Offline'}"
        )

    def _apply_live(self, live):
        child = self.avatar
        child.remove_css_class("offline")
        child.remove_css_class("live")
        child.add_css_class("live" if live else "offline")


class VideoArea(Gtk.GLArea):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.player = None
        self.render_ctx = None
        self._proc_fn = None
        self._init_params = None
        self._gl = None
        self._pending_url = None
        self.connect("realize", self.on_realize)
        self.connect("render", self.on_render)
        self.connect("unrealize", self.on_unrealize)

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
            hwdec="auto-safe",
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
        )

        @self.player.event_callback(mpv.MpvEventID.END_FILE)
        def on_end(event):
            reason = getattr(event.data, "reason", None) if event.data else None
            reason_name = getattr(reason, "name", None) if reason is not None else None
            idle(self.app.on_playback_ended, reason_name)

        self.render_ctx = mpv.MpvRenderContext(
            self.player,
            "opengl",
            opengl_init_params={"get_proc_address": self._proc_fn},
        )
        self.render_ctx.update_cb = lambda: idle(self.queue_render)

        # Keep the persistent mute badge in sync with mpv's real mute state
        # (covers click-toggle, and any future mute source).
        self.player.observe_property(
            "mute", lambda _name, val: idle(self.app.on_mute_changed, bool(val))
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
        self.current_channel = None
        self.current_display_name = None
        self.resolve_gen = 0
        self.send_times = []

        self.set_title("omatv")
        self.set_default_size(1440, 900)

        self.toast_overlay = Adw.ToastOverlay()

        header = Adw.HeaderBar()
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title_box.set_halign(Gtk.Align.CENTER)
        title_label = Gtk.Label(label="omatv")
        title_label.add_css_class("title")
        title_label.set_halign(Gtk.Align.CENTER)
        self.subtitle = Gtk.Label(label="pick a streamer to start watching")
        self.subtitle.add_css_class("subtitle")
        self.subtitle.add_css_class("dim-label")
        self.subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle.set_max_width_chars(52)
        self.subtitle.set_halign(Gtk.Align.CENTER)
        self.game_pill = Gtk.Label(label="")
        self.game_pill.add_css_class("game-pill")
        self.game_pill.set_halign(Gtk.Align.CENTER)
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
        user_chip.append(self.user_avatar)
        user_chip.append(user_name)
        self.user_btn.set_child(user_chip)
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
        self.rail = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_start=12,
            margin_end=12,
            margin_top=4,
            margin_bottom=8,
        )
        self.rail.set_halign(Gtk.Align.CENTER)
        rail_scroll.set_child(self.rail)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(rail_scroll)

        self.video_stack = Gtk.Stack()
        placeholder = Adw.StatusPage(
            icon_name="video-display-symbolic",
            title="Nothing playing",
            description="Click a streamer above to start watching.",
        )
        placeholder.add_css_class("dim-placeholder")

        self.video = VideoArea(self)
        self.video_stack.add_named(placeholder, "placeholder")
        self.video_stack.add_named(self.video, "video")

        video_overlay = Gtk.Overlay()
        video_overlay.set_child(self.video_stack)
        self.status_revealer = Gtk.Revealer(
            valign=Gtk.Align.START,
            halign=Gtk.Align.CENTER,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
        )
        status_pill = Gtk.Box(spacing=8)
        status_pill.add_css_class("osd-pill")
        self.spinner = Gtk.Spinner(spinning=False)
        self.status_label = Gtk.Label(label="")
        status_pill.append(self.spinner)
        status_pill.append(self.status_label)
        self.status_revealer.set_child(status_pill)
        video_overlay.add_overlay(self.status_revealer)

        # Persistent mute badge — stays visible in the top-right while muted.
        self.mute_badge = Gtk.Label(label="🔇 Muted")
        self.mute_badge.add_css_class("mute-badge")
        self.mute_badge.set_halign(Gtk.Align.END)
        self.mute_badge.set_valign(Gtk.Align.START)
        self.mute_badge.set_margin_top(14)
        self.mute_badge.set_margin_end(14)
        self.mute_badge.set_visible(False)
        video_overlay.add_overlay(self.mute_badge)

        video_overlay.set_hexpand(True)
        video_overlay.set_vexpand(True)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES
        )
        scroll.connect("scroll", self.on_video_scroll)
        video_overlay.add_controller(scroll)

        click = Gtk.GestureClick(button=0)
        click.connect("released", self.on_video_click)
        video_overlay.add_controller(click)

        self.webview = self.build_chat_view()

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(video_overlay)
        paned.set_end_child(self.webview)
        paned.set_shrink_start_child(True)
        paned.set_shrink_end_child(False)
        # Only the video side should absorb window resizes; the chat keeps
        # its dragged width instead of growing with the window.
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(False)
        self.paned = paned
        self.chat_width = DEFAULT_CHAT_WIDTH
        settings = load_settings()
        saved_chat = settings.get("chat_width")
        if isinstance(saved_chat, int) and saved_chat >= DEFAULT_CHAT_WIDTH:
            self.chat_width = saved_chat
        paned.set_position(
            self.get_width() - self.chat_width if self.get_width() > 700 else 1080
        )
        self.webview.set_size_request(320, -1)

        # persist divider position when the user drags it
        self._paned_save_timer = None

        def on_paned_move(*a):
            # Only treat position changes as a user drag if the pointer is
            # over the paned (drag gesture) — otherwise it's a WM resize and
            # we must NOT let it corrupt the saved chat width.
            if self._pin_started and self._user_dragging:
                if self._paned_save_timer:
                    GLib.source_remove(self._paned_save_timer)
                self._paned_save_timer = GLib.timeout_add(400, self.save_chat_width)
            else:
                # WM resize or programmatic move: re-pin instead of saving
                if self._pin_started:
                    self._re_pin()

        paned.connect("notify::position", on_paned_move)
        self._pin_started = False
        self._user_dragging = False

        # Track actual user drags on the divider
        def on_drag_begin(*a):
            self._user_dragging = True
            return True

        def on_drag_end(*a):
            self._user_dragging = False
            return False

        # Gtk.Paned divider is draggable via its own gesture; use the
        # position-notify + pointer state heuristic: if position changes while
        # a button is held, it's a drag.
        self._btn_down = False
        self._btn_gesture = Gtk.GestureClick(button=0)
        self._btn_gesture.connect("pressed", on_drag_begin)
        self._btn_gesture.connect("released", on_drag_end)
        paned.add_controller(self._btn_gesture)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(paned)
        self.toast_overlay.set_child(content)

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

        self.connect("map", self.on_first_map)

        action_quit = Gio.SimpleAction(name="quit")
        action_quit.connect("activate", lambda *a: app.quit())
        self.add_action(action_quit)
        app.set_accels_for_action("win.quit", ["<Ctrl>q"])

    def on_first_map(self, *a):
        width = self.get_width()
        if width > 700:
            self.paned.set_position(width - self.chat_width)
        self.start_chat_pin()

    def start_chat_pin(self):
        self._pin_started = True
        # Re-pin the chat to its saved width whenever it drifts (WM resize).
        def pin():
            if self._user_dragging:
                return True
            self._re_pin()
            return True
        GLib.timeout_add(200, pin)

    def _re_pin(self):
        width = self.get_width()
        if width > 700:
            desired = width - self.chat_width
            if self.paned.get_position() != desired:
                self.paned.set_position(desired)

    def save_chat_width(self):
        width = self.get_width()
        if width <= 0:
            return False
        pos = self.paned.get_position()
        chat = max(DEFAULT_CHAT_WIDTH, width - pos)
        self.chat_width = chat
        settings = load_settings()
        settings["chat_width"] = chat
        save_settings(settings)
        self._paned_save_timer = None
        return False

    def build_chat_view(self):
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("omatv")
        manager.connect("script-message-received::omatv", self.on_bridge_message)

        settings = WebKit.Settings(
            enable_javascript=True,
            enable_developer_extras=True,
            enable_write_console_messages_to_stdout=True,
        )
        wv = WebKit.WebView(settings=settings, user_content_manager=manager)
        bg = Gdk.RGBA()
        bg.parse("#131317")
        wv.set_background_color(bg)

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
                try:
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
        emotes = []
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
        for url in urls:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Client-ID": self.api.client_id,
                        "Authorization": "Bearer " + self.api.token,
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.load(r).get("data", [])
                for e in data:
                    eid = e.get("id", "")
                    name = e.get("name", "")
                    if eid and name:
                        emotes.append(
                            {
                                "name": name,
                                "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0",
                                "provider": "Twitch",
                            }
                        )
            except Exception:
                # skip URL failures (e.g. empty broadcaster_id -> 400)
                continue
        return json.dumps(emotes, ensure_ascii=False)

    def fetch_emote_sets(self, set_ids):
        """Resolve a comma-separated list of emote-set IDs via Helix into a
        JSON string of emotes. These are the sets the user has access to
        (from the IRC emote-sets tag), including subscriber emotes from every
        channel they're subscribed to.

        NOTE: the /chat/emotes/set endpoint accepts only ONE emote_set_id per
        request (multiple IDs return 0 results), so fetch each set separately."""
        emotes = []
        ids = [s.strip() for s in set_ids.split(",") if s.strip()]
        for sid in ids:
            try:
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
                for e in data:
                    eid = e.get("id", "")
                    name = e.get("name", "")
                    if eid and name:
                        emotes.append(
                            {
                                "name": name,
                                "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0",
                                "provider": "Twitch",
                            }
                        )
            except Exception:
                # skip sets that fail to resolve (bad UUIDs etc.)
                continue
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

    def on_video_scroll(self, ctrl, dx, dy):
        if not self.video.player:
            return False
        try:
            vol = float(self.video.player.volume)
        except Exception:
            return False
        step = 5 if abs(dy) >= abs(dx) else 5
        new_vol = vol - step if (dy + dx) > 0 else vol + step
        self.video.set_volume(new_vol)
        self.flash_status(f"Volume {int(new_vol)}%", transient=True)
        return True

    def on_video_click(self, gesture, n, x, y):
        b = gesture.get_current_button()
        # Single left click toggles mute; double-click (n=2) still fullscreens.
        if b == 1 and n == 1:
            # Mute toggle — the persistent badge (top-right) reflects state.
            self.video.toggle_mute()
        elif b == 1 and n >= 2:
            self.toggle_fullscreen()
        elif b == 8:
            self.toggle_fullscreen()

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_mute_changed(self, muted):
        """Show/hide the persistent mute badge from mpv's real mute state."""
        self.mute_badge.set_visible(bool(muted) and self.video.player is not None)

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

    def update_header(self, display_name, title, game):
        """Set the header to show channel name + stream title + game pill."""
        self.title_label.set_text(display_name)
        if title:
            self.subtitle.set_text(title)
            self.subtitle.remove_css_class("dim-label")
            self.subtitle.set_tooltip_text(title)
        else:
            self.subtitle.set_text("pick a streamer to start watching")
            self.subtitle.add_css_class("dim-label")
        if game:
            self.game_pill.set_text(game)
            self.game_pill.set_visible(True)
        else:
            self.game_pill.set_text("")
            self.game_pill.set_visible(False)

    def poll_stream_info(self):
        """Periodic refresh of the header title/game while a stream is
        playing (streamers retitle / change games mid-stream). Returns True
        to keep the timer alive."""
        login = self.current_channel
        if not login or self.video_stack.get_visible_child_name() != "video":
            return False
        gen = self.resolve_gen
        display = self.current_display_name or login

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
                # Keep the chat header viewer count fresh while playing.
                GLib.idle_add(
                    self.push_js,
                    {"type": "viewer_count", "viewers": info.get("viewers", 0)},
                )

        threading.Thread(target=work, daemon=True).start()
        return True

    def play_channel(self, login, name=None, channel_id=None):
        login = login.lower()
        if self.current_channel == login and self.video_stack.get_visible_child_name() == "video":
            return
        # Resolve the channel ID if not provided, so the chat/emote loader
        # always has a valid broadcaster id (empty -> Twitch 400s).
        if not channel_id:
            try:
                channel_id = self.api.get_user_id(login) or None
            except Exception:
                channel_id = None
        self.current_channel = login
        self.current_display_name = name or login
        self.resolve_gen += 1
        gen = self.resolve_gen
        self.update_header(name or login, f"starting stream…", "")
        self.set_busy_status(f"Starting {name or login}…")
        self.video.stop()
        self.kill_streamlink()

        def work():
            url = ""
            err = ""
            # Streamlink in external-http mode downloads the HLS itself and
            # re-serves it as a continuous HTTP stream on localhost. mpv reads
            # that like a live pipe (no HLS segment anchoring), which is what
            # streamlink --player mpv does on the CLI and why it has lower
            # latency than handing mpv the m3u8 URL directly.
            for quality in ("best", "720p60", "720p", "worst"):
                try:
                    proc = subprocess.Popen(
                        [
                            "streamlink",
                            "--twitch-low-latency",
                            "twitch.tv/" + login,
                            quality,
                            "--player-external-http",
                            "--player-external-http-port", "0",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    self.streamlink_proc = proc
                    found_url = None
                    import re as _re
                    for line in proc.stdout:
                        m = _re.search(r"http://(?:127\.0\.0\.1|localhost):(\d+)/", line)
                        if m:
                            found_url = "http://127.0.0.1:" + m.group(1) + "/"
                            break
                    if found_url:
                        url = found_url
                        break
                    err = "streamlink did not report an HTTP endpoint"
                except Exception as e:
                    err = str(e)

            def done():
                if gen != self.resolve_gen:
                    self.kill_streamlink()
                    return False
                if url:
                    self.video.play_url(url)
                    self.video_stack.set_visible_child_name("video")
                    self.hide_status()
                    self.update_header(name or login, "loading stream…", "")
                    self.push_js(
                        {
                            "type": "channel",
                            "login": login,
                            "name": name or login,
                            "id": channel_id,
                        }
                    )
                    # Fetch stream info (title + game) in the background and update
                    # the header when it arrives.
                    def fetch_info():
                        try:
                            info = self.api.stream_info(login)
                        except Exception:
                            info = None
                        if gen != self.resolve_gen:
                            return
                        if info:
                            GLib.idle_add(
                                self.update_header,
                                name or login,
                                info.get("title", ""),
                                info.get("game", ""),
                            )
                            # Tell chat the stream start time for the uptime pill
                            # plus viewer count for the header.
                            GLib.idle_add(
                                self.push_js,
                                {
                                    "type": "stream_start",
                                    "started_at": info.get("started_at", ""),
                                    "viewers": info.get("viewers", 0),
                                },
                            )
                        else:
                            GLib.idle_add(self.update_header, name or login, "", "")

                    threading.Thread(target=fetch_info, daemon=True).start()
                else:
                    self.kill_streamlink()
                    self.video_stack.set_visible_child_name("placeholder")
                    self.hide_status()
                    self.hide_mute_badge()
                    self.update_header("omatv", "", "")
                    self.show_toast(f"Could not open {name or login}: {err or 'stream unavailable'}", timeout=6)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def kill_streamlink(self):
        proc = getattr(self, "streamlink_proc", None)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
            self.streamlink_proc = None

    def on_playback_ended(self, reason):
        if reason == "ERROR":
            if self.current_channel:
                self.show_toast(f"Playback error — {self.current_channel} may be offline")
        elif reason == "EOF":
            self.video_stack.set_visible_child_name("placeholder")
            self.current_channel = None
            self.current_display_name = None
            self.hide_mute_badge()
            self.update_header("omatv", "", "")
            self.push_js({"type": "stream_start", "started_at": "", "viewers": 0})
        return False

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
        existing = {i["id"] for i in infos}
        for bid in list(self.buttons):
            if bid not in existing:
                self.buttons[bid].get_parent().remove(self.buttons[bid])
                del self.buttons[bid]

        for info in infos:
            btn = self.buttons.get(info["id"])
            if btn is None:
                btn = StreamerButton(info)
                btn.connect(
                    "clicked",
                    lambda b: self.play_channel(
                        b.info["login"], b.info["name"], b.info["id"]
                    ),
                )
                self.rail.append(btn)
                self.buttons[info["id"]] = btn
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

    def start_loops(self):
        def loop():
            while True:
                self.refresh_follows()
                self.poll_stream_info()
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
        win.present()


if __name__ == "__main__":
    app = OmatvApp()
    try:
        app.run([])
    finally:
        pass
