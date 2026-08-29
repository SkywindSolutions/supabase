#!/usr/bin/env python3
"""
Build groups/grp_manson/wind_tide.json from the proven Gator wind_tide.json
production dashboard.

Reuse strategy (per task: reuse proven patterns, verify correctness):
  - Gator's wind_tide.json is itself derived from the Hertz template (via
    build_gator_dashboard.py) and is the closest match to Manson's data
    profile (single-product wind+tide combined dashboard, thunderstorm +
    sea-state panels included, wind_nbm model, kt-native display).
  - Verified in DB (2026-08-24): grp_manson has the SAME data profile as
    grp_gator for every panel in the Gator dashboard:
      * wind_nbm  (windspdmean/lb/ub, winddirmean) — 4,890 rows, height 10
      * wind_nbm.pot (thunderstorm) — 2,458 rows
      * gfs_wave  (htsgw/perpw/dirpw sea state) — 49 rows
      * tide_blend (tidemean/lb/ub, datum MSL ft) — 10,973 rows
    (grp_manson additionally has tide_nbm/tide_astro, which the wind+tide
    dashboard does not query — no extra panels needed.)
  - No rainfall (qpf6h/12h/24h are NULL — Hertz-specific), no gust column,
    no hail columns for grp_manson — matching gator.
  - Single location: "Manson Vermilion" (locid 8766072, Vermilion Bay LA),
    timezone America/Chicago.
  - No Location Coordinates panel (matches gator; coordinates_display=false).
  - Wind Forecast direction axis: 8-point compass value-mappings
    (N/NE/E/SE/S/SW/W/NW) inherited from the Gator template — the h=12 panel
    height makes uPlot tick every 50 deg, which the octant mappings label
    exactly (see build_gator_dashboard.py for the transform).
"""
import copy
import json
import sys
from pathlib import Path

GATOR = Path(__file__).resolve().parents[1] / "volumes" / "grafana" / "provisioning" / "dashboards" / "groups" / "grp_gator" / "wind_tide.json"
OUT = Path(__file__).resolve().parents[1] / "volumes" / "grafana" / "provisioning" / "dashboards" / "groups" / "grp_manson" / "wind_tide.json"

GID = "grp_manson"


def main() -> None:
    with open(GATOR) as fh:
        d = json.load(fh)

    # ------------------------------------------------------------ group id
    # Replace EVERY grp_gator reference (panel SQL, variable queries, etc.)
    # with grp_manson.
    def walk_replace(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    obj[k] = v.replace("grp_gator", GID)
                else:
                    walk_replace(v)
        elif isinstance(obj, list):
            for v in obj:
                walk_replace(v)

    walk_replace(d)

    # ------------------------------------------------------------- metadata
    d["uid"] = "wind_tide-grp_manson"
    d["title"] = "Manson Forecasts \u2014 Wind & Tide Forecast"
    d["description"] = (
        "Wind, Tide, and Sea State forecast data for Manson Construction "
        "locations (Manson Vermilion). Customer-facing dashboard."
    )
    d["tags"] = ["customer", "manson", "wind", "tide", "combined"]
    d["refresh"] = "5m"
    d["timezone"] = "utc"
    d["time"] = {"from": "now", "to": "now+7d"}
    d["version"] = 1

    # ------------------------------------------------ variable defaults
    for var in d.get("templating", {}).get("list", []):
        name = var.get("name")
        if name == "model":
            # same model as gator — wind_nbm verified for manson
            var["current"] = {"text": "wind_nbm", "value": "wind_nbm"}
        if name == "location":
            # single location
            var["current"] = {"text": "Manson Vermilion", "value": "Manson Vermilion"}
        if name == "location_tz":
            # America/Chicago (Vermilion Bay, LA) — fallback via
            # forecast_locations/customer_groups lookup already in place
            var["current"] = {"text": "America/Chicago", "value": "America/Chicago"}

    # ------------------------------------------------ display-name overrides
    # The customer-facing location name must appear in tables/dropdowns while
    # SQL filters stay on the real locname.  Mirror the generator's
    # _patch_cte_display_names pattern for the Tide by Location table.
    for p in d["panels"]:
        if p.get("title") == "Tide by Location":
            for t in p.get("targets", []):
                sql = t.get("rawSql", "")
                sql = sql.replace(
                    'b.locname AS "Location",',
                    "COALESCE("
                    "(SELECT g2.metadata->'display_name_overrides'->>b.locname "
                    "FROM public.customer_groups g2 "
                    f"WHERE g2.group_id = '{GID}' "
                    "AND g2.metadata ? 'display_name_overrides'), "
                    "b.locname"
                    ') AS "Location",',
                )
                t["rawSql"] = sql

    # --------------------------------------------------------- footer/back
    # Gator has no footer; keep as-is (no home.json for Manson either).

    # --------------------------------------------------------- write out
    d["panels"] = d["panels"]

    with open(OUT, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {OUT}")
    print(f"  panels: {len(d['panels'])}")
    for p in d["panels"]:
        g = p.get("gridPos", {})
        print(
            f"    id={p.get('id')} y={g.get('y')} x={g.get('x')} w={g.get('w')} h={g.get('h')} "
            f"{p.get('type')} title={p.get('title')!r}"
        )


if __name__ == "__main__":
    sys.exit(main())