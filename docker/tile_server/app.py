"""
Skywind Solutions — Tile Server
================================
Serves forecast_grid_data as GeoJSON FeatureCollections of coloured
rectangle polygons for Grafana Geomap's built-in `geojson` layer.

Each 1° grid cell becomes a GeoJSON Polygon whose `probability` property
is used by Grafana's style rules to colour it blue → yellow → red.  The
server auto-detects the native grid spacing so it works for any resolution
(0.1°, 0.25°, 0.5°, 1°, …).

Endpoints
---------
GET /geojson/<group_id>/<model>/<ts_safe>
    group_id  — tenant group (e.g. grp_europe_grid)
    model     — model name (e.g. grid_ensemble)
    ts_safe   — UTC valid time in YYYY-MM-DDTHHMM[Z] format (no colons)
                e.g. 2026-07-12T0800Z  → 2026-07-12 08:00 UTC

    Returns a GeoJSON FeatureCollection with one Polygon Feature per grid
    cell.  Empty FeatureCollection (200) when no data exists for the key.

GET /timestamps/<group_id>/<model>
    Returns a JSON array of available timestamps in ts_safe format, sorted
    ascending.  Used as a fallback if the Grafana variable query fails.

GET /health
    Returns 200 "ok".

Configuration (environment variables)
--------------------------------------
DB_HOST          PostgreSQL host           (default: db)
DB_PORT          PostgreSQL port           (default: 5432)
DB_NAME          PostgreSQL database name  (default: postgres)
DB_USER          PostgreSQL user           (default: postgres)
DB_PASSWORD      PostgreSQL password       (required)
TILE_SERVER_PORT Server port               (default: 8080)
CACHE_TTL_SEC    Result cache TTL seconds  (default: 300)
"""

import json
import os
import re
import threading
import time
from datetime import timezone

import numpy as np
import psycopg2
import psycopg2.extras
from flask import Flask, Response, abort, jsonify, request
from matplotlib import colormaps
from scipy.interpolate import RegularGridInterpolator

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DB_HOST = os.environ.get("DB_HOST", "db")
_DB_PORT = int(os.environ.get("DB_PORT", 5432))
_DB_NAME = os.environ.get("DB_NAME", "postgres")
_DB_USER = os.environ.get("DB_USER", "postgres")
_CACHE_TTL = int(os.environ.get("CACHE_TTL_SEC", 300))

# ---------------------------------------------------------------------------
# Colormap  (matches dashboard continuous-BlYlRd / RdYlBu_r)
# ---------------------------------------------------------------------------

_CMAP = colormaps["RdYlBu_r"]   # 0 → blue,  0.5 → yellow,  1 → red


def _hex(p: float) -> str:
    """Map probability [0, 1] to a hex color string via RdYlBu_r."""
    r, g, b, _ = _CMAP(max(0.0, min(1.0, float(p))))
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


# ---------------------------------------------------------------------------
# In-process TTL cache
# ---------------------------------------------------------------------------
#
# lru_cache alone is not used because we want time-based expiry: when new
# forecast data is ingested the cache should refresh within CACHE_TTL_SEC.

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (time.monotonic() - entry["t"]) < _CACHE_TTL:
            return entry["v"]
    return None


def _cache_set(key: tuple, value):
    with _CACHE_LOCK:
        _CACHE[key] = {"v": value, "t": time.monotonic()}
        # Evict expired entries if the cache grows large (>500 keys)
        if len(_CACHE) > 500:
            cutoff = time.monotonic() - _CACHE_TTL
            for k in [k for k, e in _CACHE.items() if e["t"] < cutoff]:
                del _CACHE[k]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect():
    return psycopg2.connect(
        host=_DB_HOST,
        port=_DB_PORT,
        dbname=_DB_NAME,
        user=_DB_USER,
        password=os.environ["DB_PASSWORD"],
        connect_timeout=10,
        options="-c statement_timeout=15000",   # 15 s query timeout
    )


_TS_PATTERNS = [
    # YYYY-MM-DDTHHMM[Z]   — URL-safe, no colons in time (our primary format)
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})Z?$"),
     lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:00Z"),
    # YYYY-MM-DDTHH:MM[:SS][Z]  — full ISO
    (re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z?)$"),
     lambda m: m.group(1)),
]


def _parse_ts(ts_safe: str) -> str:
    """Convert URL-safe timestamp to a PostgreSQL-compatible ISO string."""
    for pattern, fmt in _TS_PATTERNS:
        m = pattern.match(ts_safe.strip())
        if m:
            return fmt(m)
    raise ValueError(f"Unrecognised timestamp format: {ts_safe!r}")


# ---------------------------------------------------------------------------
# Grid data fetching
# ---------------------------------------------------------------------------

def _fetch_grid(group_id: str, model: str, ts_pg: str):
    """
    Fetch one time step from forecast_grid_data.

    Returns (lats, lons, grid) where:
        lats  — sorted 1-D float64 array of unique latitudes
        lons  — sorted 1-D float64 array of unique longitudes
        grid  — 2-D float64 array (lats × lons), NaN for missing cells

    Returns None if no rows exist for the given (group_id, model, timestamp).
    """
    key = ("grid", group_id, model, ts_pg)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT latitude, longitude, probability
                FROM   public.forecast_grid_data
                WHERE  group_id    = %s
                  AND  model       = %s
                  AND  "timestamp" = %s::timestamptz
                  AND  probability IS NOT NULL
                ORDER  BY latitude, longitude
                """,
                (group_id, model, ts_pg),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        _cache_set(key, None)
        return None

    lats = np.array(sorted({r[0] for r in rows}))
    lons = np.array(sorted({r[1] for r in rows}))
    lat_idx = {float(v): i for i, v in enumerate(lats)}
    lon_idx = {float(v): i for i, v in enumerate(lons)}

    grid = np.full((len(lats), len(lons)), np.nan)
    for lat, lon, prob in rows:
        grid[lat_idx[float(lat)], lon_idx[float(lon)]] = float(prob)

    result = (lats, lons, grid)
    _cache_set(key, result)
    return result


def _fetch_timestamps(group_id: str, model: str):
    """Return sorted list of available valid timestamps in URL-safe format."""
    key = ("ts", group_id, model)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT
                    to_char("timestamp" AT TIME ZONE 'UTC',
                             'YYYY-MM-DD"T"HH24MI"Z"') AS ts_safe
                FROM  public.forecast_grid_data
                WHERE group_id    = %s
                  AND model       = %s
                  AND probability IS NOT NULL
                ORDER BY 1
                """,
                (group_id, model),
            )
            result = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    _cache_set(key, result)
    return result


# ---------------------------------------------------------------------------
# GeoJSON builder
# ---------------------------------------------------------------------------

def _build_geojson(
    lats: np.ndarray,
    lons: np.ndarray,
    grid: np.ndarray,
    min_prob: float = 0.03,
) -> bytes:
    """
    Build a compact GeoJSON FeatureCollection byte string.

    Each feature is a rectangle Polygon centred on its (lat, lon) grid point.
    Cell half-widths are inferred from the grid spacing so this function works
    at any native resolution.

    Feature properties:
        probability  — raw [0, 1] float (used by Grafana styleRules)
        color        — hex fill colour pre-computed from the shared colormap
    """
    # Detect cell half-widths from the grid spacing.
    # Inflate by 1.5 % so adjacent cells slightly overlap, eliminating the
    # 1-pixel anti-aliasing gap that OpenLayers/Canvas leaves between polygons.
    _INFLATE = 1.015
    half_lat = float((lats[1] - lats[0]) / 2) * _INFLATE if len(lats) > 1 else 0.5
    half_lon = float((lons[1] - lons[0]) / 2) * _INFLATE if len(lons) > 1 else 0.5

    features = []
    for i in range(len(lats)):
        lat = float(lats[i])
        for j in range(len(lons)):
            p = grid[i, j]
            if np.isnan(p):
                continue
            if p < min_prob:
                continue
            lon = float(lons[j])
            # GeoJSON uses [longitude, latitude] ordering
            coords = [[
                [lon - half_lon, lat - half_lat],
                [lon + half_lon, lat - half_lat],
                [lon + half_lon, lat + half_lat],
                [lon - half_lon, lat + half_lat],
                [lon - half_lon, lat - half_lat],
            ]]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": coords},
                "properties": {
                    "probability": round(float(p), 4),
                    "color": _hex(p),
                },
            })

    fc = {"type": "FeatureCollection", "features": features}
    return json.dumps(fc, separators=(",", ":")).encode("utf-8")


_EMPTY_FC = b'{"type":"FeatureCollection","features":[]}'


# ---------------------------------------------------------------------------
# Grid interpolation
# ---------------------------------------------------------------------------

def _interpolate_grid(
    lats: np.ndarray,
    lons: np.ndarray,
    grid: np.ndarray,
    step: float,
) -> tuple:
    """
    Bilinear interpolation of a regular lat/lon grid to a finer step size.

    Uses scipy RegularGridInterpolator with method='linear' (bilinear).
    Points outside the original domain are NaN (no extrapolation).
    Returns the original arrays unchanged when step >= native spacing.
    """
    if len(lats) < 2 or len(lons) < 2:
        return lats, lons, grid

    native_step = min(float(lats[1] - lats[0]), float(lons[1] - lons[0]))
    if step >= native_step:
        return lats, lons, grid

    # NaN-safe: temporarily fill NaN with nearest finite value so the
    # interpolator doesn't bleed NaN into valid cells near the boundary.
    # We restore NaN after for any point that was outside the convex hull.
    grid_filled = grid.copy()
    nan_mask = np.isnan(grid_filled)
    if nan_mask.any():
        finite_mean = float(np.nanmean(grid_filled))
        grid_filled[nan_mask] = finite_mean

    interp = RegularGridInterpolator(
        (lats, lons),
        grid_filled,
        method="linear",
        bounds_error=False,
        fill_value=np.nan,
    )

    new_lats = np.arange(float(lats[0]),  float(lats[-1])  + step * 0.5, step)
    new_lons = np.arange(float(lons[0]),  float(lons[-1])  + step * 0.5, step)
    ll, mm = np.meshgrid(new_lats, new_lons, indexing="ij")
    new_grid = interp((ll, mm))
    # Clip to valid probability range
    new_grid = np.clip(new_grid, 0.0, 1.0)
    return new_lats, new_lons, new_grid

# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[\w\-]{1,64}$")


def _valid_ident(s: str) -> bool:
    return bool(_IDENT_RE.match(s))


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/geojson/<group_id>/<model>/<ts_safe>")
def geojson_layer(group_id: str, model: str, ts_safe: str):
    """
    Return a GeoJSON FeatureCollection of grid-cell rectangles for the
    specified forecast group, model, and valid time.
    """
    if not _valid_ident(group_id) or not _valid_ident(model):
        abort(400, description="Invalid group_id or model identifier")

    try:
        ts_pg = _parse_ts(ts_safe)
    except ValueError as exc:
        abort(400, description=str(exc))

    result = _fetch_grid(group_id, model, ts_pg)
    if result is None:
        body = _EMPTY_FC
    else:
        lats, lons, grid = result
        # Parse optional output resolution (default 0.25° bilinear interpolation)
        try:
            res = float(request.args.get("res", "0.2"))
            if not (0.01 <= res <= 10.0):
                raise ValueError
        except ValueError:
            res = 0.2
        lats, lons, grid = _interpolate_grid(lats, lons, grid, res)
        # Optional minimum probability — cells below this are omitted entirely
        # so the basemap labels/borders show through in low-probability regions.
        try:
            min_prob = float(request.args.get("min_prob", "0.03"))
            min_prob = max(0.0, min(1.0, min_prob))
        except ValueError:
            min_prob = 0.03
        body = _build_geojson(lats, lons, grid, min_prob=min_prob)

    native_res = "interpolated" if result else "empty"
    return Response(
        body,
        status=200,
        mimetype="application/geo+json",
        headers={
            **_cors_headers(),
            "Cache-Control": f"public, max-age={_CACHE_TTL}",
            "X-Source-Resolution": "1.0deg-native",
            "X-Output-Resolution": native_res,
        },
    )


@app.route("/geojson/<group_id>/<model>/<ts_safe>", methods=["OPTIONS"])
def geojson_preflight(group_id: str, model: str, ts_safe: str):
    return Response("", status=204, headers=_cors_headers())


@app.route("/timestamps/<group_id>/<model>")
def timestamps(group_id: str, model: str):
    """List available valid times in URL-safe format for the given group/model."""
    if not _valid_ident(group_id) or not _valid_ident(model):
        abort(400, description="Invalid group_id or model identifier")
    ts_list = _fetch_timestamps(group_id, model)
    return Response(
        json.dumps(ts_list),
        mimetype="application/json",
        headers={**_cors_headers(), "Cache-Control": f"public, max-age={_CACHE_TTL}"},
    )


@app.route("/health")
def health():
    """Kubernetes / Docker Compose health probe."""
    return "ok", 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("TILE_SERVER_PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
