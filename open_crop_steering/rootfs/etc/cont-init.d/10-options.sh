#!/usr/bin/with-contenv bashio
# =============================================================================
# cont-init.d/10-options — one-shot startup, runs BEFORE any s6 service.
#
# The SAME image runs both ways (plan locked decision #1), and this script
# auto-detects which:
#
#   * ADD-ON mode  — the HA Supervisor injected SUPERVISOR_TOKEN. Add-on
#     options are read from the Supervisor API via bashio::config.
#   * STANDALONE   — no Supervisor. Config comes from the container
#     environment (`docker run -e ...` / docker-compose). bashio::config
#     is NEVER called, so there is no Supervisor API to hang on.
#
# Either way it writes /run/ocs.env — the single env file every s6
# service `run` script sources — so the services are mode-agnostic.
#
# Responsibilities (both modes):
#   1. Ensure the /data sub-directories the workers expect exist.
#   2. Bootstrap the audit-chain HMAC key (operator-supplied > /data
#      file > freshly generated + persisted).
#   3. Materialise /run/ocs.env.
# =============================================================================
set -e

ENV_FILE=/run/ocs.env
DATA_DIR=/data
HMAC_KEY_FILE="${DATA_DIR}/hmac_key"
DEFAULT_DB_URL="postgresql+asyncpg://ocs:ocs@127.0.0.1:5432/open_crop_steering"

# --- mode detection --------------------------------------------------------
# Add-on iff the Supervisor handed us its token (same rule as
# backend/app/config.py). Standalone otherwise.
if bashio::var.has_value "${SUPERVISOR_TOKEN:-}"; then
  MODE="addon"
else
  MODE="standalone"
fi
bashio::log.info "Open Crop Steering: preparing configuration (${MODE} mode)."

# --- /data layout ----------------------------------------------------------
# postgres           — the bundled Postgres 16 cluster data directory
# postgres-backups   — pg_dump cold backups (worker-seal + pre-backup hook)
# audit-seal-exports — nightly sealed audit exports
# influx-cache       — cached InfluxDB schema metadata
mkdir -p \
  "${DATA_DIR}/postgres" \
  "${DATA_DIR}/postgres-backups" \
  "${DATA_DIR}/audit-seal-exports" \
  "${DATA_DIR}/influx-cache"

# --- HMAC key bootstrap ----------------------------------------------------
# Precedence: add-on option / env var > /data/hmac_key file > generate one.
# Generating + persisting keeps the key stable across restarts — rotating
# it would break verification of historical audit rows.
HMAC_KEY_ID="${HMAC_KEY_ID_CURRENT:-1}"
HMAC_KEY="${HMAC_KEY_CURRENT:-}"
if [ "${MODE}" = "addon" ]; then
  _opt_key="$(bashio::config 'hmac_key_current')"
  bashio::var.has_value "${_opt_key}" && HMAC_KEY="${_opt_key}"
  _opt_id="$(bashio::config 'hmac_key_id_current')"
  bashio::var.has_value "${_opt_id}" && HMAC_KEY_ID="${_opt_id}"
fi
if [ -z "${HMAC_KEY}" ]; then
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

# --- gather settings -------------------------------------------------------
# Add-on mode reads the Supervisor options; standalone reads the container
# environment. The bundled Postgres cluster backs the add-on; standalone
# may point DATABASE_URL at an external database (and leaves the bundled
# cluster idle) or fall back to the bundled default.
if [ "${MODE}" = "addon" ]; then
  OCS_LOG_LEVEL="$(bashio::config 'log_level')"
  OCS_LLM_BASE_URL="$(bashio::config 'llm_base_url')"
  OCS_LLM_API_KEY="$(bashio::config 'llm_api_key')"
  OCS_LLM_MODEL="$(bashio::config 'llm_model')"
  OCS_INFLUX_URL="$(bashio::config 'influx_url')"
  OCS_INFLUX_TOKEN="$(bashio::config 'influx_token')"
  OCS_INFLUX_ORG="$(bashio::config 'influx_org')"
  OCS_INFLUX_BUCKET="$(bashio::config 'influx_bucket')"
  OCS_TELEGRAM_BOT_TOKEN="$(bashio::config 'telegram_bot_token')"
  OCS_TELEGRAM_CHAT_ID="$(bashio::config 'telegram_chat_id')"
  OCS_SEAL_EXPORT_TARGET="$(bashio::config 'seal_export_target')"
  OCS_DATABASE_URL="${DEFAULT_DB_URL}"
  OCS_HA_URL=""
  OCS_HA_TOKEN=""
else
  OCS_LOG_LEVEL="${LOG_LEVEL:-info}"
  OCS_LLM_BASE_URL="${LLM_BASE_URL:-}"
  OCS_LLM_API_KEY="${LLM_API_KEY:-}"
  OCS_LLM_MODEL="${LLM_MODEL:-claude-opus-4-7}"
  OCS_INFLUX_URL="${INFLUX_URL:-}"
  OCS_INFLUX_TOKEN="${INFLUX_TOKEN:-}"
  OCS_INFLUX_ORG="${INFLUX_ORG:-}"
  OCS_INFLUX_BUCKET="${INFLUX_BUCKET:-homeassistant}"
  OCS_TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  OCS_TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
  OCS_SEAL_EXPORT_TARGET="${SEAL_EXPORT_TARGET:-}"
  OCS_DATABASE_URL="${DATABASE_URL:-${DEFAULT_DB_URL}}"
  OCS_HA_URL="${HA_URL:-}"
  OCS_HA_TOKEN="${HA_TOKEN:-}"
fi
[ -z "${OCS_LOG_LEVEL}" ] && OCS_LOG_LEVEL="info"

# --- write the env file every service sources ------------------------------
{
  echo "LOG_LEVEL=${OCS_LOG_LEVEL}"
  echo "LLM_BASE_URL=${OCS_LLM_BASE_URL}"
  echo "LLM_API_KEY=${OCS_LLM_API_KEY}"
  echo "LLM_MODEL=${OCS_LLM_MODEL}"
  echo "INFLUX_URL=${OCS_INFLUX_URL}"
  echo "INFLUX_TOKEN=${OCS_INFLUX_TOKEN}"
  echo "INFLUX_ORG=${OCS_INFLUX_ORG}"
  echo "INFLUX_BUCKET=${OCS_INFLUX_BUCKET}"
  echo "TELEGRAM_BOT_TOKEN=${OCS_TELEGRAM_BOT_TOKEN}"
  echo "TELEGRAM_CHAT_ID=${OCS_TELEGRAM_CHAT_ID}"
  echo "SEAL_EXPORT_TARGET=${OCS_SEAL_EXPORT_TARGET}"
  echo "HMAC_KEY_${HMAC_KEY_ID}=${HMAC_KEY}"
  echo "HMAC_KEY_ID_CURRENT=${HMAC_KEY_ID}"
  echo "DATABASE_URL=${OCS_DATABASE_URL}"
  echo "OCS_FRONTEND_DIR=/opt/frontend"
  # PGDATA is read by the postgres service's run script.
  echo "PGDATA=${DATA_DIR}/postgres"
  # Standalone passes HA credentials through; in add-on mode config.py
  # derives them from SUPERVISOR_TOKEN, so these stay blank.
  echo "HA_URL=${OCS_HA_URL}"
  echo "HA_TOKEN=${OCS_HA_TOKEN}"
  # Standalone-only dev auth escape hatch (blank unless the operator
  # set OCS_DEV_AUTH); add-on mode never consults it.
  echo "OCS_DEV_AUTH=${OCS_DEV_AUTH:-}"
  # Observe-only / shadow mode — when set the executor never runs.
  echo "OCS_OBSERVE_ONLY=${OCS_OBSERVE_ONLY:-}"
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

bashio::log.info "Configuration written to ${ENV_FILE}."
