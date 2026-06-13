"""
Flask web server for the SoundTouch Home Assistant add-on.

Runs inside a container under gunicorn (see gunicorn.conf.py) and is exposed in
the Home Assistant sidebar via Ingress. Home Assistant handles authentication,
so there is no app-level auth here. Config and persistent state live in /data.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import socket
import threading
import time
from urllib.parse import urlparse, urlunparse

import requests as _req
from flask import Flask, Response, jsonify, request, render_template, stream_with_context, make_response
from soundtouch import SoundTouch
from upnp_player import UPnPPlayer
from discovery import discover
import state as st
import mqtt_bridge

_server_port: int = 5000   # updated at startup; used by background threads

# Optional MQTT-discovery bridge (None when no broker configured).
_mqtt: "mqtt_bridge.MqttBridge | None" = None

# Config + persistent state live in the add-on's /data volume (survives restarts
# and updates). config.default.json (bundled in the image) seeds it on first run.
DATA_DIR            = "/data"
CONFIG_PATH         = os.path.join(DATA_DIR, "config.json")
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.default.json")
OPTIONS_PATH        = os.path.join(DATA_DIR, "options.json")

app = Flask(__name__)


# ── access control ─────────────────────────────────────────────────────────
# The add-on runs with host_network, so port 5000 is reachable on the LAN.
# Home Assistant Ingress (which authenticates the UI) proxies every UI/API
# request from a fixed internal address. Only the stream proxy must stay open
# to the LAN (the SoundTouch fetches it for hardware presets); everything else
# is restricted to the Ingress proxy so the control API isn't exposed
# unauthenticated to the whole network.
INGRESS_SOURCE_IP = "172.30.32.2"


@app.before_request
def _restrict_to_ingress():
    # The speaker fetches /api/stream/<id> directly over the LAN — leave it open.
    if request.path.startswith("/api/stream/"):
        return None
    # request.remote_addr is the real peer; we are not behind a trusted reverse
    # proxy, so X-Forwarded-* headers are intentionally ignored.
    if request.remote_addr != INGRESS_SOURCE_IP:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    return None


# ── config helpers ─────────────────────────────────────────────────────────

_config_cache: dict | None = None
_config_mtime: float       = 0.0
_config_lock               = threading.Lock()


def load_config() -> dict:
    """
    Return config.json as a dict.  The file is only re-read when it changes
    on disk (mtime check), so repeated calls within a single request are free.
    """
    global _config_cache, _config_mtime
    with _config_lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = 0.0
        if _config_cache is None or mtime != _config_mtime:
            with open(CONFIG_PATH) as f:
                _config_cache = json.load(f)
            _config_mtime = mtime
        return dict(_config_cache)   # shallow copy — callers must not mutate nested objects


def save_config(cfg: dict):
    global _config_cache, _config_mtime
    with _config_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _config_cache = None
        _config_mtime = 0.0


def preset_by_id(preset_id: int) -> dict | None:
    return next((p for p in load_config()["presets"] if p["id"] == preset_id), None)


def _seed_config():
    """Copy the bundled default config into /data on first run."""
    if os.path.exists(CONFIG_PATH):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEFAULT_CONFIG_PATH) as f:
        default = json.load(f)
    with open(CONFIG_PATH, "w") as f:
        json.dump(default, f, indent=2)
    print("[server] Seeded /data/config.json from defaults")


def _apply_addon_options():
    """
    Merge the add-on options (Supervisor writes them to /data/options.json) into
    config.json.  Only device_host / auto_discover are accepted; presets are
    managed at runtime in the web UI and persisted to /data/config.json.
    """
    try:
        with open(OPTIONS_PATH) as f:
            opts = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    cfg = load_config()
    changed = False
    host = (opts.get("device_host") or "").strip()
    if host and cfg["device"].get("host") != host:
        cfg["device"]["host"] = host
        changed = True
    auto = opts.get("auto_discover")
    if isinstance(auto, bool) and cfg["device"].get("auto_discover") != auto:
        cfg["device"]["auto_discover"] = auto
        changed = True
    if changed:
        save_config(cfg)
        print(f"[server] Applied add-on options (host={host or 'auto'}, auto_discover={auto})")


# ── SSE event bus ──────────────────────────────────────────────────────────
# Browser clients subscribe via GET /api/events.  We push JSON blobs to all
# connected clients whenever the SoundTouch WebSocket delivers an update.

_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _sse_subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=30)
    with _sse_lock:
        _sse_subscribers.append(q)
    return q


def _sse_unsubscribe(q: queue.Queue):
    with _sse_lock:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass


def _sse_push(event_type: str, data: dict):
    msg = json.dumps({"type": event_type, "data": data})
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_subscribers.remove(q)


# ── device singletons ──────────────────────────────────────────────────────

_st:   SoundTouch | None = None
_upnp: UPnPPlayer | None = None
_dev_lock = threading.Lock()


def _resolve_host() -> str:
    cfg = load_config()
    host = cfg["device"].get("host")
    if not host:
        host = discover(timeout=8)
        if host:
            cfg["device"]["host"] = host
            save_config(cfg)
    if not host:
        raise RuntimeError("SoundTouch not found. Set 'device_host' in the add-on options.")
    return host


def get_device() -> SoundTouch:
    global _st
    with _dev_lock:
        if _st is not None:
            return _st
        host = _resolve_host()
        cfg = load_config()
        _st = SoundTouch(
            host,
            port=cfg["device"].get("port", 8090),
            ws_port=cfg["device"].get("ws_port", 8080),
        )
        _setup_ws_callbacks(_st)
        _st.start_websocket()
        return _st


def get_upnp() -> UPnPPlayer:
    global _upnp
    with _dev_lock:
        if _upnp is None:
            _upnp = UPnPPlayer(_resolve_host())
        return _upnp


# ── auto-resume + physical button handling ─────────────────────────────────
#
# WHY we use state.json instead of an in-memory _prev_source variable:
#
#   When the SoundTouch enters standby its WebSocket server drops the
#   connection.  Our client reconnects.  By that point the in-memory
#   _prev_source might still be "UPNP" (the STANDBY event arrived after
#   the disconnect, or the event never arrived at all).  Reading the
#   persisted "device_source" from state.json is the only reliable way to
#   know the device was in STANDBY across a reconnect or add-on restart.
#
# WHY we track _last_explicit_play_time:
#
#   The SoundTouch 20 emits transient STANDBY nowPlayingUpdated events
#   during stream initialisation (e.g. after a physical button wakes it
#   from standby and UPnP kicks in).  Without the guard, the STANDBY →
#   UPNP transition looks identical to a genuine power-ON and triggers a
#   spurious auto-resume.  Suppressing for 30 s after any explicit play
#   eliminates the false trigger while leaving genuine power-ON detection
#   intact.

_last_explicit_play_time: float = 0.0   # updated by _play_preset_id
_last_explicit_off_time:  float = 0.0   # updated when the speaker is powered off via the add-on
_RESUME_SUPPRESS_AFTER_OFF = 120        # seconds to suppress auto-resume after an explicit power-off


_PRIVATE_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local + cloud metadata (169.254.169.254)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6
]


def _validate_stream_url(url: str) -> None:
    """Raise ValueError if url targets a private/loopback address or non-HTTP scheme."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed (got {parsed.scheme!r})")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve hostname {hostname!r}: {e}")
    for _fam, _type, _proto, _canon, sockaddr in results:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        for net in _PRIVATE_NETS:
            if addr in net:
                raise ValueError(
                    f"Stream URL resolves to a private/loopback address ({sockaddr[0]})"
                )


def _resolve_public_ip(host: str) -> str:
    """
    Resolve `host`, reject it if it resolves to any private/loopback/link-local
    address, and return the first public IP. Returning the exact resolved IP lets
    callers pin it for the connection, closing the DNS-rebinding window between
    validation and the actual fetch.
    """
    try:
        results = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve hostname {host!r}: {e}")
    public: str | None = None
    for _fam, _type, _proto, _canon, sockaddr in results:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if any(addr in net for net in _PRIVATE_NETS):
            raise ValueError(f"{host!r} resolves to a private/loopback address ({addr})")
        if public is None:
            public = sockaddr[0]
    if public is None:
        raise ValueError(f"{host!r} did not resolve to a usable address")
    return public


def _safe_fetch(method: str, url: str, **kwargs):
    """
    DNS-rebinding-safe HTTP fetch. Downgrades HTTPS→HTTP (the SoundTouch 20 can't
    do TLS on media anyway), resolves + validates the host ONCE, then connects to
    that exact IP while preserving the original Host header for virtual-host
    routing. Redirects are NOT followed automatically — callers re-validate the
    Location through this same function.
    """
    if url.startswith("https://"):
        url = "http://" + url[len("https://"):]
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise ValueError(f"Only http/https URLs are allowed (got {parsed.scheme!r})")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")
    ip   = _resolve_public_ip(host)
    port = parsed.port or 80
    netloc = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    pinned = urlunparse(parsed._replace(netloc=netloc))
    headers = dict(kwargs.pop("headers", None) or {})
    headers["Host"] = parsed.netloc   # keep the original host[:port] for routing
    kwargs.setdefault("allow_redirects", False)
    return _req.request(method, pinned, headers=headers, **kwargs)


def _resolve_stream_url(url: str) -> str:
    """
    If url is a PLS or M3U playlist, fetch it and return the first direct
    stream URL inside.  Otherwise return url unchanged.
    Always returns an HTTP URL (downgrades HTTPS for SoundTouch 20 firmware).
    """
    if not url:
        return url
    _validate_stream_url(url)
    if url.startswith("https://"):
        url = "http://" + url[8:]

    # Quick check: is this obviously a playlist by extension or content-type?
    lower = url.lower()
    is_playlist = any(lower.endswith(ext) for ext in (".pls", ".m3u", ".m3u8", ".xspf"))
    if not is_playlist:
        try:
            head = _safe_fetch("HEAD", url, timeout=5)
            if head.status_code in (301, 302, 303, 307, 308):
                location = head.headers.get("Location", "")
                if location:
                    head = _safe_fetch("HEAD", location, timeout=5)
            ct = head.headers.get("Content-Type", "")
            is_playlist = any(x in ct for x in ("scpls", "mpegurl", "xspf"))
        except Exception:
            pass

    if not is_playlist:
        return url  # Already a direct stream

    # Fetch and parse the playlist (capped at 8 KB)
    try:
        r = _safe_fetch("GET", url, timeout=10, stream=True)
        raw = b""
        for chunk in r.iter_content(chunk_size=1024):
            raw += chunk
            if len(raw) > 8192:
                break
        r.close()
        text = raw.decode("utf-8", errors="replace")
        # PLS: look for File1=<url>  (or File2, etc.)
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("file") and "=" in line:
                candidate = line.split("=", 1)[1].strip()
                if candidate.startswith("http"):
                    if candidate.startswith("https://"):
                        candidate = "http://" + candidate[8:]
                    print(f"[server] Resolved playlist {url} → {candidate}")
                    return candidate
        # M3U: first non-comment line that is a URL
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("http"):
                if line.startswith("https://"):
                    line = "http://" + line[8:]
                print(f"[server] Resolved M3U {url} → {line}")
                return line
    except Exception as e:
        print(f"[server] Playlist resolution failed for {url}: {e}")

    return url  # Fall back to original if parsing fails


def _play_preset_id(preset_id: int) -> bool:
    """
    Play a virtual preset via UPnP.  Safe to call from any thread.
    Returns True on success, False on any failure.
    """
    global _last_explicit_play_time
    preset = preset_by_id(preset_id)
    if not preset:
        print(f"[server] No virtual preset for id={preset_id}")
        return False
    url = _resolve_stream_url(preset.get("stream_url", ""))
    if not url:
        print(f"[server] Preset {preset_id} has no stream URL")
        return False
    try:
        get_upnp().play_stream(url, preset.get("name", ""))
        # Record play time BEFORE patching state so that the nowPlayingUpdated
        # events that follow don't misfire auto-resume.
        _last_explicit_play_time = time.time()
        st.patch({
            "last_preset_id":   preset_id,
            "now_playing_name": preset.get("name"),
            "now_playing_icon": preset.get("icon"),
        })
        _sse_push("nowPlaying", {
            "station":   preset.get("name"),
            "icon":      preset.get("icon"),
            "status":    "PLAY_STATE",
            "preset_id": preset_id,
        })
        if _mqtt:
            _mqtt.publish_now_playing({
                "source": "UPNP", "status": "PLAY_STATE",
                "station": preset.get("name"), "preset_id": preset_id,
            }, source_option=preset.get("name"))
        print(f"[server] Playing preset {preset_id}: {preset.get('name')}")
        return True
    except Exception as e:
        print(f"[server] Error playing preset {preset_id}: {e}")
        _sse_push("error", {"message": str(e)})
        return False


def _auto_resume():
    """Resume the last played preset on a genuine power-on / add-on restart."""
    time.sleep(3)   # give the device a moment to finish booting
    # Respect an intentional power-off issued from the add-on: if the user just
    # turned the speaker off, don't immediately wake it back up.
    if time.time() - _last_explicit_off_time < _RESUME_SUPPRESS_AFTER_OFF:
        print("[server] Auto-resume suppressed — recent explicit power-off")
        return
    saved = st.load()
    last_id = saved.get("last_preset_id")
    if not last_id:
        print("[server] Auto-resume: no last preset saved — skipping")
        return
    try:
        np     = get_device().now_playing()
        src    = np.get("source") or ""
        status = np.get("status") or ""
        # Never wake a speaker that is currently in standby — auto-resume is only
        # meant to RESUME a device that has come back on, not turn one on.
        if src == "STANDBY":
            print("[server] Auto-resume: device in standby — skipping (won't wake it)")
            return
        if status in ("PLAY_STATE", "BUFFERING_STATE"):
            print("[server] Auto-resume: device already playing — skipping")
            return
    except Exception:
        pass
    print(f"[server] Auto-resume: playing preset {last_id}")
    _play_preset_id(last_id)


def _setup_ws_callbacks(device: SoundTouch):

    def on_update(event_type: str, data: dict):

        # ── physical preset button pressed ──────────────────────────────────
        if event_type == "nowSelectionUpdated":
            pid = data.get("preset_id")
            if not pid:
                return
            print(f"[server] Physical button {pid} pressed")

            def check_and_play(preset_id: int):
                """
                Wait briefly to see if the device starts playing the preset
                natively (via LOCAL_INTERNET_RADIO stored in the hardware slot).
                If it does, just update our state.  If it doesn't (native
                playback failed), fall back to UPnP.
                """
                time.sleep(3)
                preset = preset_by_id(preset_id)
                if not preset:
                    return
                try:
                    np = device.now_playing()
                    if np.get("status") in ("PLAY_STATE", "BUFFERING_STATE"):
                        # Device is playing natively — sync our state to match
                        print(f"[server] Preset {preset_id} playing natively ✓")
                        st.patch({
                            "last_preset_id":   preset_id,
                            "now_playing_name": preset.get("name"),
                            "now_playing_icon": preset.get("icon"),
                        })
                        _sse_push("nowPlaying", {
                            "station":   preset.get("name"),
                            "icon":      preset.get("icon"),
                            "status":    "PLAY_STATE",
                            "preset_id": preset_id,
                        })
                        return
                except Exception as e:
                    print(f"[server] Native-play check error: {e}")

                # Native playback didn't start — push via UPnP
                print(f"[server] Native play timed out for preset {preset_id} — using UPnP")
                _play_preset_id(preset_id)

            threading.Thread(target=check_and_play, args=(pid,), daemon=True).start()

        # ── now-playing changed ─────────────────────────────────────────────
        elif event_type == "nowPlayingUpdated":
            src = data.get("source") or ""

            # Read the PERSISTED last-known source.  This is the reliable signal
            # for detecting STANDBY → active transitions across reconnects/restarts.
            last_src = st.load().get("device_source") or ""

            if last_src == "STANDBY" and src and src != "STANDBY":
                # Device came out of standby.  Only auto-resume if no explicit
                # play happened recently — the SoundTouch 20 emits transient
                # STANDBY events during stream init (e.g. button-press wake-up)
                # which would otherwise look identical to a genuine power-ON.
                elapsed = time.time() - _last_explicit_play_time
                if elapsed > 30:
                    print(f"[server] Power-ON detected ({last_src!r} → {src!r}) — auto-resume")
                    threading.Thread(target=_auto_resume, daemon=True).start()
                else:
                    print(f"[server] Transient STANDBY→{src!r} suppressed "
                          f"(explicit play {elapsed:.0f}s ago)")

            # Persist new source AFTER the transition check above
            st.patch({"device_source": src})

            # Inject our known station name/icon into the SSE payload
            saved = st.load()
            if saved.get("now_playing_name") and src not in ("STANDBY", ""):
                data = {**data,
                        "station": saved["now_playing_name"],
                        "icon":    saved.get("now_playing_icon")}

            _sse_push("nowPlaying", data)
            if _mqtt:
                try:
                    _mqtt.publish_now_playing(data, source_option=_source_option_for(data))
                except Exception:
                    pass

        # ── volume changed (physical knob or app) ───────────────────────────
        elif event_type == "volumeUpdated":
            try:
                vol = device.get_volume()
                _sse_push("volume", vol)
                if _mqtt:
                    _mqtt.publish_volume(vol)
            except Exception:
                pass

    def on_reconnect():
        """
        WebSocket reconnected.  Could be:
          A) Add-on just (re)started while device was playing → auto-resume
          B) Device just powered ON from standby               → auto-resume
          C) Device is still in standby after a drop           → do NOT resume
          D) Brief network blip, device still playing          → do NOT resume
        """
        print("[server] WebSocket reconnected — checking playback state")
        time.sleep(5)   # let the device settle before polling
        try:
            np = device.now_playing()
            src    = np.get("source", "") or ""
            status = np.get("status",  "") or ""

            # The persisted source is the ground truth for what the device
            # was doing BEFORE this reconnect (survives across add-on restarts).
            last_src = st.load().get("device_source") or ""

            if src == "STANDBY":
                # Device is in standby right now — user turned it off.
                # Persist STANDBY so the power-ON detection in on_update works.
                st.patch({"device_source": "STANDBY"})
                print("[server] Reconnect: device in standby — no auto-resume")

            elif status in ("PLAY_STATE", "BUFFERING_STATE"):
                # Already playing (e.g., brief blip) — nothing to do
                print(f"[server] Reconnect: already playing ({src}) — no auto-resume")

            elif last_src == "STANDBY" and src and src not in ("STANDBY", "INVALID_SOURCE"):
                # Device was in standby and has since come back on — resume
                print(f"[server] Reconnect: woke from standby ({src}) — auto-resume")
                threading.Thread(target=_auto_resume, daemon=True).start()

            else:
                # Nothing playing and not in standby → likely add-on restart → resume
                print(f"[server] Reconnect: idle (src={src!r}, last={last_src!r}) — auto-resume")
                threading.Thread(target=_auto_resume, daemon=True).start()

            # Refresh MQTT state after a WebSocket reconnect.
            if _mqtt:
                _publish_mqtt_snapshot()

        except Exception as e:
            print(f"[server] Reconnect check failed: {e}")

    device.on_update(on_update)
    device.on_reconnect(on_reconnect)


# ── SSE endpoint ───────────────────────────────────────────────────────────

@app.get("/api/events")
def api_events():
    """
    Server-Sent Events stream.  Browser connects once; we push JSON blobs
    whenever the SoundTouch WebSocket delivers volume, now-playing, or
    error events.
    """
    q = _sse_subscribe()

    def generate():
        try:
            # Send an immediate heartbeat so the browser knows we're live
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    # Keepalive comment to prevent proxies from closing the connection
                    yield ": keepalive\n\n"
        finally:
            _sse_unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering (HA ingress)
        },
    )


# ── API routes ─────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    try:
        device = get_device()
        np  = device.now_playing()
        vol = device.get_volume()

        # Overlay our station name/icon when the device is playing a UPnP stream
        saved = st.load()
        if saved.get("now_playing_name"):
            src = np.get("source", "")
            if src not in ("STANDBY", "AUX", "BLUETOOTH", "AIRPLAY", ""):
                np["station"] = saved["now_playing_name"]
                np["icon"]    = saved.get("now_playing_icon")

        return jsonify({"ok": True, "now_playing": np, "volume": vol})
    except Exception as e:
        print(f"[server] api_status: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


@app.get("/api/presets")
def api_presets():
    cfg = load_config()
    hw = []
    try:
        hw = get_device().get_hardware_presets()
    except Exception:
        pass
    return jsonify({"virtual": cfg["presets"], "hardware": hw})


@app.post("/api/preset/<int:preset_id>/play")
def api_play_preset(preset_id: int):
    preset = preset_by_id(preset_id)
    if not preset:
        return jsonify({"ok": False, "error": "Preset not found"}), 404
    if not preset.get("stream_url"):
        return jsonify({"ok": False, "error": "Preset has no stream URL configured"}), 400
    # Delegate to _play_preset_id so _last_explicit_play_time is always set.
    # Without this guard, playing from the UI could trigger a spurious auto-resume
    # if the SoundTouch emits a transient STANDBY during stream initialisation.
    ok = _play_preset_id(preset_id)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Stream failed — check add-on logs"}), 503


@app.post("/api/preset/<int:preset_id>/save")
def api_save_preset(preset_id: int):
    if not 1 <= preset_id <= 6:
        return jsonify({"ok": False, "error": "Preset ID must be 1–6"}), 400
    data = request.get_json()
    stream_url = data.get("stream_url", "").strip()
    if stream_url:
        try:
            _validate_stream_url(stream_url)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    cfg = load_config()
    for preset in cfg["presets"]:
        if preset["id"] == preset_id:
            preset["name"]       = data.get("name", preset["name"])
            preset["stream_url"] = stream_url or preset.get("stream_url", "")
            preset["icon"]       = data.get("icon", preset.get("icon", "📻"))
            break
    else:
        cfg["presets"].append({
            "id":         preset_id,
            "name":       data.get("name", f"Preset {preset_id}"),
            "stream_url": stream_url,
            "icon":       data.get("icon", "📻"),
        })
    save_config(cfg)
    if _mqtt:
        # Preset names feed the MQTT "Source" select options — republish.
        _mqtt.configure(presets=[p.get("name") for p in cfg["presets"] if p.get("name")])
    return jsonify({"ok": True})


@app.post("/api/volume")
def api_set_volume():
    data = request.get_json()
    level = data.get("level")
    if level is None:
        return jsonify({"ok": False, "error": "Missing 'level'"}), 400
    try:
        get_device().set_volume(int(level))
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[server] api_set_volume: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


@app.post("/api/control/<action>")
def api_control(action: str):
    actions = {
        "play":       lambda d: d.play(),
        "pause":      lambda d: d.pause(),
        "play_pause": lambda d: d.play_pause(),
        "stop":       lambda d: d.stop(),
        "power":      lambda d: d.power(),
        "mute":       lambda d: d.mute(),
        "volume_up":  lambda d: d.volume_up(),
        "volume_down":lambda d: d.volume_down(),
        "next":       lambda d: d.next_track(),
        "prev":       lambda d: d.prev_track(),
        "aux":        lambda d: d.select_aux(),
        "bluetooth":  lambda d: d.select_bluetooth(),
    }
    fn = actions.get(action)
    if not fn:
        return jsonify({"ok": False, "error": "Unknown action"}), 400
    try:
        fn(get_device())
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[server] api_control/{action}: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


@app.get("/api/info")
def api_info():
    try:
        d = get_device()
        return jsonify({"ok": True, "info": d.get_info(), "host": d.host})
    except Exception as e:
        print(f"[server] api_info: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


@app.get("/api/sources")
def api_sources():
    try:
        return jsonify({"ok": True, "sources": get_device().get_sources()})
    except Exception as e:
        print(f"[server] api_sources: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


@app.get("/api/bass")
def api_get_bass():
    try:
        d = get_device()
        caps  = d.get_bass_capabilities()
        level = d.get_bass() if caps["available"] else None
        return jsonify({"ok": True, "level": level, "caps": caps})
    except Exception as e:
        print(f"[server] api_get_bass: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


@app.post("/api/bass")
def api_set_bass():
    data = request.get_json()
    try:
        level = int(data["level"])
        get_device().set_bass(level)
        if _mqtt:
            _mqtt.publish_bass(level)
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[server] api_set_bass: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


# ── stream proxy ──────────────────────────────────────────────────────────

@app.get("/api/stream/<int:preset_id>")
def api_stream_proxy(preset_id: int):
    """
    Transparent HTTP audio proxy for a preset stream.

    The SoundTouch fetches this URL when a hardware preset stored with
    source=LOCAL_INTERNET_RADIO is recalled.  Routing through the add-on means:
      • HTTPS streams are transparently downgraded (SoundTouch 20 firmware
        does not support TLS on media streams)
      • The preset location URL never changes even if the upstream moves
      • ICY metadata is forwarded so the device gets station/track info

    This endpoint is reachable by the speaker on the LAN because the add-on
    runs with host_network: true.
    """
    preset = preset_by_id(preset_id)
    if not preset:
        return "Preset not found", 404
    stream_url = _resolve_stream_url(preset.get("stream_url", ""))
    if not stream_url:
        return "No stream URL configured", 404

    try:
        _hdrs = {"User-Agent": "SoundTouch/1.0", "Icy-MetaData": "1"}
        upstream = _safe_fetch("GET", stream_url, stream=True, timeout=15, headers=_hdrs)
        # Follow one redirect (re-validated + IP-pinned by _safe_fetch)
        if upstream.status_code in (301, 302, 303, 307, 308):
            location = upstream.headers.get("Location", "")
            upstream = _safe_fetch("GET", location, stream=True, timeout=15, headers=_hdrs)
        content_type = upstream.headers.get("Content-Type", "audio/mpeg")

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        resp_headers = {"Content-Type": content_type}
        for h, v in upstream.headers.items():
            if h.lower().startswith("icy-"):
                resp_headers[h] = v

        return Response(
            stream_with_context(generate()),
            headers=resp_headers,
        )
    except Exception as e:
        print(f"[proxy] Stream error for preset {preset_id}: {e}")
        return "Stream error", 502


# ── hardware preset sync ───────────────────────────────────────────────────

def _get_host_ip() -> str:
    """
    Return the add-on host's LAN IP — the address the SoundTouch can reach
    back to for hardware-preset streams (the add-on runs on host_network).

    Uses the UDP-connect trick: connecting a UDP socket sets its source
    address via the kernel routing table without sending any packets, so
    this works even when the remote host is unreachable.
    """
    # Primary: use the route to the SoundTouch so we pick the right interface.
    try:
        host = _resolve_host()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # Fallback: any routable interface via the default gateway.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"   # last resort — hardware preset sync will fail visibly


def _resync_hardware_presets():
    """
    Write all virtual presets into the SoundTouch hardware slots.
    Safe to call from a background thread (no Flask request context needed).
    Uses the add-on's stream proxy URL so the device always has a reachable,
    HTTP endpoint regardless of upstream stream format.
    """
    try:
        device = get_device()
        host_ip = _get_host_ip()
        cfg     = load_config()
        for preset in cfg["presets"]:
            pid        = preset["id"]
            name       = preset.get("name", f"Preset {pid}")
            stream_url = preset.get("stream_url", "")
            if not stream_url:
                continue
            proxy_url = f"http://{host_ip}:{_server_port}/api/stream/{pid}"
            try:
                device.store_preset(
                    preset_id=pid,
                    source="LOCAL_INTERNET_RADIO",
                    location=proxy_url,
                    item_name=name,
                    type_attr="stationurl",
                )
                print(f"[server] Hardware preset {pid} → {proxy_url}")
            except Exception as e:
                print(f"[server] store_preset {pid} failed: {e}")
    except Exception as e:
        print(f"[server] _resync_hardware_presets failed: {e}")


@app.post("/api/sync-hardware-presets")
def api_sync_hardware_presets():
    """
    Write all configured virtual presets into the SoundTouch hardware preset
    slots using the undocumented /storePreset endpoint.

    Each preset is stored as LOCAL_INTERNET_RADIO with a URL pointing back to
    this add-on's stream proxy.  When the user presses a physical button the
    device fetches the proxy URL and plays the resolved stream — no WebSocket
    interception needed.
    """
    try:
        device = get_device()
        host_ip = _get_host_ip()
        cfg     = load_config()
        results = []
        for preset in cfg["presets"]:
            pid        = preset["id"]
            name       = preset.get("name", f"Preset {pid}")
            stream_url = preset.get("stream_url", "")
            if not stream_url:
                results.append({"id": pid, "ok": False, "reason": "no stream_url"})
                continue
            # Use the add-on stream proxy — always HTTP, handles redirects,
            # works even when the real stream is HTTPS
            proxy_url = f"http://{host_ip}:{_server_port}/api/stream/{pid}"
            try:
                device.store_preset(
                    preset_id=pid,
                    source="LOCAL_INTERNET_RADIO",
                    location=proxy_url,
                    item_name=name,
                    type_attr="stationurl",
                )
                results.append({"id": pid, "ok": True, "proxy_url": proxy_url})
                print(f"[server] Stored hardware preset {pid}: {name} → {proxy_url}")
            except Exception as e:
                results.append({"id": pid, "ok": False, "reason": "sync failed"})
                print(f"[server] Failed to store preset {pid}: {e}")
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        print(f"[server] api_sync_hardware_presets: {e}")
        return jsonify({"ok": False, "error": "Internal error"}), 503


# ── Device reconnect ──────────────────────────────────────────────────────

def _reset_device():
    """Stop the current device WebSocket and clear both singletons."""
    global _st, _upnp
    with _dev_lock:
        if _st is not None:
            try:
                _st.stop_websocket()
            except Exception:
                pass
            _st = None
        _upnp = None


@app.post("/api/device/reconnect")
def api_device_reconnect():
    """
    Rediscover the SoundTouch device (e.g. after a factory reset or IP change).

    Accepts an optional JSON body {"host": "x.x.x.x"} to skip auto-discovery
    and connect directly to a known IP.  Without a body (or with host omitted /
    null) the server runs mDNS + SSDP discovery to locate the device.

    On success: updates config.json, tears down the stale singleton, creates a
    new connection, and kicks off a hardware preset resync in the background.
    """
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip() or None

    if not host:
        host = discover(timeout=10)
    if not host:
        return jsonify({
            "ok": False,
            "error": "Device not found on the network. Enter the IP address manually.",
        }), 404

    # Validate: actually reach the device before saving the new host
    from soundtouch import SoundTouch as _ST
    try:
        probe = _ST(host, port=8090)
        probe.get_info()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Found {host} but could not connect: {e}"}), 503

    # Persist new host
    cfg = load_config()
    cfg["device"]["host"] = host
    save_config(cfg)

    # Drop old singleton → next get_device() builds a fresh one
    _reset_device()

    try:
        get_device()
    except Exception as e:
        print(f"[server] api_device_reconnect setup: {e}")
        return jsonify({"ok": False, "error": "Reconnected but setup failed"}), 503

    threading.Thread(target=_resync_hardware_presets, daemon=True).start()
    return jsonify({"ok": True, "host": host, "resyncing": True})


# ── debug ──────────────────────────────────────────────────────────────────

@app.get("/api/debug")
def api_debug():
    d = get_device()
    out = {}
    for key, fn in {
        "info":         d.get_info,
        "sources":      d.get_sources,
        "hw_presets":   d.get_hardware_presets,
        "now_playing":  d.now_playing,
        "capabilities": d.get_capabilities,
        "volume":       d.get_volume,
    }.items():
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": str(e)}
    out["state"] = st.load()
    return jsonify(out)


# ── UI ─────────────────────────────────────────────────────────────────────

def _app_version() -> str:
    return os.environ.get("BUILD_VERSION") or str(int(time.time()))


@app.get("/")
def index():
    # Behind Home Assistant Ingress the app is served under a per-session token
    # path; X-Ingress-Path carries that prefix so the UI can resolve relative
    # asset/API URLs via <base href>. Empty when accessed directly.
    resp = make_response(render_template(
        "index.html",
        version=_app_version(),
        ingress_path=request.headers.get("X-Ingress-Path", ""),
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"]  = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ── startup ────────────────────────────────────────────────────────────────

def warmup():
    try:
        d    = get_device()
        info = d.get_info()
        print(f"Connected to: {info.get('name')} ({info.get('type')}) at {d.host}")
        # Write all presets into the hardware slots so physical buttons work
        # immediately after (re)start without needing a manual sync
        time.sleep(5)   # let the device finish its own boot tasks
        _resync_hardware_presets()
        # Device is confirmed reachable now — refresh MQTT state (covers the case
        # where MQTT connected before the speaker was reachable).
        _publish_mqtt_snapshot()
    except Exception as e:
        print(f"Could not connect at startup: {e}")


# ── MQTT discovery bridge ───────────────────────────────────────────────────
#
# Home Assistant core has no MQTT media_player platform, so the SoundTouch is
# exposed as a set of standard MQTT-discovery entities (switch/number/select/
# button/sensor) grouped as one device.  Enabled only when run.sh found the
# Supervisor MQTT service and exported MQTT_HOST.

def _source_option_for(np: dict) -> str | None:
    """Map a now-playing payload to one of the MQTT "Source" select options."""
    src = (np.get("source") or "").upper()
    if src == "AUX":
        return "AUX"
    if src == "BLUETOOTH":
        return "Bluetooth"
    name = np.get("station") or st.load().get("now_playing_name")
    if name and name in [p.get("name") for p in load_config()["presets"]]:
        return name
    return None


def _mqtt_power(on: bool):
    # The SoundTouch only has a power toggle, so only act on a real state change.
    # Record OFF intent (and clear it on ON) so the auto-resume detection doesn't
    # immediately wake the speaker back up after the user turns it off.
    global _last_explicit_off_time
    d = get_device()
    is_on = (d.now_playing().get("source") or "") not in ("STANDBY", "")
    if on == is_on:
        return
    _last_explicit_off_time = 0.0 if on else time.time()
    d.power()


def _mqtt_select_source(option: str):
    if option == "AUX":
        get_device().select_aux()
        return
    if option == "Bluetooth":
        get_device().select_bluetooth()
        return
    for p in load_config()["presets"]:
        if p.get("name") == option:
            _play_preset_id(p["id"])
            return
    print(f"[mqtt] unknown source option {option!r}")


def _publish_mqtt_snapshot():
    """
    Publish a full state snapshot.  Called whenever both the broker and the
    device are up: on MQTT (re)connect (bridge on_ready), after warmup, and on
    WebSocket reconnect.  A no-op until the device is reachable / MQTT connected.
    """
    if not _mqtt:
        return
    try:
        d  = get_device()
        np = d.now_playing()
        _mqtt.publish_now_playing(np, source_option=_source_option_for(np))
        _mqtt.publish_volume(d.get_volume())
        caps = d.get_bass_capabilities()
        if caps.get("available"):
            _mqtt.publish_bass(d.get_bass())
    except Exception as e:
        print(f"[server] MQTT snapshot failed: {e}")


def _start_mqtt():
    """Connect the MQTT bridge once the device is reachable (background thread)."""
    global _mqtt
    if not os.environ.get("MQTT_HOST"):
        print("[server] MQTT bridge disabled (no broker configured)")
        return

    handlers = {
        "power":         _mqtt_power,
        "set_volume":    lambda v: get_device().set_volume(int(v)),
        "set_bass":      lambda v: get_device().set_bass(int(v)),
        "select_source": _mqtt_select_source,
        "play_pause":    lambda: get_device().play_pause(),
        "next":          lambda: get_device().next_track(),
        "previous":      lambda: get_device().prev_track(),
        "mute":          lambda: get_device().mute(),
    }

    # Resolve the device id so the entity unique_ids are stable.  The speaker may
    # not be reachable the instant we start (discovery, boot order) — retry a few
    # times before falling back to a generic id.
    device_id, device_name = "", "SoundTouch 20"
    bass_caps = None
    for _ in range(5):
        try:
            info = get_device().get_info()
            device_id   = info.get("deviceID", "") or ""
            device_name = info.get("name") or device_name
            bass_caps   = get_device().get_bass_capabilities()
            break
        except Exception as e:
            print(f"[server] MQTT: device not ready yet ({e}); retrying in 5s…")
            time.sleep(5)
    if not device_id:
        print("[server] MQTT: speaker unreachable — publishing with a generic id. "
              "If discovery isn't working, set the 'device_host' add-on option.")

    bridge = mqtt_bridge.MqttBridge(
        handlers,
        device_id=device_id,
        device_name=device_name,
        on_ready=lambda: threading.Thread(target=_publish_mqtt_snapshot, daemon=True).start(),
    )
    bridge.configure(
        presets=[p.get("name") for p in load_config()["presets"] if p.get("name")],
        bass_caps=bass_caps,
    )
    # Set the global before start() so the on_ready callback (fired from the MQTT
    # network thread on connect) sees the bridge.
    _mqtt = bridge
    if not bridge.start():
        _mqtt = None


def _startup(port: int = 5000):
    """
    Initialise background tasks.

    Called by gunicorn's post_worker_init hook (see gunicorn.conf.py).
    Daemon threads must be started here rather than at module import time
    because forking (gunicorn) kills threads that ran in the parent process.
    """
    global _server_port
    _server_port = port
    _seed_config()
    _apply_addon_options()
    threading.Thread(target=warmup, daemon=True).start()
    threading.Thread(target=_start_mqtt, daemon=True).start()
    print(f"[server] Background tasks started (port={_server_port})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    _startup(args.port)
    print(f"SoundTouch add-on running at http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
