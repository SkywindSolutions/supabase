#!/usr/bin/env python3
"""
Skywind SMS Alert Service.

A lightweight HTTP API and background checker that sends SMS alerts when tide
values cross user-configured thresholds.  Designed to run as a Docker sidecar
alongside the Grafana + Supabase stack.

Architecture
------------
- HTTP API (Flask) exposes CRUD endpoints at ``/api/alerts/subscriptions``.
- Authentication: validates requests by forwarding the caller's Grafana session
  cookie to Grafana's ``/api/user`` endpoint, extracting username and org role.
- Background thread polls the ``forecast_data`` table every CHECK_INTERVAL_SECONDS,
  compares current interpolated tide values against active subscriptions, and
  sends SMS via Twilio when a threshold is breached.

Required environment variables
------------------------------
POSTGRES_HOST          PostgreSQL hostname             (default: supabase-db)
POSTGRES_PORT          PostgreSQL port                 (default: 5432)
POSTGRES_DB            PostgreSQL database name        (default: postgres)
POSTGRES_USER          PostgreSQL user                 (default: postgres)
POSTGRES_PASSWORD      PostgreSQL password             (required)
GRAFANA_INTERNAL_URL   Grafana URL reachable from this container (default: http://grafana:3000)
TWILIO_ACCOUNT_SID     Twilio Account SID              (required for SMS)
TWILIO_AUTH_TOKEN      Twilio Auth Token               (required for SMS)
TWILIO_FROM_NUMBER     Twilio phone number (E.164)     (required for SMS)
CHECK_INTERVAL_SECONDS Seconds between threshold checks (default: 300)
LOG_LEVEL              Logging verbosity               (default: INFO)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timezone
from typing import Any

import psycopg
from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "supabase-db")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
GRAFANA_INTERNAL_URL = os.environ.get("GRAFANA_INTERNAL_URL", "http://grafana:3000")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Reason: E.164 format validation — prevents malformed numbers reaching Twilio.
PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
# Reason: accept HH:MM (24-hour) for quiet-hours input.
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("alert_service")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db_conn() -> psycopg.Connection:
    """Open a new database connection."""
    return psycopg.connect(
        host=POSTGRES_HOST,
        port=int(POSTGRES_PORT),
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def db_execute(query: str, params: tuple | None = None) -> list[dict]:
    """Run a query and return rows as dicts."""
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        if cur.description is None:
            return []
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def db_mutate(query: str, params: tuple | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE and return affected row count."""
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# Grafana auth helper
# ---------------------------------------------------------------------------


def authenticate_grafana_request() -> dict[str, Any] | None:
    """Validate caller via Grafana session cookie.

    Forwards the ``grafana_session`` cookie to Grafana's ``/api/user`` endpoint.
    Returns the user dict on success, ``None`` on failure.
    """
    session_cookie = request.cookies.get("grafana_session") or request.cookies.get(
        "grafana_session_expiry"
    )

    # Reason: also support basic-auth forwarding for admin testing with curl.
    auth_header = request.headers.get("Authorization", "")

    if not session_cookie and not auth_header:
        return None

    grafana_url = f"{GRAFANA_INTERNAL_URL}/api/user"
    req = urllib.request.Request(grafana_url)

    if session_cookie:
        req.add_header("Cookie", f"grafana_session={session_cookie}")
    if auth_header:
        req.add_header("Authorization", auth_header)

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Grafana auth check failed: %s", exc)

    return None


def get_user_teams(grafana_user: dict) -> list[dict]:
    """Fetch the authenticated user's team memberships from Grafana."""
    session_cookie = request.cookies.get("grafana_session") or request.cookies.get(
        "grafana_session_expiry"
    )
    auth_header = request.headers.get("Authorization", "")

    grafana_url = f"{GRAFANA_INTERNAL_URL}/api/user/teams"
    req = urllib.request.Request(grafana_url)

    if session_cookie:
        req.add_header("Cookie", f"grafana_session={session_cookie}")
    if auth_header:
        req.add_header("Authorization", auth_header)

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Grafana teams fetch failed: %s", exc)

    return []


def extract_group_ids(teams: list[dict]) -> list[str]:
    """Derive group_id values from Grafana team names.

    Team names like 'Portrichey' or 'Boston Inner Harbor' map to group_ids like
    ``grp_portrichey`` or ``grp_boston`` in the database.  Rather than reverse-
    engineering the naming convention, we fetch all distinct group_ids from the DB
    and match against team names using a normalized substring check.
    """
    # Reason: skip meta-teams that don't correspond to customer groups.
    skip_teams = {"internal", "customers"}
    team_names = [
        t.get("name", "").strip().lower()
        for t in teams
        if t.get("name", "").strip().lower() not in skip_teams
    ]
    if not team_names:
        return []

    try:
        rows = db_execute(
            "SELECT DISTINCT group_id FROM forecast_data WHERE tidemean IS NOT NULL"
        )
    except Exception as exc:
        log.error("Failed to query group_ids: %s", exc)
        return []

    all_group_ids = [r["group_id"] for r in rows]
    matched: list[str] = []

    for gid in all_group_ids:
        # Reason: normalize both sides by stripping prefixes, underscores, and
        # spaces so 'grp_portrichey' matches team 'Port Richey' and
        # 'grp_cape_cod' matches 'Cape Cod Bay'.
        gid_norm = gid.replace("grp_", "").replace("_", "")
        for tname in team_names:
            tname_norm = tname.replace(" ", "")
            if gid_norm in tname_norm or tname_norm in gid_norm:
                matched.append(gid)
                break

    return matched


def get_allowed_groups(user: dict) -> list[str] | None:
    """Return the list of group_ids this user may access, or None for admins."""
    if _is_admin(user):
        return None  # Admins see all groups.
    teams = get_user_teams(user)
    return extract_group_ids(teams)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


def _error(msg: str, status: int = 400) -> tuple[Response, int]:
    return jsonify({"error": msg}), status


def _parse_time(val: str | None) -> dt_time | None:
    """Parse an HH:MM string into a ``datetime.time``, or return None."""
    if not val:
        return None
    val = val.strip()
    m = TIME_RE.match(val)
    if not m:
        return None
    return dt_time(int(m.group(1)), int(m.group(2)))


def _in_quiet_window(quiet_start: dt_time | str | None, quiet_end: dt_time | str | None) -> bool:
    """Return True if the current UTC time-of-day falls within quiet hours.

    Handles wrap-around midnight (e.g. 22:00 → 06:00).
    """
    if quiet_start is None or quiet_end is None:
        return False
    # Reason: DB may return time objects or strings depending on driver version.
    if isinstance(quiet_start, str):
        quiet_start = _parse_time(quiet_start)
    if isinstance(quiet_end, str):
        quiet_end = _parse_time(quiet_end)
    if quiet_start is None or quiet_end is None:
        return False

    now_t = datetime.now(timezone.utc).time()
    if quiet_start <= quiet_end:
        return quiet_start <= now_t <= quiet_end
    # Wraps midnight: e.g. 22:00 → 06:00
    return now_t >= quiet_start or now_t <= quiet_end


def _require_auth() -> dict[str, Any] | tuple[Response, int]:
    """Authenticate and return Grafana user dict, or an error response."""
    user = authenticate_grafana_request()
    if user is None:
        return _error("Authentication required", 401)
    return user


def _is_admin(user: dict) -> bool:
    role = (user.get("orgRole") or user.get("role") or "").lower()
    return user.get("isGrafanaAdmin", False) or role in ("admin", "editor")


@app.route("/api/alerts/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


@app.route("/api/alerts/subscriptions", methods=["GET"])
def list_subscriptions():
    """List alert subscriptions for the authenticated user."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    login = user.get("login", "")

    if _is_admin(user):
        # Admins see all subscriptions.
        rows = db_execute(
            "SELECT * FROM alert_subscriptions ORDER BY created_at DESC"
        )
    else:
        rows = db_execute(
            "SELECT * FROM alert_subscriptions WHERE grafana_login = %s ORDER BY created_at DESC",
            (login,),
        )

    # Reason: serialize datetime and time objects for JSON response.
    for row in rows:
        for key in ("last_triggered_at", "created_at", "updated_at"):
            if isinstance(row.get(key), datetime):
                row[key] = row[key].isoformat()
        for key in ("quiet_start", "quiet_end"):
            if isinstance(row.get(key), dt_time):
                row[key] = row[key].strftime("%H:%M")

    return jsonify(rows)


# ---------------------------------------------------------------------------
# SMS Terms & Consent page — public, no auth required.
# Provides the URL that Twilio needs as proof of consumer opt-in.
# ---------------------------------------------------------------------------

SMS_TERMS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skywind Solutions – SMS Alert Terms &amp; Consent</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         max-width: 700px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #222; }
  h1 { font-size: 1.5rem; }
  h2 { font-size: 1.15rem; margin-top: 1.5em; }
  ul { padding-left: 1.4em; }
  .updated { color: #666; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>Skywind Solutions &ndash; SMS Alert Terms &amp; Consent</h1>
<p class="updated">Last updated: April 7, 2026</p>

<h2>What You Are Signing Up For</h2>
<p>By enabling SMS tide alerts on the Skywind Solutions dashboard, you consent to
receive automated text messages when tide levels at your selected locations cross
thresholds you have configured. These are <strong>transactional, alert-only
messages</strong> &mdash; we will never send marketing or promotional content.</p>

<h2>Message Frequency</h2>
<p>Message frequency varies based on your alert configuration and tide conditions.
You control the minimum time between messages (cooldown) and optional quiet hours
when no messages will be sent.</p>

<h2>Message &amp; Data Rates</h2>
<p>Standard message and data rates from your wireless carrier may apply.</p>

<h2>How to Opt Out</h2>
<p>You can stop receiving SMS alerts at any time by:</p>
<ul>
  <li>Deleting or disabling your alert subscriptions in the Skywind dashboard, or</li>
  <li>Replying <strong>STOP</strong> to any message you receive from us.</li>
</ul>

<h2>Consent Collection</h2>
<p>Consent is collected electronically through our dashboard. When creating an SMS
alert subscription, the user must check a consent checkbox confirming they have
read and agree to these terms before the subscription can be saved. The timestamp
of consent is recorded alongside the subscription.</p>

<h2>Privacy</h2>
<p>Phone numbers are stored solely for the purpose of delivering tide alerts and
are not shared with third parties. See our full privacy policy for details.</p>
</body>
</html>"""


@app.route("/api/alerts/sms-terms", methods=["GET"])
def sms_terms():
    """Public SMS consent/terms page — no authentication required."""
    return SMS_TERMS_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/alerts/subscriptions", methods=["POST"])
def create_subscription():
    """Create a new alert subscription."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    data = request.get_json(silent=True)
    if not data:
        return _error("JSON body required")

    phone = (data.get("phone_number") or "").strip()
    location_id = (data.get("location_id") or "").strip()
    location_name = (data.get("location_name") or "").strip()
    alert_type = (data.get("alert_type") or "").strip().lower()
    group_id = (data.get("group_id") or "").strip()

    try:
        threshold = float(data.get("threshold_value", ""))
    except (ValueError, TypeError):
        return _error("threshold_value must be a number")

    if not PHONE_RE.match(phone):
        return _error("phone_number must be E.164 format (e.g. +15551234567)")

    if alert_type not in ("above", "below"):
        return _error("alert_type must be 'above' or 'below'")

    if not location_id:
        return _error("location_id is required")

    if not group_id:
        return _error("group_id is required")

    login = user.get("login", "")

    # Reason: non-admin users can only create subscriptions for groups they belong to.
    allowed = get_allowed_groups(user)
    if allowed is not None and group_id not in allowed:
        return _error("Not authorized for this group", 403)

    cooldown = int(data.get("cooldown_minutes", 60))

    quiet_start = _parse_time(data.get("quiet_start"))
    quiet_end = _parse_time(data.get("quiet_end"))
    if (quiet_start is None) != (quiet_end is None):
        return _error("Both quiet_start and quiet_end are required, or neither")

    if not data.get("sms_consent"):
        return _error("SMS consent is required")

    db_mutate(
        """INSERT INTO alert_subscriptions
               (group_id, grafana_login, phone_number, location_id,
                location_name, alert_type, threshold_value, cooldown_minutes,
                quiet_start, quiet_end, consented_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
        (group_id, login, phone, location_id, location_name, alert_type,
         threshold, cooldown, quiet_start, quiet_end),
    )

    log.info(
        "Created alert subscription: user=%s, location=%s, type=%s, threshold=%s",
        login, location_id, alert_type, threshold,
    )

    return jsonify({"message": "Subscription created"}), 201


@app.route("/api/alerts/subscriptions/<int:sub_id>", methods=["PUT"])
def update_subscription(sub_id: int):
    """Update an existing alert subscription."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    data = request.get_json(silent=True)
    if not data:
        return _error("JSON body required")

    login = user.get("login", "")

    # Verify ownership (admins can update any).
    existing = db_execute(
        "SELECT * FROM alert_subscriptions WHERE id = %s", (sub_id,)
    )
    if not existing:
        return _error("Subscription not found", 404)
    if not _is_admin(user) and existing[0]["grafana_login"] != login:
        return _error("Not authorized", 403)

    updates: list[str] = []
    params: list[Any] = []

    if "phone_number" in data:
        phone = (data["phone_number"] or "").strip()
        if not PHONE_RE.match(phone):
            return _error("phone_number must be E.164 format")
        updates.append("phone_number = %s")
        params.append(phone)

    if "alert_type" in data:
        at = (data["alert_type"] or "").strip().lower()
        if at not in ("above", "below"):
            return _error("alert_type must be 'above' or 'below'")
        updates.append("alert_type = %s")
        params.append(at)

    if "threshold_value" in data:
        try:
            tv = float(data["threshold_value"])
        except (ValueError, TypeError):
            return _error("threshold_value must be a number")
        updates.append("threshold_value = %s")
        params.append(tv)

    if "enabled" in data:
        updates.append("enabled = %s")
        params.append(bool(data["enabled"]))

    if "cooldown_minutes" in data:
        updates.append("cooldown_minutes = %s")
        params.append(int(data["cooldown_minutes"]))

    if "quiet_start" in data or "quiet_end" in data:
        qs = _parse_time(data.get("quiet_start"))
        qe = _parse_time(data.get("quiet_end"))
        updates.append("quiet_start = %s")
        params.append(qs)
        updates.append("quiet_end = %s")
        params.append(qe)

    if not updates:
        return _error("No fields to update")

    updates.append("updated_at = NOW()")
    params.append(sub_id)

    db_mutate(
        f"UPDATE alert_subscriptions SET {', '.join(updates)} WHERE id = %s",
        tuple(params),
    )

    log.info("Updated subscription %d for user %s", sub_id, login)
    return jsonify({"message": "Subscription updated"})


@app.route("/api/alerts/subscriptions/<int:sub_id>", methods=["DELETE"])
def delete_subscription(sub_id: int):
    """Delete an alert subscription."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    login = user.get("login", "")

    existing = db_execute(
        "SELECT * FROM alert_subscriptions WHERE id = %s", (sub_id,)
    )
    if not existing:
        return _error("Subscription not found", 404)
    if not _is_admin(user) and existing[0]["grafana_login"] != login:
        return _error("Not authorized", 403)

    db_mutate("DELETE FROM alert_subscriptions WHERE id = %s", (sub_id,))

    log.info("Deleted subscription %d for user %s", sub_id, login)
    return jsonify({"message": "Subscription deleted"})


@app.route("/api/alerts/locations", methods=["GET"])
def list_locations():
    """List available tide locations, optionally filtered by group_id."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    group_id = request.args.get("group_id", "").strip()

    allowed = get_allowed_groups(user)

    # Reason: non-admin users can only query locations within their allowed groups.
    if group_id:
        if allowed is not None and group_id not in allowed:
            return _error("Not authorized for this group", 403)
        rows = db_execute(
            """SELECT DISTINCT locid, locname, group_id
                 FROM forecast_data
                WHERE tidemean IS NOT NULL AND group_id = %s
                ORDER BY locname""",
            (group_id,),
        )
    elif allowed is None:
        rows = db_execute(
            """SELECT DISTINCT locid, locname, group_id
                 FROM forecast_data
                WHERE tidemean IS NOT NULL
                ORDER BY group_id, locname"""
        )
    else:
        return _error("group_id parameter required")

    return jsonify(rows)


@app.route("/api/alerts/groups", methods=["GET"])
def list_groups():
    """List available group_ids that have tide data."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user

    allowed = get_allowed_groups(user)

    if allowed is None:
        # Admin — return all groups.
        rows = db_execute(
            """SELECT DISTINCT group_id
                 FROM forecast_data
                WHERE tidemean IS NOT NULL
                ORDER BY group_id"""
        )
        return jsonify([r["group_id"] for r in rows])

    if not allowed:
        return jsonify([])

    # Reason: parameterized IN-clause via ANY to keep it injection-safe.
    rows = db_execute(
        """SELECT DISTINCT group_id
             FROM forecast_data
            WHERE tidemean IS NOT NULL AND group_id = ANY(%s)
            ORDER BY group_id""",
        (allowed,),
    )
    return jsonify([r["group_id"] for r in rows])


# ---------------------------------------------------------------------------
# Twilio SMS
# ---------------------------------------------------------------------------


def send_sms(to_number: str, body: str) -> bool:
    """Send an SMS via Twilio REST API.

    Returns True on success, False on failure.  Uses urllib (no third-party
    Twilio SDK) to keep dependencies minimal.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        log.warning("Twilio credentials not configured — skipping SMS to %s", to_number)
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    payload = urllib.parse.urlencode({
        "To": to_number,
        "From": TWILIO_FROM_NUMBER,
        "Body": body,
    }).encode()

    req = urllib.request.Request(url, data=payload, method="POST")
    # Reason: Twilio uses HTTP Basic Auth with SID:Token.
    import base64
    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                log.info("SMS sent to %s", to_number)
                return True
            log.error("Twilio returned status %d", resp.status)
    except urllib.error.HTTPError as exc:
        log.error("Twilio HTTP error %d: %s", exc.code, exc.read().decode()[:200])
    except (urllib.error.URLError, OSError) as exc:
        log.error("Twilio request failed: %s", exc)

    return False


# ---------------------------------------------------------------------------
# Background alert checker
# ---------------------------------------------------------------------------


def check_alerts() -> None:
    """Query active subscriptions and trigger SMS when thresholds are breached.

    Compares the latest interpolated tidemean value for each subscribed location
    against the subscription's threshold.  Respects cooldown_minutes to avoid
    repeat alerts.
    """
    log.debug("Running alert check cycle")

    try:
        subs = db_execute(
            """SELECT * FROM alert_subscriptions
                WHERE enabled = TRUE
                  AND (last_triggered_at IS NULL
                       OR last_triggered_at < NOW() - (cooldown_minutes || ' minutes')::INTERVAL)"""
        )
    except Exception as exc:
        log.error("Failed to fetch subscriptions: %s", exc)
        return

    if not subs:
        log.debug("No active subscriptions to check")
        return

    # Batch-fetch current tide values for all relevant (group_id, locid) pairs.
    pairs = {(s["group_id"], s["location_id"]) for s in subs}

    current_values: dict[tuple[str, str], float] = {}
    for group_id, loc_id in pairs:
        try:
            rows = db_execute(
                """SELECT tidemean FROM forecast_data
                    WHERE group_id = %s
                      AND locid = %s
                      AND tidemean IS NOT NULL
                      AND timestamp >= NOW()
                      AND timestamp <= NOW() + INTERVAL '7 days'
                    ORDER BY timestamp
                    LIMIT 1""",
                (group_id, loc_id),
            )
            if rows and rows[0]["tidemean"] is not None:
                current_values[(group_id, loc_id)] = float(rows[0]["tidemean"])
        except Exception as exc:
            log.error("Failed to query tide for %s/%s: %s", group_id, loc_id, exc)

    triggered_count = 0
    for sub in subs:
        key = (sub["group_id"], sub["location_id"])
        if key not in current_values:
            continue

        # Reason: skip this subscription if current time falls within quiet hours.
        if _in_quiet_window(sub.get("quiet_start"), sub.get("quiet_end")):
            continue

        value = current_values[key]
        threshold = sub["threshold_value"]
        breached = False

        if sub["alert_type"] == "above" and value >= threshold:
            breached = True
        elif sub["alert_type"] == "below" and value <= threshold:
            breached = True

        if not breached:
            continue

        loc_display = sub["location_name"] or sub["location_id"]
        direction = "above" if sub["alert_type"] == "above" else "below"
        units = "ft"  # NOTE: could be made dynamic if unit data is stored per-location.

        body = (
            f"Skywind Tide Alert: {loc_display} tide is {value:.2f} {units}, "
            f"which is {direction} your threshold of {threshold:.2f} {units}."
        )

        if send_sms(sub["phone_number"], body):
            try:
                db_mutate(
                    "UPDATE alert_subscriptions SET last_triggered_at = NOW() WHERE id = %s",
                    (sub["id"],),
                )
                triggered_count += 1
            except Exception as exc:
                log.error("Failed to update last_triggered_at for sub %d: %s", sub["id"], exc)

    if triggered_count:
        log.info("Alert check complete: %d alerts triggered", triggered_count)
    else:
        log.debug("Alert check complete: no thresholds breached")


def alert_checker_loop() -> None:
    """Background loop that periodically runs alert checks."""
    log.info("Alert checker started (interval=%ds)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            check_alerts()
        except Exception as exc:
            log.error("Unhandled error in alert checker: %s", exc)
        time.sleep(CHECK_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# Gunicorn server hook — starts the background checker once per worker.

_checker_started = False

def start_background_checker():
    global _checker_started
    if not _checker_started:
        _checker_started = True
        checker = threading.Thread(target=alert_checker_loop, daemon=True)
        checker.start()
        log.info("Background alert checker thread started.")


@app.before_request
def _ensure_checker():
    """Lazy-start the checker on first request (works under any WSGI server)."""
    start_background_checker()


if __name__ == "__main__":
    # Direct invocation fallback (dev only).
    start_background_checker()
    port = int(os.environ.get("ALERT_SERVICE_PORT", "8090"))
    log.info("Alert service starting on port %d (dev server)", port)
    app.run(host="0.0.0.0", port=port, debug=False)
