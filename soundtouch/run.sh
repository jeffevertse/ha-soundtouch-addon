#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

bashio::log.info "Starting SoundTouch add-on…"

# Optional MQTT: when enabled and the Home Assistant MQTT service is available,
# pull the broker credentials and pass them to the app as environment variables.
# server.py publishes MQTT-discovery entities only when MQTT_HOST is set.
if bashio::config.true 'mqtt_enabled'; then
  if bashio::services.available "mqtt"; then
    MQTT_HOST="$(bashio::services mqtt 'host')"
    MQTT_PORT="$(bashio::services mqtt 'port')"
    MQTT_USER="$(bashio::services mqtt 'username')"
    MQTT_PASSWORD="$(bashio::services mqtt 'password')"
    MQTT_SSL="$(bashio::services mqtt 'ssl')"
    export MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASSWORD MQTT_SSL
    bashio::log.info "MQTT enabled — broker ${MQTT_HOST}:${MQTT_PORT}"
  else
    bashio::log.warning "mqtt_enabled is on but no MQTT service is available."
    bashio::log.warning "Install the Mosquitto broker add-on and add the MQTT integration. MQTT entities are disabled for now."
  fi
else
  bashio::log.info "MQTT disabled via the mqtt_enabled option."
fi

# Config (config.json) and persistent playback state (state.json) live in /data,
# which survives restarts and updates. server.py seeds /data/config.json from
# config.default.json on first run and merges the add-on options (device_host,
# auto_discover) from /data/options.json at startup.
cd /app
exec gunicorn -c gunicorn.conf.py server:app
