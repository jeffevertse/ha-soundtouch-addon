# Changelog

All notable changes to this add-on are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 1.1.1

### Fixed
- MQTT entities stayed `unknown` until something changed on the speaker. The
  initial state snapshot raced the broker connection (publishes before CONNACK
  were dropped). State is now published on MQTT connect, after warmup, and on
  every WebSocket reconnect, so entities populate as soon as the speaker and
  broker are both up.
- The device id used for entity unique_ids is now resolved with retries, so the
  entities get stable ids instead of the generic `soundtouch_20` fallback when
  the speaker is briefly unreachable at startup.

### Note
- If the add-on can't auto-discover the speaker (in-container mDNS/SSDP varies by
  network), set the **`device_host`** option to the speaker's IP.

## 1.1.0

### Added
- **MQTT discovery entities.** When an MQTT broker is configured, the add-on
  publishes a set of entities grouped under one "SoundTouch" device: a **Power**
  switch, **Volume** and **Bass** numbers, a **Source** select (presets + AUX +
  Bluetooth), **Play/Pause / Next / Previous / Mute** buttons, a **Now Playing**
  sensor (with attributes) and a **Playing** binary sensor. Control the speaker
  from HA dashboards, automations and voice.
  - Home Assistant core has no MQTT `media_player` platform, so these standard
    entities are published instead of a single media_player.
  - Broker credentials are taken automatically from the Home Assistant MQTT
    service (e.g. the Mosquitto broker add-on) — no manual broker config.
  - New `mqtt_enabled` option (default `true`) to turn the bridge off.
- Availability (LWT) so the entities show online/offline with the add-on.

## 1.0.0

Initial release — Home Assistant add-on port of the SoundTouch-Pi controller.

### Added
- Containerized Flask controller for the Bose SoundTouch 20, embedded in the
  Home Assistant sidebar via Ingress.
- 6 editable virtual radio presets, transport/volume/bass controls, live updates
  over SSE, and an audio stream proxy with HTTPS→HTTP downgrade.
- "Sync to Device Buttons" to store presets in the speaker's physical buttons.
- mDNS/SSDP auto-discovery, manual `device_host` option, in-UI reconnect, and
  auto-resume on power-on. Config and state persist in `/data`.

### Changed
- Config and state moved to the add-on `/data` volume; presets seeded from
  defaults on first run.
- Web UI uses Ingress-relative URLs; app-level auth removed (Home Assistant
  Ingress handles authentication).

### Removed
- Raspberry Pi WiFi/hotspot setup and Pi-specific system update/reboot/GPIO
  controls (Home Assistant manages the host).
