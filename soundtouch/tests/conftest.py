"""Put the add-on app/ dir on sys.path so tests can import server / state / mqtt_bridge."""
import os
import sys

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP)


def fake_getaddrinfo(*addrs):
    def _gai(host, port=None, *a, **k):
        return [(2, 1, 6, "", (addr, 0)) for addr in addrs]
    return _gai
