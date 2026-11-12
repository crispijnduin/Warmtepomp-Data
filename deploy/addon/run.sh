#!/usr/bin/env bash
# Startscript van de add-on: leest de instellingen uit /data/options.json en
# start de capture. SUPERVISOR_TOKEN wordt door de Supervisor meegegeven.
set -euo pipefail

CONFIG=/data/options.json

# Alles via de Supervisor-proxy; daar zit de WebSocket op /core/websocket.
export WARMTEPOMP_HA_URL="http://supervisor/core"
export WARMTEPOMP_HA_WS_URL="ws://supervisor/core/websocket"
export WARMTEPOMP_HA_TOKEN="${SUPERVISOR_TOKEN:?SUPERVISOR_TOKEN ontbreekt; zet homeassistant_api: true}"

# /share blijft bestaan als de add-on wordt bijgewerkt of opnieuw gebouwd, en
# gaat mee in de Home Assistant-back-up.
DATA_DIR=/share/warmtepomp
mkdir -p "${DATA_DIR}/export"
export WARMTEPOMP_DB_PATH="${DATA_DIR}/warmtepomp.sqlite"
export WARMTEPOMP_EXPORT_DIR="${DATA_DIR}/export"

# De tijdzone van Home Assistant overnemen, zodat dagen goed worden afgebakend.
if [ -n "${TZ:-}" ]; then
    export WARMTEPOMP_TIMEZONE="${TZ}"
fi

echo "Warmtepomp Data start; database: ${WARMTEPOMP_DB_PATH}"
# options.json wordt door load_config zelf gelezen (/data/options.json staat in
# de standaard zoekpaden), dus hoeft hier niet te worden uitgepakt.
exec warmtepomp --config "${CONFIG}" run
