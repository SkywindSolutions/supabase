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
    "grp_boston":     "Boston Inner Harbor Forecasts",
    "grp_cape_cod":   "Cape Cod Bay Forecasts",
    "grp_portland":   "Portland ME Forecasts",
    "grp_gloucester": "Gloucester MA Forecasts",
    "group_alpha":    "Alpha Maritime Forecasts",
    "group_beta":     "Beta Offshore Forecasts",
}

# Data-type → primary column used to detect whether a group has that data
DATA_TYPE_COLUMNS: dict[str, str] = {
    "visibility": "vismean",
    "wind":       "windspdmean",
    "tide":       "tidemean",
}

# Template filename → data type
TEMPLATE_NAMES: dict[str, str] = {
    "visibility.json": "visibility",
    "wind.json":       "wind",
    "tide.json":       "tide",
}


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
        f"COUNT({col}) > 0  AS has_{dtype},"
        for dtype, col in DATA_TYPE_COLUMNS.items()
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


def sanitise_folder_uid(group_id: str) -> str:
    """Convert a group_id to a safe Grafana folder UID (max 40 chars)."""
    uid = re.sub(r"[^a-zA-Z0-9\-]", "-", group_id)
    return f"cust-{uid}"[:40]


def make_group_dashboard(
    template: dict,
    group_id: str,
    dtype: str,
    display_name: str,
) -> dict:
    """
    Transform a shared customer dashboard template into a group-specific
    dashboard with the group_id hardcoded in all SQL queries.

    Changes applied:
    - Remove the `group` template variable (was visible — security hole)
    - Hardcode group_id in the `location` variable query
    - Hardcode group_id in every panel's rawSql
    - Update uid, title, and description
    """
    d = copy.deepcopy(template)

    type_labels = {
        "visibility": "Visibility",
        "wind":       "Wind",
        "tide":       "Tide",
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

    # -- Hardcode group_id in the `location` variable query ------------------
    for var in d["templating"]["list"]:
        if var.get("name") == "location":
            # Grafana stores the query in both "query" and "definition" fields
            for field in ("query", "definition"):
                if var.get(field):
                    var[field] = var[field].replace(
                        "group_id = '$group'",
                        f"group_id = '{group_id}'",
                    )
        # Clear the cached current value so Grafana picks the first real result
        var.pop("current", None)
        var.pop("options", None)

    # -- Hardcode group_id in all panel SQL ----------------------------------
    _patch_group_id(d.get("panels", []), group_id)

    return d


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


def write_dashboards_yaml(
    output_root: Path,
    group_dtypes: dict[str, set[str]],
) -> None:
    """
    Rewrite dashboards.yaml to include providers for:
    - Internal Dashboards (internal/)
    - Customer Dashboards (customer/ — admin/reference only)
    - One provider per group folder (groups/<group_id>/)

    The per-group providers are restricted to the data types that the group
    actually has data for — dashboards that don't exist on disk simply won't
    appear in Grafana.
    """
    yaml_path = output_root / "dashboards.yaml"

    lines = [
        "# Grafana dashboard provisioning — Skywind Infrastructure",
        "# Auto-generated by generate_group_dashboards.py",
        "# DO NOT EDIT BY HAND — re-run the script after adding new groups.",
        "#",
        "# Two base providers (Internal / Customer reference) plus one provider",
        "# per customer group, each isolated in its own Grafana folder.",
        "#",
        "# disableDeletion: true  — dashboard is not removed if the JSON file is",
        "#                          deleted (protects against accidental removal).",
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

    for group_id, dtypes in sorted(group_dtypes.items()):
        # Skip the shared reference groups — they use the shared customer/ folder
        if group_id in ("group_alpha", "group_beta"):
            continue
        display_name = GROUP_DISPLAY_NAMES.get(group_id, group_id)
        folder_uid = sanitise_folder_uid(group_id)
        dtypes_str = ", ".join(sorted(dtypes)) if dtypes else "none"
        lines += [
            "",
            f"  # {display_name} ({dtypes_str})",
            f"  - name: {display_name}",
            "    orgId: 1",
            f"    folder: {display_name}",
            f"    folderUid: {folder_uid}",
            "    type: file",
            "    disableDeletion: true",
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
        help="Comma-separated group_ids to process (skips DB query, all data types assumed).",
    )
    parser.add_argument(
        "--group-types-json",
        default=None,
        metavar="JSON",
        help=(
            "JSON object mapping group_id → list of data types.  Skips DB query. "
            'Example: \'{"grp_boston":["visibility"],"grp_cape_cod":["tide"]}\''
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
        # Manual list — assume all data types present
        group_dtypes: dict[str, set[str]] = {
            g.strip(): set(DATA_TYPE_COLUMNS.keys())
            for g in args.groups.split(",")
        }
    else:
        conn_str = args.db_url or (
            f"postgresql://postgres:{postgres_password}@127.0.0.1:5432/postgres"
        )
        log.info("Discovering groups from DB: %s", conn_str.split("@")[-1])
        group_dtypes = discover_groups(conn_str)

    if not group_dtypes:
        log.warning("No groups found.  Nothing to generate.")
        return

    log.info("Groups discovered: %s", list(group_dtypes.keys()))

    # -- Generate dashboards --------------------------------------------------
    groups_root = output_root / "groups"

    for group_id, dtypes in sorted(group_dtypes.items()):
        # Skip the old shared reference groups — they stay in customer/
        if group_id in ("group_alpha", "group_beta"):
            log.info("Skipping shared reference group: %s", group_id)
            continue

        display_name = GROUP_DISPLAY_NAMES.get(group_id, group_id)
        group_dir = groups_root / group_id

        if not args.dry_run:
            group_dir.mkdir(parents=True, exist_ok=True)

        for dtype in sorted(DATA_TYPE_COLUMNS.keys()):
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

            dash = make_group_dashboard(
                templates[dtype], group_id, dtype, display_name
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
