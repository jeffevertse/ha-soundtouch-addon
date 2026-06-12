# SoundTouch add-on documentation

Control a Bose SoundTouch 20 over your LAN and stream internet radio directly to
it, bypassing the Bose cloud (TuneIn / INTERNET_RADIO). The add-on talks to the
speaker over its local HTTP API (port 8090), WebSocket (8080) and UPnP/DLNA, and
exposes a web UI in the Home Assistant sidebar through Ingress.

## Requirements

- **Home Assistant OS** or **Supervised** (add-ons aren't available on Container/Core).
- A Bose SoundTouch 20 on the **same LAN/subnet** as your Home Assistant host.
- Home Assistant reachable on the local network (the add-on runs with
  `host_network` so it can discover the speaker and serve streams to it).

## Installation

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**.
2. Add `https://github.com/jeffevertse/ha-soundtouch-addon`.
3. Open the **SoundTouch** add-on → **Install** → **Start**.
4. Open the **SoundTouch** panel from the sidebar (or **Open Web UI**).

## Configuration

```yaml
device_host: ""          # leave empty to auto-discover, or set e.g. "192.168.1.61"
auto_discover: true      # use mDNS/SSDP discovery when device_host is empty
```

- **device_host** — Set this if auto-discovery doesn't find the speaker (some
  networks block mDNS/SSDP multicast, e.g. across VLANs or with client isolation).
  Find the IP in your router's DHCP client list.
- **auto_discover** — When `device_host` is empty and this is on, the add-on
  scans the network for the SoundTouch on startup.

Changes to these options take effect on the next add-on start/restart.

> **Tip:** Give the SoundTouch a **DHCP reservation** in your router so its IP
> never changes. If it does change, use **Find / Reconnect Device** in the web UI
> (it appears automatically when the speaker becomes unreachable), or update
> `device_host` and restart.

## Usage

### Presets
- Tap a preset to play it.
- Tap the ✏️ pencil on a preset to set its **name**, **stream URL** and **emoji**.
  Use any public HTTP/HTTPS MP3 or AAC stream (find some at
  [radio-browser.info](https://www.radio-browser.info)). HTTPS is downgraded to
  HTTP automatically because the SoundTouch 20 firmware can't do TLS on media.
- Preset edits persist across restarts and updates (stored in `/data`).

### Sync to Device Buttons
"Sync to Device Buttons" writes all configured presets into the speaker's 6
physical preset buttons. They are stored as `LOCAL_INTERNET_RADIO` pointing back
at this add-on's built-in stream proxy (`/api/stream/<id>`), so pressing a
physical button plays the right station with no app open. This relies on the
speaker being able to reach the add-on on the LAN (provided by `host_network`).

### Auto-resume
When the speaker powers back on (or the add-on restarts), it resumes the last
station that was playing. State is persisted in `/data/state.json`.

### Other sources
AUX and Bluetooth inputs can be selected from the **Other Sources** section.

## Troubleshooting

**"SoundTouch not found" / web UI shows a reconnect button**
- Confirm the speaker is powered on and on the same subnet as Home Assistant.
- Set `device_host` to the speaker's IP and restart the add-on.
- Use **Find / Reconnect Device** in the UI to rediscover after an IP change.

**Presets play from the app but physical buttons don't**
- Press **Sync to Device Buttons** again.
- Physical buttons fetch streams from the add-on; this needs `host_network`
  (enabled by default) and the speaker reachable on the LAN.

**A station won't play**
- Verify the stream URL works in a browser/VLC. Station-page URLs aren't streams;
  use the direct `.mp3`/`.aac` or `.pls`/`.m3u` URL. Playlists (`.pls`, `.m3u`,
  `.m3u8`, `.xspf`) are resolved to the first stream automatically.

**Logs**
- Open the add-on → **Log** tab to see discovery, playback and proxy messages.

## How it works

| Component        | Role                                                                 |
| ---------------- | -------------------------------------------------------------------- |
| Flask + gunicorn | Web UI, JSON API, SSE live updates, audio stream proxy               |
| `soundtouch.py`  | SoundTouch HTTP API (8090) + WebSocket (8080) client                 |
| `upnp_player.py` | UPnP/DLNA AVTransport — pushes streams to the speaker                |
| `discovery.py`   | mDNS/SSDP discovery of the speaker                                   |
| `/data`          | Persistent `config.json` (presets/device) and `state.json`          |

## Privacy & security

- The add-on talks only to your SoundTouch on the LAN and to the radio stream
  URLs you configure. No Bose account or cloud is involved.
- The web UI is served behind Home Assistant Ingress, which handles
  authentication — there's no separate password.
