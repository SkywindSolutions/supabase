#!/usr/bin/env python3
"""
sync_grafana_activity.py — Grafana user activity tracker.

Polls the Grafana Admin API for all users, detects changes in each user's
``lastSeenAt`` timestamp, and records new activity events into the
``grafana_access_log`` PostgreSQL table.  Designed to run periodically via
cron (e.g. every 15 minutes).

Usage
-----
    # From supabase/docker/ (same dir as .env):
    python3 scripts/sync_grafana_activity.py

    # Or with explicit env vars:
    GRAFANA_ADMIN_USER=admin GRAFANA_ADMIN_PASSWORD=secret \
    POSTGRES_HOST=localhost POSTGRES_PASSWORD=secret \
        python3 scripts/sync_grafana_activity.py

Required environment / .env variables
-------------------------------------
GRAFANA_ADMIN_USER      Grafana admin username
GRAFANA_ADMIN_PASSWORD  Grafana admin password
POSTGRES_HOST           PostgreSQL host          (default: supabase-db)
POSTGRES_PORT           PostgreSQL port          (default: 5432)
POSTGRES_DB             PostgreSQL database      (default: postgres)
POSTGRES_USER           PostgreSQL user          (default: postgres)
POSTGRES_PASSWORD       PostgreSQL password      (required)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync_grafana_activity")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
DOCKER_DIR = SCRIPT_DIR.parent
ENV_FILE = DOCKER_DIR / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file (no interpolation)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


_file_env = _load_env_file(ENV_FILE)


def _cfg(key: str, default: str = "") -> str:
    """Return env var from environment first, then .env file, then default."""
    return os.environ.get(key) or _file_env.get(key) or default


GRAFANA_ADMIN_USER = _cfg("GRAFANA_ADMIN_USER", "admin")
GRAFANA_ADMIN_PASSWORD = _cfg("GRAFANA_ADMIN_PASSWORD")
POSTGRES_HOST = _cfg("POSTGRES_HOST", "supabase-db")
POSTGRES_PORT = _cfg("POSTGRES_PORT", "5432")
POSTGRES_DB = _cfg("POSTGRES_DB", "postgres")
POSTGRES_USER = _cfg("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = _cfg("POSTGRES_PASSWORD")

if not GRAFANA_ADMIN_PASSWORD:
    log.error("GRAFANA_ADMIN_PASSWORD is required")
    sys.exit(1)
if not POSTGRES_PASSWORD:
    log.error("POSTGRES_PASSWORD is required")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Grafana API helpers — stdlib only (no requests dependency)
# ---------------------------------------------------------------------------
import urllib.error
import urllib.request


def _docker_container_ip(container_name: str) -> str:
    """Get a Docker container's internal network IP address."""
    try:
        result = subprocess.run(
            [
                "sudo", "docker", "inspect", container_name,
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        ip = result.stdout.strip().split("\n")[0]
        if ip:
            return ip
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return ""


# Resolve container IPs for host-side execution (bypasses Docker DNS)
_grafana_ip = _docker_container_ip("supabase-grafana")
GRAFANA_URL = f"http://{_grafana_ip or 'localhost'}:3000"

_db_ip = _docker_container_ip("supabase-db")
if _db_ip and POSTGRES_HOST in ("supabase-db", "db", "localhost"):
    POSTGRES_HOST = _db_ip

# Basic auth header
_auth_bytes = f"{GRAFANA_ADMIN_USER}:{GRAFANA_ADMIN_PASSWORD}".encode()
_auth_header = "Basic " + base64.b64encode(_auth_bytes).decode()


def grafana_get(path: str) -> dict | list:
    """GET request to the Grafana API. Returns parsed JSON."""
    url = f"{GRAFANA_URL}/api{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": _auth_header,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------
def fetch_grafana_users() -> list[dict]:
    """Fetch all Grafana users via the admin search API."""
    page = 1
    per_page = 100
    all_users: list[dict] = []
    while True:
        data = grafana_get(f"/users/search?page={page}&perpage={per_page}")
        users = data.get("users", [])
        all_users.extend(users)
        if len(users) < per_page:
            break
        page += 1
    return all_users


def parse_grafana_timestamp(ts: str) -> datetime | None:
    """Parse Grafana ISO-8601 timestamp to a timezone-aware datetime."""
    if not ts:
        return None
    # Grafana returns e.g. "2026-04-19T10:30:00Z" or "0001-01-01T00:00:00Z"
    # The 0001 date means "never seen"
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.year <= 1:
        return None
    return dt


def sync_account_status(cur, users: list[dict]) -> int:
    """
    Reconcile public.customer_account_status.is_active against each user's
    Grafana isDisabled flag.  The Customer Access Statistics dashboard
    filters on this table, so account enable/disable state stays accurate
    even when the deactivate/reactivate script is bypassed (e.g. manual
    disable in the Grafana UI).

    Only customer logins are tracked (NOT internal admin/test_* accounts).
    Returns the number of status rows updated.
    """
    INTERNAL_LOGINS = {GRAFANA_ADMIN_USER, "dan", "matt"}
    updated = 0

    for user in users:
        login = user.get("login", "")
        if not login:
            continue
        # Skip admin/internal accounts; the dashboard already excludes these.
        if login.startswith("test_") or login in INTERNAL_LOGINS:
            continue
        is_active = not bool(user.get("isDisabled", False))
        try:
            cur.execute(
                """
                INSERT INTO public.customer_account_status (grafana_login, is_active)
                VALUES (%s, %s)
                ON CONFLICT (grafana_login) DO UPDATE
                    SET is_active = EXCLUDED.is_active, updated_at = NOW()
                """,
                (login, is_active),
            )
            if cur.rowcount > 0:
                updated += 1
        except psycopg.Error:
            # Table may not exist yet on old DBs; don't break the sync.
            log.warning(
                "Could not update customer_account_status for %s "
                "(run db/migrations/20260822000002_create_customer_account_status.sql)",
                login,
            )

    return updated


def sync_activity() -> int:
    """
    Compare Grafana user lastSeenAt with the most recent recorded value
    in grafana_access_log.  Insert rows for users whose activity changed.
    Also reconciles customer_account_status.is_active.
    Returns the number of new rows inserted.
    """
    users = fetch_grafana_users()
    log.info("Fetched %d users from Grafana API", len(users))

    conninfo = (
        f"host={POSTGRES_HOST} port={POSTGRES_PORT} dbname={POSTGRES_DB} "
        f"user={POSTGRES_USER} password={POSTGRES_PASSWORD}"
    )
    inserted = 0

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            # Reconcile account active/inactive state first (dashboard filter).
            status_updated = sync_account_status(cur, users)
            if status_updated:
                log.info("Synced %d customer_account_status row(s)", status_updated)

            for user in users:
                login = user.get("login", "")
                user_id = user.get("id", 0)
                last_seen = parse_grafana_timestamp(user.get("lastSeenAt", ""))

                if not last_seen:
                    continue  # Never logged in

                # Skip the admin account — we only care about customer/test accounts
                if user.get("isAdmin") and login == GRAFANA_ADMIN_USER:
                    continue

                # Check most recent recorded last_seen_at for this user
                cur.execute(
                    """
                    SELECT last_seen_at
                    FROM public.grafana_access_log
                    WHERE grafana_login = %s
                    ORDER BY recorded_at DESC
                    LIMIT 1
                    """,
                    (login,),
                )
                row = cur.fetchone()
                prev_last_seen = row[0] if row else None

                # Only insert if the timestamp actually changed
                if prev_last_seen and prev_last_seen == last_seen:
                    continue

                cur.execute(
                    """
                    INSERT INTO public.grafana_access_log
                        (grafana_login, grafana_user_id, last_seen_at)
                    VALUES (%s, %s, %s)
                    """,
                    (login, user_id, last_seen),
                )
                inserted += 1
                log.info("  Recorded activity: %s (last_seen=%s)", login, last_seen)

        conn.commit()

    return inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("Starting Grafana activity sync (%s)", GRAFANA_URL)
    try:
        count = sync_activity()
        log.info("Sync complete: %d new activity records", count)
    except urllib.error.URLError as exc:
        log.error("Cannot reach Grafana API at %s: %s", GRAFANA_URL, exc)
        sys.exit(1)
    except psycopg.Error as exc:
        log.error("Database error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
