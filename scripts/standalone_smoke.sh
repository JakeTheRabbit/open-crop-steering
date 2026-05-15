#!/usr/bin/env bash
# =============================================================================
# standalone_smoke.sh — Phase 12 standalone-mode smoke test.
#
# Brings up the standalone compose stack, waits for the API to report
# healthy, exercises /healthz + /readyz, then tears the stack down.
#
# This proves the SAME image runs outside Home Assistant: mode detection
# (no SUPERVISOR_TOKEN -> standalone), the `migrate` one-shot, `serve`,
# and the worker services all start from one image driven by env vars.
#
# NOT a CI gate by default
# ------------------------
# This needs a Docker daemon and builds the full multi-stage image, so it
# is too heavy for the per-PR unit/integration job. Run it manually, or
# wire it into a dedicated (slower) docker-build CI job. It does NOT need
# a real Home Assistant: /healthz and /readyz only touch the process and
# Postgres — the HA_URL in the compose file can stay a placeholder for
# the smoke test (the worker loops will log HA-connection errors, which
# is expected and fine for a smoke check).
#
# Usage:
#   scripts/standalone_smoke.sh
#
# Exit code 0 = the API came up healthy and /readyz reported ready.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.standalone.yml"
API_URL="http://127.0.0.1:8099"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"   # seconds to wait for /healthz

# Pick `docker compose` (v2) or `docker-compose` (v1).
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 2
fi

compose() { ${DC} -f "${COMPOSE_FILE}" "$@"; }

cleanup() {
  echo "--- tearing down the standalone stack ---"
  compose down --volumes --remove-orphans || true
}
trap cleanup EXIT

echo "=== Open Crop Steering — standalone smoke test ==="
echo "compose file: ${COMPOSE_FILE}"

echo "--- building + starting the standalone stack ---"
# --build so the image reflects the current tree; -d to background it.
compose up -d --build

echo "--- waiting up to ${HEALTH_TIMEOUT}s for ${API_URL}/healthz ---"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
until curl -fsS "${API_URL}/healthz" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    echo "ERROR: API did not become healthy within ${HEALTH_TIMEOUT}s." >&2
    echo "--- ocs-api logs ---" >&2
    compose logs ocs-api >&2 || true
    exit 1
  fi
  sleep 3
done
echo "OK: /healthz responded."

echo "--- GET ${API_URL}/healthz ---"
curl -fsS "${API_URL}/healthz"
echo

echo "--- GET ${API_URL}/readyz ---"
# /readyz touches Postgres — it must report ready for the smoke to pass.
readyz_body="$(curl -fsS "${API_URL}/readyz")"
echo "${readyz_body}"
if ! echo "${readyz_body}" | grep -q '"status":[[:space:]]*"ready"'; then
  echo "ERROR: /readyz did not report ready." >&2
  exit 1
fi
echo "OK: /readyz reported ready."

echo "=== standalone smoke test PASSED ==="
# cleanup runs via the EXIT trap.
