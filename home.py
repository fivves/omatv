"""omatv Home — a grid of live followed channels, each a real muted player.

Design notes (seamless-navigation refactor):

* The grid is a hand-laid-out ``GridSurface`` (a ``Gtk.Fixed`` subclass whose
  *minimum* width is small) instead of a ``FlowBox``. The app computes the
  responsive cells itself, so a tile's cell is a value we own: it never
  reflows or re-sorts just because we navigated to the watch view, and it can
  be tweened to a new cell when a stream starts or stops.

* Every tile's ``VideoArea`` (a GLArea) lives in that one surface for its
  whole life. Moving a realized GLArea between containers would unrealize it
  and tear down its mpv instance, so the home grid and the watch video MUST
  share a parent — that is the whole point of the single surface. Because a
  stream is loaded at SOURCE quality once (see :data:`STREAM_QUALITY`), the
  tile the user clicks *is* the watch player: selecting it only moves it.

* One motion language: 300 ms OutCubic for grid reflow, membership changes,
  fades and the tile<->watch grow. See ``Anim``.

* Everything emits lines through ``dbg`` when ``OMATV_UI_DEBUG=1`` so the
  behaviour is provable from a log.
"""

import os
import re
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

UI_DEBUG = os.environ.get("OMATV_UI_DEBUG", "") not in ("", "0", "false", "no")

# Motion instrumentation (perf work, Oct 2026): OMATV_ANIM_STATS=1 prints one
# frame-gap report per motion. Frame GAPS, not fps: a gap is what a human feels
# as a hitch, and the old step-counted engine hid them (a slow frame just ate a
# step, stretching the tween instead of showing up as a dropped frame).
FRAME_STATS = os.environ.get("OMATV_ANIM_STATS", "") not in ("", "0", "false", "no")

# Column-change reflows genuinely change every tile's size, so the per-frame
# GLArea re-allocation cannot be avoided by moving only; it can be paid ONCE per
# tile instead of once per frame. A/B: OMATV_SNAP_RESIZE=1.
# ON by default: it measured as the whole difference for a column change
# (late frames 8-12 of ~15 -> 0-3, p50 back to a locked 16.7ms). Set
# OMATV_SNAP_RESIZE=0 to go back to tweening the size.
SNAP_RESIZE = os.environ.get("OMATV_SNAP_RESIZE", "1") not in ("", "0", "false", "no")

# While a grid motion is in flight, tile players may skip render ticks: the
# picture is moving anyway, and the UI frame budget is what the eye notices.
# A/B: OMATV_TILE_THROTTLE=1.
TILE_RENDER_THROTTLE = os.environ.get("OMATV_TILE_THROTTLE", "") not in ("", "0", "false", "no")
TILE_RENDER_SECS = 1.0 / float(os.environ.get("OMATV_TILE_FPS", "30"))


class FrameProbe:
    """Frame-interval recorder shared by every job in one motion."""

    def __init__(self):
        self._t0 = None
        self._last = None
        self._dts = []
        self._label = ""
        self._start_ms = None

    def begin(self, label):
        if not FRAME_STATS:
            return
        self._t0 = time.monotonic()
        self._last = None
        self._dts = []
        self._label = label
        self._start_ms = None

    def frame(self, now=None):
        if self._t0 is None:
            return
        if now is None:
            now = time.monotonic()
        if self._start_ms is None:
            # Latency from "a motion was asked for" to "the first frame it
            # actually drew": if this is large, the motion starts late, which is
            # felt exactly like a low frame rate.
            self._start_ms = (now - self._t0) * 1000.0
        if self._last is None:
            self._last = now
            return
        self._dts.append((now - self._last) * 1000.0)
        self._last = now

    def end(self):
        if self._t0 is None:
            return
        dur = (time.monotonic() - self._t0) * 1000.0
        ds = sorted(self._dts)
        self._t0 = None
        n = len(ds)
        if not n:
            return
        p50 = ds[n // 2]
        p95 = ds[min(n - 1, int(n * 0.95))]
        over = sum(1 for d in ds if d > 20.0)
        print(
            f"[omatv][anim] {self._label} motion={dur:.0f}ms start="
            f"{self._start_ms if self._start_ms is not None else -1:.0f}ms "
            f"frames={n} p50={p50:.1f}ms p95={p95:.1f}ms max={ds[-1]:.1f}ms "
            f"over20ms={over}",
            flush=True,
        )


_T0 = time.monotonic()

# Every stream — tiles and the watch view alike — is loaded at SOURCE quality.
# The quality-selection / upgrade path was removed deliberately: switching
# quality mid-view was the sluggish part. One load, never re-loaded.
STREAM_QUALITY = "best"

# Tiles must never richer the file: the user asked for source everywhere.
# hwdec for tiles is decided in main.py (TILE_HWDEC) because of the Vulkan
# hwaccel segfault; see the comment there.


def dbg(msg):
    if UI_DEBUG:
        print(f"[omatv][ui] {int((time.monotonic() - _T0) * 1000):>7}ms {msg}", flush=True)


def fmt_rect(r):
    return "(" + ",".join(str(int(v)) for v in r) + ")"


def ease_out_cubic(t):
    t = 1.0 - t
    return 1.0 - t * t * t


# One motion language for the whole app. The cadence is the frame clock, so
# there is no fixed frame interval any more: motion is timed in wall clock ms.
MOTION_MS = 300


class _Job:
    """One running tween: a deadline plus two callbacks."""

    __slots__ = ("key", "t0", "dur", "step", "done", "label")

    def __init__(self, key, dur_ms, step, done, label):
        self.key = key
        self.t0 = time.monotonic()
        self.dur = max(0.001, dur_ms / 1000.0)
        self.step = step
        self.done = done
        self.label = label

    def t(self, now):
        return min(1.0, (now - self.t0) / self.dur)


class Anim:
    """Per-owner tween registry, driven by ONE frame-clock callback.

    The previous engine gave every job its own 16 ms GLib timeout and advanced a
    step counter, so (a) a reflow of N tiles meant N timers firing inside the
    same frame, and (b) a slow frame still consumed only one step, which
    stretched the animation past MOTION_MS exactly when the machine was busy: a
    nominal 300 ms reflow measured 400-860 ms with real players on screen, a
    third of its frames late. Motion is wall clock now, so a slow frame is
    skipped rather than accumulated, and one tick drives every job.
    """

    def __init__(self, owner, widget):
        self.owner = owner
        self.widget = widget        # any mapped widget: the frame clock source
        self._jobs = {}
        self._tid = None
        self._idle = []
        self._probe = FrameProbe()

    # ---- public API (unchanged for callers) -----------------------------
    def cancel(self, key):
        if self._jobs.pop(key, None) is None:
            return
        if not self._jobs:
            self._stop()

    def cancel_all(self):
        if not self._jobs:
            return
        self._jobs.clear()
        self._stop()

    def busy(self):
        return bool(self._jobs)

    def on_idle(self, cb):
        """Run cb once, after every job running right now has finished."""
        if not self._jobs:
            cb()
        else:
            self._idle.append(cb)

    def rect(self, key, start, end, on_step, on_done=None, label=""):
        start = tuple(start)
        end = tuple(end)
        if start == end:
            on_step(end)
            if on_done:
                on_done()
            return
        dbg(
            f"tween start key={key} from={fmt_rect(start)} to={fmt_rect(end)} "
            f"dur={MOTION_MS} {label}"
        )

        def step(e):
            r = tuple(int(round(a + (b - a) * e)) for a, b in zip(start, end))
            on_step(r)
            if UI_DEBUG:
                # Never format a debug string per frame when debug is off.
                dbg(f"tween step key={key} rect={fmt_rect(r)} {label}")

        def done():
            on_step(end)
            if on_done:
                on_done()

        self._add(key, MOTION_MS, step, done, label)

    def fade(self, key, widget, a0, a1, on_done=None, label=""):
        if abs(a1 - a0) < 0.001:
            widget.set_opacity(a1)
            if on_done:
                on_done()
            return
        dbg(f"fade start key={key} {a0:.2f}->{a1:.2f} {label}")

        def step(e):
            widget.set_opacity(a0 + (a1 - a0) * e)

        def done():
            widget.set_opacity(a1)
            if on_done:
                on_done()

        self._add(key, MOTION_MS, step, done, label)

    # ---- engine ---------------------------------------------------------
    def _add(self, key, dur_ms, step, done, label):
        if not self._jobs:
            self._probe.begin(label)
        self._jobs[key] = _Job(key, dur_ms, step, done, label)
        self._start()

    def _start(self):
        if self._tid is not None:
            return
        self._tid = self.widget.add_tick_callback(self._tick)
        # Safety net. A tick callback does not fire while the widget is unmapped
        # or the window is occluded; measured in a nested compositor a motion
        # stalled 140-340 ms with jobs still alive, and anything waiting on them
        # (the deferred player sync, dropping a departed tile) waits too. This
        # repeats while jobs are alive and force-finishes anything past its
        # deadline, so a motion can never be left visually stuck mid-way.
        GLib.timeout_add(100, self._safety)

    def _tick(self, widget, clock):
        now = time.monotonic()
        self._probe.frame(now)
        for key, job in list(self._jobs.items()):
            if self._jobs.get(key) is not job:
                continue                    # a callback replaced it
            t = job.t(now)
            if t >= 1.0:
                self._jobs.pop(key, None)
                job.done()
                if UI_DEBUG:
                    dbg(f"tween done key={key} {job.label}")
            else:
                job.step(ease_out_cubic(t))
        if self._jobs:
            return GLib.SOURCE_CONTINUE
        self._tid = None
        self._finish_idle()
        return GLib.SOURCE_REMOVE

    def _safety(self):
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if job.t(now) >= 1.0:
                self._jobs.pop(key, None)
                job.done()
        if not self._jobs:
            self._stop()
            self._finish_idle()
            return False
        return True

    def _stop(self):
        if self._tid is not None:
            self.widget.remove_tick_callback(self._tid)
            self._tid = None

    def _finish_idle(self):
        self._probe.end()
        cbs, self._idle = self._idle, []
        for cb in cbs:
            cb()



HOME_CSS = b"""
/* The pinned Home button is a peer of the channel buttons, not a highlighted
   control: a 44px circle matching the channel avatars (.avatar-letter in
   main.py carries the same radius and background). Deliberately no
   active-state styling: Eddie asked for no emphasis, the page content is the
   cue for where you are. */
.home-circle {
  border-radius: 9999px;
  background: #3f3f46;
  color: #efeff1;
  /* 48px, matching the channel avatars: a Picture.avatar-img FILLS its 48px
     button, so a 44px circle here read as visibly smaller next to them. */
  min-width: 48px;
  min-height: 48px;
}
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
/* Loading scrim: the SAME colour as .tile, so fading it out reveals the
   picture instead of fading a dark rectangle over a dark rectangle. Opacity
   lives on this scrim and never on the GLArea: a GtkGLArea below opacity 1
   forces an offscreen render path in GSK, which is a cost we will not pay
   twelve times over on a live grid. */
.tile-loading {
  background: #101014;
  border-radius: 12px;
}
.tile-spinner {
  min-width: 26px;
  min-height: 26px;
  color: #a970ff;
}
"""

MIN_TILE_W = 140
TILE_TARGET_W = 360
GRID_MARGIN = 16
GRID_GAP = 12
MAX_TILE_COLS = 4

# Grid loading state. A tile shows a spinner over an opaque scrim until mpv
# has actually produced a first frame, then the scrim crossfades away so the
# picture arrives instead of popping. The reveal is EVENT driven (mpv pushes
# the frame size), so a settled grid does no polling at all; this deadline is
# only the backstop for a stream that never produces a picture.
LOAD_GIVE_UP_S = 30


class GridSurface(Gtk.Fixed):
    """A fixed surface whose *minimum* width is tiny so the window may shrink.

    Children are positioned with ``move()`` and sized with ``set_size_request``
    by :class:`HomePage`; overflow is clipped. Reporting a small minimum keeps
    the window's own minimum width from being driven by tile sizes (the bug the
    user hit: the right edge clipped off).
    """

    def __init__(self):
        super().__init__()
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            mn = 2 * GRID_MARGIN + MIN_TILE_W
            child = self.get_first_child()
            widest = 0
            while child is not None:
                widest = max(widest, child.get_width() or 0)
                child = child.get_next_sibling()
            return (mn, max(mn, 2 * GRID_MARGIN + widest), -1, -1)
        child = self.get_first_child()
        deepest = 0
        while child is not None:
            deepest = max(deepest, child.get_height() or 0)
            child = child.get_next_sibling()
        return (0, 2 * GRID_MARGIN + deepest, -1, -1)


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
    icon.add_css_class("home-circle")
    # 48px to match the channel avatars exactly, with the glyph scaled up a
    # little so it keeps the same visual weight inside the bigger circle.
    icon.set_pixel_size(22)
    icon.set_size_request(48, 48)
    icon.set_valign(Gtk.Align.CENTER)
    icon.set_halign(Gtk.Align.CENTER)
    icon.set_overflow(Gtk.Overflow.HIDDEN)
    overlay.set_child(icon)

    # The count fades in/out (Revealer CROSSFADE) rather than popping.
    count = Gtk.Label(label="")
    count.add_css_class("home-count")
    count.set_valign(Gtk.Align.END)
    count.set_halign(Gtk.Align.END)
    count.set_margin_bottom(2)
    count.set_margin_end(2)
    count_rev = Gtk.Revealer(
        transition_type=Gtk.RevealerTransitionType.CROSSFADE,
        transition_duration=MOTION_MS,
        child=count,
        reveal_child=False,
    )
    count_rev.set_valign(Gtk.Align.END)
    count_rev.set_halign(Gtk.Align.END)
    overlay.add_overlay(count_rev)

    btn.set_child(overlay)
    btn.set_margin_top(4)
    btn.set_margin_bottom(8)
    btn._count = count
    btn._count_rev = count_rev
    btn.connect("clicked", lambda *a: win.enter_home())
    return btn


def show_home(win):
    win.enter_home()


def show_watch(win):
    win.enter_watch()


def open_channel(win, login, name=None, cid=None):
    """Select a channel through the normal path (rail button -> watch)."""
    win.open_channel(login, name or login, cid)


class HomePage:
    """Owns the tile set and the geometry of the grid on a shared surface."""

    MAX_PLAYERS = 12

    _css_loaded = False

    def __init__(self, win, video_area_cls, surface, scroller):
        self.win = win
        self.VA = video_area_cls
        self.surface = surface
        self.scroller = scroller
        self.tiles = {}
        self.order = []
        # Channels opened by paste-to-watch are not followed, so the 60s
        # follows refresh does not know about them. Their cids are protected
        # here until the channel is left again, otherwise update() would evict
        # the tile mid-watch and take the player down with it.
        # Guest tiles exist ONLY to own a player for a pasted/dropped channel
        # that may not be followed. self.pinned protects them from the 60s
        # follows refresh evicting them mid-watch; self.guest is what keeps them
        # OUT of the grid itself, which is the point: a channel you dragged in
        # must never show up as a home tile.
        self.pinned = set()
        self.guest = set()
        # cid of a guest tile that has been STOPPED and hidden but deliberately
        # kept alive for re-use. See park_guest(): destroying the widget that was
        # just the full-size watch video blanks the whole window.
        self.parked = None
        self.anim = Anim(self, surface)
        self._active = False
        self.mode = "home"
        self._sync_timer = None
        self._sync_deferred = False
        self._reflow_timer = None
        self._load_timers = {}
        self._install_css()

        # Shown (by the window) when nobody is live. Kept here so both files
        # refer to one instance.
        self.empty = Adw.StatusPage(
            icon_name="user-home-symbolic",
            title="Nobody's live",
            description="Channels you follow appear here the moment they go live.",
        )
        self.empty.add_css_class("dim-placeholder")

        adj = self.scroller.get_vadjustment()
        adj.connect("value-changed", lambda *a: self._queue_sync())
        adj.connect("changed", lambda *a: self._queue_sync())

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

    # ---- data from the existing 60s follows loop ----------------------
    def update(self, infos):
        live = [i for i in (infos or []) if i.get("live")]
        live.sort(key=lambda i: -(i.get("viewers") or 0))

        seen = set()
        new_tiles = []
        for info in live:
            cid = info["id"]
            seen.add(cid)
            tile = self.tiles.get(cid)
            if tile is None:
                tile = Tile(self, info)
                tile.set_opacity(0.0)
                self.tiles[cid] = tile
                self.order.append(cid)
                self.surface.put(tile, 0, 0)
                new_tiles.append(tile)
                dbg(
                    f"grid membership add channel={info['login']} cid={cid} "
                    f"tile={tile.uid} player={tile.va.pid}"
                )
            else:
                tile.update_info(info)

        removed = []
        for cid in list(self.tiles):
            if cid in seen or cid in self.pinned:
                continue
            tile = self.tiles[cid]
            if self.win.is_watch_tile(tile):
                # Never evict the tile that is grown into the watch video.
                # Pulling a realized GLArea out of the surface unrealizes it
                # and tears mpv down, so a followed channel that ended
                # mid-watch would blank the screen instead of holding its last
                # frame.
                continue
            self.tiles.pop(cid)
            if cid in self.order:
                self.order.remove(cid)
            removed.append(tile)
            dbg(
                f"grid membership remove channel={tile.login} cid={cid} "
                f"tile={tile.uid}"
            )

        self._set_count(len(self.grid_cids()))
        self.win.set_home_empty(not self.grid_cids())

        self._reflow(motion=True, departing=removed, arriving=new_tiles)
        self._queue_sync()

    def add_pinned(self, info):
        """Add a channel that is not followed as a temporary grid tile.

        Paste-to-watch needs a real tile because a tile is what owns the
        streamlink child and the mpv player; there is no tile-less player in
        this app. The cid goes into self.pinned so update() leaves it alone on
        the next follows refresh.
        """
        cid = info.get("id")
        if not cid:
            return None
        tile = self.tiles.get(cid)
        if tile is None:
            # Re-use the parked guest widget rather than building (and later
            # destroying) a second GLArea.
            tile = self._reclaim_parked(info)
        if tile is not None:
            if self.parked == cid:
                # The channel we parked is being opened again: bring it back.
                self.parked = None
                tile.set_visible(True)
            tile.update_info(info)
            self.pinned.add(cid)
            if cid not in self.order:
                # A reclaimed parked widget arrives under a new cid: register it
                # as this channel's guest tile.
                self.order.append(cid)
                self.guest.add(cid)
            return tile
        tile = Tile(self, info)
        tile.set_opacity(0.0)
        self.tiles[cid] = tile
        self.order.append(cid)
        self.pinned.add(cid)
        self.guest.add(cid)
        self.surface.put(tile, 0, 0)
        dbg(
            f"grid guest add channel={info.get('login')} cid={cid} "
            f"tile={tile.uid} player={tile.va.pid}"
        )
        self._reflow(motion=True, arriving=[tile])
        self._set_count(len(self.grid_cids()))
        self.win.set_home_empty(not self.grid_cids())
        self._queue_sync()
        return tile

    def unpin(self, cid):
        """Stop protecting a guest tile; the next refresh evicts it."""
        if cid in self.pinned:
            self.pinned.discard(cid)
            dbg(f"grid guest unpin cid={cid}")

    def is_guest(self, cid):
        return cid in self.guest

    def grid_cids(self):
        """cids that actually occupy a home grid cell (guests excluded)."""
        return [cid for cid in self.order if cid not in self.guest]

    def remove_guest(self, cid):
        """Erase a guest tile entirely, player and all.

        A dropped channel is a one-shot: leaving the watch view destroys it
        rather than shrinking it back into a cell it does not have, so nothing
        is left behind on the home grid.
        """
        tile = self.tiles.pop(cid, None)
        self.pinned.discard(cid)
        self.guest.discard(cid)
        if cid in self.order:
            self.order.remove(cid)
        if tile is None:
            return
        self.anim.cancel(f"grow-{tile.uid}")
        self.anim.cancel(f"arr-{tile.uid}")
        self._stop_tile(tile)
        if tile.get_parent() is not None:
            self.surface.remove(tile)
        print(f"[omatv][guest] remove cid={cid} tile={tile.uid}", flush=True)
        if self.parked == cid:
            self.parked = None
        self._set_count(len(self.grid_cids()))
        self.win.set_home_empty(not self.grid_cids())

    def park_guest(self, cid):
        """Stop a guest tile and keep its WIDGET alive, out of the grid.

        Destroying the widget that was just the full-size watch video is what
        blanked Eddie's whole window: removing it from the surface unrealizes
        its GLArea, which frees the mpv render context and terminates mpv on the
        same GL context GSK paints every other widget with, so the header, grid
        and chat all go black while the process stays alive (no core dump, no
        Python traceback, window still mapped). Measured twice: once from the
        60s eviction fade and once from an immediate remove.

        So the tile is STOPPED and hidden instead, and the next pasted/dropped
        channel re-uses it (_reclaim_parked). Nothing is destroyed while it could
        still be the thing on screen; app close tears guests down through the
        path that has always been clean.
        """
        tile = self.tiles.get(cid)
        if tile is None:
            return None
        self._stop_tile(tile)
        self.anim.cancel(f"grow-{tile.uid}")
        tile.set_opacity(0.0)
        tile.set_visible(False)
        self.pinned.add(cid)
        self.guest.add(cid)
        self.parked = cid
        print(f"[omatv][guest] park cid={cid} tile={tile.uid} (widget kept alive)",
              flush=True)
        self._set_count(len(self.grid_cids()))
        self.win.set_home_empty(not self.grid_cids())
        return tile

    def _reclaim_parked(self, info):
        """Move the parked guest tile onto a NEW channel and hand it back.

        Re-using the widget is the whole point of parking: no GLArea is ever
        unrealized for a channel that was recently on screen. Returns None when
        there is nothing to re-use or the cid already matches.
        """
        cid = self.parked
        new = info.get("id")
        if not cid or not new or cid == new:
            return None
        tile = self.tiles.pop(cid, None)
        self.pinned.discard(cid)
        self.guest.discard(cid)
        if cid in self.order:
            self.order.remove(cid)
        self.parked = None
        if tile is None:
            return None
        tile.cid = new
        tile.login = info["login"]
        tile.name = info.get("name") or info["login"]
        tile.info = dict(info)
        tile.cell = None
        tile.watch = False
        tile.update_info(info)
        self.tiles[new] = tile
        # The widget never left the surface while parked, so it must NOT be put
        # again (Gtk.Fixed.put on a parented child is a warning, not a re-parent).
        # Show it again from transparent: _select fades a tile whose opacity is
        # below 1.0 back up, and a still-hidden widget would grow into an
        # invisible video area (the black-screen bug from the rail-switch path).
        tile.set_opacity(0.0)
        tile.set_visible(True)
        print(f"[omatv][guest] reclaim {cid} -> {new} tile={tile.uid}", flush=True)
        return tile

    def sweep_guests(self, keep=None):
        """Erase leftover guest tiles, sparing the given cid and the parked one.

        A guest that is neither the current watch tile nor parked is stopped and
        was not on screen, so this is the ordinary eviction path.
        """
        for cid in list(self.guest):
            if cid == keep or cid == self.parked:
                continue
            self.remove_guest(cid)

    def _set_count(self, n):
        btn = getattr(self.win, "home_btn", None)
        if btn is None or not hasattr(btn, "_count"):
            return
        if n > 0:
            if btn._count.get_text() != str(n):
                btn._count.set_text(str(n))
            if not btn._count_rev.get_reveal_child():
                btn._count_rev.set_reveal_child(True)
        else:
            if btn._count_rev.get_reveal_child():
                btn._count_rev.set_reveal_child(False)

    # ---- responsive even grid -----------------------------------------
    def viewport_size(self):
        return self.scroller.get_width(), self.scroller.get_height()

    def _queue_reflow(self, *a):
        if self._reflow_timer:
            dbg("reflow already queued")
            return
        dbg(f"reflow queued viewport_w={self.scroller.get_width()}")
        self._reflow_timer = GLib.timeout_add(40, lambda: (self._reflow(), False)[1])

    def _compute_cells(self):
        avail = self.scroller.get_width() - 2 * GRID_MARGIN
        if avail <= 60:
            avail = 320
        unit = TILE_TARGET_W + GRID_GAP
        cols = max(1, min(MAX_TILE_COLS, int((avail + GRID_GAP) // unit)))
        while cols > 1 and (avail - (cols - 1) * GRID_GAP) // cols < MIN_TILE_W:
            cols -= 1
        tile_w = max(MIN_TILE_W, (avail - (cols - 1) * GRID_GAP) // cols)
        tile_h = int(tile_w * 9 / 16)
        cells = {}
        # Guest tiles take no cell. _reflow skips any cid without one, so a
        # dropped channel neither appears on the grid nor shifts real tiles.
        grid_order = [cid for cid in self.order if cid not in self.guest]
        for idx, cid in enumerate(grid_order):
            r, c = divmod(idx, cols)
            x = GRID_MARGIN + c * (tile_w + GRID_GAP)
            y = GRID_MARGIN + r * (tile_h + GRID_GAP)
            cells[cid] = (x, y, tile_w, tile_h)
        return cols, cells

    def _reflow(self, motion=True, departing=(), arriving=()):
        self._reflow_timer = None
        cols, cells = self._compute_cells()
        self.cols = cols
        dbg(
            f"reflow run viewport_w={self.scroller.get_width()} cols={cols} tiles={len(cells)}"
        )
        for cid, tile in self.tiles.items():
            target = cells.get(cid)
            if target is None:
                continue
            tile.cell = target
            if self.win.is_watch_tile(tile):
                continue  # grown into the watch view; keep its transitory rect
            if not tile.mapped_ok():
                tile.place(target)
            elif motion and tuple(tile.rect) != tuple(target):
                if tuple(tile.rect[2:]) == tuple(target[2:]):
                    # Same-size cell change is a pure MOVE: never touch a size
                    # request for it, because re-allocating the tile's GLArea
                    # makes mpv rebuild its render target every frame, measured
                    # as the worst frame-gap case of any motion here.
                    self.anim.rect(
                        f"cell-{tile.uid}", tile.rect, target,
                        lambda r, t=tile: t.move_to(r[0], r[1]),
                        label=f"reflow {tile.login}",
                    )
                elif SNAP_RESIZE:
                    tile.snap_size(target)
                    self.anim.rect(
                        f"cell-{tile.uid}", tile.rect, target,
                        lambda r, t=tile: t.move_to(r[0], r[1]),
                        label=f"reflow {tile.login}",
                    )
                else:
                    self.anim.rect(
                        f"cell-{tile.uid}", tile.rect, target,
                        lambda r, t=tile: t.place(r), label=f"reflow {tile.login}",
                    )
            elif tuple(tile.rect) != tuple(target):
                tile.place(target)
        for tile in arriving:
            if self.mode == "home":
                self.anim.fade(
                    f"arr-{tile.uid}", tile, 0.0, 1.0, label=f"arrive {tile.login}"
                )
            else:
                # A stream that goes live while the WATCH view is up must never
                # appear over the video: it would sit on top of the grown player
                # as a black box until its own decoder produced a first frame.
                # It arrived at opacity 0 - keep it there, out of the layout -
                # and let it fade in only when home is shown again.
                tile.set_opacity(0.0)
                tile.set_visible(False)
        for tile in departing:
            self._depart_tile(tile)
        self._queue_sync()

    def _depart_tile(self, tile):
        self.anim.fade(
            f"dept-{tile.uid}", tile, tile.get_opacity(), 0.0,
            on_done=lambda: self._drop_tile(tile), label=f"depart {tile.login}",
        )

    def _drop_tile(self, tile):
        self._stop_tile(tile)
        if tile.get_parent() is not None:
            self.surface.remove(tile)
            dbg(f"grid tile dropped channel={tile.login} tile={tile.uid}")

    def tile_rects(self):
        return {cid: tuple(self.tiles[cid].cell or (0, 0, 0, 0)) for cid in self.order}

    def home_cell(self, tile):
        return tuple(tile.cell) if tile.cell else None

    # ---- viewport-driven playback -------------------------------------
    def _queue_sync(self):
        """Start/stop tile players, at most once, once the motion is over.

        _sync_players picks the tiles to run from their CURRENT rects and can
        spawn a streamlink child per entering tile, which is CPU and I/O heavy:
        doing that 60 ms into a 300 ms tween is what turned a rearrangement into
        a stutter. It also read half-tweened rects, so deferring is more correct
        as well as smoother.
        """
        if self._sync_timer or self._sync_deferred:
            return
        if self.anim.busy():
            self._sync_deferred = True
            self.anim.on_idle(self._sync_pending)
            return
        self._sync_timer = GLib.timeout_add(60, self._sync_players)

    def _sync_pending(self):
        self._sync_deferred = False
        self._queue_sync()

    def set_mode(self, mode):
        self.mode = mode
        if mode == "home":
            # Back on the grid every tile shows its chrome and re-enters the
            # scroller's layout again. Belt and braces: the handoff also calls
            # set_tile_watch(tile, False).
            for cid, t in self.tiles.items():
                if cid in self.guest:
                    # Never come back onto the grid: a guest is only ever the
                    # channel currently open in the watch view.
                    t.set_visible(False)
                    continue
                self.set_tile_chrome(t, True)
                t.set_visible(True)
        else:
            # Watch view: ONLY the grown tile may contribute to the surface's
            # measured extent. A Gtk.Fixed reports its children's extent as its
            # minimum, so leaving the hidden (opacity 0) tiles in the layout
            # floors the scroller's height at the whole grid's depth - measured
            # 2836px at a 640px-wide window - which keeps a scrollbar under the
            # video and stops the window shrinking. set_visible (NOT
            # set_child_visible) is what removes a child from the measure;
            # measured on this app.
            wt = self.win.watch_tile
            for t in self.tiles.values():
                t.set_visible(t is wt)
        self._queue_sync()

    def set_active(self, active):
        self._active = active
        if active:
            self._queue_sync()

    def _visible_tiles(self):
        adj = self.scroller.get_vadjustment()
        top = adj.get_value()
        bottom = top + adj.get_page_size()
        out = []
        for cid in self.order:
            tile = self.tiles.get(cid)
            if tile is None or tile.get_parent() is None:
                continue
            r = tile.rect
            if r[3] <= 0:
                continue
            if r[1] + r[3] >= top and r[1] <= bottom:
                out.append((r[1], tile))
        if not out and self.tiles:
            return None
        out.sort(key=lambda pair: pair[0])
        return [tile for _, tile in out]

    def _sync_players(self):
        self._sync_timer = None
        if not self._active or self.mode != "home":
            return False
        visible = self._visible_tiles()
        if visible is None:
            return False
        keep = visible[: self.MAX_PLAYERS]
        for tile in keep:
            self._start_tile(tile)
        for cid in self.order:
            tile = self.tiles.get(cid)
            if tile is not None and tile not in keep:
                self._stop_tile(tile)
        return False

    def restart_all(self):
        """Return to home: restart visible tiles, in visible order."""
        self._queue_sync()

    def pause_all(self):
        for cid in self.order:
            tile = self.tiles.get(cid)
            if tile is not None:
                self._stop_tile(tile)

    # ---- first-frame fades (grid) --------------------------------------
    #
    # Event-driven, not polled. mpv pushes the stream's frame size the moment a
    # picture exists (VideoArea._on_video_params), so a settled grid costs
    # nothing at all and a reveal lands on the frame that actually painted.
    # The one-shot watchdog exists only for a stream that never produces a
    # picture, so the scrim cannot spin forever.
    def _mark_tile_loading(self, tile):
        """Show the loading scrim on a tile that has no picture yet."""
        veil = getattr(tile, "load_veil", None)
        if veil is None or getattr(tile, "loading", False):
            return
        tile.loading = True
        tile.loaded = False
        tile._load_started = time.monotonic()
        self.anim.cancel(f"veil-{tile.uid}")
        veil.set_opacity(1.0)
        veil.set_visible(True)
        tile.va.first_frame_cb = lambda t=tile: self._on_tile_first_frame(t)
        self._load_timers[tile.uid] = GLib.timeout_add_seconds(
            int(LOAD_GIVE_UP_S), self._load_watchdog, tile
        )

    def _drop_load_watch(self, tile):
        """Disarm the first-frame callback and the give-up watchdog."""
        src = self._load_timers.pop(tile.uid, None)
        if src is not None:
            GLib.source_remove(src)
        va = getattr(tile, "va", None)
        if va is not None and getattr(va, "first_frame_cb", None) is not None:
            va.first_frame_cb = None

    def _on_tile_first_frame(self, tile):
        if getattr(tile, "loading", False):
            self._reveal_tile(tile)

    def _load_watchdog(self, tile):
        # This source has fired; drop its id first so _drop_load_watch does not
        # try to remove a source that is already gone.
        self._load_timers.pop(tile.uid, None)
        if getattr(tile, "loading", False):
            if UI_DEBUG:
                dbg(f"tile load gave up channel={tile.login} tile={tile.uid}")
            # A lost notification on a stream that IS painting still deserves
            # the crossfade; a genuinely dead one just drops its scrim.
            self._reveal_tile(tile, snap=not tile.va.is_rendering())
        return False

    def _clear_tile_loading(self, tile):
        """The tile is not loading anything: no spinner (it is not working)."""
        tile.loading = False
        self._drop_load_watch(tile)
        veil = getattr(tile, "load_veil", None)
        if veil is not None:
            self.anim.cancel(f"veil-{tile.uid}")
            veil.set_visible(False)
            veil.set_opacity(1.0)

    def _reveal_tile(self, tile, snap=False):
        """First frame is up: crossfade the scrim away so the video arrives."""
        tile.loading = False
        tile.loaded = True
        self._drop_load_watch(tile)
        veil = getattr(tile, "load_veil", None)
        if veil is None:
            return
        if snap or not veil.get_visible():
            self.anim.cancel(f"veil-{tile.uid}")
            veil.set_visible(False)
            veil.set_opacity(1.0)
            return
        if UI_DEBUG:
            dbg(
                f"tile first frame channel={tile.login} tile={tile.uid} "
                f"after={(time.monotonic() - tile._load_started) * 1000:.0f}ms"
            )
        self.anim.fade(
            f"veil-{tile.uid}", veil, veil.get_opacity(), 0.0,
            on_done=lambda: veil.set_visible(False),
            label=f"load in {tile.login}",
        )

    def _start_tile(self, tile):
        if tile.want or tile.va.player is None:
            return
        tile.want = True
        # A tile that is (re)starting has no picture yet: show the scrim +
        # spinner until mpv paints its first frame, then crossfade it away.
        self._mark_tile_loading(tile)
        cmd = [
            "streamlink", "--twitch-low-latency", "twitch.tv/" + tile.login,
            STREAM_QUALITY, "--player-external-http", "--player-external-http-port", "0",
        ]
        dbg(f"tile start channel={tile.login} tile={tile.uid} player={tile.va.pid} cmd={' '.join(cmd)}")
        threading.Thread(target=self._open_tile_stream, args=(tile, cmd), daemon=True).start()

    def _open_tile_stream(self, tile, cmd):
        url = ""
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            tile.streamlink_proc = proc
            for line in proc.stdout:
                m = re.search(r"http://(?:127\.0\.0\.1|localhost):(\d+)/", line)
                if m:
                    url = "http://127.0.0.1:" + m.group(1) + "/"
                    break
        except Exception:
            pass
        if not url and proc is not None:
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
                tile.want = False
                # Nothing to load: drop the spinner rather than spin forever
                # over a stream that never resolved.
                self._clear_tile_loading(tile)
                GLib.timeout_add(8000, lambda: (self._sync_players(), False)[1])
            return False

        if url and proc is not None:
            threading.Thread(target=self._watch_tile_proc, args=(tile, proc), daemon=True).start()
        GLib.idle_add(land)

    def _watch_tile_proc(self, tile, proc):
        try:
            proc.wait()
        except Exception:
            pass
        if tile.want and tile.streamlink_proc is proc and not tile.watch:
            tile.streamlink_proc = None
            tile.want = False
            GLib.timeout_add(1500, lambda: (self._sync_players(), False)[1])

    def _stop_tile(self, tile):
        if not tile.want and tile.streamlink_proc is None:
            return
        tile.want = False
        self._kill_tile(tile)
        # A stopped tile is not loading anything: no spinner on it. The next
        # start re-arms the scrim, so a restart fades in again.
        self._clear_tile_loading(tile)
        if tile.va.player is not None:
            try:
                tile.va.player.command("stop")
            except Exception:
                pass
        dbg(f"tile stop channel={tile.login} tile={tile.uid} player={tile.va.pid}")

    def _kill_tile(self, tile):
        proc = tile.streamlink_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            tile.streamlink_proc = None

    def _mute_tile(self, tile, player):
        """Tiles never make sound — except the one grown into the watch view."""
        if not tile.watch:
            try:
                player.mute = True
            except Exception as e:
                print("tile mute set failed:", e, flush=True)
        if getattr(tile, "_mute_forced", None) is player:
            return
        tile._mute_forced = player

        def enforce(_name, val):
            # The watch video is allowed to be unmuted; only enforce on tiles
            # that are holding their place in the grid.
            if tile.watch:
                return
            if not val:
                try:
                    player.mute = True
                except Exception:
                    pass

        try:
            player.observe_property("mute", enforce)
        except Exception as e:
            print("tile mute observe failed:", e, flush=True)

    def set_tile_watch(self, tile, on):
        tile.watch = bool(on)
        if tile.watch:
            # The watch view keeps its own busy pill and must never show the
            # grid's loading scrim over the full-size player.
            self._reveal_tile(tile, snap=True)
        # The tile's own overlay (avatar, LIVE badge, name, title, game,
        # viewers) belongs to the GRID. On the watch view that information is
        # already in the top bar and above the chat, so it fades out instead of
        # sitting on top of the full-size video.
        self.set_tile_chrome(tile, not tile.watch)
        player = tile.va.player
        if player is None:
            return
        try:
            if on:
                player.mute = False
            else:
                player.mute = True
        except Exception:
            pass

    def set_tile_chrome(self, tile, show, fade=True):
        """Show or hide a tile's overlay chrome (grid info, never the video)."""
        boxes = [b for b in (getattr(tile, "top_box", None),
                             getattr(tile, "bottom_box", None)) if b is not None]
        if not boxes:
            return

        def finish_hide():
            # Only hide if this tile is STILL the watch tile: a superseded fade
            # must never hide chrome we just brought back.
            if getattr(tile, "watch", False):
                for b in boxes:
                    b.set_visible(False)

        if show:
            for b in boxes:
                b.set_visible(True)
            if not fade:
                for b in boxes:
                    b.set_opacity(1.0)
                return
            for i, b in enumerate(boxes):
                self.anim.fade(f"tilechrome{i}-{tile.uid}", b, b.get_opacity(), 1.0,
                               label=f"chrome {tile.login}")
        else:
            if not fade:
                for b in boxes:
                    b.set_opacity(0.0)
                finish_hide()
                return
            for i, b in enumerate(boxes):
                self.anim.fade(f"tilechrome{i}-{tile.uid}", b, b.get_opacity(), 0.0,
                               on_done=finish_hide, label=f"chrome {tile.login}")

    # ---- tile geometry driven by the window's handoff ------------------
    def grow_tile(self, tile, rect, on_done=None):
        self.anim.rect(
            f"grow-{tile.uid}", tile.rect, rect,
            lambda r: tile.place(r), on_done=on_done, label=f"grow {tile.login}",
        )

    def restore_tile(self, tile, on_done=None):
        if tile.cell is None:
            if on_done:
                on_done()
            return
        self.anim.rect(
            f"grow-{tile.uid}", tile.rect, tile.cell,
            lambda r: tile.place(r), on_done=on_done, label=f"restore {tile.login}",
        )

    def snap_home(self, tile):
        """Immediately (same frame) put a tile back on its grid cell."""
        self.anim.cancel(f"grow-{tile.uid}")
        if tile.cell:
            tile.place(tile.cell)

    def snap_tile(self, tile, rect):
        """Immediately (same frame) place a tile at an exact rect.

        Used on a content resize so the grown video tracks the window edge
        instead of lagging behind a 300ms grow tween. That tween restarts on
        every resize event (Hyprland streams them), so while a drag is in
        flight the player is left mid-animation - visually drifting away from
        the video area and never lining up with it. A resize is not a
        user-intent animation, so it must not be one.
        """
        self.anim.cancel(f"grow-{tile.uid}")
        tile.place(rect)

    def fade_tile(self, tile, a, on_done=None, label=""):
        self.anim.fade(f"tile-{tile.uid}", tile, tile.get_opacity(), a,
                       on_done=on_done, label=label or f"tile {tile.login}")

    def find_tile(self, login=None, cid=None):
        if cid and cid in self.tiles:
            return self.tiles[cid]
        if login:
            for tile in self.tiles.values():
                if tile.login == login:
                    return tile
        return None


class Tile(Gtk.Overlay):
    """One live channel: a muted (unless watched) source-quality player."""

    _seq = 0

    def __init__(self, page, info):
        super().__init__()
        Tile._seq += 1
        self.uid = Tile._seq
        self.page = page
        self.info = dict(info)
        self.cid = info["id"]
        self.login = info["login"]
        self.name = info.get("name") or info["login"]
        self.want = False
        self.watch = False
        self.streamlink_proc = None
        self._avatar_path = None
        self.cell = None
        self.rect = (0, 0, 0, 0)
        # Grid loading state, owned by HomePage: loading = scrim is up waiting
        # for a first frame, loaded = that stream has painted at least once.
        self.loading = False
        self.loaded = False
        self._load_started = 0.0

        self.add_css_class("tile")
        self.va = page.VA(page.win, tile=True)
        self.set_child(self.va)
        self.va.connect("realize", self._on_gl_realize)
        self.va.connect("map", lambda *a: page._queue_sync())

        # Loading scrim + spinner, sitting UNDER the chrome so the LIVE badge
        # and the channel info stay readable while a stream connects. A tile
        # that has no picture yet shows this instead of an empty black card,
        # and HomePage crossfades it away on the first real frame. Nothing in
        # here is interactive, so input must pass straight through to the
        # tile's own click gesture (can_target False kills the whole subtree,
        # which is exactly right for a pure decoration).
        veil = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        veil.add_css_class("tile-loading")
        veil.set_halign(Gtk.Align.FILL)
        veil.set_valign(Gtk.Align.FILL)
        veil.set_hexpand(True)
        veil.set_vexpand(True)
        veil.set_can_target(False)
        self.spinner = Adw.Spinner()
        self.spinner.add_css_class("tile-spinner")
        self.spinner.set_halign(Gtk.Align.CENTER)
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.spinner.set_hexpand(True)
        self.spinner.set_vexpand(True)
        veil.append(self.spinner)
        veil.set_visible(False)
        self.load_veil = veil
        self.add_overlay(veil)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.add_css_class("tile-top")
        top.set_valign(Gtk.Align.START)
        # Kept on self so the watch view can hide the tile's chrome: on the
        # full-size player this information already lives in the top bar and
        # above the chat, and leaving it overlaid covers the video.
        self.top_box = top
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)
        self.live_badge = Gtk.Label(label="● LIVE")
        self.live_badge.add_css_class("tile-live")
        self.live_badge.set_valign(Gtk.Align.CENTER)
        top.append(self.live_badge)
        self.add_overlay(top)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom.add_css_class("tile-overlay")
        bottom.set_valign(Gtk.Align.END)
        # Same reason as top_box: the grid shows name/title/game/viewers, the
        # watch view already shows them in the top bar and above the chat.
        self.bottom_box = bottom
        # The profile icon sits IN this row, to the left of the text, so it
        # reads as the channel's avatar beside its name. It used to live in the
        # top box on its own, which put it in the opposite corner from the text
        # it belongs to. Centred rather than top-aligned: the text column is
        # three lines tall (name / title / game+viewers), and a top-aligned
        # 34px circle next to a ~55px column reads as a mistake.
        self.avatar = Adw.Avatar.new(34, self.name, False)
        self.avatar.set_show_initials(True)
        self.avatar.set_valign(Gtk.Align.CENTER)
        bottom.append(self.avatar)
        # The text needs its own column: as direct children of the horizontal
        # row the labels would sit side by side, and the ellipsize would never
        # kick in. hexpand keeps the viewers label out at the right edge.
        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_col.set_hexpand(True)
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
        text_col.append(self.name_lbl)
        text_col.append(self.title_lbl)
        text_col.append(meta)
        bottom.append(text_col)
        self.add_overlay(bottom)

        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.add_controller(click)
        self.set_cursor_from_name("pointer")

        self.update_info(info)

    def mapped_ok(self):
        return self.rect[2] > 0 and self.rect[3] > 0

    def snap_size(self, rect):
        """Take the target SIZE now, keep the current position.

        Paired with a position-only tween this pays the GLArea re-allocation and
        the mpv render-target rebuild ONCE for a column change instead of once
        per animated frame.
        """
        w, h = int(rect[2]), int(rect[3])
        if (w, h) == (self.rect[2], self.rect[3]):
            return
        self.rect = (self.rect[0], self.rect[1], w, h)
        self.set_size_request(max(1, w), max(1, h))

    def move_to(self, x, y):
        """Position-only move: never touches a size request.

        A Gtk.GLArea re-allocated mid-tween makes mpv rebuild its render
        target, which is the most expensive thing that can happen per frame per
        tile, and a pure cell shift does not need it.
        """
        x, y = int(x), int(y)
        if (x, y) == (self.rect[0], self.rect[1]):
            return
        self.rect = (x, y, self.rect[2], self.rect[3])
        self.page.surface.move(self, x, y)

    def place(self, rect):
        rect = tuple(int(v) for v in rect)
        if rect == self.rect:
            return
        x, y, w, h = rect
        resized = (w, h) != (self.rect[2], self.rect[3])
        self.rect = rect
        if resized:
            self.set_size_request(max(1, w), max(1, h))
        self.page.surface.move(self, x, y)

    def update_info(self, info):
        self.info.update(info)
        self.name = info.get("name") or self.name
        if self.name_lbl.get_text() != self.name:
            self.name_lbl.set_text(self.name)
        title = info.get("title") or ""
        if self.title_lbl.get_text() != title:
            self.title_lbl.set_text(title)
        if self.title_lbl.get_visible() != bool(title):
            self.title_lbl.set_visible(bool(title))
        game = info.get("game") or ""
        if self.game_lbl.get_text() != game:
            self.game_lbl.set_text(game)
        if self.game_lbl.get_visible() != bool(game):
            self.game_lbl.set_visible(bool(game))
        vt = _fmt_viewers(info.get("viewers") or 0)
        if self.viewers_lbl.get_text() != vt:
            self.viewers_lbl.set_text(vt)

        local = info.get("local_avatar")
        if local and local != self._avatar_path and os.path.exists(local):
            self._avatar_path = local
            try:
                tex = Gdk.Texture.new_from_file(Gio.File.new_for_path(local))
                self.avatar.set_custom_image(tex)
            except Exception:
                pass

    def _on_gl_realize(self, *_a):
        if self.va.player is not None:
            self.page._mute_tile(self, self.va.player)
        self.page._queue_sync()

    def _on_click(self, gesture, n, x, y):
        if n == 1:
            self.page.win.select_tile(self)
        return False
