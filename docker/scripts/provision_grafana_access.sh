#!/usr/bin/env bash
# =============================================================================
# provision_grafana_access.sh
# Task: P1.2 — Configure Grafana Access Control & Authentication
# Created: 2026-03-17
# =============================================================================
# Creates Grafana teams, folder permissions, and test user accounts via the
# Grafana HTTP API.
#
# What this script does:
#   1. Creates two org-level teams:
#        - Internal  → can see all folders (Customer Dashboards + Internal
#                       Dashboards + all per-group folders)
#        - Customers → can only see their own per-group folder (no access to
#                       Customer Dashboards, Internal Dashboards, or other
#                       groups' folders)
#   2. Grants folder permissions using the Grafana access-control API:
#        - Internal  team → Editor on both base folders + all group folders
#        - Per-group teams → Viewer on their own folder only
#   3. Creates one test user per group:
#        - test_corpuschristi (password in .env as TEST_PW_CORPUSCHRISTI)
#        - corpus_christi    (password in .env as PW_CORPUS_CHRISTI)
#        - test_chatham      (password in .env as TEST_PW_CHATHAM)
#        - chatham           (password in .env as PW_CHATHAM)
#        - test_portrichey  (password in .env as TEST_PW_PORTRICHEY)
#        - test_clearwater   (password in .env as TEST_PW_CLEARWATER)
#        - test_internal    (password in .env as TEST_PW_INTERNAL)
#   4. Adds each user to their appropriate team.
#   5. Sets each user's Grafana home dashboard preference so they land on
#      their group's primary dashboard after login (combined wind+visibility >
#      visibility > wind > tide priority for customer accounts;
#      internal-visibility for the internal account). Uses the user's own credentials — Grafana OSS does not
#      expose an admin API for setting another user's preferences.
#
# Authentication method: Grafana local accounts (no external IdP required).
# OAuth via Supabase can be layered on later without changing folder/team
# structure — set GF_AUTH_GENERIC_OAUTH_* env vars in docker-compose and
# re-run this script to re-assign any existing accounts to teams.
#
# Usage:
#   cd supabase/docker
#   bash scripts/provision_grafana_access.sh
#
# Requirements:
#   - .env file in supabase/docker with GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD
#   - supabase-grafana container running (accessed via internal Docker IP:3000)
#   - curl, jq, sudo docker access
#
# Note: connects to Grafana via internal Docker network (http://CONTAINER_IP:3000)
# rather than the external HTTPS URL to avoid curl hanging on the self-signed cert.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$DOCKER_DIR/.env"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env not found at $ENV_FILE" >&2
    exit 1
fi

# Read specific variables safely from .env (handles values with spaces/special chars)
# Returns empty string (not exit 1) when the key is absent.
_get_env() { grep "^${1}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }

GRAFANA_ADMIN_USER=$(_get_env GRAFANA_ADMIN_USER)
GRAFANA_ADMIN_PASSWORD=$(_get_env GRAFANA_ADMIN_PASSWORD)
TEST_PW_INTERNAL=${TEST_PW_INTERNAL:-$(_get_env TEST_PW_INTERNAL)}
TEST_PW_PORTRICHEY=${TEST_PW_PORTRICHEY:-$(_get_env TEST_PW_PORTRICHEY)}
TEST_PW_CLEARWATER=${TEST_PW_CLEARWATER:-$(_get_env TEST_PW_CLEARWATER)}
TEST_PW_CORPUSCHRISTI=${TEST_PW_CORPUSCHRISTI:-$(_get_env TEST_PW_CORPUSCHRISTI)}
PW_CORPUS_CHRISTI=${PW_CORPUS_CHRISTI:-$(_get_env PW_CORPUS_CHRISTI)}
TEST_PW_CHATHAM=${TEST_PW_CHATHAM:-$(_get_env TEST_PW_CHATHAM)}
PW_CHATHAM=${PW_CHATHAM:-$(_get_env PW_CHATHAM)}
PW_SENDERO=${PW_SENDERO:-$(_get_env PW_SENDERO)}
TEST_PW_SENDERO=${TEST_PW_SENDERO:-$(_get_env TEST_PW_SENDERO)}
PW_MSC=${PW_MSC:-$(_get_env PW_MSC)}
TEST_PW_MSC=${TEST_PW_MSC:-$(_get_env TEST_PW_MSC)}
PW_PASCAGOULA=${PW_PASCAGOULA:-$(_get_env PW_PASCAGOULA)}
TEST_PW_PASCAGOULA=${TEST_PW_PASCAGOULA:-$(_get_env TEST_PW_PASCAGOULA)}

: "${GRAFANA_ADMIN_USER:?GRAFANA_ADMIN_USER not set in .env}"
: "${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD not set in .env}"

# Use internal Docker network to bypass Caddy HTTPS (curl from the VM host to
# the external HTTPS URL hangs due to self-signed cert / loopback SNI issues).
GRAFANA_INTERNAL_IP=$(sudo docker inspect supabase-grafana \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null | head -1)
if [[ -z "$GRAFANA_INTERNAL_IP" ]]; then
    echo "ERROR: Could not determine Grafana container IP. Is supabase-grafana running?" >&2
    exit 1
fi
GRAFANA_URL="http://${GRAFANA_INTERNAL_IP}:3000"
AUTH="${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}"

# Apply safe defaults if not set in .env or environment
TEST_PW_INTERNAL="${TEST_PW_INTERNAL:-TestInternal2026!}"
TEST_PW_PORTRICHEY="${TEST_PW_PORTRICHEY:-PortRichey2026!}"
TEST_PW_CLEARWATER="${TEST_PW_CLEARWATER:-TestClearwater2026!}"
TEST_PW_CORPUSCHRISTI="${TEST_PW_CORPUSCHRISTI:-CorpusChristi2026!}"
PW_CORPUS_CHRISTI="${PW_CORPUS_CHRISTI:-CorpusChristi2026!}"
TEST_PW_CHATHAM="${TEST_PW_CHATHAM:-Chatham2026!}"
PW_CHATHAM="${PW_CHATHAM:-Chatham2026!}"
PW_SENDERO="${PW_SENDERO:-SenderoWind2026!}"
TEST_PW_SENDERO="${TEST_PW_SENDERO:-Sendero2026!}"
PW_MSC="${PW_MSC:-MSCWinds2026!}"
TEST_PW_MSC="${TEST_PW_MSC:-MSC2026!}"
PW_PASCAGOULA="${PW_PASCAGOULA:-PascagoulaWindVis2026!}"
TEST_PW_PASCAGOULA="${TEST_PW_PASCAGOULA:-Pascagoula2026!}"
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
gf_api() {
    # gf_api METHOD /path [body_json]
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

log() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# Wait for Grafana to respond
wait_for_grafana() {
    log "Waiting for Grafana at $GRAFANA_URL ..."
    local i
    for i in $(seq 1 30); do
        if curl -sku "$AUTH" --max-time 3 "${GRAFANA_URL}/api/health" | grep -q '"database"'; then
            log "Grafana is ready."
            return 0
        fi
        sleep 2
    done
    echo "ERROR: Grafana did not become ready in time." >&2
    exit 1
}

# Create or retrieve a team by name; echoes team ID
ensure_team() {
    local name="$1"
    local existing_id
    existing_id=$(gf_api GET "/teams/search?name=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$name")" \
        | jq -r '.teams[]? | select(.name=='"\"$name\""') | .id // empty' | head -1)
    if [[ -n "$existing_id" ]]; then
        log "  Team '$name' already exists (id=$existing_id)"
        echo "$existing_id"
    else
        local result
        result=$(gf_api POST "/teams" "{\"name\":\"$name\",\"email\":\"\"}")
        local new_id
        new_id=$(echo "$result" | jq -r '.teamId // empty')
        log "  Created team '$name' (id=$new_id)"
        echo "$new_id"
    fi
}

# Create a local Grafana user; echoes user ID
ensure_user() {
    local login="$1"
    local name="$2"
    local email="$3"
    local password="$4"
    local existing_id
    existing_id=$(gf_api GET "/users/lookup?loginOrEmail=$login" | jq -r '.id // empty')
    if [[ -n "$existing_id" ]]; then
        log "  User '$login' already exists (id=$existing_id)"
        echo "$existing_id"
    else
        local result
        result=$(gf_api POST "/admin/users" \
            "{\"login\":\"$login\",\"name\":\"$name\",\"email\":\"$email\",\"password\":\"$password\",\"OrgId\":1}")
        local new_id
        new_id=$(echo "$result" | jq -r '.id // empty')
        log "  Created user '$login' (id=$new_id)"
        echo "$new_id"
    fi
}

# Add user to team (idempotent — Grafana returns 400 if already member, we ignore)
add_user_to_team() {
    local team_id="$1"
    local user_id="$2"
    local result
    result=$(gf_api POST "/teams/$team_id/members" "{\"userId\":$user_id}" 2>/dev/null || true)
    if echo "$result" | grep -q '"message":"User is already'\''\|already added'; then
        log "    User $user_id already in team $team_id"
    else
        log "    Added user $user_id to team $team_id"
    fi
}

# Grant a team permission on a folder by folderUid
# Grafana permission values: 1=View, 2=Edit, 4=Admin
set_folder_team_permission() {
    local folder_uid="$1"
    local team_id="$2"
    local permission="${3:-1}"   # default View
    local body
    body=$(jq -n \
        --argjson teamId "$team_id" \
        --argjson permission "$permission" \
        '{items:[{teamId:$teamId, permission:$permission}]}')
    local result
    result=$(gf_api POST "/folders/$folder_uid/permissions" "$body")
    log "    Set permission ($permission) on folder $folder_uid for team $team_id"
}

# Resolve the primary dashboard UID for a customer group.
# Priority order: combined wind+visibility > visibility > wind > tide.
# Looks for matching JSON files in the group provisioning directory so the
# result is always in sync with what generate_group_dashboards.py produced.
resolve_home_dashboard_uid() {
    local group_id="$1"
    local groups_dir="${DOCKER_DIR}/volumes/grafana/provisioning/dashboards/groups/${group_id}"
    for dtype in wind_visibility visibility wind tide; do
        if [[ -f "${groups_dir}/${dtype}.json" ]]; then
            echo "${dtype}-${group_id}"
            return
        fi
    done
    echo ""   # No provisioned dashboard found; caller will skip
}

# Set the homeDashboardUID preference for a Grafana user.
# Must use the user's own credentials — Grafana OSS has no admin API for
# setting another user's preferences.
set_user_home_dashboard() {
    local login="$1"
    local password="$2"
    local dashboard_uid="$3"

    if [[ -z "$dashboard_uid" ]]; then
        log "  Skipping home dashboard for '$login' (no dashboard found in groups folder)"
        return 0
    fi

    local user_auth="${login}:${password}"
    local result
    result=$(curl -sk -u "$user_auth" -X PATCH \
        -H "Content-Type: application/json" \
        -d "{\"homeDashboardUID\":\"${dashboard_uid}\"}" \
        "${GRAFANA_URL}/api/user/preferences")

    if echo "$result" | jq -e '.message == "Preferences updated"' > /dev/null 2>&1; then
        log "  Home dashboard set to '$dashboard_uid' for $login"
    else
        log "  WARNING: Failed to set home dashboard for $login — $result"
    fi
}

# Patch folder permissions — adds a team permission entry WITHOUT wiping existing entries
# Grafana permission values: 1=View, 2=Edit, 4=Admin
patch_folder_team_permission() {
    local folder_uid="$1"
    local team_id="$2"
    local permission="${3:-1}"   # default View
    # First read current permissions
    local current
    current=$(gf_api GET "/folders/$folder_uid/permissions" | jq -c '.')
    # Build updated list: keep only explicit team entries (teamId > 0), strip the
    # default org-role entries (teamId == 0 / role == "Viewer|Editor") which would
    # otherwise grant every org viewer access to every folder, then add this team.
    local updated
    updated=$(echo "$current" | jq --argjson tid "$team_id" --argjson perm "$permission" \
        '[.[] | select(.teamId > 0 and .teamId != $tid)] + [{teamId:$tid, permission:$perm}]')
    local body
    body=$(jq -n --argjson items "$updated" '{items:$items}')
    gf_api POST "/folders/$folder_uid/permissions" "$body" > /dev/null
    log "    Set permission ($permission) on folder $folder_uid for team $team_id"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
wait_for_grafana

log "=== Step 1: Create org-level teams ==="
INTERNAL_TEAM_ID=$(ensure_team "Internal")
CUSTOMERS_TEAM_ID=$(ensure_team "Customers")

# Per-group teams (one per customer group)
log "=== Step 2: Create per-group customer teams ==="
TEAM_PORTRICHEY=$(ensure_team "Portrichey")
TEAM_CLEARWATER=$(ensure_team "Clearwater")
TEAM_CORPUSCHRISTI=$(ensure_team "Corpus Christi")
TEAM_CHATHAM=$(ensure_team "Chatham")
TEAM_SENDERO=$(ensure_team "Sendero")
TEAM_OCEANCAY=$(ensure_team "MSC Cruises")
TEAM_PASCAGOULA=$(ensure_team "Pascagoula")

log "=== Step 3: Create test user accounts ==="
USER_PORTRICHEY=$(ensure_user "test_portrichey" "Test Portrichey" "test_portrichey@skywind.internal" "${TEST_PW_PORTRICHEY:-PortRichey2026!}")
USER_CLEARWATER=$(ensure_user "test_clearwater" "Test Clearwater" "test_clearwater@skywind.internal" "${TEST_PW_CLEARWATER:-TestClearwater2026!}")
USER_CORPUSCHRISTI=$(ensure_user "test_corpuschristi" "Test Corpus Christi" "test_corpuschristi@skywind.internal" "${TEST_PW_CORPUSCHRISTI:-CorpusChristi2026!}")
USER_CORPUS_CHRISTI=$(ensure_user "corpus_christi" "Corpus Christi" "corpus_christi@skywind.internal" "${PW_CORPUS_CHRISTI:-CorpusChristi2026!}")
USER_CHATHAM=$(ensure_user "test_chatham" "Test Chatham" "test_chatham@skywind.internal" "${TEST_PW_CHATHAM:-Chatham2026!}")
USER_CHATHAM_CUST=$(ensure_user "chatham" "Chatham" "chatham@skywind.internal" "${PW_CHATHAM:-Chatham2026!}")
USER_INTERNAL=$(ensure_user "test_internal" "Test Internal"   "test_internal@skywind.internal"   "$TEST_PW_INTERNAL")
USER_SENDERO=$(ensure_user "sendero" "Sendero" "sendero@skywind.internal" "$PW_SENDERO")
USER_TEST_SENDERO=$(ensure_user "test_sendero" "Test Sendero" "test_sendero@skywind.internal" "$TEST_PW_SENDERO")
USER_MSC=$(ensure_user "msccruises" "MSC Cruises" "msccruises@skywind.internal" "$PW_MSC")
USER_TEST_MSC=$(ensure_user "test_msc" "Test MSC" "test_msc@skywind.internal" "$TEST_PW_MSC")
USER_PASCAGOULA=$(ensure_user "pascagoula" "Pascagoula" "pascagoula@skywind.internal" "$PW_PASCAGOULA")
USER_TEST_PASCAGOULA=$(ensure_user "test_pascagoula" "Test Pascagoula" "test_pascagoula@skywind.internal" "$TEST_PW_PASCAGOULA")

log "=== Step 4: Assign users to teams ==="
add_user_to_team "$TEAM_PORTRICHEY" "$USER_PORTRICHEY"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_PORTRICHEY"

add_user_to_team "$TEAM_CLEARWATER" "$USER_CLEARWATER"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_CLEARWATER"

add_user_to_team "$TEAM_CORPUSCHRISTI" "$USER_CORPUSCHRISTI"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_CORPUSCHRISTI"

add_user_to_team "$TEAM_CORPUSCHRISTI" "$USER_CORPUS_CHRISTI"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_CORPUS_CHRISTI"

add_user_to_team "$TEAM_CHATHAM" "$USER_CHATHAM"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_CHATHAM"

add_user_to_team "$TEAM_CHATHAM" "$USER_CHATHAM_CUST"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_CHATHAM_CUST"

add_user_to_team "$INTERNAL_TEAM_ID" "$USER_INTERNAL"

add_user_to_team "$TEAM_SENDERO" "$USER_SENDERO"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_SENDERO"

add_user_to_team "$TEAM_SENDERO" "$USER_TEST_SENDERO"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_TEST_SENDERO"

add_user_to_team "$TEAM_OCEANCAY" "$USER_MSC"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_MSC"

add_user_to_team "$TEAM_OCEANCAY" "$USER_TEST_MSC"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_TEST_MSC"

add_user_to_team "$TEAM_PASCAGOULA" "$USER_PASCAGOULA"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_PASCAGOULA"

add_user_to_team "$TEAM_PASCAGOULA" "$USER_TEST_PASCAGOULA"
add_user_to_team "$CUSTOMERS_TEAM_ID" "$USER_TEST_PASCAGOULA"

log "=== Step 5: Set folder permissions ==="
# Folder UIDs (must match dashboards.yaml)
declare -A FOLDER_UIDS=(
    ["customer-dashboards"]="customer-dashboards"
    ["internal-dashboards"]="internal-dashboards"
    ["public-dashboards"]="public-dashboards"
    ["portrichey"]="cust-grp-portrichey"
    ["clearwater"]="cust-grp-clearwater"
    ["corpuschristi"]="cust-grp-corpuschristi"
    ["chatham"]="cust-grp-chatham"
    ["sendero"]="cust-grp-sendero"
    ["oceancay"]="cust-grp-oceanCay"
    ["pascagoula"]="cust-grp-pascagoula"
)

# Internal team: Viewer on Customer Dashboards and Internal Dashboards
patch_folder_team_permission "${FOLDER_UIDS[customer-dashboards]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[internal-dashboards]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[public-dashboards]}" "$INTERNAL_TEAM_ID" 1

# Internal team also gets access to all group folders (for support/debugging)
patch_folder_team_permission "${FOLDER_UIDS[portrichey]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[clearwater]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[corpuschristi]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[chatham]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[sendero]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[oceancay]}" "$INTERNAL_TEAM_ID" 1
patch_folder_team_permission "${FOLDER_UIDS[pascagoula]}" "$INTERNAL_TEAM_ID" 1

# Per-group teams: Viewer on their folder only
patch_folder_team_permission "${FOLDER_UIDS[portrichey]}" "$TEAM_PORTRICHEY" 1
patch_folder_team_permission "${FOLDER_UIDS[clearwater]}" "$TEAM_CLEARWATER" 1
patch_folder_team_permission "${FOLDER_UIDS[corpuschristi]}" "$TEAM_CORPUSCHRISTI" 1
patch_folder_team_permission "${FOLDER_UIDS[chatham]}" "$TEAM_CHATHAM" 1
patch_folder_team_permission "${FOLDER_UIDS[sendero]}" "$TEAM_SENDERO" 1
patch_folder_team_permission "${FOLDER_UIDS[oceancay]}" "$TEAM_OCEANCAY" 1
patch_folder_team_permission "${FOLDER_UIDS[pascagoula]}" "$TEAM_PASCAGOULA" 1

log "=== Step 6: Set home dashboards for test users ==="
# Customer accounts: resolve primary dashboard from the group provisioning folder
# (visibility > wind > tide priority; skipped gracefully if nothing is found).
set_user_home_dashboard "test_portrichey" "$TEST_PW_PORTRICHEY"  "$(resolve_home_dashboard_uid grp_portrichey)"
set_user_home_dashboard "test_clearwater" "$TEST_PW_CLEARWATER"  "$(resolve_home_dashboard_uid grp_clearwater)"
set_user_home_dashboard "test_corpuschristi" "$TEST_PW_CORPUSCHRISTI"  "$(resolve_home_dashboard_uid grp_corpuschristi)"
set_user_home_dashboard "corpus_christi" "$PW_CORPUS_CHRISTI"  "$(resolve_home_dashboard_uid grp_corpuschristi)"
set_user_home_dashboard "test_chatham" "$TEST_PW_CHATHAM"  "$(resolve_home_dashboard_uid grp_chatham)"
set_user_home_dashboard "chatham" "$PW_CHATHAM"  "$(resolve_home_dashboard_uid grp_chatham)"
# Internal account: always land on the internal visibility dashboard
set_user_home_dashboard "test_internal"   "$TEST_PW_INTERNAL"    "internal-visibility"
set_user_home_dashboard "sendero"         "$PW_SENDERO"          "$(resolve_home_dashboard_uid grp_sendero)"
set_user_home_dashboard "test_sendero"    "$TEST_PW_SENDERO"     "$(resolve_home_dashboard_uid grp_sendero)"
set_user_home_dashboard "msccruises"      "$PW_MSC"              "$(resolve_home_dashboard_uid grp_oceanCay)"
set_user_home_dashboard "test_msc"        "$TEST_PW_MSC"         "$(resolve_home_dashboard_uid grp_oceanCay)"
set_user_home_dashboard "pascagoula"      "$PW_PASCAGOULA"       "$(resolve_home_dashboard_uid grp_pascagoula)"
set_user_home_dashboard "test_pascagoula" "$TEST_PW_PASCAGOULA"  "$(resolve_home_dashboard_uid grp_pascagoula)"

log "=== Done ==="
log ""
log "Test accounts created:"
log "  Username          Password env var       Team"
log "  ---------------   --------------------   ----------------------"
log "  test_portrichey  TEST_PW_PORTRICHEY     Portrichey + Customers"
log "  test_clearwater   TEST_PW_CLEARWATER     Clearwater + Customers"
log "  test_corpuschristi TEST_PW_CORPUSCHRISTI  Corpus Christi + Customers"
log "  corpus_christi     PW_CORPUS_CHRISTI      Corpus Christi + Customers"
log "  test_chatham       TEST_PW_CHATHAM        Chatham + Customers"
log "  chatham            PW_CHATHAM             Chatham + Customers"
log "  test_internal     TEST_PW_INTERNAL       Internal"
log "  sendero            PW_SENDERO             Sendero + Customers"
log "  test_sendero       TEST_PW_SENDERO        Sendero + Customers"
log "  msccruises         PW_MSC                 Ocean Cay + Customers"
log "  test_msc           TEST_PW_MSC            Ocean Cay + Customers"
log "  pascagoula         PW_PASCAGOULA          Pascagoula + Customers"
log "  test_pascagoula    TEST_PW_PASCAGOULA     Pascagoula + Customers"
log ""
log "To change a test password, update the variable in .env and re-run this script."
log "To add a new customer group:"
log "  1. Add data in the DB under the new group_id"
log "  2. Run: python3 scripts/generate_group_dashboards.py --db-url ..."
log "  3. Reload Grafana provisioning: docker exec supabase-grafana wget -qO- --post-data '' http://localhost:3000/api/admin/provisioning/dashboards/reload"
log "  4. Re-run this script"
