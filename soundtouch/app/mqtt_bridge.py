"""
Optional MQTT bridge — exposes the SoundTouch in Home Assistant as standard
MQTT-discovery entities.

Home Assistant core has no MQTT `media_player` platform, so instead of one
media_player we publish a set of supported entities (switch / number / select /
button / sensor / binary_sensor) grouped under a single "SoundTouch" device.
Together they give full control from dashboards, automations and voice.

Enabled when MQTT_HOST is present in the environment (run.sh pulls the broker
credentials from the Supervisor MQTT service). A no-op otherwise, so the rest of
the add-on works fine without a broker.
"""

from __future__ import annotations

import json
import os
import re
import threading

try:
    import paho.mqtt.client as mqtt
except ImportError:  # paho not installed → bridge silently disabled
    mqtt = None

DISCOVERY_PREFIX = "homeassistant"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_") or "20"


class MqttBridge:
    """
    Publishes discovery + state and routes inbound commands to `handlers`.

    handlers is a dict of callables:
        power(on: bool), set_volume(int), set_bass(int), select_source(str),
        play_pause(), next(), previous(), mute()
    """

    def __init__(self, handlers: dict, *, device_id: str = "", device_name: str = "SoundTouch 20",
                 on_ready=None):
        self._handlers     = handlers
        self._on_ready     = on_ready   # called after each (re)connect + discovery publish
        self._uid          = "bose_soundtouch_" + _slug(device_id or "20")
        self._device_name  = device_name or "SoundTouch 20"
        self._base         = f"soundtouch/{self._uid}"
        self._avail        = f"{self._base}/availability"
        self._client       = None
        self._connected    = threading.Event()
        self._lock         = threading.Lock()
        self._presets: list[str]      = []
        self._bass_caps: dict | None  = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        host = os.environ.get("MQTT_HOST")
        if not host or mqtt is None:
            print("[mqtt] disabled (no MQTT_HOST or paho-mqtt not installed)")
            return False
        port = int(os.environ.get("MQTT_PORT") or 1883)
        user = os.environ.get("MQTT_USER") or None
        pw   = os.environ.get("MQTT_PASSWORD") or None

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._uid)
        if user:
            c.username_pw_set(user, pw)
        if (os.environ.get("MQTT_SSL") or "").lower() in ("true", "1"):
            c.tls_set()
        c.will_set(self._avail, "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        self._client = c
        try:
            c.connect(host, port, keepalive=60)
        except Exception as e:
            print(f"[mqtt] connect to {host}:{port} failed: {e}")
            return False
        c.loop_start()
        print(f"[mqtt] connecting to {host}:{port} as {self._uid}")
        return True

    def stop(self):
        if not self._client:
            return
        try:
            self._client.publish(self._avail, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    # ── configuration (presets / bass caps drive discovery) ────────────────

    def configure(self, presets: list[str] | None = None, bass_caps: dict | None = None):
        with self._lock:
            if presets is not None:
                self._presets = presets
            if bass_caps is not None:
                self._bass_caps = bass_caps
        if self._connected.is_set():
            self.publish_discovery()

    # ── discovery ──────────────────────────────────────────────────────────

    def _device_block(self) -> dict:
        return {
            "identifiers":  [self._uid],
            "name":         self._device_name,
            "manufacturer": "Bose",
            "model":        "SoundTouch 20",
        }

    def _disc(self, component: str, object_id: str, payload: dict):
        full = {
            **payload,
            "unique_id":          f"{self._uid}_{object_id}",
            "availability_topic": self._avail,
            "device":             self._device_block(),
        }
        topic = f"{DISCOVERY_PREFIX}/{component}/{self._uid}/{object_id}/config"
        self._client.publish(topic, json.dumps(full), retain=True)

    def publish_discovery(self):
        if not self._client:
            return
        b = self._base
        with self._lock:
            caps    = self._bass_caps
            presets = list(self._presets)

        self._disc("switch", "power", {
            "name":          "Power",
            "command_topic": f"{b}/power/set",
            "state_topic":   f"{b}/power/state",
            "icon":          "mdi:power",
        })
        self._disc("number", "volume", {
            "name":          "Volume",
            "command_topic": f"{b}/volume/set",
            "state_topic":   f"{b}/volume/state",
            "min": 0, "max": 100, "step": 1,
            "icon":          "mdi:volume-high",
        })
        if caps and caps.get("available"):
            self._disc("number", "bass", {
                "name":          "Bass",
                "command_topic": f"{b}/bass/set",
                "state_topic":   f"{b}/bass/state",
                "min": caps.get("min", -9), "max": caps.get("max", 9), "step": 1,
                "icon":          "mdi:speaker",
            })
        self._disc("select", "source", {
            "name":          "Source",
            "command_topic": f"{b}/source/set",
            "state_topic":   f"{b}/source/state",
            "options":       presets + ["AUX", "Bluetooth"],
            "icon":          "mdi:radio",
        })
        for oid, label, icon in [
            ("play_pause", "Play/Pause", "mdi:play-pause"),
            ("next",       "Next",       "mdi:skip-next"),
            ("previous",   "Previous",   "mdi:skip-previous"),
            ("mute",       "Mute",       "mdi:volume-mute"),
        ]:
            self._disc("button", oid, {
                "name":          label,
                "command_topic": f"{b}/{oid}/set",
                "icon":          icon,
            })
        self._disc("sensor", "now_playing", {
            "name":                  "Now Playing",
            "state_topic":           f"{b}/now_playing/state",
            "json_attributes_topic": f"{b}/now_playing/attributes",
            "icon":                  "mdi:music",
        })
        self._disc("binary_sensor", "playing", {
            "name":         "Playing",
            "state_topic":  f"{b}/playing/state",
            "device_class": "running",
        })
        print("[mqtt] discovery published")

    # ── state publishing ───────────────────────────────────────────────────

    def _pub(self, suffix: str, value: str, retain: bool = True):
        if self._client and self._connected.is_set():
            self._client.publish(f"{self._base}/{suffix}", value, retain=retain)

    def publish_now_playing(self, np: dict, source_option: str | None = None):
        status  = np.get("status", "") or ""
        src     = np.get("source", "") or ""
        playing = status in ("PLAY_STATE", "BUFFERING_STATE")

        self._pub("power/state", "OFF" if src == "STANDBY" else "ON")
        self._pub("playing/state", "ON" if playing else "OFF")

        station = np.get("station") or (np.get("content_item") or {}).get("name") or ""
        meta    = " — ".join([p for p in (np.get("artist"), np.get("track")) if p])
        if station and meta:
            text = f"{station}: {meta}"
        else:
            text = station or meta or ("Idle" if src in ("STANDBY", "") else src.title())
        self._pub("now_playing/state", text[:255])
        self._pub("now_playing/attributes", json.dumps({
            "source":    src,
            "status":    status,
            "station":   station or None,
            "artist":    np.get("artist"),
            "track":     np.get("track"),
            "album":     np.get("album"),
            "preset_id": np.get("preset_id"),
        }))
        if source_option:
            self._pub("source/state", source_option)

    def publish_volume(self, vol: dict):
        actual = vol.get("actual")
        if actual is not None:
            self._pub("volume/state", str(actual))

    def publish_bass(self, level):
        if level is not None:
            self._pub("bass/state", str(level))

    def publish_source(self, option: str):
        if option:
            self._pub("source/state", option)

    # ── command handling ───────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            print(f"[mqtt] connection refused: {reason_code}")
            return
        print("[mqtt] connected")
        self._connected.set()
        client.publish(self._avail, "online", retain=True)
        client.subscribe(f"{self._base}/+/set")
        self.publish_discovery()
        # Now that the connection is up, let the app push a fresh state snapshot
        # (publishes issued before CONNACK would otherwise be dropped).
        if self._on_ready:
            try:
                self._on_ready()
            except Exception as e:
                print(f"[mqtt] on_ready error: {e}")

    def _on_message(self, client, userdata, msg):
        try:
            suffix  = msg.topic[len(self._base) + 1:]      # e.g. "volume/set"
            key     = suffix.rsplit("/set", 1)[0]          # e.g. "volume"
            payload = msg.payload.decode("utf-8", "replace").strip()
            h = self._handlers
            if   key == "power":      h["power"](payload.upper() == "ON")
            elif key == "volume":     h["set_volume"](int(float(payload)))
            elif key == "bass":       h["set_bass"](int(float(payload)))
            elif key == "source":     h["select_source"](payload)
            elif key == "play_pause": h["play_pause"]()
            elif key == "next":       h["next"]()
            elif key == "previous":   h["previous"]()
            elif key == "mute":       h["mute"]()
            else:
                print(f"[mqtt] ignoring unknown command topic {msg.topic}")
        except Exception as e:
            print(f"[mqtt] command error on {msg.topic}: {e}")
