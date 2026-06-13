"""_source_option_for maps now-playing payloads to MQTT 'Source' select options."""
import server


def test_source_option_mapping(monkeypatch):
    cfg = {"presets": [{"id": 1, "name": "BBC Radio 4"}, {"id": 2, "name": "Jazz24"}]}
    monkeypatch.setattr(server, "load_config", lambda: cfg)
    monkeypatch.setattr(server.st, "load", lambda: {})

    assert server._source_option_for({"source": "AUX"}) == "AUX"
    assert server._source_option_for({"source": "BLUETOOTH"}) == "Bluetooth"
    assert server._source_option_for({"source": "UPNP", "station": "Jazz24"}) == "Jazz24"
    # station not in presets → no option
    assert server._source_option_for({"source": "UPNP", "station": "Mystery FM"}) is None
    # falls back to the persisted now_playing_name
    monkeypatch.setattr(server.st, "load", lambda: {"now_playing_name": "BBC Radio 4"})
    assert server._source_option_for({"source": "UPNP"}) == "BBC Radio 4"
