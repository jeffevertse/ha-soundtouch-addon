# SoundTouch

Control a Bose SoundTouch 20 and stream internet radio directly to it, bypassing
the Bose cloud. The controller's web UI is embedded in the Home Assistant
sidebar via Ingress.

## Features

- 6 editable virtual radio presets (any public MP3/AAC stream URL)
- Transport controls, volume and bass
- UPnP/DLNA streaming with automatic HTTPS→HTTP downgrade for the SoundTouch 20
- Writes presets into the speaker's 6 physical buttons ("Sync to Device Buttons")
- Auto-discovers the speaker via mDNS/SSDP; auto-resumes on power-on

## Configuration

| Option          | Default | Description                                                                 |
| --------------- | ------- | --------------------------------------------------------------------------- |
| `device_host`   | _empty_ | Speaker IP (e.g. `192.168.1.61`). Leave empty to auto-discover.             |
| `auto_discover` | `true`  | Discover the speaker on the network via mDNS/SSDP when no host is set.       |

Presets themselves are edited in the web UI and persist in the add-on's data
volume (they are **not** add-on options).

See **Documentation** (DOCS.md) for full usage and troubleshooting.
