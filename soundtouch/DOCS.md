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
mqtt_enabled: true       # publish MQTT-discovery entities (see "Home Assistant entities")
```

- **device_host** — Set this if auto-discovery doesn't find the speaker (some
  networks block mDNS/SSDP multicast, e.g. across VLANs or with client isolation).
  Find the IP in your router's DHCP client list.
- **auto_discover** — When `device_host` is empty and this is on, the add-on
  scans the network for the SoundTouch on startup.
- **mqtt_enabled** — Publish Home Assistant entities over MQTT discovery (see
  below). Turn off if you only want the web UI.

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

## Home Assistant entities (MQTT)

With `mqtt_enabled: true` and an MQTT broker configured, the add-on publishes
these entities via MQTT discovery, grouped under a single **SoundTouch** device:

| Entity                         | Type            | What it does                                   |
| ------------------------------ | --------------- | ---------------------------------------------- |
| **Power**                      | `switch`        | Turn the speaker on / off (toggle)             |
| **Volume**                     | `number` 0–100  | Set the volume                                 |
| **Bass**                       | `number`        | Set bass (only if the speaker supports it)     |
| **Source**                     | `select`        | Pick a preset, or AUX / Bluetooth              |
| **Play/Pause, Next, Previous, Mute** | `button`s | Transport controls                             |
| **Now Playing**                | `sensor`        | Station/track text, with attributes            |
| **Playing**                    | `binary_sensor` | On while audio is playing                       |

> **Why not a single `media_player` entity?** Home Assistant core has no MQTT
> `media_player` platform — MQTT discovery doesn't support it — so the add-on
> publishes the standard entities above instead. They cover the same control
> surface for dashboards, automations and voice.

### Setting up the MQTT broker (one-time)

If you don't already have MQTT in Home Assistant:

1. **Settings → Add-ons → Add-on store** → install **Mosquitto broker** → **Start**.
2. Home Assistant should auto-discover it: **Settings → Devices & Services** →
   configure the **MQTT** integration (or add it and point it at the Mosquitto
   broker, "core-mosquitto"). Leave it using the broker's defaults.
3. Restart the **SoundTouch** add-on. It picks up the broker credentials
   automatically (no broker settings to enter here) and the **SoundTouch** device
   appears under **Settings → Devices & Services → MQTT**.

The add-on log shows `MQTT enabled — broker …` and `[mqtt] discovery published`
when it's working. If you see `no MQTT service is available`, finish step 2 and
restart the add-on.

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

**MQTT entities don't appear**
- Confirm the Mosquitto broker add-on is running and the MQTT integration is
  configured (see "Setting up the MQTT broker"). Then restart this add-on.
- Check the add-on log for `MQTT enabled` / `[mqtt] discovery published`.
- The entities live under **Settings → Devices & Services → MQTT → SoundTouch**.

**Logs**
- Open the add-on → **Log** tab to see discovery, playback, MQTT and proxy messages.

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
