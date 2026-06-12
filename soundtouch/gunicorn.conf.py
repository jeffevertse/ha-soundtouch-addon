"""
Gunicorn configuration for the SoundTouch Home Assistant add-on.

Worker model
------------
- workers=1    : a single worker keeps the in-memory device/WebSocket singleton
                 authoritative (and is plenty for a household)
- worker_class : gthread — thread-based so SSE and the audio proxy (both
                 long-lived connections) don't block the API endpoints
- threads=8    : 6 preset audio proxies + 1 SSE stream + API headroom
- timeout=120  : well above the 25 s SSE keepalive; gunicorn only kills workers
                 that are *silent* this long, so active streams are never cut
"""

bind             = "0.0.0.0:5000"
workers          = 1
worker_class     = "gthread"
threads          = 8
timeout          = 120
graceful_timeout = 30

# Log to stdout/stderr so the Supervisor add-on log picks everything up.
accesslog      = "-"
errorlog       = "-"
loglevel       = "info"
capture_output = True


def post_worker_init(worker):
    """
    Start background tasks inside the worker once it is fully initialised.
    Daemon threads must be started here — they are NOT inherited across the
    fork() gunicorn uses to create worker processes.
    """
    import server
    port = int(bind.split(":")[-1])
    server._startup(port)
