#!/usr/bin/env python3
"""
update_tc_dashboard_panel.py
Task: P2.x — Tropical Cyclone Dashboard Panel Management

Reads tropical cyclone threat status from PostgreSQL and dynamically
updates a customer-facing Grafana dashboard JSON (per group) to include
or exclude the tropical cyclone information panel at the top.

When a threat exists, the panel (an official NHC forecast cone graphic with
storm metadata) is injected at position 0 of the dashboard's panels array.
When no threat exists, the panel is removed if present, and the remaining
panels are shifted up to reclaim the layout space.

Grafana provisioning polls the JSON file every 30 seconds
(updateIntervalSeconds: 30 in dashboards.yaml), so changes are visible
within half a minute.

Architecture
------------
This script is designed to run on the Grafana server (or wherever the
provisioned dashboard JSON files live).  It can be triggered:
  - After fetch_tropical_cyclones.py completes (on the ingestion server)
  - On a periodic cron schedule (e.g., every 5 minutes)
  - Via a webhook or CI/CD pipeline

The script requires:
  - Read access to PostgreSQL (for querying tropical_cyclone_status)
  - Write access to the dashboard JSON provisioning directory

Usage
-----
  python3 scripts/update_tc_dashboard_panel.py --group grp_hertz --production
  python3 scripts/update_tc_dashboard_panel.py --group grp_pascagoula --production

When --dashboard-filename is omitted, the group's production dashboard is
used automatically when known (see GROUP_DASHBOARD_FILENAMES); groups
without an entry fall back to the test dashboard (hurricane_test.json).

Options:
  --db-url          PostgreSQL connection string
                    default: postgresql://postgres:<password>@127.0.0.1:5432/postgres
  --dashboard-dir   Path to the Grafana provisioning dashboards directory
                    default: ./volumes/grafana/provisioning/dashboards
  --group           Target group ID (default: grp_pascagoula)
  --dashboard-filename  Dashboard filename override (defaults to the group's
                    known production dashboard, else hurricane_test.json)
  --production      Allow modification of production dashboards
  --dry-run         Print what would be done without modifying files
  --force-remove    Remove the TC panel unconditionally (for testing)

Examples
--------
  # Hertz (Tampa / New Orleans) production wind & tide dashboard:
  python3 scripts/update_tc_dashboard_panel.py --group grp_hertz --production

  # Print current threat status without modifying files:
  python3 scripts/update_tc_dashboard_panel.py --group grp_hertz --status

The script also supports being called with --status to just print the
current threat status without modifying any files.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The panel ID assigned to the tropical cyclone panel.
# Using a high value to avoid conflicts with existing panels (1-106 range).
TC_PANEL_ID = 200
TC_PANEL_TYPE = "text"
TC_PANEL_MARKER = "Tropical Cyclone Information"

# ---------------------------------------------------------------------------
# HTML rendering — builds the panel content from DB query results
# ---------------------------------------------------------------------------

STORM_DATA_SQL = """
SELECT
    storm_name,
    storm_type,
    ROUND(ABS(center_lat)::numeric, 1)::text AS center_lat_str,
    ROUND(ABS(center_lon)::numeric, 1)::text AS center_lon_str,
    wind_mph,
    COALESCE(movement, '--') AS movement,
    pressure_mb,
    advisory_number,
    TO_CHAR(advisory_time AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI UTC') AS advisory_time_str,
    nhc_graphic_url,
    nhc_info_url,
    threat_reason
FROM public.tropical_cyclone_status
WHERE group_id = 'grp_pascagoula'
  AND threat = true
  AND refreshed_at > NOW() - INTERVAL '6 hours'
ORDER BY advisory_time DESC NULLS LAST
LIMIT 1;
"""


def query_storm_data(conn_str: str | None, group_id: str) -> dict[str, Any] | None:
    """Query the latest threatening storm for a group.

    Returns a dict of storm fields or None if no threat is active.
    """
    if not conn_str:
        return None
    try:
        import psycopg2
    except ImportError:
        log.error("psycopg2 not available")
        return None
    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(STORM_DATA_SQL.replace('grp_pascagoula', group_id))
                row = cur.fetchone()
                if not row:
                    return None
                columns = [desc[0] for desc in cur.description]
                return dict(zip(columns, row))
    except Exception as exc:
        log.error("Failed to query storm data for %s: %s", group_id, exc)
        return None


def render_storm_html(storm: dict[str, Any] | None) -> str:
    """Build the full HTML string for the TC panel from storm data.

    When *storm* is None, returns the empty string (panel body blank).
    """
    if not storm:
        return ""

    name = storm.get("storm_name", "--")
    stype = storm.get("storm_type", "--")
    lat = storm.get("center_lat_str", "--")
    lon = storm.get("center_lon_str", "--")
    wind = storm.get("wind_mph")
    wind_str = f"{wind} mph" if wind is not None else "--"
    movement = storm.get("movement", "--")
    pressure = storm.get("pressure_mb")
    pressure_str = f"{pressure} mb" if pressure is not None else "--"
    adv = storm.get("advisory_number")
    adv_str = f"#{adv}" if adv is not None else "--"
    adv_time = storm.get("advisory_time_str", "--")
    graphic_url = storm.get("nhc_graphic_url", "")
    info_url = storm.get("nhc_info_url", "#")

    # Escape values for safe HTML embedding
    import html as htmlmod
    name_e = htmlmod.escape(name)
    stype_e = htmlmod.escape(stype)
    lat_e = htmlmod.escape(lat)
    lon_e = htmlmod.escape(lon)
    wind_e = htmlmod.escape(wind_str)
    movement_e = htmlmod.escape(movement)
    pressure_e = htmlmod.escape(pressure_str)
    adv_e = htmlmod.escape(adv_str)
    adv_time_e = htmlmod.escape(adv_time)
    graphic_url_e = htmlmod.escape(graphic_url)
    info_url_e = htmlmod.escape(info_url)

    return (
        '<h3 style="margin-bottom:8px;">'
        '<img src="https://www.nhc.noaa.gov/gifs/xml_logo_nhc.gif" '
        'alt="NHC" '
        'style="height:24px;vertical-align:middle;margin-right:8px;">'
        'National Hurricane Center &mdash; Tropical Cyclone Information'
        '</h3>'
        '<div style="display:flex;flex-wrap:wrap;gap:16px;">'
        '<div style="flex:1;min-width:300px;">'
        '<table style="width:100%;border-collapse:collapse;">'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Storm:</td>'
        f'<td style="padding:4px 8px;">{name_e}</td></tr>'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Classification:</td>'
        f'<td style="padding:4px 8px;">{stype_e}</td></tr>'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Location:</td>'
        f'<td style="padding:4px 8px;">{lat_e}&deg;N {lon_e}&deg;W</td></tr>'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Max Winds:</td>'
        f'<td style="padding:4px 8px;">{wind_e}</td></tr>'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Movement:</td>'
        f'<td style="padding:4px 8px;">{movement_e}</td></tr>'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Pressure:</td>'
        f'<td style="padding:4px 8px;">{pressure_e}</td></tr>'
        f'<tr><td style="padding:4px 8px;font-weight:bold;">Advisory:</td>'
        f'<td style="padding:4px 8px;">{adv_e} ({adv_time_e})</td></tr>'
        '</table>'
        f'<p style="margin-top:12px;">'
        f'<a href="{info_url_e}" '
        'target="_blank" rel="noopener">'
        'View Official NHC Public Advisory &rarr;</a></p>'
        '</div>'
        f'<div style="flex:2;min-width:400px;text-align:center;">'
        f'<a href="{info_url_e}" '
        'target="_blank" rel="noopener">'
        f'<img src="{graphic_url_e}" '
        f'alt="NHC Forecast Cone &mdash; {name_e}" '
        'style="max-width:100%;height:auto;'
        'border:1px solid #ccc;border-radius:4px;">'
        '</a>'
        '<p style="font-size:0.85em;color:#888;margin-top:4px;">'
        'Official NHC Forecast Cone &mdash; '
        f'<a href="{info_url_e}" '
        'target="_blank" rel="noopener">'
        'View interactive version</a></p>'
        '</div>'
        '</div>'
        '<hr style="margin:16px 0 4px 0;">'
    )


def build_tc_panel(html_content: str) -> dict[str, Any]:
    """Build the Grafana Text panel with pre-rendered HTML content."""
    return {
        "datasource": {
            "type": "postgres",
            "uid": "supabase-postgres",
        },
        "description": (
            "Official National Hurricane Center tropical cyclone information. "
            "Panel is dynamically added/removed based on threat status."
        ),
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "mappings": [],
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "green", "value": None}],
                },
            },
            "overrides": [],
        },
        "gridPos": {"h": 16, "w": 24, "x": 0, "y": 0},
        "id": TC_PANEL_ID,
        "options": {
            "content": html_content,
            "mode": "html",
        },
        "pluginVersion": "",
        "title": TC_PANEL_MARKER,
        "type": TC_PANEL_TYPE,
    }


def has_tc_panel(panels: list[dict]) -> bool:
    """Check if the TC panel already exists in the panels list."""
    return any(
        p.get("title") == TC_PANEL_MARKER and p.get("id") == TC_PANEL_ID
        for p in panels
    )


def _shift_panels_down(panels: list[dict], shift_y: int) -> None:
    """Shift all panels down by shift_y rows."""
    for panel in panels:
        g = panel.get("gridPos", {})
        g["y"] = g.get("y", 0) + shift_y
        if "panels" in panel:
            _shift_panels_down(panel["panels"], shift_y)


def _shift_panels_up(panels: list[dict], shift_y: int) -> None:
    """Shift all panels up by shift_y rows (for removal)."""
    for panel in panels:
        g = panel.get("gridPos", {})
        new_y = g.get("y", 0) - shift_y
        g["y"] = max(0, new_y)
        if "panels" in panel:
            _shift_panels_up(panel["panels"], shift_y)


def add_tc_panel(dashboard: dict, html_content: str) -> bool:
    """Insert the TC panel at the top of the dashboard.

    The panel's content is the pre-rendered HTML generated by
    ``render_storm_html()`` so no template-variable magic is needed.

    Returns True if the panel was added, False if it already existed.
    """
    panels = dashboard.get("panels", [])
    if has_tc_panel(panels):
        return False

    panel = build_tc_panel(html_content)
    panel_h = panel["gridPos"]["h"]

    _shift_panels_down(panels, panel_h)
    panels.insert(0, panel)
    dashboard["panels"] = panels

    log.info("Added TC panel at top of dashboard (height=%d)", panel_h)
    return True


def update_tc_panel_content(dashboard: dict, html_content: str) -> bool:
    """Update the HTML content of an existing TC panel.

    Returns True if the content was updated, False if panel not found.
    """
    for p in dashboard.get("panels", []):
        if p.get("id") == TC_PANEL_ID:
            old_len = len(p.get("options", {}).get("content", ""))
            p.setdefault("options", {})["content"] = html_content
            log.info(
                "Updated TC panel content (%d chars → %d chars)",
                old_len, len(html_content),
            )
            return True
    return False


def remove_tc_panel(dashboard: dict) -> bool:
    """Remove the TC panel from the dashboard and shift panels up.

    Returns True if the panel was removed, False if it wasn't present.
    """
    panels = dashboard.get("panels", [])
    tc_panels = [
        p for p in panels
        if p.get("title") == TC_PANEL_MARKER and p.get("id") == TC_PANEL_ID
    ]

    if not tc_panels:
        log.info("TC panel not present in dashboard (nothing to remove)")
        return False

    tc_h = tc_panels[0]["gridPos"].get("h", 16)

    dashboard["panels"] = [
        p for p in panels
        if not (p.get("title") == TC_PANEL_MARKER and p.get("id") == TC_PANEL_ID)
    ]

    _shift_panels_up(dashboard["panels"], tc_h)

    log.info("Removed TC panel (height=%d) and shifted panels up", tc_h)
    return True


# ---------------------------------------------------------------------------
# Database query
# ---------------------------------------------------------------------------

def query_threat_status(
    conn_str: str | None,
    group_id: str,
) -> list[dict[str, Any]]:
    """Query the tropical_cyclone_status table for active threats.

    Returns a list of storm data dicts (empty list if no threats).
    """
    if not conn_str:
        return []

    try:
        import psycopg2
    except ImportError:
        log.error("psycopg2 not available")
        return []

    sql = """
        SELECT
            atcf_id, storm_name, storm_type,
            center_lat, center_lon,
            wind_mph, movement, pressure_mb,
            advisory_number,
            TO_CHAR(advisory_time AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI UTC') AS advisory_time_str,
            nhc_graphic_url, nhc_info_url,
            threat_reason
        FROM public.tropical_cyclone_status
        WHERE group_id = %s
          AND threat = true
          AND refreshed_at > NOW() - INTERVAL '6 hours'
        ORDER BY advisory_time DESC NULLS LAST;
    """

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id,))
                rows = cur.fetchall()
                if not rows:
                    return []

                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        log.error("Failed to query threat status for %s: %s", group_id, exc)
        return []


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def load_env(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file."""
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def build_conn_str(db_url: str | None) -> str | None:
    """Build a PostgreSQL connection string.

    When running on the Grafana server (outside the Docker network), the
    Supabase connection pooler (supavisor) proxies port 5432 and requires
    the tenant ID as part of the username:

        postgres.<tenant_id>

    The tenant ID is read from POOLER_TENANT_ID in the .env file.
    """
    if db_url:
        return db_url
    env_path = Path(__file__).resolve().parent.parent / ".env"
    env = load_env(env_path)
    password = env.get("POSTGRES_PASSWORD", "postgres")
    tenant_id = env.get("POOLER_TENANT_ID", "")
    if tenant_id:
        return (
            f"postgresql://postgres.{tenant_id}:{password}"
            f"@127.0.0.1:5432/postgres"
        )
    return f"postgresql://postgres:{password}@127.0.0.1:5432/postgres"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Production dashboard filenames — modifying these requires --production
# ---------------------------------------------------------------------------
PRODUCTION_FILENAMES: set[str] = {
    "wind_visibility.json",
    "wind.json",
    "visibility.json",
    "tide.json",
    "tide_wind.json",
    "wind_tide.json",
}

# Default production dashboard filename per group.  Used when
# --dashboard-filename is not provided, so the panel manager can be run with
# just `--group <id>` (plus --production) for a known customer dashboard.
# Groups not listed here fall back to the test dashboard (hurricane_test.json).
GROUP_DASHBOARD_FILENAMES: dict[str, str] = {
    "grp_pascagoula": "wind_visibility.json",
    "grp_hertz": "wind_tide.json",
    "grp_gator": "wind_tide.json",
    "grp_manson": "wind_tide.json",
    "grp_portrichey": "tide.json",
}

# Public dashboard filenames per group.  When a group has an externally
# shared (public) dashboard, the panel manager also keeps the TC panel in
# sync there (same threat row drives both the customer and public views).
# The value is the filename under the provisioning `public/` directory.
PUBLIC_DASHBOARD_FILENAMES: dict[str, str] = {
    "grp_portrichey": "port_richey_tide_public.json",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage tropical cyclone panel in Grafana dashboards",
    )
    parser.add_argument(
        "--db-url",
        help="PostgreSQL connection string",
    )
    parser.add_argument(
        "--dashboard-dir",
        default=None,
        help="Path to Grafana provisioning dashboards directory",
    )
    parser.add_argument(
        "--group",
        default="grp_pascagoula",
        help="Target customer group ID (default: grp_pascagoula)",
    )
    parser.add_argument(
        "--dashboard-filename",
        default=None,
        help=(
            "Dashboard filename to manage.  Defaults to the group's production "
            "dashboard when known (see GROUP_DASHBOARD_FILENAMES), otherwise "
            "the test dashboard (hurricane_test.json).  Use --production to "
            "target production dashboards."
        ),
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help=(
            "Allow modification of production dashboards.  Without this flag, "
            "the script refuses to modify production dashboard filenames "
            "(wind_visibility.json, wind.json, visibility.json, tide.json, "
            "tide_wind.json)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without modifying files",
    )
    parser.add_argument(
        "--force-remove",
        action="store_true",
        help="Remove the TC panel unconditionally (for testing)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current threat status without modifying files",
    )
    args = parser.parse_args()

    # Resolve the dashboard filename: explicit flag wins, otherwise use the
    # group's known production dashboard, else the test dashboard.
    dashboard_filename = (
        args.dashboard_filename
        or GROUP_DASHBOARD_FILENAMES.get(args.group, "hurricane_test.json")
    )

    # Safety check: refuse to modify production dashboards unless --production.
    # --status is read-only, so it is exempt from this guard.
    if (
        not args.status
        and dashboard_filename in PRODUCTION_FILENAMES
        and not args.production
    ):
        log.error(
            "Refusing to modify production dashboard '%s'. "
            "Use --production to allow, or omit --dashboard-filename to use "
            "the default test dashboard (hurricane_test.json).",
            dashboard_filename,
        )
        sys.exit(1)

    # Determine the dashboard directory
    script_dir = Path(__file__).resolve().parent
    if args.dashboard_dir:
        dashboard_dir = Path(args.dashboard_dir)
    else:
        # Default: relative to script location
        dashboard_dir = script_dir.parent / "volumes" / "grafana" / "provisioning" / "dashboards"

    if not dashboard_dir.exists():
        log.error("Dashboard directory not found: %s", dashboard_dir)
        log.error("Use --dashboard-dir to specify the correct path")
        sys.exit(1)

    conn_str = build_conn_str(args.db_url)

    # Query threat status
    threats = query_threat_status(conn_str, args.group)

    if args.status:
        if threats:
            print(f"Active threats for {args.group}:")
            for t in threats:
                print(f"  {t['storm_name']} ({t['atcf_id']})")
                print(f"    Type: {t['storm_type']}")
                print(f"    Center: {t['center_lat']}°N, {t['center_lon']}°W")
                print(f"    Winds: {t['wind_mph']} mph")
                print(f"    Advisory: #{t['advisory_number']} ({t['advisory_time_str']})")
                print(f"    Graphic: {t.get('nhc_graphic_url', 'N/A')}")
                print(f"    Info: {t.get('nhc_info_url', 'N/A')}")
        else:
            print(f"No active threats for {args.group}")
        return

    # Resolve the dashboard paths to manage:
    #   1. The group's (customer) production dashboard.
    #   2. The group's public dashboard, when one is registered
    #      (PUBLIC_DASHBOARD_FILENAMES) — the same threat row drives both.
    target_paths: list[Path] = [
        dashboard_dir / "groups" / args.group / dashboard_filename,
    ]
    public_filename = (
        PUBLIC_DASHBOARD_FILENAMES.get(args.group)
        if not args.dashboard_filename
        else None
    )
    if public_filename:
        public_path = dashboard_dir / "public" / public_filename
        if public_path.exists():
            target_paths.append(public_path)
            log.info("Public dashboard also managed: %s", public_path)
        else:
            log.warning(
                "Public dashboard registered for %s but not found: %s",
                args.group, public_path,
            )

    for dashboard_path in target_paths:
        apply_tc_panel_update(
            dashboard_path,
            conn_str=conn_str,
            group_id=args.group,
            force_remove=args.force_remove,
            dry_run=args.dry_run,
        )


def apply_tc_panel_update(
    dashboard_path: Path,
    conn_str: str | None,
    group_id: str,
    force_remove: bool = False,
    dry_run: bool = False,
) -> None:
    """Add/remove/refresh the TC panel on a single dashboard file.

    Reads the dashboard JSON at *dashboard_path*, evaluates the current
    threat for *group_id* from tropical_cyclone_status, and applies the
    appropriate panel change.  Writes atomically when modified.
    """
    if not dashboard_path.exists():
        log.error("Dashboard file not found: %s", dashboard_path)
        log.error(
            "Use --dashboard-filename to specify a different file, "
            "or check that the file exists."
        )
        sys.exit(1)

    log.info("Dashboard: %s", dashboard_path)

    # Read the dashboard JSON
    try:
        with open(dashboard_path) as fh:
            dashboard = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read dashboard JSON: %s", exc)
        sys.exit(1)

    # Query the latest storm data for HTML rendering
    storm = query_storm_data(conn_str, group_id)
    has_threat = storm is not None and not force_remove
    panel_present = has_tc_panel(dashboard.get("panels", []))

    log.info(
        "Threat: %s, TC panel present: %s, force_remove: %s",
        has_threat, panel_present, force_remove,
    )

    modified = False

    if force_remove or not has_threat:
        # No threat — remove panel if present
        if panel_present:
            if dry_run:
                log.info("[DRY-RUN] Would remove TC panel from %s", dashboard_path.name)
            else:
                remove_tc_panel(dashboard)
                modified = True
        else:
            log.info("No threat and no panel — dashboard unchanged")
    else:
        # Threat exists — render HTML and add/update panel
        html_content = render_storm_html(storm)
        content_len = len(html_content)

        if not panel_present:
            if dry_run:
                log.info("[DRY-RUN] Would add TC panel (%d chars) to %s",
                         content_len, dashboard_path.name)
            else:
                add_tc_panel(dashboard, html_content)
                modified = True
                log.info("TC panel added with %d chars of rendered HTML", content_len)
        else:
            # Panel exists — update its content with fresh data
            if dry_run:
                log.info("[DRY-RUN] Would update TC panel content (%d chars)",
                         content_len)
            else:
                updated = update_tc_panel_content(dashboard, html_content)
                if updated:
                    modified = True
                    log.info("TC panel content refreshed (%d chars)", content_len)
                else:
                    log.warning("TC panel found by marker but not by ID — unexpected")

    # Write if modified
    if modified:
        # Bump version number
        dashboard["version"] = dashboard.get("version", 1) + 1

        if dry_run:
            log.info("[DRY-RUN] Would write updated dashboard JSON")
            print(json.dumps(dashboard, indent=2))
        else:
            # Write atomically: write to temp file, then rename
            tmp_path = dashboard_path.with_suffix(".json.tmp")
            try:
                with open(tmp_path, "w") as fh:
                    json.dump(dashboard, fh, indent=2)
                tmp_path.replace(dashboard_path)
                log.info(
                    "Updated %s (version %d)",
                    dashboard_path, dashboard["version"],
                )
            except OSError as exc:
                log.error("Failed to write dashboard JSON: %s", exc)
                if tmp_path.exists():
                    tmp_path.unlink()
                sys.exit(1)

        log.info(
            "Grafana provisioning will pick up the change within "
            "30 seconds (updateIntervalSeconds)"
        )
    else:
        log.info("No changes needed")


if __name__ == "__main__":
    main()
