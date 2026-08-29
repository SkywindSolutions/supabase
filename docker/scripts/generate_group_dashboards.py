#!/usr/bin/env python3
"""
generate_group_dashboards.py
Task: P1.2 — Configure Grafana Access Control & Authentication
Created: 2026-03-17

Generates per-group Grafana dashboard JSON files from the shared customer
dashboard templates.  For each (group_id, data_type) pair where the group
has non-NULL data, a dashboard is written to:

    volumes/grafana/provisioning/dashboards/groups/<group_id>/<type>.json

The script also rewrites dashboards.yaml to add a provisioning provider for
each group folder so that Grafana's provisioning engine picks them up on the
next reload.

Key security change: the $group template variable is removed from customer
dashboards and the group_id is hardcoded directly in every SQL query.  This
prevents URL-parameter manipulation from exposing cross-group data.

Usage (run from supabase/docker/):
    python3 scripts/generate_group_dashboards.py [options]

Options:
    --db-url    PostgreSQL connection string
                default: postgresql://postgres:<POSTGRES_PASSWORD>@127.0.0.1:5432/postgres
                Reads POSTGRES_PASSWORD from .env if not specified.
    --templates Path to customer dashboard templates.
                default: ./volumes/grafana/provisioning/dashboards/customer
    --output    Root provisioning directory.
                default: ./volumes/grafana/provisioning/dashboards
    --groups    Comma-separated list of group_ids to process.
                If omitted, all group_ids in forecast_data are discovered from
                the database.
    --dry-run   Print what would be written without creating files.
"""

import argparse
import copy
import json
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Group display-name registry
# Maps group_id → human-readable folder name shown in Grafana.
# Extend this dict when new customer groups are added.
# ---------------------------------------------------------------------------
GROUP_DISPLAY_NAMES: dict[str, str] = {
    "grp_mobilebay":     "Mobile Bay Forecasts",
    "grp_pascagoula":    "Pascagoula Forecasts",
    "grp_portrichey":    "Port Richey Forecasts",
    "grp_clearwater":    "Clearwater Forecasts",
    "grp_corpuschristi": "Corpus Christi Forecasts",
    "grp_chatham":       "Chatham Forecasts",
    "grp_oceanCay":      "Ocean Cay Forecasts",
    "grp_sendero":       "Sendero Forecasts",
    "grp_ssamarine":     "SSA Marine Forecasts",
    "grp_carnival":      "Carnival Forecasts",
    "grp_lakeWorth":     "Lake Worth Forecasts",
    "grp_penobscot":     "Penobscot Forecasts",
    "grp_hertz":         "Hertz Forecasts",
    "grp_gator":         "Gator Forecasts",
    "grp_manson":        "Manson Forecasts",
}

# Data-type → columns checked to detect whether a group has that data.
# A group is considered to have a data type when ANY of its columns has at
# least one non-NULL row.  Visibility uses both vismean and nbmvis so that
# groups whose model produces only nbmvis (e.g. model_1) are detected correctly.
DATA_TYPE_COLUMNS: dict[str, list[str]] = {
    "visibility": ["vismean", "nbmvis"],
    "wind":       ["windspdmean"],
    "tide":       ["tidemean"],
}


# Template filename → data type
TEMPLATE_NAMES: dict[str, str] = {
    "visibility.json": "visibility",
    "wind.json":       "wind",
    "tide.json":       "tide",
}

COMBINED_DASHBOARDS: dict[str, tuple[str, ...]] = {
    "wind_visibility": ("visibility", "wind"),
    "tide_wind": ("wind", "tide"),
}

COMBINED_DASHBOARD_GROUPS: dict[str, tuple[str, ...]] = {
    "grp_mobilebay": ("wind_visibility",),
    "grp_pascagoula": ("wind_visibility",),
    "grp_ssamarine": ("tide_wind",),
    "grp_carnival": ("tide_wind",),
    "grp_penobscot": ("tide_wind",),
}

TIDE_GRAFANA_UNITS: dict[str, str] = {
    "ft": "lengthft",
    "m": "suffix: m",
}

LEGACY_WIND_HEIGHT_M: dict[str, int] = {
    "grp_ssamarine": 10,
    "grp_penobscot": 10,
}

# Per-group, per-dashboard default model mapping.
# Single source of truth: scripts/default_models.json (also consumed by
# alert_service.py so SMS alerts follow the dashboard default automatically).
# Schema: {"groups": {<group_id>: {<data_type>: <model_name>}, ...}, ...}
_DEFAULT_MODELS_PATH = Path(__file__).resolve().parent / "default_models.json"


def _load_default_models() -> dict[str, dict[str, str]]:
    """Load the per-group default-model mapping from default_models.json.

    Returns an empty dict if the file is missing or malformed; the rest of the
    script treats absence as "no default" and the model dropdown's first value
    is used.
    """
    try:
        with open(_DEFAULT_MODELS_PATH) as fh:
            data = json.load(fh)
        groups = data.get("groups", {})
        # Filter out underscore-prefixed metadata keys defensively.
        return {gid: dtypes for gid, dtypes in groups.items()
                if isinstance(gid, str) and not gid.startswith("_")
                and isinstance(dtypes, dict)}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to load %s: %s — proceeding with no defaults",
                    _DEFAULT_MODELS_PATH, exc)
        return {}


DEFAULT_MODEL: dict[str, dict[str, str]] = _load_default_models()


def load_env(env_path: Path) -> dict[str, str]:
    """Read key=value pairs from a .env file, ignoring comments and blanks."""
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


def discover_groups(conn_str: str) -> dict[str, set[str]]:
    """
    Query the database to find which (group_id, data_type) combinations have
    at least one non-NULL row.

    Returns:
        {group_id: {data_type, ...}, ...}
    """
    try:
        import psycopg2  # type: ignore
    except ImportError:
        log.error(
            "psycopg2 is not installed.  Install it with: pip install psycopg2-binary"
        )
        sys.exit(1)

    result: dict[str, set[str]] = {}
    checks = " ".join(
        f"({' + '.join(f'COUNT({c})' for c in cols)}) > 0  AS has_{dtype},"
        for dtype, cols in DATA_TYPE_COLUMNS.items()
    ).rstrip(",")

    sql = f"""
        SELECT group_id, {checks}
        FROM public.forecast_data
        GROUP BY group_id
        ORDER BY group_id;
    """

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                for row in cur.fetchall():
                    grp = row[0]
                    dtypes: set[str] = set()
                    for i, dtype in enumerate(DATA_TYPE_COLUMNS.keys()):
                        if row[i + 1]:
                            dtypes.add(dtype)
                    result[grp] = dtypes
    except Exception as exc:
        log.error("Database query failed: %s", exc)
        sys.exit(1)

    return result


def discover_selected_groups(conn_str: str, groups: list[str]) -> dict[str, set[str]]:
    """
    Query the database for a fixed list of groups and return data types that
    have at least one non-NULL row for each group.

    Returns:
        {group_id: {data_type, ...}, ...}
    """
    try:
        import psycopg2  # type: ignore
    except ImportError:
        log.error(
            "psycopg2 is not installed.  Install it with: pip install psycopg2-binary"
        )
        sys.exit(1)

    if not groups:
        return {}

    checks = " ".join(
        f"({' + '.join(f'COUNT({c})' for c in cols)}) > 0  AS has_{dtype},"
        for dtype, cols in DATA_TYPE_COLUMNS.items()
    ).rstrip(",")

    sql = f"""
        SELECT group_id, {checks}
        FROM public.forecast_data
        WHERE group_id = ANY(%s)
        GROUP BY group_id
        ORDER BY group_id;
    """

    result: dict[str, set[str]] = {group_id: set() for group_id in groups}

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (groups,))
                for row in cur.fetchall():
                    group_id = row[0]
                    dtypes: set[str] = set()
                    for i, dtype in enumerate(DATA_TYPE_COLUMNS.keys()):
                        if row[i + 1]:
                            dtypes.add(dtype)
                    result[group_id] = dtypes
    except Exception as exc:
        log.error("Database query failed: %s", exc)
        sys.exit(1)

    return result


def count_group_locations(conn_str: str, group_id: str, dtype: str) -> int:
    """
    Count the number of unique locations for a group and data type.
    Returns the count, or -1 on error (non-fatal).
    """
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return -1

    cols = DATA_TYPE_COLUMNS.get(dtype)
    if not cols:
        return -1
    # A row is considered to have data if any of the detection columns is non-NULL.
    not_null_clause = " OR ".join(f"{c} IS NOT NULL" for c in cols)

    sql = f"""
        SELECT COUNT(DISTINCT locname)
        FROM public.forecast_data
        WHERE group_id = %s AND ({not_null_clause});
    """

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id,))
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception as exc:
        log.warning("Failed to count locations for %s/%s: %s", group_id, dtype, exc)
        return -1


def get_default_location(conn_str: str, group_id: str, dtype: str) -> str | None:
    """Return the first location with data for a group/data type."""
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return None

    cols = DATA_TYPE_COLUMNS.get(dtype)
    if not cols:
        return None
    not_null_clause = " OR ".join(f"{c} IS NOT NULL" for c in cols)

    sql = f"""
        SELECT locname
        FROM public.forecast_data
        WHERE group_id = %s AND ({not_null_clause})
        GROUP BY locname
        ORDER BY locname
        LIMIT 1;
    """

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        log.warning("Failed to get default location for %s/%s: %s", group_id, dtype, exc)
        return None


def count_group_wind_heights(conn_str: str, group_id: str) -> int:
    """Count distinct wind forecast heights for a group."""
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return -1

    legacy_wind_height_m = LEGACY_WIND_HEIGHT_M.get(group_id, 20)

    sql = f"""
        SELECT COUNT(DISTINCT COALESCE(height_m, {legacy_wind_height_m}))
        FROM public.forecast_data
        WHERE group_id = %s AND windspdmean IS NOT NULL;
    """

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id,))
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception as exc:
        log.warning("Failed to count wind heights for %s: %s", group_id, exc)
        return -1


def get_group_unit_preference(
    conn_str: str,
    group_id: str,
    variable_key: str,
    fallback_unit: str,
) -> str:
    """Return a group-level display unit from forecast metadata, if available."""
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return fallback_unit

    sql = """
        SELECT public.fn_forecast_display_unit(%s, NULL, %s, %s);
    """

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id, variable_key, fallback_unit))
                row = cur.fetchone()
                return row[0] if row and row[0] else fallback_unit
    except Exception as exc:
        log.warning(
            "Failed to get unit preference for %s/%s: %s",
            group_id,
            variable_key,
            exc,
        )
        return fallback_unit


def sanitise_folder_uid(group_id: str) -> str:
    """Convert a group_id to a safe Grafana folder UID (max 40 chars)."""
    uid = re.sub(r"[^a-zA-Z0-9\-]", "-", group_id)
    return f"cust-{uid}"[:40]


def load_customer_metadata(conn_str: str, group_id: str) -> dict:
    """Load customer profile metadata from the database.

    Returns the ``metadata`` JSONB column from ``customer_groups`` as a
    Python dict, or an empty dict when the group has no profile or the DB
    is unavailable.
    """
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return {}

    sql = "SELECT metadata::text FROM public.customer_groups WHERE group_id = %s;"

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id,))
                row = cur.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
                return {}
    except Exception as exc:
        log.warning("Failed to load customer metadata for %s: %s", group_id, exc)
        return {}


def _patch_location_variable_display_names(
    dashboard: dict,
    group_id: str,
) -> None:
    """Modify the ``location`` template-variable query to show display names.

    When the customer's ``customer_groups.metadata`` contains a
    ``display_name_overrides`` map, the location dropdown shows the
    customer-facing name while ``$location`` still resolves to the real
    ``locname`` value so that existing SQL queries continue to work.
    """
    for var in dashboard.get("templating", {}).get("list", []):
        if var.get("name") != "location":
            continue
        for field in ("query", "definition"):
            query = var.get(field, "")
            if not query:
                continue

            # Step 1: Prefix unqualified column refs with f.
            qualified = re.sub(
                r"\b(group_id|locname|model)\b",
                r"f.\1",
                query,
            )
            qualified = re.sub(
                r"\b(tidemean|windspdmean|vismean|nbmvis)\s+IS\s+NOT\s+NULL",
                r"f.\1 IS NOT NULL",
                qualified,
            )

            # Step 2: Transform SELECT DISTINCT to use display-name overrides.
            # Use __value / __text column aliases so Grafana unambiguously
            # knows which column is the value (real locname) and which is the
            # display text (customer-facing name).
            new_query = re.sub(
                r"SELECT\s+DISTINCT\s+f\.locname\s+FROM\s+forecast_data\s*",
                "SELECT DISTINCT f.locname AS __value, "
                "COALESCE(g.metadata->'display_name_overrides'->>f.locname, f.locname) AS __text "
                "FROM forecast_data f "
                "LEFT JOIN public.customer_groups g "
                "ON g.group_id = f.group_id AND g.metadata ? 'display_name_overrides' ",
                qualified,
                count=1,
            )
            new_query = new_query.replace("ORDER BY 1", "ORDER BY 2")
            var[field] = new_query

        # Preserve an already-set default location so the dashboard renders
        # immediately; only remove stale options.
        var.pop("options", None)


def _patch_sql_display_names(
    panels: list,
    group_id: str,
) -> None:
    """Replace ``locname AS "<Title>"`` in panel SQL with display-name lookup.

    Handles three patterns:
    1. Simple queries (direct ``FROM forecast_data``)
    2. Subquery patterns (``FROM (SELECT DISTINCT ON ...) sub``)
    3. CTE patterns (``WITH ...``)

    For each, the ``locname`` column displayed to the user is wrapped with
    ``COALESCE(g.metadata->'display_name_overrides'->>..., ...)`` so the
    customer-facing name appears instead of the stored locname.
    """
    for panel in panels:
        for target in panel.get("targets", []):
            raw_sql = target.get("rawSql", "")
            if not raw_sql:
                continue

            # Only patch queries that display locname as a column
            if 'locname AS "' not in raw_sql and "locname AS '" not in raw_sql:
                continue

            has_cte = bool(re.search(r"^\s*WITH\s+", raw_sql, re.IGNORECASE | re.MULTILINE))
            has_subquery = raw_sql.count("SELECT") > 1 or raw_sql.count("FROM") > 1

            if has_cte:
                result = _patch_cte_display_names(raw_sql)
            elif has_subquery:
                result = _patch_subquery_display_names(raw_sql)
            else:
                result = _patch_simple_display_names(raw_sql)

            if result != raw_sql:
                target["rawSql"] = result

        if "panels" in panel:
            _patch_sql_display_names(panel["panels"], group_id)


def _patch_simple_display_names(sql: str) -> str:
    """Patch a simple ``SELECT locname AS "Location" FROM forecast_data ...`` query."""
    # Prefix unqualified column refs with f.
    prefixed = re.sub(
        r"\b(group_id|locname|model|datum|timestamp|startdt|forecastdtutc|height_m)\b",
        r"f.\1",
        sql,
    )
    prefixed = re.sub(
        r"\b(tidemean|windspdmean|vismean|nbmvis|tideub|tidelb)\s+IS\s+NOT\s+NULL",
        r"f.\1 IS NOT NULL",
        prefixed,
    )

    result = re.sub(
        r'\bf\.locname\s+AS\s+"(Location)"',
        r"COALESCE(g.metadata->'display_name_overrides'->>f.locname, f.locname) AS "
        r'"\1"',
        prefixed,
    )

    if "customer_groups" not in result:
        result = re.sub(
            r"(FROM\s+(public\.)?forecast_data\b)",
            r"\1 f\n"
            r"LEFT JOIN public.customer_groups g "
            r"ON g.group_id = f.group_id AND g.metadata ? 'display_name_overrides'",
            result,
            count=1,
        )
    return result


def _patch_subquery_display_names(sql: str) -> str:
    """Patch a subquery pattern like SELECT locname AS Location FROM (SELECT ...) sub."""
    # Extract the hardcoded group_id from the SQL
    group_id_match = re.search(r"group_id\s*=\s*'([^']+)'", sql)
    group_id = group_id_match.group(1) if group_id_match else ""

    result = sql
    result = re.sub(
        r'\blocname\s+AS\s+"(Location)"',
        r"COALESCE(g.metadata->'display_name_overrides'->>sub.locname, sub.locname) AS "
        r'"\1"',
        result,
    )
    if group_id and "customer_groups" not in result:
        result = re.sub(
            r"(\)\s+sub\b)",
            r"\1\n"
            r"LEFT JOIN public.customer_groups g "
            rf"ON g.group_id = '{group_id}' AND g.metadata ? 'display_name_overrides'",
            result,
            count=1,
        )
    return result


def _patch_cte_display_names(sql: str) -> str:
    """Patch a CTE query where a CTE selects ``locname AS "Location"``.

    Uses a correlated subquery with the hardcoded ``group_id`` extracted
    from the existing SQL to resolve the display name from
    ``customer_groups.metadata``, avoiding any join restructuring.
    """
    # Extract the hardcoded group_id from the SQL
    group_id_match = re.search(r"group_id\s*=\s*'([^']+)'", sql)
    group_id = group_id_match.group(1) if group_id_match else ""

    result = sql

    if group_id:
        # Replace locname AS "Location" with a correlated subquery
        result = re.sub(
            r'\blocname\s+AS\s+"(Location)"',
            r"COALESCE("
            r"(SELECT g2.metadata->'display_name_overrides'->>nearest_points.locname "
            r"FROM public.customer_groups g2 "
            rf"WHERE g2.group_id = '{group_id}' "
            r"AND g2.metadata ? 'display_name_overrides'), "
            r"nearest_points.locname"
            r') AS "\1"',
            result,
        )

    return result


def _add_coordinates_panel(
    dashboard: dict,
    group_id: str,
    conn_str: str | None,
) -> None:
    """Add a coordinates display panel showing lat/lon of the selected location.

    The panel queries ``forecast_locations`` for the current ``$location`` and
    displays latitude and longitude in decimal degrees.  Only added when the
    group has location records with non-NULL coordinates.

    The panel is inserted after the first section row or at the top of the
    second section (e.g. after the "Location Summary" row in combined
    dashboards).  Existing panels are shifted down to make room.
    """
    if not conn_str:
        return

    # Check if any locations have coordinates for this group
    has_coords = False
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return

    sql = """
        SELECT 1 FROM public.forecast_locations
        WHERE group_id = %s AND latitude IS NOT NULL AND longitude IS NOT NULL
        LIMIT 1;
    """
    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (group_id,))
                has_coords = cur.fetchone() is not None
    except Exception as exc:
        log.warning("Failed to check coordinates for %s: %s", group_id, exc)
        return

    if not has_coords:
        return

    coord_panel = {
        "datasource": {"type": "postgres", "uid": "supabase-postgres"},
        "description": "Latitude and longitude of the selected forecast location.",
        "fieldConfig": {
            "defaults": {
                "color": {
                    "mode": "thresholds"
                },
                "mappings": [],
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {
                            "color": "text",
                            "value": None
                        }
                    ]
                },
                "custom": {
                    "align": "center"
                }
            },
            "overrides": []
        },
        "gridPos": {"h": 3, "w": 24, "x": 0, "y": 0},
        "id": 0,  # Will be assigned after existing max id
        "options": {
            "cellHeight": "md",
            "footer": {
                "countRows": False,
                "fields": "",
                "reducer": ["sum"],
                "show": False,
            },
            "showHeader": False,
        },
        "pluginVersion": "",
        "targets": [
            {
                "datasource": {"type": "postgres", "uid": "supabase-postgres"},
                "editorMode": "code",
                "format": "table",
                "rawQuery": True,
                "rawSql": (
                    "SELECT\n"
                    f"  '(' || ROUND(latitude::numeric, 6) || ', ' || ROUND(longitude::numeric, 6) || ')' AS coordinates\n"
                    f"FROM public.forecast_locations\n"
                    f"WHERE group_id = '{group_id}'\n"
                    f"  AND locname = '$location';\n"
                ),
                "refId": "A",
            }
        ],
        "title": "Location Coordinates",
        "type": "table",
    }

    # Assign an ID that doesn't conflict with existing panels
    existing_ids = [p.get("id", 0) for p in dashboard.get("panels", [])]
    for panel_list in dashboard.get("panels", []):
        if "panels" in panel_list:
            existing_ids.extend(sp.get("id", 0) for sp in panel_list["panels"])
    coord_panel["id"] = max(existing_ids + [9999]) + 1

    # Place at the VERY TOP (y=0, before any row headers) so the panel is
    # dashboard-level and NOT inside the repeated wind-height section.
    panels = dashboard.get("panels", [])

    # Shift ALL existing panels down by the coordinates panel height
    shift_y = coord_panel["gridPos"]["h"]
    for p in panels:
        g = p.get("gridPos", {})
        g["y"] = g.get("y", 0) + shift_y
        if "panels" in p:
            _shift_panels_down(p["panels"], shift_y)

    # Insert at position 0
    panels.insert(0, coord_panel)


def _shift_panels_down(panels: list, shift_y: int) -> None:
    """Shift all panels in a list down by shift_y rows."""
    for panel in panels:
        g = panel.get("gridPos", {})
        g["y"] = g.get("y", 0) + shift_y
        if "panels" in panel:
            _shift_panels_down(panel["panels"], shift_y)


def make_group_dashboard(
    template: dict,
    group_id: str,
    dtype: str,
    display_name: str,
    location_count: int = -1,
    default_location: str | None = None,
    wind_height_count: int = -1,
    tide_display_unit: str = "ft",
    customer_metadata: dict | None = None,
    conn_str: str | None = None,
) -> dict:
    """
    Transform a shared customer dashboard template into a group-specific
    dashboard with the group_id hardcoded in all SQL queries.

    Changes applied:
    - Remove the `group` template variable (was visible — security hole)
    - Hardcode group_id in the `location` variable query
    - Hardcode group_id in every panel's rawSql
    - Update uid, title, and description
    - For tide dashboards with only 1 location: remove "Tide by Location" panel
      and adjust "Current Tide" panel positioning to sit next to Tide Forecast Data
    - Apply display-name overrides from customer metadata
    - Add coordinates panel if location metadata exists
    """
    d = copy.deepcopy(template)
    legacy_wind_height_m = LEGACY_WIND_HEIGHT_M.get(group_id, 20)

    type_labels = {
        "visibility": "Visibility",
        "wind":       "Wind",
        "tide":       "Tide",
        "wind_visibility": "Wind & Visibility",
        "tide_wind": "Tide & Wind",
    }
    label = type_labels.get(dtype, dtype.title())

    # -- Update dashboard metadata -------------------------------------------
    d["uid"] = f"{dtype}-{group_id}"
    d["title"] = f"{display_name} — {label} Forecast"
    d["description"] = (
        f"{label} forecast data for {display_name}. "
        "Customer-facing dashboard."
    )
    # Prevent users from saving changes through the UI
    d["editable"] = False

    # -- Remove `group` template variable ------------------------------------
    d.setdefault("templating", {}).setdefault("list", [])
    d["templating"]["list"] = [
        v for v in d["templating"]["list"] if v.get("name") != "group"
    ]


    # -- Hardcode group_id in template-variable queries ----------------------
    for var in d["templating"]["list"]:
        # Grafana stores query text in both "query" and "definition" fields.
        for field in ("query", "definition"):
            if var.get(field):
                var[field] = var[field].replace(
                    "group_id = '$group'",
                    f"group_id = '{group_id}'",
                )

        # Set the default for the model variable if specified
        if var.get("name") == "model":
            default_model = DEFAULT_MODEL.get(group_id, {}).get(dtype)
            if default_model:
                var["current"] = {"text": default_model, "value": default_model}
            else:
                var.pop("current", None)
        elif var.get("name") == "height_m":
            if wind_height_count == 1:
                var["current"] = {
                    "text": str(legacy_wind_height_m),
                    "value": str(legacy_wind_height_m),
                }
            else:
                var["current"] = {"text": "All", "value": "$__all"}
            var["hide"] = 2 if wind_height_count == 1 else 0
        elif var.get("name") == "location" and default_location:
            var["current"] = {"text": default_location, "value": default_location}
        else:
            # Clear the cached current value so Grafana picks the first real result
            var.pop("current", None)
        var.pop("options", None)

    # -- Hardcode group_id in all panel SQL ----------------------------------
    _patch_group_id(d.get("panels", []), group_id)

    # -- Tide panels always read the display view ----------------------------
    # v_forecast_data_display carries unit conversion AND the vertical-datum
    # offset (from forecast_locations.metadata -> vertical_datum), so tide
    # dashboards always present the configured display datum (e.g. MLLW for
    # Penobscot).  For ft groups the display columns equal the stored values
    # (offset 0), preserving backward compatibility.
    if "tide" in dtype:
        _patch_tide_display_units(d, tide_display_unit)
    if "wind" in dtype and legacy_wind_height_m != 20:
        _patch_legacy_wind_height(d, legacy_wind_height_m)

    # -- Apply display-name overrides from customer metadata -----------------
    if customer_metadata:
        display_overrides = customer_metadata.get("display_name_overrides", {})
        if display_overrides:
            _patch_location_variable_display_names(d, group_id)
            _patch_sql_display_names(d.get("panels", []), group_id)

    # -- Add coordinates panel if location metadata exists -------------------
    feature_flags = (customer_metadata or {}).get("feature_flags", {})
    if feature_flags.get("coordinates_display", False) and conn_str:
        _add_coordinates_panel(d, group_id, conn_str)

    # -- Filter panels for single-location groups (tide only) -----------------
    if dtype == "tide" and location_count == 1:
        panels = d.get("panels", [])
        # Remove Tide by Location panel (redundant when only one location exists)
        panels = [p for p in panels if not str(p.get("title", "")).startswith("Tide by Location")]
        d["panels"] = panels
        # Collapse the layout so Current Tide and Tide Forecast Data share row 2
        # rather than leaving the Tide by Location gap on row 2 and pushing the
        # forecast data table down to row 3.
        # Layout: Tide Forecast Data on the left (x=0,w=20), Current Tide stat
        # on the right (x=20,w=4), both at y=12 with matching height.
        for panel in panels:
            title = str(panel.get("title", ""))
            grid = panel.get("gridPos", {})
            if title.startswith("Current Tide"):
                grid["x"] = 20
                grid["w"] = 4
                grid["y"] = 12
                grid["h"] = 9
            elif title.startswith("Tide Forecast Data"):
                grid["x"] = 0
                grid["w"] = 20
                grid["y"] = 12
                grid["h"] = 9

    return d


def make_combined_group_dashboard(
    templates: dict[str, dict],
    group_id: str,
    combined_type: str,
    display_name: str,
    default_location: str | None = None,
    wind_height_count: int = -1,
    tide_display_unit: str = "ft",
    customer_metadata: dict | None = None,
    conn_str: str | None = None,
) -> dict:
    """Build a single customer dashboard from multiple customer templates."""
    source_types = COMBINED_DASHBOARDS[combined_type]
    d = copy.deepcopy(templates[source_types[0]])

    d["uid"] = f"{combined_type}-{group_id}"
    combined_labels = {
        "wind_visibility": "Wind & Visibility",
        "tide_wind": "Tide & Wind",
    }
    combined_label = combined_labels.get(combined_type, combined_type.replace("_", " & ").title())

    d["title"] = f"{display_name} — {combined_label} Forecast"
    d["description"] = (
        f"{combined_label} forecast data for {display_name}. "
        "Customer-facing dashboard."
    )
    d["editable"] = False
    d["tags"] = ["customer", *source_types, "combined"]
    d["time"] = {"from": "now", "to": "now+7d" if combined_type == "tide_wind" else "now+24h"}

    panels: list[dict] = []
    next_id = 1
    y_offset = 0
    for source_type in source_types:
        template_panels = copy.deepcopy(templates[source_type].get("panels", []))
        if not template_panels:
            continue

        min_y = min(panel.get("gridPos", {}).get("y", 0) for panel in template_panels)
        max_bottom = 0
        for panel in template_panels:
            grid = panel.setdefault("gridPos", {})
            grid["y"] = grid.get("y", 0) - min_y + y_offset
            max_bottom = max(max_bottom, grid["y"] + grid.get("h", 0))
            panel["id"] = next_id
            next_id += 1
        panels.extend(template_panels)
        y_offset = max_bottom

    d["panels"] = panels

    combined_vars: list[dict] = []
    seen_vars: set[str] = set()
    for source_type in source_types:
        for var in templates[source_type].get("templating", {}).get("list", []):
            name = var.get("name")
            if name in seen_vars:
                continue
            seen_vars.add(name)
            new_var = copy.deepcopy(var)
            if name == "model":
                default_model = DEFAULT_MODEL.get(group_id, {}).get(source_type)
                if default_model:
                    new_var["current"] = {"text": default_model, "value": default_model}
                new_var["hide"] = 2
            combined_vars.append(new_var)
    d.setdefault("templating", {})["list"] = combined_vars

    d = make_group_dashboard(
        d, group_id, combined_type, display_name, default_location=default_location,
        wind_height_count=wind_height_count,
        tide_display_unit=tide_display_unit,
        customer_metadata=customer_metadata,
        conn_str=conn_str,
    )
    if "wind" in source_types:
        _patch_non_nbm_wind_startdt_filters(d.get("panels", []), f"'{group_id}'")
    if combined_type == "tide_wind":
        _patch_tide_wind_dashboard(d, group_id, customer_metadata)
    d["time"] = {"from": "now", "to": "now+7d" if combined_type == "tide_wind" else "now+24h"}
    d["tags"] = ["customer", *source_types, "combined"]
    return d


def _patch_tide_wind_dashboard(dashboard: dict, group_id: str, customer_metadata: dict | None = None) -> None:
    legacy_wind_height_m = LEGACY_WIND_HEIGHT_M.get(group_id, 20)
    _add_location_timezone_variable(dashboard, group_id)
    _patch_tide_wind_time_panel(dashboard.get("panels", []), group_id, legacy_wind_height_m)
    removed_y = _remove_panel_by_title_prefix(dashboard.get("panels", []), "Wind Direction Forecast")
    if removed_y is not None:
        _shift_panels_below(dashboard.get("panels", []), removed_y, -9)
    _patch_tide_wind_speed_direction_panel(dashboard.get("panels", []), group_id, legacy_wind_height_m)
    # Insert a section row before Wind Speed by Location so it is NOT
    # captured by the height_m repeat on the wind section row.
    _insert_windspeed_table_section_break(dashboard.get("panels", []))
    # Split the single $model variable into $wind_model and $tide_model so
    # wind and tide panels each use the correct default model. Without this,
    # the combined dashboard's single $model covers one data type only and
    # the other shows "No data".
    _split_combined_model_variables(dashboard, group_id)
    # Fix Gusts panel height to h=3 so Prob Thunder fits below it
    _fix_wind_gusts_height(dashboard.get("panels", []))
    # Add optional feature-flag panels after structural patches are done
    if customer_metadata:
        _add_feature_flag_panels(dashboard, group_id, customer_metadata)


def _insert_windspeed_table_section_break(panels: list) -> None:
    """Insert a non-repeating row before the Wind Speed by Location table.

    Without this section break the table is captured by the ``repeat=height_m``
    on the wind section row and duplicates for each wind height.
    """
    for i, p in enumerate(panels):
        if "Wind Speed by Location" not in str(p.get("title", "")):
            continue
        # Find the maximum existing ID
        max_id = max((pp.get("id", 0) for pp in panels), default=0)
        for pp in panels:
            if "panels" in pp:
                max_id = max(max_id, max((sp.get("id", 0) for sp in pp["panels"]), default=0))

        row_y = p.get("gridPos", {}).get("y", 0)
        row_panel = {
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": row_y},
            "id": max_id + 1,
            "panels": [],
            "title": "Wind Speed Summary",
            "type": "row",
        }
        # Shift the Wind Speed table and everything below down by 1
        for j in range(i, len(panels)):
            g = panels[j].get("gridPos", {})
            g["y"] = g.get("y", 0) + 1
        panels.insert(i, row_panel)
        return


def _split_combined_model_variables(dashboard: dict, group_id: str) -> None:
    """Replace the single ``model`` variable with ``wind_model`` and ``tide_model``.

    Updates all panel SQL to reference the correct variable so wind panels
    use the wind default and tide panels use the tide default.
    """
    templating = dashboard.get("templating", {}).get("list", [])
    model_var = None
    model_idx = None
    for i, var in enumerate(templating):
        if var.get("name") == "model":
            model_var = var
            model_idx = i
            break
    if model_var is None:
        return

    # Clone the variable for wind
    wind_model_var = copy.deepcopy(model_var)
    wind_model_var["name"] = "wind_model"
    wind_model_var["label"] = "Wind Model"
    wind_default = DEFAULT_MODEL.get(group_id, {}).get("wind")
    if wind_default:
        wind_model_var["current"] = {"text": wind_default, "value": wind_default}
    else:
        wind_model_var.pop("current", None)

    # Clone the variable for tide — its query must filter by tide columns.
    tide_model_var = copy.deepcopy(model_var)
    tide_model_var["name"] = "tide_model"
    tide_model_var["label"] = "Tide Model"
    tide_default = DEFAULT_MODEL.get(group_id, {}).get("tide")
    if tide_default:
        tide_model_var["current"] = {"text": tide_default, "value": tide_default}
    else:
        tide_model_var.pop("current", None)
    # Fix the tide_model query: change windspdmean → tidemean so it lists
    # actual tide models (e.g. tide_blend, tide_nbm, tide_astro).
    for field in ("query", "definition"):
        q = tide_model_var.get(field, "")
        if "windspdmean" in q:
            q = q.replace("windspdmean", "tidemean")
            tide_model_var[field] = q

    # Ensure both variables have a group_id filter
    for var in (wind_model_var, tide_model_var):
        for field in ("query", "definition"):
            q = var.get(field, "")
            if q and "group_id = '" not in q and "group_id='" not in q:
                # Append group_id filter before ORDER BY
                if "ORDER BY" in q:
                    q = q.replace("ORDER BY", f"AND group_id = '{group_id}' ORDER BY")
                else:
                    q = q + f"\nAND group_id = '{group_id}'"
                var[field] = q

    # Replace the single model variable with wind_model + tide_model
    templating[model_idx:model_idx + 1] = [wind_model_var, tide_model_var]

    # Patch all panel SQL: $model → $wind_model for wind queries,
    # $model → $tide_model for tide queries, and $model → $wind_model
    # for probability-of-thunder queries.
    _patch_model_references(dashboard.get("panels", []))

    # Also patch template variable queries that reference $model
    for var in templating:
        for field in ("query", "definition"):
            query = var.get(field, "")
            if "$model" not in query:
                continue
            if "windspdmean" in query.upper() or "WINDDIR" in query.upper():
                var[field] = query.replace("$model", "$wind_model")
            elif "tidemean" in query.upper():
                var[field] = query.replace("$model", "$tide_model")
            else:
                var[field] = query.replace("$model", "$wind_model")


def _patch_model_references(panels: list) -> None:
    """Replace ``$model`` in panel SQL with the correct model variable.

    Wind-related queries get ``$wind_model``, tide-related queries get
    ``$tide_model``, and thunderstorm-probability queries get ``$wind_model``.
    """
    for panel in panels:
        for target in panel.get("targets", []):
            raw_sql = target.get("rawSql", "")
            if not raw_sql or "$model" not in raw_sql:
                continue

            sql_upper = raw_sql.upper()
            # Determine which model variable to use
            if "TIDEMEAN" in sql_upper or "ASTROTIDE" in sql_upper or "PRELIMTIDE" in sql_upper:
                new_var = "$tide_model"
            elif "POT" in sql_upper and "THUNDER" in (panel.get("title") or "").upper():
                new_var = "$wind_model"
            elif "WINDSPDMEAN" in sql_upper or "WINDSPD" in sql_upper or "WINDDIR" in sql_upper:
                new_var = "$wind_model"
            else:
                new_var = "$wind_model"  # default to wind

            target["rawSql"] = raw_sql.replace("$model", new_var)

        if "panels" in panel:
            _patch_model_references(panel["panels"])


def _fix_wind_gusts_height(panels: list) -> None:
    """Reduce the Gusts stat panel height from 6 to 3.

    In the combined layout, Gusts occupies 6 rows (same span as Speed +
    Direction combined) which leaves no room for the Prob Thunderstorms
    stat.  Reducing h to 3 also shifts everything BELOW Gusts's bottom
    edge upward to close the gap, while leaving panels at the same y
    that are stacked alongside Gusts (e.g. Direction at x=18) untouched.
    """
    for panel in panels:
        title = str(panel.get("title", ""))
        if "Gusts" in title:
            grid = panel.get("gridPos", {})
            if grid.get("h", 0) == 6:
                old_h = grid["h"]
                gusts_bottom = grid["y"] + old_h
                grid["h"] = 3
                new_bottom = grid["y"] + grid["h"]
                shift = new_bottom - gusts_bottom  # negative
                for other in panels:
                    if other is panel:
                        continue
                    og = other.get("gridPos", {})
                    # Only shift panels whose top is AT or BELOW the old bottom edge
                    if og.get("y", 0) >= gusts_bottom:
                        og["y"] = max(0, og.get("y", 0) + shift)
        if "panels" in panel:
            _fix_wind_gusts_height(panel["panels"])


def _add_feature_flag_panels(
    dashboard: dict,
    group_id: str,
    customer_metadata: dict,
) -> None:
    """Add optional panels driven by customer feature flags.

    Currently supports:
    - ``thunderstorm_prob``: adds a "Prob of Thunderstorms" stat panel
      and a "Location Summary & Tide" section row header.
    """
    feature_flags = customer_metadata.get("feature_flags", {})
    panels = dashboard.get("panels", [])

    # Find the maximum existing panel ID
    max_id = max((p.get("id", 0) for p in panels), default=0)
    for p in panels:
        if "panels" in p:
            max_id = max(max_id, max((sp.get("id", 0) for sp in p["panels"]), default=0))

    next_id = max_id + 1

    if feature_flags.get("thunderstorm_prob", False):
        # Find where the tide section begins
        tide_index = None
        for i, p in enumerate(panels):
            if str(p.get("title", "")).startswith("Tide") and p.get("type") != "row":
                tide_index = i
                break

        # Find the Gusts panel to place Prob Thunder right below it
        gusts_panel = None
        gusts_index = None
        for i, p in enumerate(panels):
            if "Gusts" in str(p.get("title", "")):
                gusts_panel = p
                gusts_index = i
                break

        # Add the Prob of Thunderstorms panel below Gusts
        if gusts_panel is not None:
            gusts_y = gusts_panel["gridPos"]["y"]
            gusts_h = gusts_panel["gridPos"]["h"]
            prob_y = gusts_y + gusts_h  # right below Gusts

            prob_panel = {
                "datasource": {"type": "postgres", "uid": "supabase-postgres"},
                "description": "Probability of Thunder in the next 6 hours.",
                "fieldConfig": {
                    "defaults": {
                        "color": {"mode": "thresholds"},
                        "mappings": [],
                        "thresholds": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "green", "value": None},
                                {"color": "yellow", "value": 0.2},
                                {"color": "orange", "value": 0.4},
                                {"color": "red", "value": 0.6},
                            ],
                        },
                        "unit": "percentunit",
                    },
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "pot"},
                            "properties": [
                                {"id": "displayName", "value": "Prob of Thunderstorms"},
                                {"id": "color", "value": {"mode": "thresholds"}},
                            ],
                        }
                    ],
                },
                "gridPos": {"h": 3, "w": 3, "x": 21, "y": prob_y},
                "id": next_id,
                "options": {
                    "colorMode": "background",
                    "graphMode": "none",
                    "justifyMode": "center",
                    "orientation": "auto",
                    "reduceOptions": {
                        "calcs": ["lastNotNull"],
                        "fields": "",
                        "values": False,
                    },
                    "text": {"valueSize": 34},
                    "textMode": "value",
                },
                "targets": [
                    {
                        "datasource": {"type": "postgres", "uid": "supabase-postgres"},
                        "editorMode": "code",
                        "format": "table",
                        "rawQuery": True,
                        "rawSql": (
                            "WITH selected_run AS (\n"
                            "  SELECT MAX(startdt) AS startdt\n"
                            "  FROM forecast_data\n"
                            "  WHERE startdt <= NOW()::timestamptz\n"
                            f"    AND pot IS NOT NULL\n"
                            "    AND locname = '$location'\n"
                            f"    AND group_id = '{group_id}'\n"
                            "    AND model = '$wind_model'\n"
                            ")\n"
                            "SELECT\n"
                            "  pot\n"
                            "FROM forecast_data\n"
                            "WHERE\n"
                            "  pot IS NOT NULL\n"
                            "  AND locname = '$location'\n"
                            f"  AND group_id = '{group_id}'\n"
                            "  AND model = '$wind_model'\n"
                            "  AND startdt = (SELECT startdt FROM selected_run)\n"
                            "ORDER BY timestamp DESC\n"
                            "LIMIT 1"
                        ),
                        "refId": "A",
                    }
                ],
                "title": "Prob of Thunderstorms",
                "type": "stat",
            }
            next_id += 1
            # Insert after Gusts and shift panels below prob_y down
            insert_idx = gusts_index + 1
            panels.insert(insert_idx, prob_panel)
            for j in range(insert_idx + 1, len(panels)):
                g = panels[j].get("gridPos", {})
                if g.get("y", 0) >= prob_y:
                    g["y"] = g.get("y", 0) + 3

        # Place the Location Summary & Tide row at the transition to tide
        if tide_index is not None:
            # Recalculate tide_index after prob thunder insertion
            tide_index = None
            for i, p in enumerate(panels):
                if str(p.get("title", "")).startswith("Tide") and p.get("type") != "row":
                    tide_index = i
                    break

            # Find the new max y in sections before tide
            pre_tide_max_y = 0
            for i, p in enumerate(panels):
                if tide_index is not None and i >= tide_index:
                    break
                g = p.get("gridPos", {})
                end_y = g.get("y", 0) + g.get("h", 0)
                if end_y > pre_tide_max_y:
                    pre_tide_max_y = end_y

            row_panel = {
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": pre_tide_max_y},
                "id": next_id,
                "panels": [],
                "title": "Location Summary & Tide",
                "type": "row",
            }
            next_id += 1
            panels.insert(tide_index, row_panel)
            for j in range(tide_index + 1, len(panels)):
                g = panels[j].get("gridPos", {})
                if g.get("y", 0) >= pre_tide_max_y:
                    g["y"] = g.get("y", 0) + 1


def _add_location_timezone_variable(dashboard: dict, group_id: str) -> None:
    templating = dashboard.setdefault("templating", {}).setdefault("list", [])
    if any(var.get("name") == "location_tz" for var in templating):
        return

    query = (
        "SELECT COALESCE("
        f"(SELECT timezone FROM public.forecast_locations WHERE group_id = '{group_id}' AND locname = '$location' LIMIT 1), "
        f"(SELECT default_timezone FROM public.customer_groups WHERE group_id = '{group_id}'), "
        "'UTC')"
    )
    templating.append({
        "datasource": {"type": "postgres", "uid": "supabase-postgres"},
        "definition": query,
        "hide": 2,
        "includeAll": False,
        "multi": False,
        "name": "location_tz",
        "query": query,
        "refresh": 2,
        "regex": "",
        "sort": 0,
        "type": "query",
        "label": "Location Timezone",
    })


def _patch_tide_wind_time_panel(panels: list, group_id: str, legacy_wind_height_m: int) -> None:
    for panel in panels:
        if panel.get("title") == "" and str(panel.get("description", "")).startswith("Timestamp of"):
            grid = panel.setdefault("gridPos", {})
            if grid.get("h", 0) < 3:
                _shift_panels_in_grid_region(
                    panels,
                    min_x=grid.get("x", 0),
                    min_y=grid.get("y", 0) + grid.get("h", 0),
                    max_y=grid.get("y", 0) + 9,
                    y_delta=1,
                )
                grid["h"] = 3
            panel["type"] = "table"
            panel["description"] = "Current UTC and forecast-location local time for the latest wind sample at this height."
            panel["fieldConfig"]["defaults"].pop("unit", None)
            panel["fieldConfig"]["defaults"].setdefault("custom", {})["align"] = "center"
            panel["options"] = {
                "cellHeight": "md",
                "footer": {
                    "countRows": False,
                    "fields": "",
                    "reducer": ["sum"],
                    "show": False,
                },
                "showHeader": False,
            }
            panel["targets"][0]["format"] = "table"
            panel["targets"][0]["rawSql"] = (
                "WITH latest AS (\n"
                "  SELECT MAX(timestamp) AS ts\n"
                "  FROM forecast_data\n"
                "  WHERE windspdmean IS NOT NULL\n"
                "    AND locname = '$location'\n"
                f"    AND group_id = '{group_id}'\n"
                "    AND model = '$model'\n"
                f"    AND (height_m = ${{height_m:raw}} OR (height_m IS NULL AND ${{height_m:raw}} = {legacy_wind_height_m}))\n"
                "    AND timestamp <= NOW()::TIMESTAMPTZ\n"
                ")\n"
                "SELECT 'UTC' AS \"Label\", to_char(ts AT TIME ZONE 'UTC', 'MM-DD HH24:MI') AS \"Time\"\n"
                "FROM latest\n"
                "WHERE ts IS NOT NULL\n"
                "UNION ALL\n"
                "SELECT 'Local' AS \"Label\", to_char(ts AT TIME ZONE '$location_tz', 'MM-DD HH24:MI') AS \"Time\"\n"
                "FROM latest\n"
                "WHERE ts IS NOT NULL"
            )
        if "panels" in panel:
            _patch_tide_wind_time_panel(panel["panels"], group_id, legacy_wind_height_m)


def _shift_panels_in_grid_region(
    panels: list,
    min_x: int,
    min_y: int,
    max_y: int,
    y_delta: int,
) -> None:
    for panel in panels:
        grid = panel.get("gridPos", {})
        if grid.get("x", 0) >= min_x and min_y <= grid.get("y", 0) <= max_y:
            grid["y"] = max(0, grid.get("y", 0) + y_delta)
        if "panels" in panel:
            _shift_panels_in_grid_region(panel["panels"], min_x, min_y, max_y, y_delta)


def _remove_panel_by_title_prefix(panels: list, title_prefix: str) -> int | None:
    for index, panel in enumerate(list(panels)):
        if str(panel.get("title", "")).startswith(title_prefix):
            removed = panels.pop(index)
            return removed.get("gridPos", {}).get("y")
        if "panels" in panel:
            removed_y = _remove_panel_by_title_prefix(panel["panels"], title_prefix)
            if removed_y is not None:
                return removed_y
    return None


def _shift_panels_below(panels: list, y_threshold: int, y_delta: int) -> None:
    for panel in panels:
        grid = panel.get("gridPos", {})
        if grid.get("y", 0) > y_threshold:
            grid["y"] = max(0, grid.get("y", 0) + y_delta)
        if "panels" in panel:
            _shift_panels_below(panel["panels"], y_threshold, y_delta)


def _patch_tide_wind_speed_direction_panel(panels: list, group_id: str, legacy_wind_height_m: int) -> None:
    for panel in panels:
        title = str(panel.get("title", ""))
        if title.startswith("Wind Speed Forecast"):
            panel["title"] = "Wind Forecast (${height_m} m)"
            panel["description"] = (
                "Wind speed forecast with predicted wind direction on the right y-axis. "
            )
            panel["targets"][0]["rawSql"] = _tide_wind_overlay_sql(group_id, legacy_wind_height_m)
            overrides = panel.setdefault("fieldConfig", {}).setdefault("overrides", [])
            overrides.extend(_wind_direction_overlay_overrides())
            return
        if "panels" in panel:
            _patch_tide_wind_speed_direction_panel(panel["panels"], group_id, legacy_wind_height_m)


def _tide_wind_overlay_sql(group_id: str, legacy_wind_height_m: int) -> str:
    return (
        "WITH src AS (\n"
        "  SELECT\n"
        "    timestamp AS \"time\",\n"
        "    windspdmean,\n"
        "    windspdlb,\n"
        "    windspdub,\n"
        "    winddirmean\n"
        "  FROM forecast_data\n"
        "  WHERE\n"
        "    windspdmean IS NOT NULL\n"
        "    AND locname = '$location'\n"
        f"    AND group_id = '{group_id}'\n"
        "    AND model = '$model'\n"
        "    AND '$model' NOT IN ('wind_nbm', 'tide_nbm')\n"
        f"    AND (height_m = ${{height_m:raw}} OR (height_m IS NULL AND ${{height_m:raw}} = {legacy_wind_height_m}))\n"
        "    AND $__timeFilter(timestamp)\n"
        "  UNION ALL\n"
        "  SELECT\n"
        "    forecastdtutc AS \"time\",\n"
        "    windspdmean,\n"
        "    windspdlb,\n"
        "    windspdub,\n"
        "    winddirmean\n"
        "  FROM forecast_data\n"
        "  WHERE\n"
        "    windspdmean IS NOT NULL\n"
        "    AND locname = '$location'\n"
        f"    AND group_id = '{group_id}'\n"
        "    AND model = '$model'\n"
        "    AND '$model' IN ('wind_nbm', 'tide_nbm')\n"
        f"    AND (height_m = ${{height_m:raw}} OR (height_m IS NULL AND ${{height_m:raw}} = {legacy_wind_height_m}))\n"
        "    AND startdt = (\n"
        "      SELECT MAX(startdt)\n"
        "      FROM forecast_data\n"
        "      WHERE startdt <= $__timeFrom()\n"
        "        AND locname = '$location'\n"
        f"        AND group_id = '{group_id}'\n"
        "        AND model = '$model'\n"
        f"        AND (height_m = ${{height_m:raw}} OR (height_m IS NULL AND ${{height_m:raw}} = {legacy_wind_height_m}))\n"
        "        AND windspdmean IS NOT NULL\n"
        "    )\n"
        ")\n"
        "SELECT\n"
        "  src.\"time\",\n"
        "  src.windspdmean,\n"
        "  src.windspdlb,\n"
        "  src.windspdub,\n"
        "  w.v1_s0 AS winddirmean,\n"
        "  w.v1_sm1 AS winddirmean_sm1,\n"
        "  w.v1_sp1 AS winddirmean_sp1,\n"
        "  (MOD(MOD(src.winddirmean::numeric, 360) + 360, 360))::float AS winddir_tooltip\n"
        "FROM src\n"
        "JOIN public.fn_wrap_directions(\n"
        "  (SELECT array_agg(\"time\" ORDER BY \"time\") FROM src),\n"
        "  (SELECT array_agg(winddirmean ORDER BY \"time\") FROM src)\n"
        ") w USING (\"time\")\n"
        "ORDER BY src.\"time\""
    )


def _wind_direction_overlay_overrides() -> list[dict]:
    base_properties = [
        {"id": "unit", "value": "degree"},
        {"id": "min", "value": 0},
        {"id": "max", "value": 360},
        {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}},
        {"id": "custom.axisPlacement", "value": "right"},
        {"id": "custom.axisLabel", "value": "Direction (\u00b0)"},
        {"id": "custom.lineStyle", "value": {"dash": [6, 6], "fill": "dash"}},
        {"id": "custom.lineWidth", "value": 2},
        {"id": "custom.fillOpacity", "value": 0},
        {"id": "custom.showPoints", "value": "auto"},
    ]
    return [
        {
            "matcher": {"id": "byName", "options": "winddirmean"},
            "properties": [
                {"id": "displayName", "value": "Predicted Wind Direction"},
                *base_properties,
                {"id": "custom.hideFrom", "value": {"legend": False, "tooltip": True, "viz": False}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "winddirmean_sm1"},
            "properties": [
                *base_properties,
                {"id": "custom.hideFrom", "value": {"legend": True, "tooltip": True, "viz": False}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "winddirmean_sp1"},
            "properties": [
                *base_properties,
                {"id": "custom.hideFrom", "value": {"legend": True, "tooltip": True, "viz": False}},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "winddir_tooltip"},
            "properties": [
                {"id": "displayName", "value": "Predicted Wind Direction"},
                {"id": "unit", "value": "degree"},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}},
                {"id": "custom.axisPlacement", "value": "right"},
                {"id": "custom.lineWidth", "value": 0},
                {"id": "custom.showPoints", "value": "never"},
                {"id": "custom.hideFrom", "value": {"legend": True, "tooltip": False, "viz": False}},
            ],
        },
    ]


def _patch_tide_display_units(dashboard: dict, display_unit: str) -> None:
    grafana_unit = TIDE_GRAFANA_UNITS.get(display_unit, f"suffix:{display_unit}")
    field_names = {
        "astrotide": "astrotide_display",
        "prelimtide": "prelimtide_display",
        "tidemean": "tidemean_display",
        "tidelb": "tidelb_display",
        "tideub": "tideub_display",
    }
    _patch_tide_sql(dashboard.get("panels", []))
    _replace_exact_json_values(dashboard, field_names)
    _replace_json_value(dashboard, "lengthft", grafana_unit)
    _replace_json_value(dashboard, "Tide Height (ft", f"Tide Height ({display_unit}")


def _patch_legacy_wind_height(value, legacy_wind_height_m: int) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str):
                child = child.replace("COALESCE(height_m, 20)", f"COALESCE(height_m, {legacy_wind_height_m})")
                child = child.replace(
                    "height_m IS NULL AND ${height_m:raw} = 20",
                    f"height_m IS NULL AND ${{height_m:raw}} = {legacy_wind_height_m}",
                )
                child = child.replace(
                    "height_m IS NULL AND 20 = 20",
                    f"height_m IS NULL AND {legacy_wind_height_m} = {legacy_wind_height_m}",
                )
                value[key] = child
            else:
                _patch_legacy_wind_height(child, legacy_wind_height_m)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str):
                child = child.replace("COALESCE(height_m, 20)", f"COALESCE(height_m, {legacy_wind_height_m})")
                child = child.replace(
                    "height_m IS NULL AND ${height_m:raw} = 20",
                    f"height_m IS NULL AND ${{height_m:raw}} = {legacy_wind_height_m}",
                )
                child = child.replace(
                    "height_m IS NULL AND 20 = 20",
                    f"height_m IS NULL AND {legacy_wind_height_m} = {legacy_wind_height_m}",
                )
                value[index] = child
            else:
                _patch_legacy_wind_height(child, legacy_wind_height_m)


def _patch_tide_sql(panels: list) -> None:
    replacements = {
        "astrotide": "astrotide_display",
        "prelimtide": "prelimtide_display",
        "tidemean": "tidemean_display",
        "tidelb": "tidelb_display",
        "tideub": "tideub_display",
    }

    for panel in panels:
        for target in panel.get("targets", []):
            raw_sql = target.get("rawSql")
            if not raw_sql or "tide" not in raw_sql.lower():
                continue
            raw_sql = raw_sql.replace("FROM forecast_data", "FROM public.v_forecast_data_display")
            raw_sql = raw_sql.replace("FROM public.forecast_data", "FROM public.v_forecast_data_display")
            for source_column, display_column in replacements.items():
                raw_sql = re.sub(rf"\b{source_column}\b", display_column, raw_sql)
            target["rawSql"] = raw_sql
        if "panels" in panel:
            _patch_tide_sql(panel["panels"])


def _replace_json_value(value, old: str, new: str):
    if isinstance(value, dict):
        for key, child in value.items():
            if child == old:
                value[key] = new
            elif isinstance(child, str) and old in child:
                value[key] = child.replace(old, new)
            else:
                _replace_json_value(child, old, new)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if child == old:
                value[index] = new
            elif isinstance(child, str) and old in child:
                value[index] = child.replace(old, new)
            else:
                _replace_json_value(child, old, new)


def _replace_exact_json_values(value, replacements: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "rawSql":
                continue
            if isinstance(child, str) and child in replacements:
                value[key] = replacements[child]
            else:
                _replace_exact_json_values(child, replacements)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str) and child in replacements:
                value[index] = replacements[child]
            else:
                _replace_exact_json_values(child, replacements)


def _patch_non_nbm_wind_startdt_filters(panels: list, group_expr: str) -> None:
    """Filter combined wind forecast panels to one selected run for non-wind_nbm models."""
    old = "  AND '$model' <> 'wind_nbm'\n  AND $__timeFilter(timestamp)"
    new = (
        "  AND '$model' <> 'wind_nbm'\n"
        "  AND (height_m = ${height_m:raw} OR (height_m IS NULL AND ${height_m:raw} = 20))\n"
        "  AND startdt = (\n"
        "    SELECT MAX(startdt)\n"
        "    FROM forecast_data\n"
        "    WHERE startdt <= $__timeFrom()\n"
        "      AND locname = '$location'\n"
        f"      AND group_id = {group_expr}\n"
        "      AND model = '$model'\n"
        "      AND (height_m = ${height_m:raw} OR (height_m IS NULL AND ${height_m:raw} = 20))\n"
        "      AND windspdmean IS NOT NULL\n"
        "  )\n"
        "  AND $__timeFilter(timestamp)"
    )

    for panel in panels:
        for target in panel.get("targets", []):
            raw_sql = target.get("rawSql")
            if raw_sql and "windspdmean IS NOT NULL" in raw_sql:
                target["rawSql"] = raw_sql.replace(old, new)
        if "panels" in panel:
            _patch_non_nbm_wind_startdt_filters(panel["panels"], group_expr)


def _patch_group_id(panels: list, group_id: str) -> None:
    """Recursively replace $group placeholder in all rawSql targets."""
    for panel in panels:
        # Process targets at this level
        for target in panel.get("targets", []):
            if "rawSql" in target:
                target["rawSql"] = target["rawSql"].replace(
                    "group_id = '$group'",
                    f"group_id = '{group_id}'",
                )
        # Recurse into nested panels (rows / collapsed panels)
        if "panels" in panel:
            _patch_group_id(panel["panels"], group_id)


def _parse_existing_group_providers(yaml_path: Path) -> dict[str, dict]:
    """
    Parse the existing dashboards.yaml and return a dict of group_id → provider
    metadata for all per-group providers (those whose path ends with
    groups/<group_id>).

    Returns:
        {group_id: {"display_name": str, "dtypes_str": str}, ...}
    """
    existing: dict[str, dict] = {}
    if not yaml_path.exists():
        return existing

    text = yaml_path.read_text()
    # Match provider blocks by their path line pointing into groups/
    for match in re.finditer(
        r"#\s*(.+?)\s*\(([^)]*)\)\s*\n"
        r"\s*- name:\s*(.+)\n"
        r"(?:.*\n)*?"
        r"\s*path:\s*/etc/grafana/provisioning/dashboards/groups/(\S+)",
        text,
    ):
        display_name = match.group(3).strip()
        dtypes_str = match.group(2).strip()
        group_id = match.group(4).strip()
        existing[group_id] = {
            "display_name": display_name,
            "dtypes_str": dtypes_str,
        }
    return existing


def write_dashboards_yaml(
    output_root: Path,
    group_dtypes: dict[str, set[str]],
) -> None:
    """
    Update dashboards.yaml to include providers for:
    - Internal Dashboards (internal/)
    - Customer Dashboards (customer/ — admin/reference only)
    - One provider per group folder (groups/<group_id>/)

    This function is **additive**: existing per-group providers that are not in
    the current ``group_dtypes`` are preserved.  Providers for groups that *are*
    in ``group_dtypes`` are updated with the latest data-type annotation.
    """
    yaml_path = output_root / "dashboards.yaml"

    # Merge: existing groups are preserved, current run updates/adds entries.
    existing = _parse_existing_group_providers(yaml_path)

    # Build the merged set — current run wins for groups it touches.
    merged: dict[str, tuple[str, str]] = {}  # group_id → (display_name, dtypes_str)
    for gid, meta in existing.items():
        merged[gid] = (meta["display_name"], meta["dtypes_str"])

    for gid, dtypes in group_dtypes.items():
        display_name = GROUP_DISPLAY_NAMES.get(gid, gid)
        dtypes_str = ", ".join(sorted(dtypes)) if dtypes else "none"
        merged[gid] = (display_name, dtypes_str)

    lines = [
        "# Grafana dashboard provisioning — Skywind Infrastructure",
        "# Auto-generated by generate_group_dashboards.py",
        "# DO NOT EDIT BY HAND — re-run the script after adding new groups.",
        "#",
        "# Two base providers (Internal / Customer reference) plus one provider",
        "# per customer group, each isolated in its own Grafana folder.",
        "#",
        "# disableDeletion: false — dashboard is removed when the JSON file is",
        "#                          deleted (keeps provisioned folders in sync).",
        "# editable: false        — changes made in the UI are not saved to disk.",
        "",
        "apiVersion: 1",
        "",
        "providers:",
        "  # ----------------------------------------------------------------",
        "  # Internal dashboards — Skywind staff only",
        "  # ----------------------------------------------------------------",
        "  - name: Internal Dashboards",
        "    orgId: 1",
        "    folder: Internal Dashboards",
        "    folderUid: internal-dashboards",
        "    type: file",
        "    disableDeletion: true",
        "    editable: false",
        "    updateIntervalSeconds: 30",
        "    options:",
        "      path: /etc/grafana/provisioning/dashboards/internal",
        "",
        "  # ----------------------------------------------------------------",
        "  # Shared customer templates — visible to Internal team only.",
        "  # Customers are routed to their group-specific folder below.",
        "  # ----------------------------------------------------------------",
        "  - name: Customer Dashboards",
        "    orgId: 1",
        "    folder: Customer Dashboards",
        "    folderUid: customer-dashboards",
        "    type: file",
        "    disableDeletion: true",
        "    editable: false",
        "    updateIntervalSeconds: 30",
        "    options:",
        "      path: /etc/grafana/provisioning/dashboards/customer",
        "",
        "  # ----------------------------------------------------------------",
        "  # Per-customer-group folders",
        "  # Each folder is restricted to one Grafana team via API permissions",
        "  # (see scripts/provision_grafana_access.sh).",
        "  # Only dashboards for data types the group actually has are written",
        "  # to the group's subdirectory, so empty-data dashboards are never",
        "  # provisioned and will not appear in the Grafana folder.",
        "  # ----------------------------------------------------------------",
    ]

    for group_id in sorted(merged.keys()):
        display_name, dtypes_str = merged[group_id]
        folder_uid = sanitise_folder_uid(group_id)
        lines += [
            "",
            f"  # {display_name} ({dtypes_str})",
            f"  - name: {display_name}",
            "    orgId: 1",
            f"    folder: {display_name}",
            f"    folderUid: {folder_uid}",
            "    type: file",
            "    disableDeletion: false",
            "    editable: false",
            "    updateIntervalSeconds: 30",
            "    options:",
            f"      path: /etc/grafana/provisioning/dashboards/groups/{group_id}",
        ]

    yaml_path.write_text("\n".join(lines) + "\n")
    log.info("Wrote %s", yaml_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate per-group Grafana dashboard JSON files."
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="PostgreSQL connection string.  Falls back to .env POSTGRES_PASSWORD.",
    )
    parser.add_argument(
        "--templates",
        default=None,
        help="Path to customer dashboard templates directory.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Root provisioning directory.",
    )
    parser.add_argument(
        "--groups",
        default=None,
        help=(
            "Comma-separated group_ids to process. "
            "Data types are discovered from DB rows for each listed group."
        ),
    )
    parser.add_argument(
        "--group-types-json",
        default=None,
        metavar="JSON",
        help=(
            "JSON object mapping group_id → list of data types.  Skips DB query. "
            'Example: \'{"grp_clearwater":["tide"],"grp_corpuschristi":["visibility"]}\''
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be generated without writing files.",
    )
    args = parser.parse_args()

    # Resolve paths relative to the script's parent (supabase/docker/)
    script_dir = Path(__file__).resolve().parent
    docker_dir = script_dir.parent
    env_path = docker_dir / ".env"

    env = load_env(env_path)
    postgres_password = env.get("POSTGRES_PASSWORD", "")

    templates_dir = Path(args.templates) if args.templates else (
        docker_dir / "volumes" / "grafana" / "provisioning" / "dashboards" / "customer"
    )
    output_root = Path(args.output) if args.output else (
        docker_dir / "volumes" / "grafana" / "provisioning" / "dashboards"
    )

    if not templates_dir.exists():
        log.error("Templates directory not found: %s", templates_dir)
        sys.exit(1)
    if not output_root.exists():
        log.error("Output directory not found: %s", output_root)
        sys.exit(1)

    # -- Load templates -------------------------------------------------------
    templates: dict[str, dict] = {}
    for tmpl_name, dtype in TEMPLATE_NAMES.items():
        tmpl_path = templates_dir / tmpl_name
        if tmpl_path.exists():
            with open(tmpl_path) as fh:
                templates[dtype] = json.load(fh)
            log.info("Loaded template: %s", tmpl_path)
        else:
            log.warning("Template not found, skipping %s: %s", dtype, tmpl_path)

    # -- Discover or accept group list ----------------------------------------
    if args.group_types_json:
        raw = json.loads(args.group_types_json)
        group_dtypes: dict[str, set[str]] = {k: set(v) for k, v in raw.items()}
    elif args.groups:
        selected_groups = [g.strip() for g in args.groups.split(",") if g.strip()]
        conn_str = args.db_url or (
            f"postgresql://postgres:{postgres_password}@127.0.0.1:5432/postgres"
        )
        log.info(
            "Discovering data types for selected groups from DB: %s",
            conn_str.split("@")[-1],
        )
        group_dtypes = discover_selected_groups(conn_str, selected_groups)
    else:
        conn_str = args.db_url or (
            f"postgresql://postgres:{postgres_password}@127.0.0.1:5432/postgres"
        )
        log.info("Discovering groups from DB: %s", conn_str.split("@")[-1])
        group_dtypes = discover_groups(conn_str)

    if not group_dtypes:
        log.warning("No groups found.  Nothing to generate.")
        return

    # Build connection string once for location count queries
    conn_str = args.db_url or (
        f"postgresql://postgres:{postgres_password}@127.0.0.1:5432/postgres"
    )

    log.info("Groups discovered: %s", list(group_dtypes.keys()))

    # -- Generate dashboards --------------------------------------------------
    groups_root = output_root / "groups"

    for group_id, dtypes in sorted(group_dtypes.items()):
        # Skip shared/internal-only groups that should never become customer folders.
        if group_id in ("grp_internal",):
            log.info("Skipping shared reference group: %s", group_id)
            continue

        display_name = GROUP_DISPLAY_NAMES.get(group_id, group_id)
        group_dir = groups_root / group_id

        if not args.dry_run:
            group_dir.mkdir(parents=True, exist_ok=True)

        # Load customer profile metadata for display-name overrides, feature flags, etc.
        customer_metadata = (
            {} if args.dry_run
            else load_customer_metadata(conn_str, group_id)
        )

        combined_source_types: set[str] = set()
        for combined_type in COMBINED_DASHBOARD_GROUPS.get(group_id, ()):
            source_types = COMBINED_DASHBOARDS[combined_type]
            if all(source_type in dtypes for source_type in source_types):
                missing_templates = [source_type for source_type in source_types if source_type not in templates]
                if missing_templates:
                    log.warning(
                        "  [%s] missing templates for %s, skipping %s",
                        group_id,
                        ", ".join(missing_templates),
                        combined_type,
                    )
                    continue

                default_location = None if args.dry_run else get_default_location(
                    conn_str, group_id, source_types[0]
                )
                wind_height_count = -1 if args.dry_run else count_group_wind_heights(
                    conn_str, group_id
                )
                tide_display_unit = (
                    "ft" if args.dry_run or "tide" not in source_types
                    else get_group_unit_preference(conn_str, group_id, "tide_height", "ft")
                )
                dash = make_combined_group_dashboard(
                    templates, group_id, combined_type, display_name,
                    default_location=default_location,
                    wind_height_count=wind_height_count,
                    tide_display_unit=tide_display_unit,
                    customer_metadata=customer_metadata,
                    conn_str=conn_str,
                )
                out_file = group_dir / f"{combined_type}.json"
                combined_source_types.update(source_types)
                dtypes.add(combined_type)

                if args.dry_run:
                    log.info("  [DRY-RUN] would write %s (uid=%s)", out_file, dash["uid"])
                else:
                    with open(out_file, "w") as fh:
                        json.dump(dash, fh, indent=2)
                        fh.write("\n")
                    log.info("  Wrote %s", out_file)

        for dtype in sorted(DATA_TYPE_COLUMNS.keys()):
            if dtype in combined_source_types:
                out_file = group_dir / f"{dtype}.json"
                if not args.dry_run and out_file.exists():
                    out_file.unlink()
                    log.info("  [%s] removed stale %s", group_id, out_file.name)
                log.info("  [%s] skipping %s (covered by combined dashboard)", group_id, dtype)
                continue

            if dtype not in dtypes:
                log.info("  [%s] skipping %s (no data)", group_id, dtype)
                # Remove stale file if it exists
                out_file = group_dir / f"{dtype}.json"
                if not args.dry_run and out_file.exists():
                    out_file.unlink()
                    log.info("  [%s] removed stale %s", group_id, out_file.name)
                continue

            if dtype not in templates:
                log.warning("  [%s] no template for %s, skipping", group_id, dtype)
                continue

            # Query location count for panel filtering (especially for tide dashboards)
            location_count = -1 if args.dry_run else count_group_locations(conn_str, group_id, dtype)
            default_location = None if args.dry_run else get_default_location(conn_str, group_id, dtype)
            wind_height_count = (
                -1 if args.dry_run or dtype != "wind" else count_group_wind_heights(conn_str, group_id)
            )
            tide_display_unit = (
                "ft" if args.dry_run or dtype != "tide"
                else get_group_unit_preference(conn_str, group_id, "tide_height", "ft")
            )

            dash = make_group_dashboard(
                templates[dtype], group_id, dtype, display_name,
                location_count=location_count,
                default_location=default_location,
                wind_height_count=wind_height_count,
                tide_display_unit=tide_display_unit,
                customer_metadata=customer_metadata,
                conn_str=conn_str,
            )
            out_file = group_dir / f"{dtype}.json"

            if args.dry_run:
                log.info("  [DRY-RUN] would write %s (uid=%s)", out_file, dash["uid"])
            else:
                with open(out_file, "w") as fh:
                    json.dump(dash, fh, indent=2)
                    fh.write("\n")
                log.info("  Wrote %s", out_file)

    # -- Rewrite dashboards.yaml ----------------------------------------------
    if not args.dry_run:
        write_dashboards_yaml(output_root, group_dtypes)
    else:
        log.info("[DRY-RUN] would rewrite dashboards.yaml")

    log.info("Done.")


if __name__ == "__main__":
    main()
