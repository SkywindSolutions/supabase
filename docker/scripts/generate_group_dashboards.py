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
    "group_alpha":       "Alpha Maritime Forecasts",
    "group_beta":        "Beta Offshore Forecasts",
    "grp_mobilebay":     "Mobile Bay Forecasts",
    "grp_pascagoula":     "Pascagoula Forecasts",
    "grp_portrichey":   "Port Richey Forecasts",
    "grp_clearwater":   "Clearwater Forecasts",
    "grp_corpuschristi": "Corpus Christi Forecasts",
    "grp_chatham":      "Chatham Forecasts",
    "grp_oceanCay":     "Ocean Cay Forecasts",
    "grp_sendero":      "Sendero Forecasts",
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
}

COMBINED_DASHBOARD_GROUPS: dict[str, tuple[str, ...]] = {
    "grp_mobilebay": ("wind_visibility",),
    "grp_pascagoula": ("wind_visibility",),
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

    sql = """
        SELECT COUNT(DISTINCT COALESCE(height_m, 20))
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


def sanitise_folder_uid(group_id: str) -> str:
    """Convert a group_id to a safe Grafana folder UID (max 40 chars)."""
    uid = re.sub(r"[^a-zA-Z0-9\-]", "-", group_id)
    return f"cust-{uid}"[:40]


def make_group_dashboard(
    template: dict,
    group_id: str,
    dtype: str,
    display_name: str,
    location_count: int = -1,
    default_location: str | None = None,
    wind_height_count: int = -1,
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
    """
    d = copy.deepcopy(template)

    type_labels = {
        "visibility": "Visibility",
        "wind":       "Wind",
        "tide":       "Tide",
        "wind_visibility": "Wind & Visibility",
    }
    label = type_labels.get(dtype, dtype.title())

    # -- Update dashboard metadata -------------------------------------------
    d["uid"] = f"{dtype}-{group_id}"
    d["title"] = f"{display_name} — {label} Forecast"
    d["description"] = (
        f"{label} forecast data for {display_name}. "
        "Customer-facing dashboard. No observation data."
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
) -> dict:
    """Build a single customer dashboard from multiple customer templates."""
    source_types = COMBINED_DASHBOARDS[combined_type]
    d = copy.deepcopy(templates[source_types[0]])

    d["uid"] = f"{combined_type}-{group_id}"
    d["title"] = f"{display_name} — Wind & Visibility Forecast"
    d["description"] = (
        f"Wind and visibility forecast data for {display_name}. "
        "Customer-facing dashboard. No observation data."
    )
    d["editable"] = False
    d["tags"] = ["customer", "wind", "visibility", "combined"]
    d["time"] = {"from": "now", "to": "now+24h"}

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
    )
    _patch_non_nbm_wind_startdt_filters(d.get("panels", []), f"'{group_id}'")
    d["time"] = {"from": "now", "to": "now+24h"}
    d["tags"] = ["customer", "wind", "visibility", "combined"]
    return d


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
        if gid in ("group_alpha", "group_beta"):
            continue
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
        if group_id in ("group_alpha", "group_beta", "grp_internal"):
            log.info("Skipping shared reference group: %s", group_id)
            continue

        display_name = GROUP_DISPLAY_NAMES.get(group_id, group_id)
        group_dir = groups_root / group_id

        if not args.dry_run:
            group_dir.mkdir(parents=True, exist_ok=True)

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
                dash = make_combined_group_dashboard(
                    templates, group_id, combined_type, display_name,
                    default_location=default_location,
                    wind_height_count=wind_height_count,
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

            dash = make_group_dashboard(
                templates[dtype], group_id, dtype, display_name,
                location_count=location_count,
                default_location=default_location,
                wind_height_count=wind_height_count,
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
