#!/usr/bin/with-contenv bashio

export SOLID_LOG_LEVEL="$(bashio::config 'log_level')"

exec python3 /app/app.py
