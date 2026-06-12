# SoundTouch Home Assistant Add-on

Control a **Bose SoundTouch 20** from Home Assistant and stream internet radio
directly to it — bypassing the (now unreliable) Bose cloud. The add-on runs the
controller as a containerized web app and embeds its UI in the Home Assistant
sidebar via Ingress.

> Requires **Home Assistant OS** or a **Supervised** install (add-ons are not
> available on Home Assistant Container or Core).

## Install

1. In Home Assistant go to **Settings → Add-ons → Add-on store**.
2. Click the **⋮** menu (top-right) → **Repositories**.
3. Add this URL and click **Add**:

   ```
   https://github.com/jeffevertse/ha-soundtouch-addon
   ```

4. Close the dialog. The **SoundTouch** add-on now appears in the store under
   this repository — open it and click **Install**.
5. After it builds, click **Start**, then **Open Web UI** (or use the
   **SoundTouch** entry in the sidebar).

## What it does

- 6 virtual radio presets, editable in the web UI (any public MP3/AAC stream)
- Play / pause / next / prev / power / mute, volume and bass control
- Streams internet radio over UPnP/DLNA, auto-downgrading HTTPS to HTTP
  (the SoundTouch 20 firmware can't do TLS on media streams)
- "Sync to Device Buttons" writes the presets into the speaker's 6 physical
  buttons, pointing them back at the add-on's built-in stream proxy
- Auto-resumes the last station when the speaker powers back on

See [`soundtouch/DOCS.md`](soundtouch/DOCS.md) for configuration and
troubleshooting.

## Repository layout

```
ha-soundtouch-addon/
├── repository.yaml      # makes this an HA add-on repository
└── soundtouch/          # the add-on
```

## Credits

Based on the standalone [SoundTouch-Pi](https://github.com/) controller. The
Raspberry Pi WiFi/hotspot setup and Pi-specific system controls are
intentionally omitted from the add-on — Home Assistant manages the host.
