#!/usr/bin/env bash
# deploy_visibility_dashboards.sh — Reload Grafana to pick up visibility dashboard changes.
#
# Usage:
#   cd supabase/docker && bash scripts/deploy_visibility_dashboards.sh
#
# The visibility.json files are volume-mounted into the Grafana container at
# /etc/grafana/provisioning/dashboards/ via docker-compose.grafana.yml.
# Editing the host files is sufficient for persistence; this script triggers
# Grafana to re-read them.

set -euo pipefail

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.caddy.yml -f docker-compose.grafana.yml"

echo "--- Restarting Grafana to reload provisioned dashboards ---"
docker compose $COMPOSE_FILES restart grafana

echo ""
echo "--- Waiting for Grafana health check ---"
for i in $(seq 1 30); do
    if docker exec supabase-grafana wget -q --spider http://localhost:3000/api/health 2>/dev/null; then
        echo "Grafana is healthy."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Grafana did not become healthy within 30 seconds."
        exit 1
    fi
    sleep 1
done

echo ""
echo "--- Validating provisioned dashboards ---"

DASHBOARDS=(
    "customer-visibility"
    "internal-visibility"
    "visibility-grp_corpuschristi"
)

GRAFANA_USER="${GRAFANA_ADMIN_USER:-$(grep GRAFANA_ADMIN_USER .env 2>/dev/null | cut -d= -f2-)}"
GRAFANA_PASS="${GRAFANA_ADMIN_PASSWORD:-$(grep GRAFANA_ADMIN_PASSWORD .env 2>/dev/null | cut -d= -f2-)}"
GRAFANA_USER="${GRAFANA_USER:-admin}"

if [[ -z "$GRAFANA_PASS" ]]; then
    echo "ERROR: GRAFANA_ADMIN_PASSWORD not set and not found in .env"
    exit 1
fi

ALL_OK=true
for uid in "${DASHBOARDS[@]}"; do
    # Check dashboard exists via API
    HTTP_CODE=$(docker exec supabase-grafana wget -q -O /dev/null -S \
        --header "Authorization: Basic $(echo -n "${GRAFANA_USER}:${GRAFANA_PASS}" | base64)" \
        "http://localhost:3000/api/dashboards/uid/${uid}" 2>&1 | grep "HTTP/" | tail -1 | awk '{print $2}')

    if [ "$HTTP_CODE" = "200" ]; then
        echo "  OK: ${uid} (HTTP ${HTTP_CODE})"
    else
        echo "  FAIL: ${uid} (HTTP ${HTTP_CODE:-???})"
        ALL_OK=false
    fi
done

echo ""
echo "--- Validating run_time variable fetches data from database ---"
# Test the run_time variable SQL against the database
RUN_TIME_SQL="SELECT COUNT(DISTINCT startdt) AS run_count FROM forecast_data WHERE vismean IS NOT NULL LIMIT 1;"
RUN_COUNT=$(docker exec supabase-db psql -U postgres -d postgres -tAc "$RUN_TIME_SQL" 2>/dev/null || echo "0")

if [ "${RUN_COUNT:-0}" -gt 0 ]; then
    echo "  OK: ${RUN_COUNT} distinct startdt values found in forecast_data"
else
    echo "  WARN: No startdt values found — run_time dropdown will be empty until data is ingested"
fi

echo ""
if [ "$ALL_OK" = true ]; then
    echo "=== All visibility dashboards deployed and validated ==="
else
    echo "=== Some dashboards failed validation — check output above ==="
    exit 1
fi
