# Changelog

All notable changes to this add-on are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
