#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

bashio::log.info "Starting SoundTouch add-on…"

# Config (config.json) and persistent playback state (state.json) live in /data,
# which survives restarts and updates. server.py seeds /data/config.json from
# config.default.json on first run and merges the add-on options (device_host,
# auto_discover) from /data/options.json at startup.
cd /app
exec gunicorn -c gunicorn.conf.py server:app
