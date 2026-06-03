#!/usr/bin/env bash
# ============================================================================
# deploy.sh  --  One-command deployment for the Deep Agents reference app
# ============================================================================
#
# Single-domain reference: this script deploys ONE backend bound to
# ``MONGODB_DB`` as a local Docker stack (backend + frontend).
#
# Usage:
#   scripts/deploy.sh                Deploy the reference stack (Docker)
#   scripts/deploy.sh --status       Show running containers
#   scripts/deploy.sh --down         Tear down the Docker stack
#
# Environment overrides:
#   COMPOSE            compose CLI (default: "docker compose")
#   DEEP_AGENT_PORT    host port for backend (default: 8010)
#   FRONTEND_PORT      host port for frontend (default: 3000)
#   HEALTH_URL         override health probe URL
#   TIMEOUT            health-wait timeout in seconds (default: 180)
#   MONGODB_DB         target database (default: deep_agents)
#   NO_BUILD           set to 1 to skip image rebuild
#   NO_INDEXES         set to 1 to skip Atlas index provisioning
#   NO_SEED            set to 1 to skip seeding
#   NO_FRONTEND        set to 1 to skip the frontend container
#
set -euo pipefail

# ---------- colour helpers ----------
red()    { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }
green()  { printf '\033[0;32m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*" >&2; }
bold()   { printf '\033[1m%s\033[0m\n' "$*" >&2; }

# ---------- resolve repo root (works from any cwd) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---------- pull values from .env if present (so the file is the source of truth) ----------
# Precedence: shell env > .env file value > script default. We extract a few
# specific keys instead of ``set -a; source .env`` because .env may contain
# values with shell-special characters that ``source`` would interpret.
_env_get() {
  local key="$1"
  if [[ -f ".env" ]]; then
    awk -F= -v k="$key" '
      $0 ~ "^[[:space:]]*#" { next }
      $1 == k { sub(/^[^=]*=/, "", $0); print; exit }
    ' .env
  fi
}

# ---------- shared defaults ----------
COMPOSE="${COMPOSE:-docker compose}"
# Host port mappings. Precedence (mirrors DB_NAME below): shell env > .env
# file value > script default. Reading .env here keeps the summary the script
# prints in sync with the port docker compose actually binds.
DEEP_AGENT_PORT="${DEEP_AGENT_PORT:-$(_env_get DEEP_AGENT_PORT)}"
DEEP_AGENT_PORT="${DEEP_AGENT_PORT:-8010}"
FRONTEND_PORT="${FRONTEND_PORT:-$(_env_get FRONTEND_PORT)}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
HEALTH_URL="${HEALTH_URL:-http://localhost:${DEEP_AGENT_PORT}/health}"
TIMEOUT="${TIMEOUT:-180}"
DB_NAME="${MONGODB_DB:-$(_env_get MONGODB_DB)}"
DB_NAME="${DB_NAME:-deep_agents}"
# Re-export so ``docker compose`` interpolation (and the seed step) see
# the same value the script computed. Without this, compose's
# ${MONGODB_DB:-deep_agents} default fires when the shell doesn't have
# the variable, even though .env carries one.
export MONGODB_DB="$DB_NAME"
# Same reasoning for the port mappings: export the resolved values so compose's
# ${DEEP_AGENT_PORT:-8010} / ${FRONTEND_PORT:-3000} interpolation matches.
export DEEP_AGENT_PORT FRONTEND_PORT

# ---------- usage ----------
usage() {
  cat >&2 <<EOF
$(bold "Deep Agents Deploy Script (single-domain reference)")

Usage:
  $(basename "$0")               Deploy the reference stack (Docker)
  $(basename "$0") --status      Show running services
  $(basename "$0") --down        Tear down the Docker stack

Environment overrides:
  DEEP_AGENT_PORT=$DEEP_AGENT_PORT  FRONTEND_PORT=$FRONTEND_PORT  TIMEOUT=$TIMEOUT
  MONGODB_DB=$DB_NAME  NO_BUILD=0  NO_INDEXES=0  NO_SEED=0  NO_FRONTEND=0
EOF
  exit 2
}

# ---------- preflight checks ----------
preflight() {
  local ok=1

  # .env file
  if [[ ! -f ".env" ]]; then
    red "ERROR: .env file not found."
    echo "  Copy the example and fill in your credentials:"
    echo "    cp .env.example .env && \$EDITOR .env"
    ok=0
  else
    # Check for required keys (must have a non-placeholder value)
    for key in MONGODB_URI VOYAGE_API_KEY; do
      val=$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2-)
      if [[ -z "$val" || "$val" == *"..."* || "$val" == *"USER:PASS"* ]]; then
        red "ERROR: $key in .env is missing or still has a placeholder value."
        ok=0
      fi
    done
    # VFS_BACKEND is S3-only. Warn if not set.
    bucket=$(grep -E '^VFS_S3_BUCKET=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
    if [[ -z "$bucket" ]]; then
      red "ERROR: VFS_S3_BUCKET is required (the VFS backend is S3-only)."
      ok=0
    fi
  fi

  # Docker / compose
  if ! command -v docker &>/dev/null; then
    red "ERROR: docker is not installed or not in PATH."
    ok=0
  elif ! $COMPOSE version &>/dev/null 2>&1; then
    red "ERROR: '$COMPOSE' is not available."
    echo "  Install Docker Compose v2 or set COMPOSE to your compose binary."
    ok=0
  fi

  # curl (for health checks)
  if ! command -v curl &>/dev/null; then
    red "ERROR: curl is not installed (needed for health checks)."
    ok=0
  fi

  [[ "$ok" -eq 1 ]] || { echo; red "Preflight checks failed. Fix the above and re-run."; exit 1; }
}

# ---------- build images ----------
build_images() {
  if [[ "${NO_BUILD:-0}" == "1" ]]; then
    yellow "==> skipping image build (NO_BUILD=1)"
    return 0
  fi
  bold "==> building Docker images"
  $COMPOSE build
}

# ---------- wait for health ----------
wait_for_health() {
  local url="$1" timeout_s="$2"
  bold "==> waiting for $url (up to ${timeout_s}s)"
  local deadline=$(( $(date +%s) + timeout_s ))
  while (( $(date +%s) < deadline )); do
    if curl --silent --fail --max-time 3 "$url" >/dev/null 2>&1; then
      echo
      green "==> health check passed"
      return 0
    fi
    printf "."
    sleep 2
  done
  echo

  red "ERROR: service did not become healthy within ${timeout_s}s"
  echo
  yellow "--- Last 30 lines of backend logs ---"
  $COMPOSE logs --tail=30 deep_agent 2>&1 || true
  echo
  yellow "--- Health endpoint response ---"
  curl --silent --max-time 5 "$url" 2>&1 || echo "(no response)"
  echo
  yellow "--- Troubleshooting ---"
  echo "  1. Check .env has valid MONGODB_URI (can the container reach Atlas?)"
  echo "  2. Check Docker network: docker network inspect $(basename "$REPO_ROOT")_default"
  echo "  3. Check container status: $COMPOSE ps"
  echo "  4. Full logs: $COMPOSE logs deep_agent"
  return 1
}

# ---------- provision Atlas indexes ----------
provision_indexes() {
  if [[ "${NO_INDEXES:-0}" == "1" ]]; then
    yellow "==> skipping index provisioning (NO_INDEXES=1)"
    return 0
  fi
  bold "==> provisioning Atlas indexes for database '$DB_NAME'"
  # Idempotent admin DDL, run once before seeding so the KB vector/search
  # indexes exist for the data the seed step writes. ensure_indexes() creates
  # indexes whenever it is called directly, so no PROVISION_INDEXES_ON_BOOT is
  # needed here — that flag only gates the automatic call inside the server
  # lifespan (kept off so request-serving boots don't attempt DDL under the
  # locked-down runtime role).
  if ! $COMPOSE exec -T \
    -e MONGODB_DB="$DB_NAME" \
    deep_agent python -c "from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()" 2>&1; then
    red "ERROR: index provisioning failed"
    yellow "  Check logs: $COMPOSE logs --tail=50 deep_agent"
    return 1
  fi
  green "==> provisioned Atlas indexes"
}

# ---------- seed the reference ----------
seed_reference() {
  if [[ "${NO_SEED:-0}" == "1" ]]; then
    yellow "==> skipping seed (NO_SEED=1)"
    return 0
  fi
  bold "==> seeding reference into database '$DB_NAME'"
  if ! $COMPOSE exec -T \
    -e MONGODB_DB="$DB_NAME" \
    deep_agent deep-agent seed 2>&1; then
    red "ERROR: seeding failed"
    yellow "  Check logs: $COMPOSE logs --tail=50 deep_agent"
    return 1
  fi
  green "==> seeded reference"
}

# ---------- verify seed completeness ----------
verify_seed() {
  if [[ "${NO_SEED:-0}" == "1" ]]; then
    yellow "==> skipping seed verification (NO_SEED=1)"
    return 0
  fi
  bold "==> verifying seed completeness for database '$DB_NAME'"
  # Defense-in-depth alongside the seeder's own read-back (SeedIncompleteError):
  # a transient Atlas failover can interrupt the seed and still exit 0, leaving
  # a partial catalog. Independently compare live collection counts against the
  # committed fixture row counts; ``>=`` so extra/pre-existing rows never trip it.
  if ! $COMPOSE exec -T -e MONGODB_DB="$DB_NAME" deep_agent python - <<'PY'
import json, sys
from pathlib import Path
from deep_agent.config import get_settings
from deep_agent.persistence.mongo import get_db

s = get_settings()
db = get_db()
seeds = Path(s.seeds_dir)
rows = lambda p: json.load(p.open()) if p.exists() else []
checks = []
op_dir = seeds / "operational"
if op_dir.exists():
    for fp in sorted(op_dir.glob("*.json")):
        checks.append((fp.stem, len(rows(fp))))
for fp, coll in [
    (seeds / "knowledge_base.json", s.knowledge_base_collection),
    (seeds / "knowledge_graph.entities.json", s.knowledge_graph_collection),
]:
    if fp.exists():
        checks.append((coll, len(rows(fp))))

bad = []
for coll, expected in checks:
    got = db[coll].estimated_document_count()
    ok = got >= expected
    print(f"  {'OK   ' if ok else 'SHORT'} {coll:28} {got}/{expected}")
    if not ok:
        bad.append(coll)
sys.exit(1 if bad else 0)
PY
  then
    red "ERROR: seed verification failed — a collection has fewer docs than its fixture"
    yellow "  Likely a transient Atlas failover mid-seed. Re-run the (idempotent) seed:"
    yellow "    $COMPOSE exec -e MONGODB_DB=$DB_NAME deep_agent deep-agent seed"
    return 1
  fi
  green "==> seed verified"
}

# ---------- print summary ----------
print_summary() {
  echo
  bold "============================================"
  green "  Deployment complete (single-domain reference)"
  bold "============================================"
  echo
  echo "  Backend:    http://localhost:${DEEP_AGENT_PORT}"
  echo "  Frontend:   http://localhost:${FRONTEND_PORT}"
  echo "  Liveness:   http://localhost:${DEEP_AGENT_PORT}/live"
  echo "  Readiness:  http://localhost:${DEEP_AGENT_PORT}/ready"
  echo "  Health:     ${HEALTH_URL}"
  echo "  Chat API:   http://localhost:${DEEP_AGENT_PORT}/chat"
  echo "  Plans:      http://localhost:${DEEP_AGENT_PORT}/plans?user_id=demo-user&thread_id=<thread>"
  echo "  Messages:   http://localhost:${DEEP_AGENT_PORT}/messages?user_id=demo-user&thread_id=<thread>"
  echo
  echo "  Database:   ${DB_NAME}"
  echo
  echo "  Quick test:"
  echo "    curl -N -X POST http://localhost:${DEEP_AGENT_PORT}/chat \\"
  echo "      -H 'Content-Type: application/json' \\"
  echo "      -d '{\"user_id\":\"demo\",\"message\":\"hello\"}'"
  echo
}

# ---------- cleanup on failure ----------
_deploy_ok=0
cleanup() {
  if [[ "$_deploy_ok" -ne 1 ]]; then
    echo
    red "==> deployment failed; tearing down compose stack"
    $COMPOSE down >/dev/null 2>&1 || true
  fi
}

# ==========================================================================
#  DOCKER: deploy the reference
# ==========================================================================
deploy_default() {
  export MONGODB_DB="$DB_NAME"

  trap cleanup EXIT
  preflight
  build_images

  bold "==> starting compose stack (MONGODB_DB=$MONGODB_DB)"
  if [[ "${NO_FRONTEND:-0}" == "1" ]]; then
    $COMPOSE up -d deep_agent
  else
    $COMPOSE up -d
  fi

  wait_for_health "$HEALTH_URL" "$TIMEOUT" || exit 1
  provision_indexes || exit 1
  seed_reference || exit 1
  verify_seed || exit 1

  _deploy_ok=1
  print_summary
}

# ==========================================================================
#  STATUS: show running services
# ==========================================================================
show_status() {
  bold "==> Docker Compose status"
  $COMPOSE ps 2>/dev/null || yellow "(no compose stack running)"
}

# ==========================================================================
#  TEARDOWN
# ==========================================================================
teardown() {
  bold "==> tearing down compose stack"
  $COMPOSE down --remove-orphans
  green "==> stack removed"
}

# ==========================================================================
#  MAIN: parse arguments and dispatch
# ==========================================================================
case "${1:-}" in
  --status|-s)  show_status ;;
  --down|-d)    teardown ;;
  --help|-h)    usage ;;
  --*)
    red "ERROR: unknown flag '$1'"
    usage
    ;;
  "")           deploy_default ;;
  *)
    yellow "WARNING: positional arg '$1' is ignored"
    deploy_default
    ;;
esac
