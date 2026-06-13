"""MQTT discovery payloads + command-topic routing (no broker needed)."""
import json
import mqtt_bridge


class _FakeClient:
    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload, retain=False):
        self.pubs.append((topic, payload, retain))


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def test_slug():
    assert mqtt_bridge._slug("Bose SoundTouch!! 20") == "bose_soundtouch_20"
    assert mqtt_bridge._slug("") == "20"
    assert mqtt_bridge._slug("C4F312CF3F63") == "c4f312cf3f63"


def test_uid_and_base():
    b = mqtt_bridge.MqttBridge({}, device_id="C4F312CF3F63", device_name="Living Room")
    assert b._uid == "bose_soundtouch_c4f312cf3f63"
    assert b._base == "soundtouch/bose_soundtouch_c4f312cf3f63"


def test_discovery_payloads_retained_with_options():
    b = mqtt_bridge.MqttBridge({}, device_id="ABC")
    b._presets = ["BBC Radio 4", "Jazz24"]
    b._bass_caps = {"available": True, "min": -9, "max": 9}
    b._client = _FakeClient()
    b.publish_discovery()

    topics = [t for t, _, _ in b._client.pubs]
    assert any(t.endswith("/switch/bose_soundtouch_abc/power/config") for t in topics)
    assert any(t.endswith("/number/bose_soundtouch_abc/volume/config") for t in topics)
    assert any(t.endswith("/number/bose_soundtouch_abc/bass/config") for t in topics)
    assert any(t.endswith("/binary_sensor/bose_soundtouch_abc/playing/config") for t in topics)

    sel = next(p for t, p, _ in b._client.pubs if "/select/" in t)
    assert json.loads(sel)["options"] == ["BBC Radio 4", "Jazz24", "AUX", "Bluetooth"]

    # discovery must be retained so entities survive HA restarts
    assert all(retain for _, _, retain in b._client.pubs)


def test_bass_omitted_when_unavailable():
    b = mqtt_bridge.MqttBridge({}, device_id="ABC")
    b._bass_caps = {"available": False}
    b._client = _FakeClient()
    b.publish_discovery()
    assert not any("/bass/" in t for t, _, _ in b._client.pubs)


def test_command_topic_routing():
    called = []
    handlers = {
        "power":         lambda on: called.append(("power", on)),
        "set_volume":    lambda v: called.append(("vol", v)),
        "set_bass":      lambda v: called.append(("bass", v)),
        "select_source": lambda s: called.append(("src", s)),
        "play_pause":    lambda: called.append(("pp",)),
        "next":          lambda: called.append(("next",)),
        "previous":      lambda: called.append(("prev",)),
        "mute":          lambda: called.append(("mute",)),
    }
    b = mqtt_bridge.MqttBridge(handlers, device_id="ABC")
    base = b._base
    b._on_message(None, None, _Msg(f"{base}/volume/set", "42"))
    b._on_message(None, None, _Msg(f"{base}/power/set", "ON"))
    b._on_message(None, None, _Msg(f"{base}/source/set", "Jazz24"))
    b._on_message(None, None, _Msg(f"{base}/play_pause/set", "PRESS"))
    b._on_message(None, None, _Msg(f"{base}/previous/set", "PRESS"))

    assert ("vol", 42) in called
    assert ("power", True) in called
    assert ("src", "Jazz24") in called
    assert ("pp",) in called
    assert ("prev",) in called
