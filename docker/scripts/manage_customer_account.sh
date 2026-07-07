#!/usr/bin/env bash
# =============================================================================
# manage_customer_account.sh
# Task: P2.6 — Enable reversible deactivation/reactivation of customer accounts
# Created: 2026-07-06
# =============================================================================
# Reversibly deactivates or reactivates a customer Grafana account and their
# alert subscriptions WITHOUT deleting any data, teams, or folder permissions.
#
# What this script does:
#
#   deactivate:
#     1. Disables the Grafana user(s) via POST /api/admin/users/:id/disable
#        (prevents login; user record, teams, folder permissions preserved)
#     2. Disables alert subscriptions (enabled = false) for matching grafana_login
#
#   reactivate:
#     1. Enables the Grafana user(s) via POST /api/admin/users/:id/enable
#     2. Enables alert subscriptions (enabled = true) for matching grafana_login
#
#   status:
#     1. Shows whether the user is currently disabled or enabled in Grafana
#     2. Shows count of active/inactive alert subscriptions
#
# Usage:
#   cd supabase/docker
#
#   # By grafana login
#   bash scripts/manage_customer_account.sh --action deactivate --user sendero
#   bash scripts/manage_customer_account.sh --action reactivate --user sendero
#   bash scripts/manage_customer_account.sh --action status --user sendero
#
#   # By group_id (deactivates ALL users in that group's Grafana team)
#   bash scripts/manage_customer_account.sh --action deactivate --group grp_sendero
#
#   # Multiple users at once
#   bash scripts/manage_customer_account.sh --action deactivate --user sendero --user test_sendero
#
# Requirements:
#   - .env file in supabase/docker with GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD
#   - supabase-grafana container running (accessed via internal Docker IP:3000)
#   - supabase-db container running (for alert subscription updates)
#   - curl, jq, sudo docker access, psql
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$DOCKER_DIR/.env"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat >&2 <<EOF
Usage: $0 --action <deactivate|reactivate|status> [--user <login>]... [--group <group_id>]

Actions:
  deactivate   Disable Grafana user account and alert subscriptions
  reactivate   Re-enable Grafana user account and alert subscriptions
  status       Show current account state

Options:
  --user <login>   Grafana login (e.g. sendero). May be repeated.
  --group <id>     Group ID (e.g. grp_sendero). Deactivates all users in the
                   group's Grafana team. The team-to-group mapping must exist
                   in provision_grafana_access.sh (FOLDER_UIDS keys).

Examples:
  $0 --action deactivate --user sendero
  $0 --action deactivate --user sendero --user test_sendero
  $0 --action deactivate --group grp_sendero
  $0 --action reactivate --user sendero
  $0 --action status --user sendero
EOF
    exit 1
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
ACTION=""
USERS=()
GROUP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --action) ACTION="$2"; shift 2 ;;
        --user)   USERS+=("$2"); shift 2 ;;
        --group)  GROUP="$2"; shift 2 ;;
        --help)   usage ;;
        *)        echo "ERROR: Unknown argument: $1" >&2; usage ;;
    esac
done

if [[ -z "$ACTION" ]]; then
    echo "ERROR: --action is required (deactivate|reactivate|status)" >&2
    usage
fi

if [[ "$ACTION" != "deactivate" && "$ACTION" != "reactivate" && "$ACTION" != "status" ]]; then
    echo "ERROR: Invalid action '$ACTION'. Use deactivate, reactivate, or status." >&2
    usage
fi

if [[ -z "$GROUP" && ${#USERS[@]} -eq 0 ]]; then
    echo "ERROR: At least one --user or --group is required." >&2
    usage
fi

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env not found at $ENV_FILE" >&2
    exit 1
fi

_get_env() { grep "^${1}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }

GRAFANA_ADMIN_USER=$(_get_env GRAFANA_ADMIN_USER)
GRAFANA_ADMIN_PASSWORD=$(_get_env GRAFANA_ADMIN_PASSWORD)

: "${GRAFANA_ADMIN_USER:?GRAFANA_ADMIN_USER not set in .env}"
: "${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD not set in .env}"

# Grafana connection (internal Docker network, same as provision script)
GRAFANA_INTERNAL_IP=$(sudo docker inspect supabase-grafana \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null | head -1)
if [[ -z "$GRAFANA_INTERNAL_IP" ]]; then
    echo "ERROR: Could not determine Grafana container IP. Is supabase-grafana running?" >&2
    exit 1
fi
GRAFANA_URL="http://${GRAFANA_INTERNAL_IP}:3000"
AUTH="${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASSWORD}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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

log() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# Resolve a Grafana login to a user ID. Returns empty string if not found.
resolve_user_id() {
    local login="$1"
    gf_api GET "/users/lookup?loginOrEmail=$login" 2>/dev/null | jq -r '.id // empty'
}

# Get user's current isDisabled status
get_user_disabled_status() {
    local user_id="$1"
    local status
    status=$(gf_api GET "/users/$user_id" 2>/dev/null | jq -r '.isDisabled // empty')
    echo "$status"
}

# Get the Grafana team ID by team name
get_team_id_by_name() {
    local name="$1"
    gf_api GET "/teams/search?name=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$name")" \
        2>/dev/null | jq -r '.teams[]? | select(.name=='"\"$name\""') | .id // empty' | head -1
}

# Get all member logins for a team
get_team_member_logins() {
    local team_id="$1"
    gf_api GET "/teams/$team_id/members" 2>/dev/null | jq -r '.[].login // empty'
}

# Build comma-separated psql string for IN clause
sql_quote_list() {
    local first=1
    for val in "$@"; do
        if [[ "$first" -eq 1 ]]; then
            printf "'%s'" "$val"
            first=0
        else
            printf ", '%s'" "$val"
        fi
    done
}

# ---------------------------------------------------------------------------
# Resolve targets: users from --user args or --group
# ---------------------------------------------------------------------------
TARGET_LOGINS=()

if [[ -n "$GROUP" ]]; then
    # Map group_id to team name using the same naming convention as provision_grafana_access.sh
    # The team name is the human-readable display name (e.g. "Sendero", "Corpus Christi").
    # We look up via the folder UID convention: cust-grp-<group_id_without_grp_>
    local_suffix="${GROUP#grp_}"
    folder_uid="cust-grp-${local_suffix}"

    log "Looking up team for folder '$folder_uid' (group '$GROUP') ..."

    # Find team name: search teams that have View permission on this folder
    folder_perms=$(gf_api GET "/folders/$folder_uid/permissions" 2>/dev/null || echo "")
    if [[ -z "$folder_perms" || "$folder_perms" == "null" ]]; then
        echo "ERROR: Folder '$folder_uid' not found or has no permissions. Is the group provisioned?" >&2
        exit 1
    fi

    # Find the folder permission for the team that owns this group
    # We'll search all teams and check which team has a permission on this folder
    log "  Found folder '$folder_uid'. Resolving team members ..."

    # Get all teams from the folder permissions (only teamId > 0 entries)
    team_ids=$(echo "$folder_perms" | jq -r '.[] | select(.teamId > 0) | .teamId' 2>/dev/null || true)

    for tid in $team_ids; do
        # Get team info
        team_info=$(gf_api GET "/teams/$tid" 2>/dev/null || echo "")
        team_name=$(echo "$team_info" | jq -r '.name // empty' 2>/dev/null || true)
        if [[ -n "$team_name" ]]; then
            # Exclude Internal and Customers teams — those are org-level, not per-group
            if [[ "$team_name" != "Internal" && "$team_name" != "Customers" ]]; then
                log "  Found team '$team_name' (id=$tid) on folder '$folder_uid'"
                members=()
                while IFS= read -r member_login; do
                    if [[ -n "$member_login" ]]; then
                        members+=("$member_login")
                    fi
                done < <(get_team_member_logins "$tid")
                if [[ ${#members[@]} -gt 0 ]]; then
                    log "  Team members: ${members[*]}"
                    TARGET_LOGINS+=("${members[@]}")
                fi
            fi
        fi
    done

    if [[ ${#TARGET_LOGINS[@]} -eq 0 ]]; then
        echo "ERROR: No users found for group '$GROUP'. Check that the group is provisioned." >&2
        exit 1
    fi
fi

# Add explicitly specified users
for u in "${USERS[@]}"; do
    TARGET_LOGINS+=("$u")
done

# Remove duplicates
mapfile -t TARGET_LOGINS < <(printf '%s\n' "${TARGET_LOGINS[@]}" | sort -u)

log "Target users: ${TARGET_LOGINS[*]}"

# ---------------------------------------------------------------------------
# Resolve all user IDs upfront
# ---------------------------------------------------------------------------
declare -A USER_IDS
for login in "${TARGET_LOGINS[@]}"; do
    uid=$(resolve_user_id "$login")
    if [[ -z "$uid" ]]; then
        echo "WARNING: User '$login' not found in Grafana. Skipping." >&2
    else
        USER_IDS["$login"]="$uid"
    fi
done

if [[ ${#USER_IDS[@]} -eq 0 ]]; then
    echo "ERROR: No valid Grafana users found to process." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
case "$ACTION" in
    deactivate)
        log "=== Deactivating customer account(s) ==="

        for login in "${!USER_IDS[@]}"; do
            uid="${USER_IDS[$login]}"
            log "  Processing user '$login' (id=$uid) ..."

            # Check if already disabled
            disabled=$(get_user_disabled_status "$uid")
            if [[ "$disabled" == "true" ]]; then
                log "    User '$login' is already disabled. Skipping Grafana disable."
            else
                gf_api POST "/admin/users/$uid/disable" > /dev/null
                log "    User '$login' disabled in Grafana."
            fi

            # Disable alert subscriptions
            if sudo docker exec supabase-db psql -U postgres -d postgres -t \
                -c "UPDATE public.alert_subscriptions SET enabled = false, updated_at = NOW() WHERE grafana_login = '$login' AND enabled = true;" \
                2>/dev/null; then
                rows_affected=$(sudo docker exec supabase-db psql -U postgres -d postgres -t \
                    -c "SELECT COUNT(*) FROM public.alert_subscriptions WHERE grafana_login = '$login' AND enabled = false AND updated_at = NOW()::date;" \
                    2>/dev/null | tr -d ' ')
                log "    Alert subscriptions disabled for '$login' (rows affected: ${rows_affected:-0})"
            else
                log "    WARNING: Could not update alert subscriptions for '$login' (table may not exist)"
            fi
        done

        log "=== Deactivation complete ==="
        log "User records, teams, folder permissions, and forecast data are preserved."
        log "To re-enable: $0 --action reactivate ${USERS[@]/#/--user } ${GROUP:+--group $GROUP}"
        ;;

    reactivate)
        log "=== Reactivating customer account(s) ==="

        for login in "${!USER_IDS[@]}"; do
            uid="${USER_IDS[$login]}"
            log "  Processing user '$login' (id=$uid) ..."

            # Check if already enabled
            disabled=$(get_user_disabled_status "$uid")
            if [[ "$disabled" != "true" ]]; then
                log "    User '$login' is already enabled. Skipping Grafana enable."
            else
                gf_api POST "/admin/users/$uid/enable" > /dev/null
                log "    User '$login' enabled in Grafana."
            fi

            # Enable alert subscriptions
            if sudo docker exec supabase-db psql -U postgres -d postgres -t \
                -c "UPDATE public.alert_subscriptions SET enabled = true, updated_at = NOW() WHERE grafana_login = '$login' AND enabled = false;" \
                2>/dev/null; then
                rows_affected=$(sudo docker exec supabase-db psql -U postgres -d postgres -t \
                    -c "SELECT COUNT(*) FROM public.alert_subscriptions WHERE grafana_login = '$login' AND enabled = true AND updated_at = NOW()::date;" \
                    2>/dev/null | tr -d ' ')
                log "    Alert subscriptions enabled for '$login' (rows affected: ${rows_affected:-0})"
            else
                log "    WARNING: Could not update alert subscriptions for '$login' (table may not exist)"
            fi
        done

        log "=== Reactivation complete ==="
        ;;

    status)
        log "=== Account status ==="
        printf "%-24s %-12s %-14s %s\n" "Login" "User ID" "Grafana Status" "Alert Subs"
        printf "%-24s %-12s %-14s %s\n" "------" "-------" "--------------" "----------"

        for login in "${!USER_IDS[@]}"; do
            uid="${USER_IDS[$login]}"
            disabled=$(get_user_disabled_status "$uid")
            if [[ "$disabled" == "true" ]]; then
                gf_status="DISABLED"
            else
                gf_status="enabled"
            fi

            # Count alert subscriptions
            alert_info="N/A"
            if sudo docker exec supabase-db psql -U postgres -d postgres -t \
                -c "SELECT CONCAT(COUNT(*) FILTER (WHERE enabled), ' active / ', COUNT(*), ' total') FROM public.alert_subscriptions WHERE grafana_login = '$login';" \
                2>/dev/null | read -r alert_info; then
                alert_info=$(sudo docker exec supabase-db psql -U postgres -d postgres -t \
                    -c "SELECT CONCAT(COUNT(*) FILTER (WHERE enabled), ' active / ', COUNT(*), ' total') FROM public.alert_subscriptions WHERE grafana_login = '$login';" \
                    2>/dev/null | tr -d ' ' | head -1)
            fi

            printf "%-24s %-12s %-14s %s\n" "$login" "$uid" "$gf_status" "${alert_info:-N/A}"
        done
        ;;
esac
