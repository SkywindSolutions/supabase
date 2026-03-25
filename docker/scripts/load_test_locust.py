#!/usr/bin/env python3
"""
load_test_locust.py
Task: P2.7 - Load Testing (Grafana HTTP layer)

Simulates concurrent Grafana dashboard users by replaying the panel
query payloads that Grafana sends to PostgreSQL via its backend proxy
API (/api/ds/query).  Each virtual user represents one browser session
periodically refreshing a customer or internal dashboard.

Each user randomly alternates between the three dashboard types
(visibility, wind, tide) weighted by their expected real-world usage,
waits a realistic pause, then refreshes again.

Run from the VM (against the internal Docker network — recommended):
    docker run --rm \\
      --network supabase_network \\
      -v "$(pwd)/supabase/docker/scripts":/mnt/locust \\
      -e GRAFANA_USER=admin \\
      -e GRAFANA_PASSWORD=<password> \\
      -e GRAFANA_PATH_PREFIX="" \\
      locustio/locust \\
      -f /mnt/locust/load_test_locust.py \\
      --host http://grafana:3000 \\
      --users 20 --spawn-rate 1 --run-time 5m --headless \\
      --csv /mnt/locust/results_vm

Run from your local machine (against the public HTTPS endpoint):
    pip install locust
    GRAFANA_USER=admin \\
    GRAFANA_PASSWORD=<password> \\
    GRAFANA_PATH_PREFIX="" \\
    locust -f supabase/docker/scripts/load_test_locust.py \\
      --host https://dashboard.skywindsolutions.com \\
      --users 20 --spawn-rate 1 --run-time 5m --headless \\
      --csv results_remote

Environment variables:
    GRAFANA_USER         Grafana username (default: admin)
    GRAFANA_PASSWORD     Grafana password (required)
    GRAFANA_PATH_PREFIX  URL prefix for all API paths, e.g. "/grafana"
                         (default: "" — Grafana served at root)
    TEST_GROUP_A         group_id for tenant A (default: lt_group_alpha)
    TEST_GROUP_B         group_id for tenant B (default: lt_group_beta)
"""

import os
import time
import random

from locust import HttpUser, task, between

# ---------------------------------------------------------------------------
# Configuration — all overridable via environment variables
# ---------------------------------------------------------------------------

GRAFANA_USER = os.environ.get("GRAFANA_USER", "admin")
GRAFANA_PASSWORD = os.environ.get("GRAFANA_PASSWORD", "")

# Path prefix if Grafana is not served at root (e.g. "/grafana")
PREFIX = os.environ.get("GRAFANA_PATH_PREFIX", "").rstrip("/")

# Load-test group and location IDs (must match seed_load_test_data.sql)
TEST_GROUPS = [
    os.environ.get("TEST_GROUP_A", "lt_group_alpha"),
    os.environ.get("TEST_GROUP_B", "lt_group_beta"),
]

# Location IDs per group (index 0 = alpha, index 1 = beta)
VIS_LOCATIONS = ["LT_A_HARBOR", "LT_B_HARBOR"]
WIND_LOCATIONS = ["LT_A_OFFSHORE1", "LT_B_OFFSHORE1"]
TIDE_LOCATIONS = ["LT_A_TIDAL1", "LT_B_TIDAL1"]

# Grafana datasource UID (must match provisioning YAML)
DS_UID = "supabase-postgres"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    """Current time as epoch milliseconds (Grafana's expected from/to format)."""
    return int(time.time() * 1000)


def _build_query_body(raw_sql: str, fmt: str, from_ms: int, to_ms: int) -> dict:
    """Construct a Grafana backend /api/ds/query request body."""
    return {
        "queries": [
            {
                "datasource": {"type": "postgres", "uid": DS_UID},
                "rawSql": raw_sql,
                "format": fmt,
                "refId": "A",
                "intervalMs": 60000,
                "maxDataPoints": 1000,
            }
        ],
        "from": str(from_ms),
        "to": str(to_ms),
    }


# ---------------------------------------------------------------------------
# Locust user class
# ---------------------------------------------------------------------------

class GrafanaDashboardUser(HttpUser):
    """
    One virtual Grafana dashboard user.

    Authenticates on start, then continuously refreshes dashboard panels
    with a realistic pause between refreshes.  Queries target the load-test
    data so results are independent of the development seed data.
    """

    # Realistic pause between dashboard refreshes: 2–8 seconds
    wait_time = between(2, 8)

    def on_start(self) -> None:
        """Configure HTTP Basic Auth and assign this user to a tenant group."""
        if not GRAFANA_PASSWORD:
            raise ValueError(
                "GRAFANA_PASSWORD env var is required — set it before running Locust."
            )

        # Grafana supports HTTP Basic Auth on all API endpoints — simpler and more
        # reliable than the /api/login session-cookie approach.
        self.client.auth = (GRAFANA_USER, GRAFANA_PASSWORD)

        # Assign this user to a random tenant group so traffic is split across groups.
        idx = random.randint(0, len(TEST_GROUPS) - 1)
        self._group = TEST_GROUPS[idx]
        self._vis_loc = VIS_LOCATIONS[idx]
        self._wind_loc = WIND_LOCATIONS[idx]
        self._tide_loc = TIDE_LOCATIONS[idx]

    # -----------------------------------------------------------------------
    # Task: Visibility dashboard — 24-hour window
    # Weight 3: most frequently used dashboard type
    # -----------------------------------------------------------------------

    @task(3)
    def visibility_forecast_band(self) -> None:
        """POST panel query: 24h visibility forecast band (time-series)."""
        now = _now_ms()
        future_24h = now + 24 * 3_600_000

        sql = (
            "SELECT \"timestamp\" AS time, vismean, vislb, visub "
            "FROM public.forecast_data "
            f"WHERE vismean IS NOT NULL "
            f"  AND locid = '{self._vis_loc}' "
            f"  AND group_id = '{self._group}' "
            "  AND model = 'GFS' "
            "  AND \"timestamp\" >= NOW() "
            "  AND \"timestamp\" <= NOW() + INTERVAL '24 hours' "
            "ORDER BY \"timestamp\""
        )

        self.client.post(
            f"{PREFIX}/api/ds/query",
            json=_build_query_body(sql, "time_series", now, future_24h),
            name="panel:vis_forecast_band",
        )

    @task(2)
    def visibility_stat(self) -> None:
        """POST panel query: current visibility stat (single latest row)."""
        now = _now_ms()

        sql = (
            "SELECT \"timestamp\" AS time, vismean "
            "FROM public.forecast_data "
            f"WHERE vismean IS NOT NULL "
            f"  AND locid = '{self._vis_loc}' "
            f"  AND group_id = '{self._group}' "
            "  AND model = 'GFS' "
            "ORDER BY \"timestamp\" DESC LIMIT 1"
        )

        self.client.post(
            f"{PREFIX}/api/ds/query",
            json=_build_query_body(sql, "table", now, now + 3_600_000),
            name="panel:vis_stat",
        )

    @task(1)
    def visibility_location_table(self) -> None:
        """POST panel query: visibility summary table (DISTINCT ON per location)."""
        now = _now_ms()

        sql = (
            "SELECT DISTINCT ON (locid) locid, \"timestamp\", vismean "
            "FROM public.forecast_data "
            f"WHERE vismean IS NOT NULL "
            f"  AND group_id = '{self._group}' "
            "  AND model = 'GFS' "
            "ORDER BY locid, \"timestamp\" DESC"
        )

        self.client.post(
            f"{PREFIX}/api/ds/query",
            json=_build_query_body(sql, "table", now, now + 3_600_000),
            name="panel:vis_location_table",
        )

    # -----------------------------------------------------------------------
    # Task: Wind dashboard — 7-day window
    # Weight 2: second most common dashboard type
    # -----------------------------------------------------------------------

    @task(2)
    def wind_forecast_band(self) -> None:
        """POST panel query: 7-day wind speed forecast band (time-series)."""
        now = _now_ms()
        future_7d = now + 7 * 24 * 3_600_000

        sql = (
            "SELECT \"timestamp\" AS time, windspdmean, windspdlb, windspdub "
            "FROM public.forecast_data "
            f"WHERE windspdmean IS NOT NULL "
            f"  AND locid = '{self._wind_loc}' "
            f"  AND group_id = '{self._group}' "
            "  AND model = 'GFS' "
            "  AND \"timestamp\" >= NOW() "
            "  AND \"timestamp\" <= NOW() + INTERVAL '7 days' "
            "ORDER BY \"timestamp\""
        )

        self.client.post(
            f"{PREFIX}/api/ds/query",
            json=_build_query_body(sql, "time_series", now, future_7d),
            name="panel:wind_forecast_band",
        )

    @task(1)
    def wind_location_table(self) -> None:
        """POST panel query: wind speed summary table (DISTINCT ON per location)."""
        now = _now_ms()

        sql = (
            "SELECT DISTINCT ON (locid) locid, \"timestamp\", windspdmean "
            "FROM public.forecast_data "
            f"WHERE windspdmean IS NOT NULL "
            f"  AND group_id = '{self._group}' "
            "  AND model = 'GFS' "
            "ORDER BY locid, \"timestamp\" DESC"
        )

        self.client.post(
            f"{PREFIX}/api/ds/query",
            json=_build_query_body(sql, "table", now, now + 3_600_000),
            name="panel:wind_location_table",
        )

    # -----------------------------------------------------------------------
    # Task: Tide dashboard — 7-day window
    # Weight 1: least frequently accessed dashboard type
    # -----------------------------------------------------------------------

    @task(1)
    def tide_forecast_band(self) -> None:
        """POST panel query: 7-day tide forecast band (time-series)."""
        now = _now_ms()
        future_7d = now + 7 * 24 * 3_600_000

        sql = (
            "SELECT \"timestamp\" AS time, tidemean, tidelb, tideub "
            "FROM public.forecast_data "
            f"WHERE tidemean IS NOT NULL "
            f"  AND locid = '{self._tide_loc}' "
            f"  AND group_id = '{self._group}' "
            "  AND model = 'GFS' "
            "  AND \"timestamp\" >= NOW() "
            "  AND \"timestamp\" <= NOW() + INTERVAL '7 days' "
            "ORDER BY \"timestamp\""
        )

        self.client.post(
            f"{PREFIX}/api/ds/query",
            json=_build_query_body(sql, "time_series", now, future_7d),
            name="panel:tide_forecast_band",
        )
