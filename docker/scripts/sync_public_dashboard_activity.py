#!/usr/bin/env python3
"""
sync_public_dashboard_activity.py — Public Grafana dashboard access tracker.

Parses Caddy JSON access logs for public dashboard routes and inserts
one row per request into the ``public_dashboard_access_log`` PostgreSQL
table.  Designed to run periodically via cron (e.g. every 5 minutes).

Visitor IP addresses are never stored in raw form.  Instead, a
privacy-preserving hash is computed from the remote address + User-Agent
+ a daily-rotating salt.  This allows approximate unique-visitor counting
within a time window without storing personally identifiable information.

Usage
-----
    # From supabase/docker/ (same dir as .env):
    python3 scripts/sync_public_dashboard_activity.py

    # Or with explicit env vars:
    POSTGRES_HOST=localhost POSTGRES_PASSWORD=secret \
    CADDY_LOG_DIR=/var/log/caddy \
        python3 scripts/sync_public_dashboard_activity.py

Required environment / .env variables
-------------------------------------
POSTGRES_HOST           PostgreSQL host          (default: supabase-db)
POSTGRES_PORT           PostgreSQL port          (default: 5432)
POSTGRES_DB             PostgreSQL database      (default: postgres)
POSTGRES_USER           PostgreSQL user          (default: postgres)
POSTGRES_PASSWORD       PostgreSQL password      (required)

Optional environment / .env variables
-------------------------------------
CADDY_LOG_DIR           Directory containing public-dashboards.log
                        (default: /var/log/caddy)
SALT_SECRET             Secret used to generate daily visitor-id hashes
                        (default: auto-generated on first run — CHANGE for
                         production to ensure stable cross-restart hashing)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
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
log = logging.getLogger("sync_public_dashboard_activity")

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


POSTGRES_HOST = _cfg("POSTGRES_HOST", "supabase-db")
POSTGRES_PORT = _cfg("POSTGRES_PORT", "5432")
POSTGRES_DB = _cfg("POSTGRES_DB", "postgres")
POSTGRES_USER = _cfg("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = _cfg("POSTGRES_PASSWORD")
CADDY_LOG_DIR = Path(_cfg("CADDY_LOG_DIR", "/var/log/caddy"))
SALT_SECRET = _cfg("SALT_SECRET", "")  # empty = auto-generate per-run

if not POSTGRES_PASSWORD:
    log.error("POSTGRES_PASSWORD is required")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Caddy log file access — resolve host or container path
# ---------------------------------------------------------------------------

# docker-compose.caddy.yml mounts ./volumes/proxy/caddy/logs -> /var/log/caddy
# On the host this is at: <DOCKER_DIR>/volumes/proxy/caddy/logs/
HOST_LOG_DIR = DOCKER_DIR / "volumes" / "proxy" / "caddy" / "logs"
# Inside the container this is at: /var/log/caddy/
CONTAINER_LOG_DIR = CADDY_LOG_DIR

# Prefer the host-side path if it exists (direct volume mount)
if HOST_LOG_DIR.is_dir():
    CADDY_LOG_DIR = HOST_LOG_DIR
    log.info("Using host-side log directory: %s", CADDY_LOG_DIR)
else:
    log.info("Using container log directory: %s", CADDY_LOG_DIR)

CADDY_LOG_FILE = CADDY_LOG_DIR / "public-dashboards.log"

# State file tracks the last processed log position (byte offset).
# Stored alongside the log file so it survives container restarts.
STATE_FILE = CADDY_LOG_DIR / ".sync_public_dashboard_state"


def _resolve_caddy_log() -> tuple[Path | None, bool]:
    """Resolve the path to the Caddy public-dashboards log file.

    Returns (path_or_None, use_docker_bridge) where:
    - path_or_None: the log file path if accessible, or None
    - use_docker_bridge: True if the file was piped through docker exec
      (meaning we should write it to a temp location for processing)

    Checks, in order:
    1. Direct host-side path (volume mounted from ./volumes/proxy/caddy/logs)
    2. Host-side path with sudo read (permissions may require root)
    3. Docker container path (via docker exec)
    """
    # 1. Direct path check — readable by our user
    if CADDY_LOG_FILE.is_file():
        try:
            with open(CADDY_LOG_FILE, "rb") as fh:
                fh.read(1)
            log.info("Using Caddy log at %s (direct read)", CADDY_LOG_FILE)
            return CADDY_LOG_FILE, False
        except PermissionError:
            log.warning("Log file exists but not readable directly")

    # 2. Try reading via sudo (host-side mount is root-owned)
    try:
        result = subprocess.run(
            ["sudo", "cat", str(CADDY_LOG_FILE)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout:
            log.info("Read Caddy log via sudo at %s", CADDY_LOG_FILE)
            # Write to a temp copy so we can seek/tell on it
            tmp = Path("/tmp") / "caddy-public-dashboards.log"
            with open(tmp, "w") as fh:
                fh.write(result.stdout)
            return tmp, True
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("Cannot read Caddy log via sudo: %s", exc)

    # 3. Try reading via Docker container
    log.warning(
        "Log file not found or unreadable at %s — trying Docker container read",
        CADDY_LOG_FILE,
    )
    try:
        result = subprocess.run(
            [
                "sudo", "docker", "exec", "supabase-caddy",
                "cat", str(CADDY_LOG_FILE),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            log.info("Read Caddy log via Docker container")
            tmp = Path("/tmp") / "caddy-public-dashboards.log"
            with open(tmp, "w") as fh:
                fh.write(result.stdout)
            return tmp, True
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("Cannot read Caddy log via Docker: %s", exc)

    return None, False


def _sudo_read_state_file() -> int:
    """Read the state file from the host mount via sudo.

    Returns the byte offset, or 0 if unavailable.
    """
    try:
        result = subprocess.run(
            ["sudo", "cat", str(STATE_FILE)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            val = int(result.stdout.strip())
            log.info("Read state file via sudo: %d", val)
            return val
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 0


def _docker_read_state_file() -> int:
    """Read the state file from the Docker container.

    Returns the byte offset, or 0 if unavailable.
    """
    try:
        result = subprocess.run(
            [
                "sudo", "docker", "exec", "supabase-caddy",
                "cat", str(STATE_FILE),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            val = int(result.stdout.strip())
            log.info("Read state file from Docker container: %d", val)
            return val
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 0


def _write_state_file(pos: int) -> None:
    """Write the last-processed byte offset to the state file.

    Attempts local write first, falls back to sudo, then Docker exec.
    """
    try:
        # Try local write first (host-side mount)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            fh.write(str(pos))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        log.info("Wrote state file locally: %d", pos)
        return
    except (OSError, PermissionError) as exc:
        log.warning("Cannot write state file locally: %s", exc)

    # Fallback: write via sudo (host-side mount is root-owned)
    try:
        result = subprocess.run(
            ["sudo", "tee", str(STATE_FILE)],
            input=f"{pos}\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        log.info("Wrote state file via sudo: %d", pos)
        return
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        log.warning("Cannot write state file via sudo: %s", exc)

    # Fallback: write via Docker container
    try:
        subprocess.run(
            [
                "sudo", "docker", "exec", "-i", "supabase-caddy",
                "sh", "-c",
                f"mkdir -p $(dirname {STATE_FILE}) && echo '{pos}' > {STATE_FILE}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        log.info("Wrote state file in Docker container: %d", pos)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        log.warning("Cannot write state file via Docker: %s", exc)


def _read_state_file() -> int:
    """Read the last-processed byte offset from the state file.

    Tries, in order:
    1. Direct local read
    2. sudo read (host-side mount is root-owned)
    3. Docker container read
    """
    # 1. Direct local read
    try:
        if STATE_FILE.is_file():
            with open(STATE_FILE) as fh:
                return int(fh.read().strip())
    except (OSError, ValueError):
        pass

    # 2. Read via sudo
    val = _sudo_read_state_file()
    if val:
        return val

    # 3. Read via Docker container
    val = _docker_read_state_file()
    if val:
        return val

    return 0


# ---------------------------------------------------------------------------
# Visitor ID hashing (privacy-preserving)
# ---------------------------------------------------------------------------

def _daily_salt() -> bytes:
    """Generate a deterministic daily salt from SALT_SECRET + today's date.

    This produces the same salt for an entire calendar day (UTC), allowing
    visitor-hash comparison within a day.  The salt changes daily, making
    it infeasible to track visitors across days.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = (SALT_SECRET or "auto-generated-salt").encode()
    return hmac.digest(base, date_str.encode(), "sha256")


def compute_visitor_hash(remote_addr: str, user_agent: str) -> str:
    """Compute a privacy-preserving hash for a visitor.

    Combines remote_addr + user_agent with a daily-rotating HMAC salt.
    The result is a hex string that can be used to approximate unique
    visitors within a day, without storing raw IP addresses.
    """
    salt = _daily_salt()
    payload = f"{remote_addr}|{user_agent or ''}".encode()
    return hmac.digest(salt, payload, "sha256").hex()


# ---------------------------------------------------------------------------
# Caddy log parsing
# ---------------------------------------------------------------------------

# Slug-to-display-name mapping for known public dashboards.
# Keep in sync with the Caddyfile rewrite rules.
SLUG_MAP: dict[str, str] = {
    "Port-Richey": "Port-Richey",
    "PortRichey": "Port-Richey",
    "Hail-Forecast": "Hail-Forecast",
    "27539075035b4a64a0a9f2bebfcbd64d": "Port-Richey",
    "dcab3baeba8c468fa28339212f51818b": "Hail-Forecast",
}


def extract_dashboard_slug(request_path: str) -> str:
    """Extract a human-readable dashboard identifier from the request path.

    Strips the /public-dashboards/ or /api/public/dashboards/ prefix and
    maps known tokens to friendly names.  Falls back to the raw slug.
    """
    # Remove leading /api/public/dashboards/ or /public-dashboards/
    for prefix in ("/api/public/dashboards/", "/public-dashboards/"):
        if request_path.startswith(prefix):
            slug = request_path[len(prefix):]
            # Strip any trailing subpath (e.g., /api/public/dashboards/<token>/panels/...)
            slug = slug.split("/")[0]
            return SLUG_MAP.get(slug, slug)
    return "unknown"


def parse_caddy_log_line(line: str) -> dict | None:
    """Parse a single Caddy JSON log line into a structured record.

    Caddy JSON format (selected fields):
    {
      "ts": 1712345678.123,
      "request": {
        "method": "GET",
        "uri": "/public-dashboards/Port-Richey",
        "headers": {
          "User-Agent": ["..."],
          "Referer": ["..."]
        },
        "remote_addr": "203.0.113.42:54321"
      },
      "status": 200,
      "resp_headers": { ... }
    }
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    request = data.get("request") or {}

    # Extract timestamp
    ts = data.get("ts")
    if ts is None:
        return None
    accessed_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)

    # Only capture GET requests (actual page loads, not API calls/panel queries)
    method = request.get("method", "")
    if method != "GET":
        return None

    # Extract request URI — only capture the top-level public dashboard page itself
    uri = request.get("uri", "")
    if not uri or not uri.startswith("/public-dashboards/"):
        return None
    # Skip sub-resource requests (JS, CSS, images, fonts — anything with an extension)
    path_only = uri.split("?", 1)[0]
    last_segment = path_only.rsplit("/", 1)[-1]
    if "." in last_segment:
        return None

    # Extract remote address (strip port)
    remote_addr = request.get("remote_addr", "")
    if ":" in remote_addr:
        remote_addr = remote_addr.rsplit(":", 1)[0]

    # Extract headers
    headers = request.get("headers") or {}
    user_agent = (headers.get("User-Agent") or [""])[0]
    referrer = (headers.get("Referer") or [""])[0]

    # Extract response status — Caddy logs this at the top level, not inside resp
    status = data.get("status", 0)
    # Only record successful page loads
    if status != 200:
        return None

    return {
        "accessed_at": accessed_at,
        "dashboard_slug": extract_dashboard_slug(uri),
        "visitor_hash": compute_visitor_hash(remote_addr, user_agent),
        "user_agent": user_agent[:512] if user_agent else None,
        "referrer": referrer[:1024] if referrer else None,
        "request_path": uri[:1024],
        "status_code": int(status),
    }


# ---------------------------------------------------------------------------
# Database connection helpers
# ---------------------------------------------------------------------------

def _get_db_conninfo() -> str:
    """Resolve the correct PostgreSQL connection string.

    Tries to resolve the supabase-db container IP when running host-side,
    matching the pattern used in sync_grafana_activity.py.
    """
    host = POSTGRES_HOST

    # If host looks like a Docker container name, try resolving its IP
    if host in ("supabase-db", "db", "localhost"):
        try:
            result = subprocess.run(
                [
                    "sudo", "docker", "inspect", "supabase-db",
                    "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            ip = result.stdout.strip().split("\n")[0]
            if ip:
                host = ip
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    return (
        f"host={host} port={POSTGRES_PORT} dbname={POSTGRES_DB} "
        f"user={POSTGRES_USER} password={POSTGRES_PASSWORD}"
    )


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def sync_public_dashboard_activity() -> int:
    """Parse new entries from the Caddy public dashboard access log and
    insert them into the database.  Returns the number of rows inserted.
    """
    log_file, _ = _resolve_caddy_log()
    if log_file is None:
        log.error("Cannot locate Caddy public-dashboard log file")
        return 0

    last_pos = _read_state_file()
    file_size = log_file.stat().st_size

    if file_size <= last_pos:
        log.info("No new log entries (position %d / %d bytes)", last_pos, file_size)
        return 0

    # If the log was rotated (file shrunk), start from the beginning
    if file_size < last_pos:
        log.info("Log file rotated — resetting position from %d to 0", last_pos)
        last_pos = 0

    log.info("Processing Caddy log from position %d to %d", last_pos, file_size)

    conninfo = _get_db_conninfo()
    inserted = 0
    parsed = 0
    skipped = 0

    with open(log_file) as fh:
        fh.seek(last_pos)
        records: list[tuple] = []

        for line in fh:
            line = line.strip()
            if not line:
                continue

            record = parse_caddy_log_line(line)
            if record is None:
                skipped += 1
                continue

            records.append((
                record["accessed_at"],
                record["dashboard_slug"],
                record["visitor_hash"],
                record["user_agent"],
                record["referrer"],
                record["request_path"],
                record["status_code"],
            ))
            parsed += 1

            # Batch insert every 100 records to reduce round-trips
            if len(records) >= 100:
                inserted += _bulk_insert(conninfo, records)
                records = []

        # Insert remaining records
        if records:
            inserted += _bulk_insert(conninfo, records)

        # Update state file with current position
        new_pos = fh.tell()
        _write_state_file(new_pos)

    log.info(
        "Parsed: %d, Inserted: %d, Skipped: %d",
        parsed, inserted, skipped,
    )
    return inserted


def _bulk_insert(conninfo: str, records: list[tuple]) -> int:
    """Insert a batch of records into public_dashboard_access_log."""
    if not records:
        return 0
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO public.public_dashboard_access_log
                    (accessed_at, dashboard_slug, visitor_hash,
                     user_agent, referrer, request_path, status_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                records,
            )
        conn.commit()
    return len(records)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting public dashboard activity sync")
    try:
        count = sync_public_dashboard_activity()
        log.info("Sync complete: %d new access records", count)
    except psycopg.Error as exc:
        log.error("Database error: %s", exc)
        sys.exit(1)
    except OSError as exc:
        log.error("File/IO error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
