"""omatv Home — a grid of live followed channels, each a real muted player.

This module is deliberately self-contained so the integration into main.py
stays tiny (~20 lines): an import, a pinned Home rail button, and a content
Gtk.Stack (home | watch). Everything else — the grid, the tiles, the muted
160p players, the scroll-driven start/stop lifecycle — lives here.

main.py only has to teach VideoArea an inert mode (`tile=True`) so a tile's
mpv END_FILE / mute callbacks never reach the window's single-player hooks.
"""

import os
import re
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

HOME_CSS = b"""
.home-btn { min-width: 44px; min-height: 44px; }
.home-btn.home-active { background: rgba(169, 112, 255, 0.26); }
.home-count {
  font-size: 9px;
  font-weight: 800;
  color: #ffffff;
  background: #e91916;
  border-radius: 9999px;
  padding: 0 5px;
  min-width: 15px;
  min-height: 15px;
}
.tile {
  background: #101014;
  border-radius: 12px;
}
.tile-top { padding: 8px; }
.tile-avatar {
  border-radius: 9999px;
  background: #2a2a33;
}
.tile-live {
  font-size: 9px;
  font-weight: 800;
  color: #ffffff;
  background: #e91916;
  border-radius: 9999px;
  padding: 2px 8px;
  margin-top: 2px;
}
.tile-overlay {
  padding: 10px 12px 12px 12px;
  border-radius: 0 0 12px 12px;
  background-image: linear-gradient(to top, rgba(0, 0, 0, 0.88), rgba(0, 0, 0, 0.0));
}
.tile-name {
  font-weight: 700;
  font-size: 15px;
  color: #ffffff;
}
.tile-title { font-size: 12px; color: #d8d8dd; }
.tile-game {
  font-size: 10px;
  font-weight: 600;
  color: #b79bff;
  background: rgba(167, 139, 250, 0.14);
  border: 1px solid rgba(167, 139, 250, 0.30);
  border-radius: 9999px;
  padding: 1px 8px;
  min-height: 16px;
}
.tile-viewers {
  font-size: 11px;
  font-weight: 700;
  color: #efeff1;
}
"""


def _fmt_viewers(n):
    n = int(n or 0)
    if n >= 1000:
        return f"{n / 1000:.1f}K viewers"
    return f"{n} viewers"


def make_home_button(win):
    """The pinned Home button: house symbolic icon, live-follow count badge."""
    btn = Gtk.Button()
    btn.add_css_class("flat")
    btn.add_css_class("home-btn")
    btn.set_tooltip_text("Home — live followed channels")

    overlay = Gtk.Overlay()
    overlay.set_size_request(48, 48)

    icon = Gtk.Image.new_from_icon_name("user-home-symbolic")
    icon.set_pixel_size(24)
    icon.set_valign(Gtk.Align.CENTER)
    icon.set_halign(Gtk.Align.CENTER)
    icon.set_overflow(Gtk.Overflow.HIDDEN)
    overlay.set_child(icon)

    count = Gtk.Label(label="")
    count.add_css_class("home-count")
    count.set_valign(Gtk.Align.END)
    count.set_halign(Gtk.Align.END)
    count.set_margin_bottom(3)
    count.set_margin_end(3)
    count.set_visible(False)
    overlay.add_overlay(count)

    btn.set_child(overlay)
    btn._count = count
    btn.connect("clicked", lambda *a: show_home(win))
    return btn


def show_home(win):
    stack = getattr(win, "content_stack", None)
    if stack is not None:
        stack.set_visible_child_name("home")


def show_watch(win):
    stack = getattr(win, "content_stack", None)
    if stack is not None:
        stack.set_visible_child_name("watch")


def open_channel(win, login, name=None, cid=None):
    """Show the watch page and open a channel through the normal path."""
    show_watch(win)
    win.play_channel(login, name or login, cid)


class HomePage(Gtk.Stack):
    MARGIN = 16
    GAP = 12
    TILE_TARGET_W = 360
    MAX_PLAYERS = 12
    # 160p keeps a dozen simultaneous decoders cheap; fall back to worst.
    PROFILES = ("160p", "worst")

    _css_loaded = False

    def __init__(self, win, video_area_cls):
        super().__init__()
        self.win = win
        self.VA = video_area_cls
        self.tiles = {}
        self._active = False
        self._sync_timer = None
        self._reflow_timer = None
        self._install_css()

        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flowbox.set_homogeneous(True)
        self.flowbox.set_column_spacing(self.GAP)
        self.flowbox.set_row_spacing(self.GAP)
        self.flowbox.set_margin_top(14)
        self.flowbox.set_margin_bottom(18)
        self.flowbox.set_margin_start(self.MARGIN)
        self.flowbox.set_margin_end(self.MARGIN)
        self.flowbox.set_valign(Gtk.Align.START)
        self.flowbox.set_hexpand(True)
        self.flowbox.set_min_children_per_line(1)
        self.flowbox.set_max_children_per_line(4)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.flowbox)
        self.scroller.set_vexpand(True)
        self.scroller.connect("notify::width", self._queue_reflow)
        adj = self.scroller.get_vadjustment()
        adj.connect("value-changed", lambda *a: self._queue_sync())
        adj.connect("changed", lambda *a: self._queue_sync())

        self.empty = Adw.StatusPage(
            icon_name="user-home-symbolic",
            title="Nobody's live",
            description="Channels you follow appear here the moment they go live.",
        )
        self.empty.add_css_class("dim-placeholder")

        self.add_named(self.scroller, "grid")
        self.add_named(self.empty, "empty")
        self.set_transition_type(Gtk.StackTransitionType.NONE)
        self.set_visible_child_name("empty")

        self.connect("map", lambda *a: self._on_shown())
        self.connect("unmap", lambda *a: self._on_hidden())
        # Don't leave tile streamlink processes orphaned when the window closes.
        win.connect("close-request", lambda *a: (self._on_hidden(), False)[1])

    # ---- css -----------------------------------------------------------
    def _install_css(self):
        if HomePage._css_loaded:
            return
        HomePage._css_loaded = True
        provider = Gtk.CssProvider()
        provider.load_from_data(HOME_CSS, len(HOME_CSS))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # ---- show / hide lifecycle ----------------------------------------
    def _on_shown(self):
        self._active = True
        btn = getattr(self.win, "home_btn", None)
        if btn is not None:
            btn.add_css_class("home-active")
        self._queue_reflow()
        self._queue_sync()
        GLib.timeout_add(220, self._sync_players)

    def _on_hidden(self):
        self._active = False
        btn = getattr(self.win, "home_btn", None)
        if btn is not None:
            btn.remove_css_class("home-active")
        for tile in list(self.tiles.values()):
            self._stop_tile(tile)

    # ---- data from the existing 60s follows loop ----------------------
    def update(self, infos):
        live = [i for i in (infos or []) if i.get("live")]
        live.sort(key=lambda i: -(i.get("viewers") or 0))

        seen = set()
        for info in live:
            cid = info["id"]
            seen.add(cid)
            tile = self.tiles.get(cid)
            if tile is None:
                tile = Tile(self, info)
                self.tiles[cid] = tile
                self.flowbox.append(tile)
            else:
                tile.update_info(info)

        for cid in list(self.tiles):
            if cid not in seen:
                tile = self.tiles.pop(cid)
                self._stop_tile(tile)
                self.flowbox.remove(tile)

        self._set_count(len(seen))
        self.set_visible_child_name("grid" if seen else "empty")
        self._queue_reflow()
        self._queue_sync()
        GLib.timeout_add(200, self._sync_players)

    def _set_count(self, n):
        btn = getattr(self.win, "home_btn", None)
        if btn is None or not hasattr(btn, "_count"):
            return
        label = btn._count
        if n > 0:
            label.set_text(str(n))
            label.set_visible(True)
        else:
            label.set_visible(False)

    # ---- responsive even grid -----------------------------------------
    def _queue_reflow(self, *a):
        if self._reflow_timer:
            return
        self._reflow_timer = GLib.timeout_add(60, self._reflow)

    def _reflow(self):
        self._reflow_timer = None
        avail = self.scroller.get_width() - 2 * self.MARGIN
        if avail <= 120:
            return False
        unit = self.TILE_TARGET_W + self.GAP
        cols = max(1, min(4, int((avail + self.GAP) // unit)))
        tile_w = (avail - (cols - 1) * self.GAP) // cols
        tile_h = int(tile_w * 9 / 16)
        self.flowbox.set_min_children_per_line(cols)
        self.flowbox.set_max_children_per_line(cols)
        for tile in self.tiles.values():
            tile.set_size_request(tile_w, tile_h)
            tile.va.set_size_request(tile_w, tile_h)
        self._queue_sync()
        return False

    # ---- viewport-driven playback -------------------------------------
    def _queue_sync(self):
        if self._sync_timer:
            return
        self._sync_timer = GLib.timeout_add(60, self._sync_players)

    def _visible_tiles(self):
        adj = self.scroller.get_vadjustment()
        top = adj.get_value()
        bottom = top + adj.get_page_size()
        out = []
        for tile in self.tiles.values():
            parent = tile.get_parent()
            if parent is None:
                continue
            alloc = parent.get_allocation()
            if alloc.height <= 0:
                continue
            if alloc.y + alloc.height >= top and alloc.y <= bottom:
                out.append((alloc.y, tile))
        if not out and self.tiles:
            # Nothing laid out yet — don't mistake "not measured" for
            # "scrolled out" and stop every player.
            return None
        out.sort(key=lambda pair: pair[0])
        return [tile for _, tile in out]

    def _sync_players(self):
        self._sync_timer = None
        if not self._active:
            return False
        visible = self._visible_tiles()
        if visible is None:
            return False
        keep = visible[: self.MAX_PLAYERS]
        for tile in keep:
            self._start_tile(tile)
        for tile in list(self.tiles.values()):
            if tile not in keep:
                self._stop_tile(tile)
        return False

    def _start_tile(self, tile):
        if tile.want:
            return
        if tile.va.player is None:
            # GLArea not realized yet; the map handler will retry.
            return
        tile.want = True
        threading.Thread(
            target=self._open_tile_stream, args=(tile,), daemon=True
        ).start()

    def _open_tile_stream(self, tile):
        url = ""
        for profile in self.PROFILES:
            proc = None
            try:
                proc = subprocess.Popen(
                    [
                        "streamlink",
                        "--twitch-low-latency",
                        "twitch.tv/" + tile.login,
                        profile,
                        "--player-external-http",
                        "--player-external-http-port",
                        "0",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                tile.streamlink_proc = proc
                for line in proc.stdout:
                    m = re.search(r"http://(?:127\.0\.0\.1|localhost):(\d+)/", line)
                    if m:
                        url = "http://127.0.0.1:" + m.group(1) + "/"
                        break
            except Exception:
                pass
            if url:
                break
            # This profile never produced an endpoint: kill it before the next
            # attempt, otherwise a stalled streamlink lingers forever.
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
                tile.streamlink_proc = None

        def land():
            if not tile.want:
                self._kill_tile(tile)
                return False
            if url and tile.va.player is not None:
                tile.va.play_url(url)
                self._mute_tile(tile, tile.va.player)
            elif not url:
                # Transient resolve failure: retry on a later sync.
                tile.want = False
                GLib.timeout_add(8000, lambda: (self._sync_players(), False)[1])
            return False

        if url and proc is not None:
            # Self-healing: if this streamlink dies on its own while the tile
            # is still meant to be playing, clear `want` so the next sync
            # restarts it (otherwise the tile would freeze forever).
            threading.Thread(
                target=self._watch_tile_proc, args=(tile, proc), daemon=True
            ).start()
        GLib.idle_add(land)

    def _watch_tile_proc(self, tile, proc):
        try:
            proc.wait()
        except Exception:
            pass
        if tile.want and tile.streamlink_proc is proc:
            tile.streamlink_proc = None
            tile.want = False
            GLib.timeout_add(1500, lambda: (self._sync_players(), False)[1])

    def _stop_tile(self, tile):
        tile.want = False
        self._kill_tile(tile)
        if tile.va.player is not None:
            try:
                tile.va.player.command("stop")
            except Exception:
                pass

    def _kill_tile(self, tile):
        proc = tile.streamlink_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            tile.streamlink_proc = None

    def _mute_tile(self, tile, player):
        """Tiles never make sound: force mpv mute and keep it forced."""
        try:
            player.mute = True
        except Exception as e:
            print("tile mute set failed:", e, flush=True)
        if getattr(tile, "_mute_forced", None) is player:
            return
        tile._mute_forced = player

        def enforce(_name, val):
            if not val:
                try:
                    player.mute = True
                except Exception:
                    pass

        try:
            player.observe_property("mute", enforce)
        except Exception as e:
            print("tile mute observe failed:", e, flush=True)


class Tile(Gtk.Overlay):
    """One live channel: a muted 160p player with an info overlay."""

    def __init__(self, page, info):
        super().__init__()
        self.page = page
        self.info = dict(info)
        self.cid = info["id"]
        self.login = info["login"]
        self.name = info.get("name") or info["login"]
        self.want = False
        self.streamlink_proc = None
        self._avatar_path = None

        self.add_css_class("tile")
        self.va = page.VA(page.win, tile=True)
        self.set_child(self.va)
        self.va.connect("realize", self._on_gl_realize)
        self.va.connect("map", lambda *a: page._queue_sync())

        # top-left avatar, top-right LIVE badge
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.add_css_class("tile-top")
        top.set_valign(Gtk.Align.START)
        # Adw.Avatar enforces its own size; Gtk.Picture would report the
        # source image's natural size (300px) and cover the tile.
        self.avatar = Adw.Avatar.new(34, self.name, False)
        self.avatar.set_show_initials(True)
        top.append(self.avatar)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)
        self.live_badge = Gtk.Label(label="● LIVE")
        self.live_badge.add_css_class("tile-live")
        self.live_badge.set_valign(Gtk.Align.CENTER)
        top.append(self.live_badge)
        self.add_overlay(top)

        # bottom gradient bar: name, title, game + viewers
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bottom.add_css_class("tile-overlay")
        bottom.set_valign(Gtk.Align.END)
        self.name_lbl = Gtk.Label(label=self.name)
        self.name_lbl.add_css_class("tile-name")
        self.name_lbl.set_xalign(0.0)
        self.name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_lbl = Gtk.Label(label="")
        self.title_lbl.add_css_class("tile-title")
        self.title_lbl.set_xalign(0.0)
        self.title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.game_lbl = Gtk.Label(label="")
        self.game_lbl.add_css_class("tile-game")
        self.game_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        mspacer = Gtk.Box()
        mspacer.set_hexpand(True)
        self.viewers_lbl = Gtk.Label(label="")
        self.viewers_lbl.add_css_class("tile-viewers")
        meta.append(self.game_lbl)
        meta.append(mspacer)
        meta.append(self.viewers_lbl)
        bottom.append(self.name_lbl)
        bottom.append(self.title_lbl)
        bottom.append(meta)
        self.add_overlay(bottom)

        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.add_controller(click)
        self.set_cursor_from_name("pointer")

        self.update_info(info)

    def update_info(self, info):
        self.info.update(info)
        self.name = info.get("name") or self.name
        self.name_lbl.set_text(self.name)
        title = info.get("title") or ""
        self.title_lbl.set_text(title)
        self.title_lbl.set_visible(bool(title))
        game = info.get("game") or ""
        self.game_lbl.set_text(game)
        self.game_lbl.set_visible(bool(game))
        self.viewers_lbl.set_text(_fmt_viewers(info.get("viewers") or 0))

        local = info.get("local_avatar")
        if local and local != self._avatar_path and os.path.exists(local):
            self._avatar_path = local
            try:
                tex = Gdk.Texture.new_from_file(Gio.File.new_for_path(local))
                self.avatar.set_custom_image(tex)
            except Exception:
                pass

    def _on_gl_realize(self, *_a):
        # VideoArea created the mpv player; make sure it is muted immediately.
        if self.va.player is not None:
            self.page._mute_tile(self, self.va.player)
        self.page._queue_sync()

    def _on_click(self, gesture, n, x, y):
        if n == 1:
            open_channel(self.page.win, self.login, self.name, self.cid)
        return False
