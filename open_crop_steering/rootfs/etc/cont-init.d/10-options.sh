#!/usr/bin/with-contenv bashio
# =============================================================================
# cont-init.d/10-options — one-shot startup, runs BEFORE any s6 service.
#
# Responsibilities:
#   1. Read the add-on options (config.yaml -> /data/options.json) via
#      bashio and write them to /run/ocs.env, which every service's `run`
#      script sources. This is the bridge from HA add-on options to the
#      env vars backend/app/config.py reads.
#   2. Generate the audit-chain HMAC key on first boot if the operator
#      left hmac_key_current blank — a fresh 32-byte hex key persisted to
#      /data/hmac_key so it is stable across restarts (rotating it would
#      break verification of historical audit rows).
#   3. Ensure the /data sub-directories the workers expect exist.
#
# In standalone mode (no Supervisor) this script does not run — the
# compose file supplies env vars directly. The image is mode-agnostic.
# =============================================================================
set -e

ENV_FILE=/run/ocs.env
DATA_DIR=/data
HMAC_KEY_FILE="${DATA_DIR}/hmac_key"

bashio::log.info "Open Crop Steering: preparing add-on configuration."

# --- /data layout ----------------------------------------------------------
# postgres        — the bundled Postgres 16 cluster data directory
# postgres-backups — pg_dump cold backups (worker-seal + pre-backup hook)
# audit-seal-exports — nightly sealed audit exports
# influx-cache    — cached InfluxDB schema metadata
mkdir -p \
  "${DATA_DIR}/postgres" \
  "${DATA_DIR}/postgres-backups" \
  "${DATA_DIR}/audit-seal-exports" \
  "${DATA_DIR}/influx-cache"

# --- HMAC key bootstrap ----------------------------------------------------
# Use the operator-supplied key if set; otherwise generate + persist one.
HMAC_KEY="$(bashio::config 'hmac_key_current')"
if bashio::var.is_empty "${HMAC_KEY}"; then
  if [ -f "${HMAC_KEY_FILE}" ]; then
    HMAC_KEY="$(cat "${HMAC_KEY_FILE}")"
    bashio::log.info "HMAC key loaded from ${HMAC_KEY_FILE}."
  else
    HMAC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    printf '%s' "${HMAC_KEY}" > "${HMAC_KEY_FILE}"
    chmod 600 "${HMAC_KEY_FILE}"
    bashio::log.info "Generated a new audit-chain HMAC key (first boot)."
  fi
fi
HMAC_KEY_ID="$(bashio::config 'hmac_key_id_current')"
bashio::var.is_empty "${HMAC_KEY_ID}" && HMAC_KEY_ID=1

# --- write the env file every service sources ------------------------------
# DATABASE_URL points at the local bundled cluster on the unix socket /
# loopback; the postgres service creates the ocs role + database.
{
  echo "LOG_LEVEL=$(bashio::config 'log_level')"
  echo "LLM_BASE_URL=$(bashio::config 'llm_base_url')"
  echo "LLM_API_KEY=$(bashio::config 'llm_api_key')"
  echo "LLM_MODEL=$(bashio::config 'llm_model')"
  echo "INFLUX_URL=$(bashio::config 'influx_url')"
  echo "INFLUX_TOKEN=$(bashio::config 'influx_token')"
  echo "INFLUX_ORG=$(bashio::config 'influx_org')"
  echo "INFLUX_BUCKET=$(bashio::config 'influx_bucket')"
  echo "TELEGRAM_BOT_TOKEN=$(bashio::config 'telegram_bot_token')"
  echo "TELEGRAM_CHAT_ID=$(bashio::config 'telegram_chat_id')"
  echo "SEAL_EXPORT_TARGET=$(bashio::config 'seal_export_target')"
  echo "HMAC_KEY_${HMAC_KEY_ID}=${HMAC_KEY}"
  echo "HMAC_KEY_ID_CURRENT=${HMAC_KEY_ID}"
  echo "DATABASE_URL=postgresql+asyncpg://ocs:ocs@127.0.0.1:5432/open_crop_steering"
  echo "OCS_FRONTEND_DIR=/opt/frontend"
  # PGDATA is read by the postgres service's run script.
  echo "PGDATA=${DATA_DIR}/postgres"
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

bashio::log.info "Add-on configuration written to ${ENV_FILE}."
