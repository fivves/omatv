# omatv

A native GTK4 Twitch viewer with embedded chat, built for Linux (Wayland/X11).

omatv pairs a full-screen mpv video surface with a WebKit-based chat pane —
no browser tabs, no Electron, just a fast native window. It uses
[streamlink](https://streamlink.github.io/) to resolve streams and serves them
to mpv over a local HTTP pipe for **~2.5s end-to-end latency**.

![screenshot placeholder](https://via.placeholder.com/800x450.png?text=omatv)

## Features

- **Low-latency playback** — streamlink `--player-external-http` re-serves the
  stream as a continuous localhost stream; mpv plays right at the live edge
  (~2.5s, same as the CLI).
- **Embedded chat** — WebKit webview with Twitch IRC over WebSocket.
- **Full emote support** — 7TV, BTTV, FrankerFaceZ, and native Twitch emotes
  (global + per-channel + your own sub emotes anywhere).
- **Real badges** — actual Twitch badge images (subscriber, VIP, mod,
  broadcaster, bits, …) from the Helix badges API, with text fallbacks.
- **Follows rail** — clickable avatars of everyone you follow, live/offline
  state, viewer count, game, and title on hover.
- **Tab completion** — `@name` user mentions and emote completion while typing.
- **Sign in with a paste** — no Twitch dev portal needed; get a token from
  [twitchtokengenerator.com](https://twitchtokengenerator.com/) and paste it in.

## Requirements

- Linux with GTK4, libadwaita, WebKitGTK 6.0 (Wayland recommended)
- Python 3.10+
- [streamlink](https://streamlink.github.io/) 6+
- [mpv](https://mpv.io/) with libmpv
- `python-mpv` bindings

### Arch Linux

```bash
sudo pacman -S python-mpv python-gobject webkitgtk-6.0 libadwaita mpv streamlink
```

### Debian / Ubuntu

```bash
sudo apt install python3-mpv python3-gi gir1.2-adw-1 gir1.2-webkit-6.0 mpv streamlink
```

## Install

```bash
git clone https://github.com/fivves/omatv.git
cd omatv
./run.sh            # run in place
# or
./install.sh        # symlink into ~/.local/bin and install the desktop entry
```

## Getting a Twitch token (first run)

omatv needs a Twitch OAuth token + client ID to load your follows, emotes,
and to chat. It does **not** require registering your own app in the Twitch
dev portal:

1. Open [twitchtokengenerator.com](https://twitchtokengenerator.com/)
2. Select scopes: `chat:read`, `chat:edit`, `user:read:follows`
   (`user:read:email` if you want your avatar)
3. Authorize with your Twitch account
4. Copy the **Access Token** and **Client ID**
5. In omatv, click your avatar/name in the top-right → **Sign in…** and paste
   both. The app saves them to `~/.config/twitch.json` (mode 600).

> Tokens are stored only on your machine. omatv never sends them anywhere
> except Twitch's own API. To sign out, click your name → **Logout**.

## Usage

```bash
omatv                              # open the app
omatv <channel>                    # open and immediately play a channel
```

Click a streamer in the follows rail to watch. Use the chat pane to talk;
`@` opens user mention completion, Tab completes emotes.

## How it works

```
streamlink --twitch-low-latency --player-external-http twitch.tv/<login>
        │  re-serves HLS as continuous HTTP stream on 127.0.0.1:<port>
        ▼
mpv (embedded via Gtk.GLArea + python-mpv MpvRenderContext)
        │
Twitch IRC (WebSocket) ──► WebKit webview (chat/chat.js)
```

- **Streams:** streamlink resolves + downloads the HLS, then re-serves it as a
  continuous local HTTP stream. mpv reads it like a live pipe — no HLS segment
  anchoring, so playback starts at the live edge instead of 3 segments back.
- **Chat:** a WebKit webview loads `chat/chat.html` + `chat.js`, which connect
  directly to Twitch IRC over WebSocket. A Python bridge (`window.webkit.
  messageHandlers.omatv.postMessage`) handles fetches that need the OAuth
  token (emotes, badges) and pushes results back with `__push()`.
- **Emotes:** merged from 7TV, BTTV, FFZ (global + per-channel) and Helix
  (`chat/emotes/global`, `chat/emotes?broadcaster_id=`, and your own
  emote-set from the IRC `emote-sets` tag).
- **Badges:** `GET /chat/badges/global` + `GET /chat/badges?broadcaster_id=`
  → real badge images in chat, falling back to text pills.

## Project layout

```
omatv/
├── main.py            # GTK4 window, mpv surface, Python bridge
├── chat/
│   ├── chat.html      # chat UI shell
│   ├── chat.js        # IRC client, emote/badge rendering, composer
│   └── chat.css       # chat styling
├── assets/            # app icon (SVG)
├── run.sh             # run in place
├── install.sh         # install to ~/.local/bin + desktop entry
└── io.github.omarchy.omatv.desktop
```

## Troubleshooting

- **Stream won't start** — make sure `streamlink` and `mpv` are installed and
  on your `PATH`. Streams resolve via streamlink; if the channel is offline or
  region-blocked, playback won't start.
- **Chat shows "Read-only"** — your token lacks `chat:edit`. Generate a new
  token with that scope and sign in again.
- **No follows rail** — your token lacks `user:read:follows`.
- **Emotes/badges missing** — usually a transient API issue; switch channels
  or restart the app to reload.

## License

MIT
