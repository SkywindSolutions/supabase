#!/usr/bin/env bash
# =============================================================================
# run_load_test.sh
# Task: P2.7 - Load Testing
# Created: 2026-03-23
# =============================================================================
# Orchestrates the full P2.7 load test suite:
#
#   Step 1  Seed load-test data into the DB (lt_group_alpha / lt_group_beta)
#   Step 2  pgbench DB-layer concurrency test (20 clients, 120s, direct TCP)
#   Step 3  Locust Grafana HTTP-layer test (20 users, VM-side via Docker network)
#   Step 4  Print instructions for running the remote (local-machine) Locust test
#
# Prerequisites on the VM:
#   - Docker Compose stack running (supabase-db and grafana containers up)
#   - pgbench installed: sudo apt-get install -y postgresql-client
#   - All three scripts at their expected paths:
#       db/seeds/seed_load_test_data.sql
#       db/tests/load_test_pgbench.sql
#       supabase/docker/scripts/load_test_locust.py
#
# Usage:
#   cd /path/to/skywind_infra
#   ./supabase/docker/scripts/run_load_test.sh [OPTIONS]
#
# Options:
#   --seed-only     Seed data then exit (no tests)
#   --pgbench-only  Run pgbench step only (assumes data already seeded)
#   --locust-only   Run Locust step only  (assumes data already seeded)
#   --no-seed       Skip seeding step (use if data is already present)
#   --cleanup       Remove load test data from the DB after tests complete
#   --help          Show this help text
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve paths relative to this script's location
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

ENV_FILE="${SCRIPT_DIR}/../.env"
SEED_SQL="${REPO_ROOT}/db/seeds/seed_load_test_data.sql"
PGBENCH_SQL="${REPO_ROOT}/db/tests/load_test_pgbench.sql"
LOCUST_SCRIPT="${SCRIPT_DIR}/load_test_locust.py"
RESULTS_DIR="${REPO_ROOT}/load_test_results"

# ---------------------------------------------------------------------------
# Default flags
# ---------------------------------------------------------------------------
RUN_SEED=true
RUN_PGBENCH=true
RUN_LOCUST=true
CLEANUP_AFTER=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --seed-only)    RUN_PGBENCH=false; RUN_LOCUST=false ;;
        --pgbench-only) RUN_SEED=false;    RUN_LOCUST=false ;;
        --locust-only)  RUN_SEED=false;    RUN_PGBENCH=false ;;
        --no-seed)      RUN_SEED=false ;;
        --cleanup)      CLEANUP_AFTER=true ;;
        --help)
            grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown option: $arg  (try --help)"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { echo "[$(date '+%H:%M:%S')] $*"; }

die() { echo "ERROR: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" &>/dev/null || die "'$1' not found — install with: $2"
}

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die ".env not found at $ENV_FILE"

# Source only the variables we need; avoid executing arbitrary shell code.
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
GRAFANA_ADMIN_PASSWORD=$(grep -E '^GRAFANA_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
GRAFANA_ADMIN_USER=$(grep -E '^GRAFANA_ADMIN_USER=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "admin")
PROXY_DOMAIN=$(grep -E '^PROXY_DOMAIN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "")
# NOTE: Supavisor (the connection pooler on port 5432) requires the username
# format "postgres.<POOLER_TENANT_ID>". Plain "postgres" is rejected with
# "Tenant or user not found". psql commands use docker exec to bypass
# Supavisor entirely; pgbench uses the tenant-prefixed username.
POOLER_TENANT_ID=$(grep -E '^POOLER_TENANT_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
PG_USER="postgres.${POOLER_TENANT_ID}"

[[ -n "$POSTGRES_PASSWORD" ]] || die "POSTGRES_PASSWORD not found in $ENV_FILE"
[[ -n "$GRAFANA_ADMIN_PASSWORD" ]] || die "GRAFANA_ADMIN_PASSWORD not found in $ENV_FILE"
[[ -n "$POOLER_TENANT_ID" ]] || die "POOLER_TENANT_ID not found in $ENV_FILE"

export PGPASSWORD="$POSTGRES_PASSWORD"

# ---------------------------------------------------------------------------
# Discover the Grafana container's internal IP for VM-side Locust test
# ---------------------------------------------------------------------------
GRAFANA_CONTAINER_IP=""
if docker inspect supabase-grafana &>/dev/null; then
    GRAFANA_CONTAINER_IP=$(
        docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' supabase-grafana \
        2>/dev/null | head -n1 || true
    )
fi

GRAFANA_INTERNAL_URL="http://grafana:3000"

# Build the public HTTPS URL for the remote-machine instructions
if [[ -n "$PROXY_DOMAIN" && "$PROXY_DOMAIN" != "localhost" ]]; then
    GRAFANA_PUBLIC_URL="https://${PROXY_DOMAIN}"
else
    PUBLIC_IP=$(curl -sf --max-time 5 https://api.ipify.org || echo "YOUR_SERVER_IP")
    GRAFANA_PUBLIC_URL="https://${PUBLIC_IP}"
fi

mkdir -p "$RESULTS_DIR"

# ---------------------------------------------------------------------------
# Step 1: Seed load-test data
# ---------------------------------------------------------------------------
if [[ "$RUN_SEED" == true ]]; then
    log "=== STEP 1: Seeding load-test data ==="
    log "This inserts ~1.85M rows and may take 30–90 seconds."

    # Use docker exec to connect directly to the supabase-db container,
    # bypassing Supavisor (which requires a tenant-prefixed username).
    docker exec -i supabase-db psql -U postgres -d postgres \
        < "$SEED_SQL" \
        2>&1 | tee "${RESULTS_DIR}/seed_output.txt"

    log "Seeding complete. Output saved to ${RESULTS_DIR}/seed_output.txt"
fi

# ---------------------------------------------------------------------------
# Step 2: pgbench DB-layer concurrency test
# ---------------------------------------------------------------------------
if [[ "$RUN_PGBENCH" == true ]]; then
    log ""
    log "=== STEP 2: pgbench DB-layer concurrency test ==="
    log "20 concurrent clients, 4 worker threads, 120s duration."

    require_cmd pgbench "sudo apt-get install -y postgresql-client"

    # pgbench connects via Supavisor (port 5432) so it needs the tenant-prefixed
    # username read from .env above. Session mode (port 5432) is used so each
    # pgbench worker gets a persistent connection — correct for benchmark workloads.
    log "Warm-up: 1 client × 10s (user: ${PG_USER}) ..."
    pgbench -h 127.0.0.1 -p 5432 -U "${PG_USER}" -d postgres \
        -f "$PGBENCH_SQL" \
        -c 1 -j 1 -T 10 --no-vacuum \
        > /dev/null 2>&1 || true

    # Main run: ramp to 20 concurrent clients
    log "Running main concurrency test (20 clients, 120s) ..."
    pgbench -h 127.0.0.1 -p 5432 -U "${PG_USER}" -d postgres \
        -f "$PGBENCH_SQL" \
        -c 20 -j 4 -T 120 --no-vacuum \
        2>&1 | tee "${RESULTS_DIR}/pgbench_c20_results.txt"

    log "pgbench complete. Results saved to ${RESULTS_DIR}/pgbench_c20_results.txt"

    # Extract and display key metrics
    echo ""
    echo "--- pgbench summary ---"
    grep -E "tps|latency|transactions" "${RESULTS_DIR}/pgbench_c20_results.txt" || true
    echo "-----------------------"
fi

# ---------------------------------------------------------------------------
# Step 3: Locust Grafana HTTP-layer test (via Docker, VM-side)
# ---------------------------------------------------------------------------
if [[ "$RUN_LOCUST" == true ]]; then
    log ""
    log "=== STEP 3: Locust HTTP-layer test (VM → Grafana internal network) ==="
    log "20 virtual users, spawn rate 1 user/sec, 5-minute run."

    # Locate the Docker Compose network name so Locust can reach grafana:3000
    COMPOSE_NETWORK=$(
        docker network ls --format '{{.Name}}' \
        | grep -E 'supabase.*(default|network)' \
        | head -n1 || echo "supabase_default"
    )

    log "Using Docker network: ${COMPOSE_NETWORK}"
    log "Locust will connect to: ${GRAFANA_INTERNAL_URL}"

    docker run --rm \
        --network "${COMPOSE_NETWORK}" \
        -v "${REPO_ROOT}/supabase/docker/scripts":/mnt/locust \
        -e GRAFANA_USER="${GRAFANA_ADMIN_USER}" \
        -e GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD}" \
        -e GRAFANA_PATH_PREFIX="" \
        -e TEST_GROUP_A="lt_group_alpha" \
        -e TEST_GROUP_B="lt_group_beta" \
        locustio/locust \
        -f /mnt/locust/load_test_locust.py \
        --host "${GRAFANA_INTERNAL_URL}" \
        --users 20 \
        --spawn-rate 1 \
        --run-time 5m \
        --headless \
        --csv /mnt/locust/results_vm \
        2>&1 | tee "${RESULTS_DIR}/locust_vm_output.txt"

    # The --csv flag writes files to the mounted directory in the container,
    # which maps to supabase/docker/scripts/ on the host.
    VM_CSV_SRC="${SCRIPT_DIR}/results_vm_stats.csv"
    if [[ -f "$VM_CSV_SRC" ]]; then
        mv "${SCRIPT_DIR}/results_vm_"* "${RESULTS_DIR}/" 2>/dev/null || true
        log "Locust CSV results moved to ${RESULTS_DIR}/"
    fi

    log "VM Locust test complete. Output saved to ${RESULTS_DIR}/locust_vm_output.txt"

    echo ""
    echo "--- Locust VM summary ---"
    if [[ -f "${RESULTS_DIR}/results_vm_stats.csv" ]]; then
        column -t -s, "${RESULTS_DIR}/results_vm_stats.csv" | head -20
    else
        grep -E "requests|failures|RPS|p95" "${RESULTS_DIR}/locust_vm_output.txt" | tail -20 || true
    fi
    echo "-------------------------"
fi

# ---------------------------------------------------------------------------
# Step 4: Instructions for local-machine (remote) Locust run
# ---------------------------------------------------------------------------
log ""
log "=== STEP 4: Run from your local machine (full-stack HTTPS test) ==="
cat <<EOF

  To test the complete stack (TLS termination + Caddy + Grafana + PostgreSQL)
  run the following from your local workstation:

    pip install locust        # install once

    GRAFANA_USER=${GRAFANA_ADMIN_USER} \\
    GRAFANA_PASSWORD='<your-grafana-password>' \\
    GRAFANA_PATH_PREFIX="" \\
    locust \\
      -f supabase/docker/scripts/load_test_locust.py \\
      --host ${GRAFANA_PUBLIC_URL} \\
      --users 20 \\
      --spawn-rate 1 \\
      --run-time 5m \\
      --headless \\
      --csv load_test_results/results_remote

  Results will be written to load_test_results/results_remote_stats.csv and
  load_test_results/results_remote_failures.csv.

EOF

# ---------------------------------------------------------------------------
# Optional cleanup
# ---------------------------------------------------------------------------
if [[ "$CLEANUP_AFTER" == true ]]; then
    log "=== CLEANUP: Removing load-test data from DB ==="
    docker exec supabase-db psql -U postgres -d postgres \
        -c "DELETE FROM public.forecast_data WHERE group_id IN ('lt_group_alpha', 'lt_group_beta');"
    log "Load test data removed."
fi

log ""
log "All steps complete. Results are in: ${RESULTS_DIR}/"
log "Update PLANNING.md with the observed pgbench TPS and Locust RPS/p95 numbers."
