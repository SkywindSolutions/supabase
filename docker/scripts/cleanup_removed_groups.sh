#!/usr/bin/env bash
# =============================================================================
# cleanup_removed_groups.sh
# Removes defunct test groups from a running Grafana instance.
# =============================================================================
# Deletes Grafana users, teams, dashboards, and folders for the four removed
# sample groups: grp_boston, grp_cape_cod, grp_gloucester, grp_portland.
#
# Also deletes the corresponding forecast_data rows from the database via
# the Supabase REST API (SERVICE_ROLE_KEY required).
#
# Usage:
#   cd supabase/docker
#   bash scripts/cleanup_removed_groups.sh
#
# Requirements:
#   - .env file in supabase/docker with GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD,
#     SERVICE_ROLE_KEY, and SUPABASE_URL (or API_EXTERNAL_URL)
#   - supabase-grafana container running (accessed via internal Docker IP:3000)
#   - curl, jq, sudo docker access
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$DOCKER_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env not found at $ENV_FILE" >&2
    exit 1
fi

_get_env() { grep "^${1}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }

GRAFANA_ADMIN_USER=$(_get_env GRAFANA_ADMIN_USER)
GRAFANA_ADMIN_PASSWORD=$(_get_env GRAFANA_ADMIN_PASSWORD)
SERVICE_ROLE_KEY=$(_get_env SERVICE_ROLE_KEY)
SUPABASE_URL=$(_get_env API_EXTERNAL_URL)

: "${GRAFANA_ADMIN_USER:?GRAFANA_ADMIN_USER not set in .env}"
: "${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD not set in .env}"
: "${SERVICE_ROLE_KEY:?SERVICE_ROLE_KEY not set in .env}"
: "${SUPABASE_URL:?API_EXTERNAL_URL not set in .env}"

GRAFANA_INTERNAL_IP=$(sudo docker inspect supabase-grafana \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null | head -1)
if [[ -z "$GRAFANA_INTERNAL_IP" ]]; then
    echo "ERROR: Could not determine Grafana container IP. Is supabase-grafana running?" >&2
    exit 1
fi
GRAFANA_URL="http://${GRAFANA_INTERNAL_IP}:3000"
AUTH="${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}"

log() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

gf_api() {
    local method="$1"
    local path="$2"
    local body="${3:-}"
    local args=(-sku "$AUTH" -X "$method" -H "Content-Type: application/json"
                 --retry 2 --retry-delay 2)
    if [[ -n "$body" ]]; then
        args+=(-d "$body")
    fi
    curl "${args[@]}" "${GRAFANA_URL}/api${path}"
}

# ---------------------------------------------------------------------------
# Groups to remove
# ---------------------------------------------------------------------------
USERS_TO_DELETE=("test_boston" "test_cape_cod" "test_portland" "test_gloucester")
TEAMS_TO_DELETE=("Boston Inner Harbor" "Cape Cod Bay" "Portland ME" "Gloucester MA")
FOLDER_UIDS_TO_DELETE=("cust-grp-boston" "cust-grp-cape-cod" "cust-grp-portland" "cust-grp-gloucester")
GROUP_IDS_TO_DELETE=("grp_boston" "grp_cape_cod" "grp_portland" "grp_gloucester")

# ---------------------------------------------------------------------------
# Step 1: Delete Grafana users
# ---------------------------------------------------------------------------
log "=== Step 1: Delete Grafana users ==="
for login in "${USERS_TO_DELETE[@]}"; do
    user_id=$(gf_api GET "/users/lookup?loginOrEmail=${login}" 2>/dev/null | jq -r '.id // empty')
    if [[ -n "$user_id" ]]; then
        gf_api DELETE "/admin/users/${user_id}" > /dev/null 2>&1
        log "  Deleted user '${login}' (id=${user_id})"
    else
        log "  User '${login}' not found — skipping"
    fi
done

# ---------------------------------------------------------------------------
# Step 2: Delete Grafana teams
# ---------------------------------------------------------------------------
log "=== Step 2: Delete Grafana teams ==="
for team_name in "${TEAMS_TO_DELETE[@]}"; do
    team_id=$(gf_api GET "/teams/search?name=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$team_name")" 2>/dev/null \
        | jq -r '.teams[]? | select(.name=='"\"$team_name\""') | .id // empty' | head -1)
    if [[ -n "$team_id" ]]; then
        gf_api DELETE "/teams/${team_id}" > /dev/null 2>&1
        log "  Deleted team '${team_name}' (id=${team_id})"
    else
        log "  Team '${team_name}' not found — skipping"
    fi
done

# ---------------------------------------------------------------------------
# Step 3: Delete Grafana folders (and all dashboards inside them)
# ---------------------------------------------------------------------------
log "=== Step 3: Delete Grafana folders ==="
for folder_uid in "${FOLDER_UIDS_TO_DELETE[@]}"; do
    http_code=$(gf_api GET "/folders/${folder_uid}" 2>/dev/null | jq -r '.uid // empty')
    if [[ -n "$http_code" && "$http_code" != "null" ]]; then
        gf_api DELETE "/folders/${folder_uid}?forceDeleteRules=true" > /dev/null 2>&1
        log "  Deleted folder '${folder_uid}'"
    else
        log "  Folder '${folder_uid}' not found — skipping"
    fi
done

# ---------------------------------------------------------------------------
# Step 4: Delete forecast_data rows from database
# ---------------------------------------------------------------------------
log "=== Step 4: Delete forecast_data rows from database ==="
for group_id in "${GROUP_IDS_TO_DELETE[@]}"; do
    response=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
        "${SUPABASE_URL}/rest/v1/forecast_data?group_id=eq.${group_id}" \
        -H "apikey: ${SERVICE_ROLE_KEY}" \
        -H "Authorization: Bearer ${SERVICE_ROLE_KEY}")
    if [[ "$response" =~ ^2 ]]; then
        log "  Deleted forecast_data rows for '${group_id}'"
    else
        log "  WARNING: DELETE for '${group_id}' returned HTTP ${response}"
    fi
done

log ""
log "=== Cleanup complete ==="
log "Removed users: ${USERS_TO_DELETE[*]}"
log "Removed teams: ${TEAMS_TO_DELETE[*]}"
log "Removed folders: ${FOLDER_UIDS_TO_DELETE[*]}"
log "Removed DB data for groups: ${GROUP_IDS_TO_DELETE[*]}"
log ""
log "Reload Grafana provisioning to finalize:"
log "  docker exec supabase-grafana wget -qO- --post-data '' http://localhost:3000/api/admin/provisioning/dashboards/reload"
