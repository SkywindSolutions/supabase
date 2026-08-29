#!/usr/bin/env python3
"""
Build groups/grp_gator/wind_tide.json from the proven Hertz wind_tide.json
production dashboard (the ONLY existing group with a wind_tide dashboard).

Reuse strategy (per task: reuse proven patterns, verify correctness):
  - Keep Hertz's panel structure: Wind row + Wind Forecast (time series with
    bounds + direction overlay) + Current Time + Wind Speed/Direction/Gusts
    stats, then Tide row + Tide Forecast + Current Tide + Tide by Location +
    Tide Forecast Data table.
  - DROP panels whose data does not exist for grp_gator (verified in DB):
      * Rainfall (grp_gator.qpf6h/qpf12h/qpf24h are NULL everywhere — Hertz-
        specific columns populated only by push_hertz_forecast.py)
  - Hertz's single "Prob of Thunderstorms" stat panel is dropped and replaced
    with two panels (Penobscot hail.json pattern, verified: wind_nbm.pot is
    now populated for all 3 locations):
      * Max Prob of Thunderstorms (24h)
      * Peak Prob Time
  - Sea state panels are added (Pascagoula/Penobscot pattern, verified:
    gfs_wave htsgw/perpw/dirpw are now populated for all 3 locations),
    positioned as their own "Sea State" row above the Tide Forecast row.
  - No Location Coordinates panel (removed per customer feedback — the
    location dropdown already shows the customer-facing display name).
  - Wind units: Hertz converts kt->mph (storage is kt).  Gator's wind_nbm is
    also stored in kt — display kt natively like Pascagoula/Penobscot marine
    dashboards (velocityknot, no conversion).
  - Model: Gator wind = wind_nbm (verified).  Hertz hardcodes 'wind_nbm' in
    the wind plot and uses $model elsewhere; keep the same pattern but set
    the $model default to wind_nbm.
  - Startdt-selection subqueries: keep the proven Hertz/Penobscot pattern so
    the 7-day forecast is not truncated by the selected run.
"""
import copy
import json
import sys
from pathlib import Path

HERTZ = Path(__file__).resolve().parents[1] / "volumes" / "grafana" / "provisioning" / "dashboards" / "groups" / "grp_hertz" / "wind_tide.json"
OUT = Path(__file__).resolve().parents[1] / "volumes" / "grafana" / "provisioning" / "dashboards" / "groups" / "grp_gator" / "wind_tide.json"

GID = "grp_gator"


def main() -> None:
    with open(HERTZ) as fh:
        d = json.load(fh)

    # ------------------------------------------------------------ group id
    # Replace EVERY grp_hertz reference (panel SQL, variable queries, etc.)
    # with grp_gator FIRST, before panels are deep-copied for filtering.
    def walk_replace(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    obj[k] = v.replace("grp_hertz", GID)
                else:
                    walk_replace(v)
        elif isinstance(obj, list):
            for v in obj:
                walk_replace(v)

    walk_replace(d)

    # ------------------------------------------------------------------ panels
    # Drop panels that have no data for grp_gator:
    #   - Rainfall Forecast row + Rain Next 6h/12h/24h (no qpf)
    #   - Hertz's single-value "Prob of Thunderstorms" stat (replaced below
    #     with a Max Prob / Peak Time pair)
    #   - Back-to-home footer (no home.json for Gator)
    drop_titles = {
        "Prob of Thunderstorms",
        "Rainfall Forecast",
        "Rain Next 6h",
        "Rain Next 12h",
        "Rain Next 24h",
    }
    panels = []
    for p in d["panels"]:
        if p.get("title") in drop_titles:
            continue
        # the footer text panel links to Hertz home
        if p.get("type") == "text" and p.get("id") == 950:
            continue
        if p.get("type") == "row" and p.get("title") in ("Rainfall Forecast",):
            continue
        panels.append(copy.deepcopy(p))

    # ------------------------------------------------ reflow: collapse gaps
    # The rainfall section was removed, leaving a vertical gap where its row
    # used to be.  Find the first section row whose y is greater than the
    # max bottom edge of all panels above it, and shift that section (and
    # everything below) up to close the gap.  Runs top-down so consecutive
    # gaps collapse correctly.
    rows = [p for p in panels if p.get("type") == "row"]
    rows.sort(key=lambda p: p.get("gridPos", {}).get("y", 0))
    for row in rows:
        row_y = row.get("gridPos", {}).get("y", 0)
        above = [
            p for p in panels
            if p.get("type") != "row"
            and p.get("gridPos", {}).get("y", 0) + p.get("gridPos", {}).get("h", 0) <= row_y
        ]
        max_bottom = max((p.get("gridPos", {}).get("y", 0) + p.get("gridPos", {}).get("h", 0) for p in above), default=0)
        gap = row_y - max_bottom
        if gap <= 0:
            continue
        for p in panels:
            if p.get("gridPos", {}).get("y", 0) >= row_y:
                p["gridPos"]["y"] = max(0, p["gridPos"]["y"] - gap)

    # ------------------------------------------------ wind unit: kt (native)
    # Hertz stores kt in DB and multiplies by 1.15078 for mph display.
    # Gator wind is also stored in kt; marine customers (Pascagoula/Penobscot)
    # display kt natively.  Remove the mph conversion everywhere.
    def strip_mph(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, str):
                    value[k] = (
                        v.replace("windspdmean * 1.15078 AS windspdmean", "windspdmean AS windspdmean")
                         .replace("windspdlb * 1.15078 AS windspdlb", "windspdlb AS windspdlb")
                         .replace("windspdub * 1.15078 AS windspdub", "windspdub AS windspdub")
                         .replace("windspdub * 1.15078 AS gust", "windspdub AS gust")
                         .replace("windspdmean * 1.15078 AS windspd", "windspdmean AS windspd")
                         .replace("gust * 1.15078 AS gust", "gust AS gust")
                    )
                    if v == "velocitymph":
                        value[k] = "velocityknot"
                    elif "Wind Speed (mph)" in v:
                        value[k] = v.replace("Wind Speed (mph)", "Wind Speed (kt)")
                else:
                    strip_mph(v)
        elif isinstance(value, list):
            for v in value:
                strip_mph(v)

    strip_mph(panels)

    # ------------------------------------------------ model variable default
    # Gator wind model is wind_nbm.  Hertz's model dropdown query filters
    # gust IS NOT NULL (Hertz-only column) — switch to windspdmean.
    for var in d.get("templating", {}).get("list", []):
        name = var.get("name")
        for field in ("query", "definition"):
            q = var.get(field, "")
            if "gust IS NOT NULL" in q:
                var[field] = q.replace("gust IS NOT NULL", "windspdmean IS NOT NULL")
        if name == "model":
            var["current"] = {"text": "wind_nbm", "value": "wind_nbm"}
        if name == "location":
            # default to first location in display order (Eau Gallie Dredge)
            var["current"] = {"text": "Eau Gallie Dredge", "value": "Gator Dredge"}
        if name == "location_tz":
            # Hertz hard-codes Tampa/New Orleans; use the standard
            # forecast_locations/customer_groups lookup (Penobscot pattern).
            q = (
                "SELECT COALESCE("
                f"(SELECT timezone FROM public.forecast_locations WHERE group_id = '{GID}' AND locname = '$location' LIMIT 1), "
                f"(SELECT default_timezone FROM public.customer_groups WHERE group_id = '{GID}'), "
                "'UTC')"
            )
            var["query"] = q
            var["definition"] = q

    # ------------------------------------------------ display-name overrides
    # The customer-facing location names must appear in tables/dropdowns while
    # SQL filters stay on the real locname.  Mirror the generator's
    # _patch_cte_display_names pattern for the Tide by Location table.
    for p in panels:
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

    # ------------------------------------------------------------- metadata
    d["uid"] = "wind_tide-grp_gator"
    d["title"] = "Gator Forecasts \u2014 Wind & Tide Forecast"
    d["description"] = (
        "Wind, Tide, and Sea State forecast data for Gator Dredging locations "
        "(Eau Gallie Dredge, Eau Gallie DMMA Site, Eau Gallie Micco Disposal). "
        "Customer-facing dashboard."
    )
    d["tags"] = ["customer", "gator", "wind", "tide", "combined"]
    d["refresh"] = "5m"
    d["timezone"] = "utc"
    d["time"] = {"from": "now", "to": "now+7d"}
    d["version"] = 1

    # ---------------------------------------------------- rename Speed panel
    for p in panels:
        if p.get("title") == "Speed" and p.get("type") == "stat":
            p["title"] = "Wind Speed"
            for ov in p.get("fieldConfig", {}).get("overrides", []):
                for prop in ov.get("properties", []):
                    if prop.get("id") == "displayName" and prop.get("value") == "Speed":
                        prop["value"] = "Wind Speed"

    # ---------------------------------------- extend Wind Forecast timeseries
    # Make room below Direction/Gusts for a Peak Prob Time panel (see below).
    # The panel is extended h=9 -> h=12 so the plot fills the full column
    # height next to Peak Prob Time (x=18, y=10).  The taller panel makes
    # uPlot use the finer 0/50/100/150/... tick increment on the right
    # direction axis; the 8-point compass mappings applied in the next
    # section label those ticks N/NE/E/SE/S/SW/W/NW (instead of the
    # duplicated letters the original 4-quadrant mappings produced).
    wind_panel = next(
        p for p in panels
        if p.get("title") == "Wind Forecast" and p.get("type") == "timeseries"
    )
    wind_panel["gridPos"]["h"] += 3

    # ---------------------------------- show wind direction line (unlike Hertz)
    # Hertz intentionally hides the direction series from the plot (viz),
    # keeping only a zero-width tooltip carrier. Gator's panel description
    # ("...with predicted wind direction on the right y-axis") and axis
    # config expect the line to actually be drawn, so re-enable viz for the
    # base series and its two wrap-around copies.
    for ov in wind_panel.get("fieldConfig", {}).get("overrides", []):
        name = ov.get("matcher", {}).get("options")
        if name in ("winddirmean", "winddirmean_sm1", "winddirmean_sp1"):
            for prop in ov.get("properties", []):
                if prop.get("id") == "custom.hideFrom":
                    prop["value"]["viz"] = False

    # -------------------------- show direction line in the legend (unlike Hertz)
    # The base direction series (winddirmean) was legend-hidden in the Hertz
    # template — the dashed orange direction line drew on the plot but never
    # appeared in the legend.  Un-hide the legend entry for the base series
    # only (displayName "Predicted Wind Direction"); the wrap-around copies
    # (winddirmean_sm1/_sp1) must stay fully hidden so the legend shows a
    # single entry for the direction line.
    for ov in wind_panel.get("fieldConfig", {}).get("overrides", []):
        name = ov.get("matcher", {}).get("options")
        if name == "winddirmean":
            for prop in ov.get("properties", []):
                if prop.get("id") == "custom.hideFrom":
                    prop["value"]["legend"] = False
                    prop["value"]["tooltip"] = False

    # ----------------------- 8-point compass labels on the direction axis
    # The right-y direction axis shows compass letters via range value
    # mappings.  The original Hertz template used 4 quadrants
    # (N/E/S/W) which only looks right when uPlot ticks every 100 deg
    # (0/100/200/300 — the h=9 panel layout).  With the panel extended to
    # h=12, uPlot ticks every 50 deg (0/50/.../350).
    #
    # IMPORTANT: the bands must be keyed to the ACTUAL uPlot tick positions
    # (multiples of 50), NOT true compass octant centers (45/135/225/315).
    # The first attempt used true octants (e.g. SW = [202.5,247.5)) and the
    # tick at 250 fell into W ([247.5,292.5)) — the axis rendered
    # N,NE,E,SE,S,W,NW,N with SW missing.  Bands below are 50 deg wide and
    # centered on the ticks so each tick maps to its own label:
    #   0->N, 50->NE, 100->E, 150->SE, 200->S, 250->SW, 300->W, 350->NW
    # (If the panel is ever shrunk back to h=9, ticks at 0/100/200/300 map
    # to N/E/S/W — graceful in both layouts.)
    compass_mappings = [
        {"from": 0, "to": 25, "text": "N"},
        {"from": 25, "to": 75, "text": "NE"},
        {"from": 75, "to": 125, "text": "E"},
        {"from": 125, "to": 175, "text": "SE"},
        {"from": 175, "to": 225, "text": "S"},
        {"from": 225, "to": 275, "text": "SW"},
        {"from": 275, "to": 325, "text": "W"},
        {"from": 325, "to": 360, "text": "NW"},
    ]
    for ov in wind_panel.get("fieldConfig", {}).get("overrides", []):
        name = ov.get("matcher", {}).get("options")
        if name in ("winddirmean", "winddirmean_sm1", "winddirmean_sp1"):
            for prop in ov.get("properties", []):
                if prop.get("id") == "mappings":
                    prop["value"] = [
                        {
                            "type": "range",
                            "options": {"from": m["from"], "to": m["to"], "result": {"text": m["text"]}},
                        }
                        for m in compass_mappings
                    ]

    # -------------------------------------------- shift Tide section down
    # Make room for: Peak Prob Time row (+3), Sea State row (+1), Sea State
    # stat panels row (+3) = +7 total, inserted right after the wind section.
    tide_row = next(
        p for p in panels
        if p.get("type") == "row" and p.get("title") == "Tide Forecast"
    )
    tide_row_y = tide_row["gridPos"]["y"]
    sea_row_y = tide_row_y + 3
    sea_panels_y = sea_row_y + 1
    shift = 7
    for p in panels:
        if p.get("gridPos", {}).get("y", 0) >= tide_row_y:
            p["gridPos"]["y"] += shift

    # -------------------------------------------------- lightning panels
    # Hertz's single-value "Prob of Thunderstorms" panel was dropped above;
    # replace it with the Max Prob / Peak Time pair (Penobscot hail.json
    # pattern), reusing the empty x=21,y=7 slot next to Direction and adding
    # a new full-width row below for Peak Prob Time.
    max_prob_panel = {
        "datasource": {"type": "postgres", "uid": "supabase-postgres"},
        "description": "Maximum probability of lightning (thunderstorm) in the next 24 hours.",
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "mappings": [],
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "yellow", "value": 0.25},
                        {"color": "orange", "value": 0.5},
                        {"color": "red", "value": 0.75},
                    ],
                },
                "unit": "percentunit",
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "pot"},
                    "properties": [
                        {"id": "displayName", "value": "Lightning Prob"},
                        {"id": "decimals", "value": 0},
                    ],
                }
            ],
        },
        "gridPos": {"h": 3, "w": 3, "x": 21, "y": 7},
        "id": 103,
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value",
            "text": {"valueSize": 26},
        },
        "targets": [
            {
                "datasource": {"type": "postgres", "uid": "supabase-postgres"},
                "editorMode": "code",
                "format": "table",
                "rawQuery": True,
                "rawSql": (
                    "SELECT\n  MAX(pot) AS pot\nFROM forecast_data\nWHERE\n  pot IS NOT NULL\n"
                    f"  AND locname = '$location'\n  AND group_id = '{GID}'\n"
                    "  AND model = 'wind_nbm'\n  AND $__timeFilter(timestamp)"
                ),
                "refId": "A",
            }
        ],
        "title": "Max Prob of Thunderstorms (24h)",
        "type": "stat",
    }
    peak_time_panel = {
        "datasource": {"type": "postgres", "uid": "supabase-postgres"},
        "description": "Time of maximum lightning probability.",
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "none"},
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "unit": "time:MM-DD h:mm A",
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "peak_time"},
                    "properties": [
                        {"id": "displayName", "value": "Peak Prob Time"},
                        {"id": "unit", "value": "time:MM-DD h:mm A"},
                    ],
                }
            ],
        },
        "gridPos": {"h": 3, "w": 6, "x": 18, "y": 10},
        "id": 107,
        "options": {
            "colorMode": "none",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value",
            "text": {"valueSize": 20},
        },
        "targets": [
            {
                "datasource": {"type": "postgres", "uid": "supabase-postgres"},
                "editorMode": "code",
                "format": "table",
                "rawQuery": True,
                "rawSql": (
                    "SELECT\n  EXTRACT(EPOCH FROM timestamp) * 1000 AS peak_time\nFROM forecast_data\n"
                    f"WHERE\n  pot IS NOT NULL\n  AND locname = '$location'\n  AND group_id = '{GID}'\n"
                    "  AND model = 'wind_nbm'\n  AND $__timeFilter(timestamp)\nORDER BY pot DESC\nLIMIT 1"
                ),
                "refId": "A",
            }
        ],
        "title": "Peak Prob Time",
        "type": "stat",
    }

    # -------------------------------------------------------- sea state panels
    # Positioned as their own row above the Tide Forecast row (Penobscot
    # tide_wind.json pattern).
    sea_row = {
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": sea_row_y},
        "id": 901,
        "panels": [],
        "title": "Sea State",
        "type": "row",
    }

    def _sea_state_panel(panel_id, x, title, description, rawsql):
        return {
            "datasource": {"type": "postgres", "uid": "supabase-postgres"},
            "description": description,
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "thresholds"},
                    "decimals": 1 if "Height" in title or "Period" in title else 0,
                    "mappings": [],
                    "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                    "unit": "none",
                },
                "overrides": [],
            },
            "gridPos": {"h": 3, "w": 8, "x": x, "y": sea_panels_y},
            "id": panel_id,
            "options": {
                "colorMode": "none",
                "graphMode": "none",
                "justifyMode": "center",
                "orientation": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "text": {"valueSize": 28},
                "textMode": "value",
            },
            "targets": [
                {
                    "datasource": {"type": "postgres", "uid": "supabase-postgres"},
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "rawSql": rawsql,
                    "refId": "A",
                }
            ],
            "title": title,
            "type": "stat",
        }

    wave_height_panel = _sea_state_panel(
        104, 0, "Wave Height (m)", "Significant Wave Height (max over next 6 hours).",
        "WITH selected_run AS (\n  SELECT MAX(startdt) AS startdt\n  FROM forecast_data\n"
        f"  WHERE htsgw IS NOT NULL\n    AND locname = '$location'\n    AND group_id = '{GID}'\n)\n"
        "SELECT\n  ROUND(MAX(htsgw)::numeric, 1) AS value\nFROM forecast_data\n"
        f"WHERE htsgw IS NOT NULL\n  AND locname = '$location'\n  AND group_id = '{GID}'\n"
        "  AND startdt = (SELECT startdt FROM selected_run)",
    )
    wave_period_panel = _sea_state_panel(
        105, 8, "Wave Period (s)", "Primary Wave Period (latest forecast value).",
        "SELECT\n  ROUND(perpw::numeric, 1) AS value\nFROM forecast_data\n"
        f"WHERE perpw IS NOT NULL\n  AND locname = '$location'\n  AND group_id = '{GID}'\n"
        "  AND timestamp <= NOW()::TIMESTAMPTZ\nORDER BY timestamp DESC\nLIMIT 1",
    )
    wave_dir_panel = _sea_state_panel(
        106, 16, "Wave Direction (\u00b0)", "Primary Wave Direction (latest forecast value).",
        "SELECT\n  ROUND(dirpw::numeric, 0) AS value\nFROM forecast_data\n"
        f"WHERE dirpw IS NOT NULL\n  AND locname = '$location'\n  AND group_id = '{GID}'\n"
        "  AND timestamp <= NOW()::TIMESTAMPTZ\nORDER BY timestamp DESC\nLIMIT 1",
    )

    # Insert the new panels: lightning stats go with the wind section (before
    # the Tide row so they still repeat per-height like the rest of "Wind"),
    # sea-state row + panels go immediately before the (now-shifted) Tide row.
    tide_row_index = panels.index(tide_row)
    panels[tide_row_index:tide_row_index] = [
        max_prob_panel, peak_time_panel,
        sea_row, wave_height_panel, wave_period_panel, wave_dir_panel,
    ]

    d["panels"] = panels

    with open(OUT, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {OUT}")
    print(f"  panels: {len(panels)}")
    for p in panels:
        g = p.get("gridPos", {})
        print(
            f"    id={p.get('id')} y={g.get('y')} x={g.get('x')} w={g.get('w')} h={g.get('h')} "
            f"{p.get('type')} title={p.get('title')!r}"
        )


if __name__ == "__main__":
    sys.exit(main())