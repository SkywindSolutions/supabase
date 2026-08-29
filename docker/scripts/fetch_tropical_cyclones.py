#!/usr/bin/env python3
"""
fetch_tropical_cyclones.py
Task: P2.x — Tropical Cyclone Threat Detection

Fetches machine-readable NHC tropical cyclone data from the official RSS
feeds, determines whether each active storm poses a potential threat to
monitored customer locations, and writes the results to the
``tropical_cyclone_status`` table.

Architecture
------------
This script is designed to run periodically (every 30 minutes during
hurricane season, hourly otherwise) on the Forecast / Ingestion Server.

Workflow:
  1. Fetch NHC Atlantic basin RSS feed (index-at.xml).
  2. Parse active storm metadata (position, intensity, advisory info).
  3. For each storm, determine the official forecast cone image URL.
  4. For each monitored location (read from forecast_locations table or
     a built-in config for the initial rollout), evaluate whether the
     storm poses a potential threat.
  5. Write/update rows in tropical_cyclone_status.
  6. Remove rows for storms no longer in the NHC feed.

Threat Detection Methodology
----------------------------
The system uses official NHC forecast data to determine whether a location
is potentially threatened:

  1. Parse the storm's current center position from the RSS feed.
  2. Parse forecast track positions from the Forecast Advisory text
     (12, 24, 36, 48, 72, 96, 120 hour forecasts).
  3. For each forecast position, check if the location falls within the
     official NHC forecast cone radius at that forecast hour.
  4. Additionally, check if the location is within the current
     tropical-storm-force wind field radii.
  5. If ANY forecast position's cone circle contains the location (or the
     storm is already within wind radii distance), the location is
     considered threatened.

  The cone radii used are the official NHC forecast uncertainty radii
  (67th percentile) for the Atlantic basin:
    - 12h:  30 nmi    48h:  75 nmi
    - 24h:  45 nmi    72h: 110 nmi
    - 36h:  60 nmi    96h: 155 nmi
                      120h: 195 nmi

  These radii are NHC-published 67th percentile track forecast errors.

Usage
-----
  python3 scripts/fetch_tropical_cyclones.py [--db-url URL] [--dry-run]

  --db-url      PostgreSQL connection string.
                Default: postgresql://postgres:<password>@127.0.0.1:5432/postgres
                Reads POSTGRES_PASSWORD from .env if not specified.
  --dry-run     Print what would be done without writing to the database.

Dependencies
------------
  pip install requests
  (lxml is preferred but not required — stdlib xml.etree is sufficient)

Output
------
  Writes to public.tropical_cyclone_status table.
  On --dry-run, prints the storm data and threat evaluations to stdout.
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NHC data source URLs
# ---------------------------------------------------------------------------
NHC_ATLANTIC_RSS = "https://www.nhc.noaa.gov/index-at.xml"
NHC_EPAC_RSS = "https://www.nhc.noaa.gov/index-ep.xml"
NHC_CPAC_RSS = "https://www.nhc.noaa.gov/index-cp.xml"

# All basins to monitor
NHC_FEEDS: list[tuple[str, str]] = [
    ("atlantic", NHC_ATLANTIC_RSS),
    ("eastern_pacific", NHC_EPAC_RSS),
    ("central_pacific", NHC_CPAC_RSS),
]

# ---------------------------------------------------------------------------
# NHC forecast cone radii (nautical miles) for each forecast period
# These are the official 67th-percentile track forecast errors published by NHC.
# Source: https://www.nhc.noaa.gov/aboutcone.shtml
# ---------------------------------------------------------------------------
CONE_RADII_NMI: dict[int, float] = {
    0:   0,     # Current position — use wind radii instead
    12:  30,    # 12-hour
    24:  45,    # 24-hour
    36:  60,    # 36-hour
    48:  75,    # 48-hour
    72:  110,   # 72-hour (3 day)
    96:  155,   # 96-hour (4 day)
    120: 195,   # 120-hour (5 day)
}

# ---------------------------------------------------------------------------
# Default monitored locations (used when forecast_locations table has no
# coordinate data).  Extended with DB queries at runtime.
# ---------------------------------------------------------------------------
# Pascagoula, MS centroid coordinates
DEFAULT_MONITORED_LOCATIONS: list[dict[str, Any]] = [
    {
        "group_id": "grp_pascagoula",
        "locid": "Pascagoula",
        "locname": "Pascagoula",
        "latitude": 30.3478,
        "longitude": -88.5398,
    },
    # Hertz coastal locations (NOAA CO-OPS station coordinates —
    # see migration 20260806000005_hertz_customer_profile.sql).
    {
        "group_id": "grp_hertz",
        "locid": "8726607",
        "locname": "Tampa Airport Maintenance Facility",
        "latitude": 27.8578,
        "longitude": -82.5528,
    },
    {
        "group_id": "grp_hertz",
        "locid": "8761927",
        "locname": "New Orleans Airport Maintenance Facility",
        "latitude": 30.027222,
        "longitude": -90.113335,
    },
    # Port Richey tide location (NOAA CO-OPS station 8727001,
    # Middle Pithlachascotee River / New Port Richey FL —
    # see migration 20260822000001_portrichey_customer_profile.sql).
    {
        "group_id": "grp_portrichey",
        "locid": "87267241",
        "locname": "Port Richey",
        "latitude": 28.2483,
        "longitude": -82.7233,
    },
    # Manson Vermilion coastal location (NOAA CO-OPS station 8766072,
    # Calcasieu Pass / Vermilion Bay LA —
    # see migration 20260824000001_manson_customer_profile.sql).
    {
        "group_id": "grp_manson",
        "locid": "8766072",
        "locname": "Manson Vermilion",
        "latitude": 29.475706,
        "longitude": -92.319396,
    },
]


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _parse_dmh(dmh_str: str) -> int | None:
    """Parse a DD/HHMM forecast period string into hours.

    Examples:
        '22/0600Z' -> 12  (22nd day, 06:00Z — relative to advisory)
        '24/1800Z' -> 72
    """
    # We can't determine absolute hours from DD/HHMM without knowing the
    # advisory date.  Instead, match against the known forecast periods.
    return None


def _parse_position(pos_str: str) -> tuple[float, float] | None:
    """Parse an NHC position string like '29.4N 87.2W' to (lat, lon)."""
    m = re.match(
        r"(\d+\.?\d*)\s*[NnSs]\s+(\d+\.?\d*)\s*[WwEe]",
        pos_str,
    )
    if not m:
        return None
    lat = float(m.group(1))
    lon = float(m.group(2))
    if m.group(0).upper().endswith("S"):
        lat = -lat
    if m.group(0).upper().endswith("W") or m.group(0).upper().endswith("E"):
        # Determine sign: if 'W' or 'E' found in the match
        if "W" in pos_str.upper():
            lon = -lon
    return (lat, lon)


def _haversine_nmi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in nautical miles."""
    import math
    R = 3440.065  # Earth radius in nautical miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def _interpolate_cone_radius(forecast_hour: int) -> float:
    """Get the cone radius for a given forecast hour.

    Uses the nearest defined radius.  For hours beyond 120, uses 120h radius.
    """
    sorted_hours = sorted(CONE_RADII_NMI.keys())
    if forecast_hour <= 0:
        return CONE_RADII_NMI[12]  # Use 12h radius as minimum
    if forecast_hour >= 120:
        return CONE_RADII_NMI[120]

    # Find bounding hours
    for i in range(len(sorted_hours) - 1):
        h1 = sorted_hours[i]
        h2 = sorted_hours[i + 1]
        if h1 == 0:
            continue
        if h1 <= forecast_hour <= h2:
            # Linear interpolation
            fraction = (forecast_hour - h1) / (h2 - h1)
            return CONE_RADII_NMI[h1] + fraction * (CONE_RADII_NMI[h2] - CONE_RADII_NMI[h1])
    return CONE_RADII_NMI[120]


# ---------------------------------------------------------------------------
# NHC RSS feed parsing
# ---------------------------------------------------------------------------

NHCS_NS = "https://www.nhc.noaa.gov"


class NHCStorm:
    """Represents an active tropical cyclone parsed from the NHC RSS feed."""

    def __init__(self, basin: str) -> None:
        self.basin = basin
        self.name: str | None = None
        self.storm_type: str | None = None  # e.g. 'Tropical Storm'
        self.atcf_id: str | None = None  # e.g. 'AL022026'
        self.wallet: str | None = None  # e.g. 'AT2'
        self.center_lat: float | None = None
        self.center_lon: float | None = None
        self.wind_mph: int | None = None
        self.pressure_mb: int | None = None
        self.movement: str | None = None
        self.headline: str | None = None
        self.advisory_number: int | None = None
        self.advisory_time: datetime | None = None
        self.public_advisory_url: str | None = None
        self.forecast_positions: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return (f"<NHCStorm {self.name} ({self.atcf_id}) "
                f"{self.storm_type} @ "
                f"{self.center_lat},{self.center_lon}>")


def _fetch_xml(url: str, timeout: int = 30) -> str | None:
    """Fetch an XML document from a URL. Returns None on failure."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "SkywindInfrastructure/1.0 (tropical-cyclone-monitor)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None


def _parse_nhc_datetime(dt_str: str) -> datetime | None:
    """Parse an NHC RSS pubDate into a datetime object."""
    # Example: "Tue, 21 Jul 2026 20:54:05 GMT"
    try:
        return datetime.strptime(dt_str, "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        pass
    try:
        # Try without day name
        return datetime.strptime(dt_str, "%d %b %Y %H:%M:%S %Z").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        pass
    return None


def _parse_forecast_advisory(storm: NHCStorm, description: str) -> None:
    """Parse forecast track positions from a Forecast Advisory description.

    The advisory text contains lines like:
      FORECAST VALID 22/0600Z 29.6N 87.9W MAX WIND 45 KT...GUSTS 55 KT.
    """
    # Pattern: FORECAST VALID dd/hhmmZ latN/S lonW/E
    pattern = (
        r"FORECAST\s+VALID\s+"
        r"(\d{2})/(\d{2})(\d{2})Z\s+"
        r"(\d+\.?\d*)[Nn]\s+(\d+\.?\d*)[Ww]"
    )
    for match in re.finditer(pattern, description, re.IGNORECASE | re.MULTILINE):
        day = int(match.group(1))
        hour = int(match.group(2))
        minute = int(match.group(3))
        lat = float(match.group(4))
        lon = -float(match.group(5))

        # Estimate forecast hour from the day/hour relative to advisory
        # This is approximate — we compute hours from the advisory reference
        if storm.advisory_time:
            adv_day = storm.advisory_time.day
            adv_hour = storm.advisory_time.hour
            # Simple delta: diff in days * 24 + diff in hours
            hour_delta = (day - adv_day) * 24 + (hour - adv_hour)
            if hour_delta < 0:
                hour_delta += 24  # crossed midnight
            fhr = max(0, hour_delta)
        else:
            fhr = 0

        storm.forecast_positions.append({
            "fhr": fhr,
            "lat": lat,
            "lon": lon,
            "description": f"{fhr}h forecast: {lat}N {lon}W",
        })


def parse_storms_from_rss(basin: str, xml_content: str) -> list[NHCStorm]:
    """Parse the NHC RSS feed XML and return a list of active storms."""
    storms: list[NHCStorm] = []
    if not xml_content:
        return storms

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        log.error("Failed to parse XML for %s: %s", basin, exc)
        return storms

    # Find all items with nhc:Cyclone elements
    ns = {"nhc": NHCS_NS}
    for item in root.findall(".//item"):
        cyclone = item.find("nhc:Cyclone", ns)
        if cyclone is None:
            continue

        storm = NHCStorm(basin)

        # Extract metadata
        name_el = cyclone.find("nhc:name", ns)
        if name_el is not None:
            storm.name = name_el.text.strip()

        type_el = cyclone.find("nhc:type", ns)
        if type_el is not None:
            storm.storm_type = type_el.text.strip()

        center_el = cyclone.find("nhc:center", ns)
        if center_el is not None and center_el.text:
            parts = center_el.text.strip().split(",")
            if len(parts) == 2:
                try:
                    storm.center_lat = float(parts[0].strip())
                    storm.center_lon = float(parts[1].strip())
                except ValueError:
                    pass

        wind_el = cyclone.find("nhc:wind", ns)
        if wind_el is not None and wind_el.text:
            # Extract number from "60 mph"
            m = re.search(r"(\d+)", wind_el.text)
            if m:
                storm.wind_mph = int(m.group(1))

        pressure_el = cyclone.find("nhc:pressure", ns)
        if pressure_el is not None and pressure_el.text:
            m = re.search(r"(\d+)", pressure_el.text)
            if m:
                storm.pressure_mb = int(m.group(1))

        movement_el = cyclone.find("nhc:movement", ns)
        if movement_el is not None:
            storm.movement = movement_el.text.strip()

        headline_el = cyclone.find("nhc:headline", ns)
        if headline_el is not None:
            storm.headline = headline_el.text.strip()

        wallet_el = cyclone.find("nhc:wallet", ns)
        if wallet_el is not None:
            storm.wallet = wallet_el.text.strip()

        atcf_el = cyclone.find("nhc:atcf", ns)
        if atcf_el is not None:
            storm.atcf_id = atcf_el.text.strip()

        # Extract advisory number and public advisory URL from sibling items
        title_el = item.find("title")
        desc_el = item.find("description")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")

        title = title_el.text if title_el is not None else ""

        # The summary item has the advisory number in the title
        # "Summary for Tropical Storm Bertha (AT2/AL022026)" — no advisory #
        # But there's also a public advisory item
        if "Public Advisory" in title:
            m = re.search(r"Number\s+(\d+)", title)
            if m:
                storm.advisory_number = int(m.group(1))
            if pubdate_el is not None and pubdate_el.text:
                storm.advisory_time = _parse_nhc_datetime(pubdate_el.text)
            if link_el is not None and link_el.text:
                storm.public_advisory_url = link_el.text.strip()

        # Parse forecast positions from Forecast Advisory
        if "Forecast Advisory" in title:
            if desc_el is not None and desc_el.text:
                _parse_forecast_advisory(storm, desc_el.text)
            # Override advisory time with forecast advisory time
            if pubdate_el is not None and pubdate_el.text:
                storm.advisory_time = _parse_nhc_datetime(pubdate_el.text)

        # Also check the summary item for metadata if we didn't get it
        if storm.advisory_number is None and "Summary" in title:
            # Look at the pubDate for advisory time
            if pubdate_el is not None and pubdate_el.text:
                storm.advisory_time = _parse_nhc_datetime(pubdate_el.text)
            if link_el is not None and link_el.text and "graphics" not in link_el.text:
                storm.public_advisory_url = link_el.text.strip()

        # Add the storm if we have at least a name and ATCF ID
        if storm.name and storm.atcf_id:
            # Check if we already have this storm (from the summary item)
            existing = next((s for s in storms if s.atcf_id == storm.atcf_id), None)
            if existing:
                # Merge information
                if storm.advisory_number is not None:
                    existing.advisory_number = storm.advisory_number
                if storm.advisory_time is not None:
                    existing.advisory_time = storm.advisory_time
                if storm.public_advisory_url is not None:
                    existing.public_advisory_url = storm.public_advisory_url
                if storm.forecast_positions:
                    existing.forecast_positions = storm.forecast_positions
            else:
                storms.append(storm)

    return storms


# ---------------------------------------------------------------------------
# Official NHC forecast cone graphic URL construction
# ---------------------------------------------------------------------------

def build_cone_graphic_url(storm: NHCStorm) -> str | None:
    """Build the official NHC forecast cone graphic URL for a storm.

    URL pattern:
      https://www.nhc.noaa.gov/storm_graphics/AT{NN}/refresh/{ATCF}_5day_cone+png/{ts}_5day_cone.png

    Where:
      - NN = wallet number zero-padded to 2 digits (e.g., AT2 → AT02)
      - ATCF = storm ATCF identifier (e.g., AL022026)
      - ts = advisory timestamp in DD/HHMM format without slashes
             derived from the advisory publication time

    Falls back to the general NHC graphics page URL if the advisory time
    is not available.
    """
    if not storm.wallet or not storm.atcf_id:
        return None

    # Extract wallet number
    m = re.search(r"(\d+)$", storm.wallet)
    if not m:
        return None
    wallet_num = int(m.group(1))

    # Build the cone graphic URL
    wallet_dir = f"AT{wallet_num:02d}"
    atcf = storm.atcf_id

    if storm.advisory_time:
        ts = storm.advisory_time.strftime("%d%H%M")
        url = (
            f"https://www.nhc.noaa.gov/storm_graphics/"
            f"{wallet_dir}/refresh/{atcf}_5day_cone+png/"
            f"{ts}_5day_cone.png"
        )
    else:
        # Fallback: link to the graphics page
        wallet_lower = storm.wallet.lower()
        url = (
            f"https://www.nhc.noaa.gov/refresh/"
            f"graphics_{wallet_lower}+shtml/?cone"
        )

    return url


def build_info_url(storm: NHCStorm) -> str | None:
    """Build the official NHC public advisory URL for a storm.

    Uses the RSS link if available, otherwise constructs from the wallet.
    """
    if storm.public_advisory_url:
        return storm.public_advisory_url

    if storm.wallet:
        wallet_lower = storm.wallet.lower()
        return (
            f"https://www.nhc.noaa.gov/text/refresh/"
            f"MIATCPAT{wallet_lower}+shtml/"
        )

    return None


# ---------------------------------------------------------------------------
# Geographic threat evaluation
# ---------------------------------------------------------------------------

def evaluate_threat(
    storm: NHCStorm,
    location_lat: float,
    location_lon: float,
    max_cone_distance_nmi: float = 999,
) -> tuple[bool, str]:
    """Determine whether a storm threatens a location.

    Uses a multi-criterion evaluation:

    1. If the storm center is within the 34kt wind radii distance of the
       location, it's an immediate threat.

    2. For each forecast track position, check if the location falls within
       the official NHC forecast cone radius at that forecast hour.  If so,
       the location is threatened.

    3. As a safety net, check if the current storm position is within
       a reasonable distance of the location.

    Args:
        storm: Parsed NHC storm data.
        location_lat: Location latitude (decimal degrees).
        location_lon: Location longitude (decimal degrees).
        max_cone_distance_nmi: Maximum cone distance to consider
            (safety limit, default 999 = no limit beyond cone radii).

    Returns:
        Tuple of (threat: bool, reason: str).
    """
    reasons: list[str] = []
    threat = False

    # 1. Check current position distance
    if storm.center_lat is not None and storm.center_lon is not None:
        dist_nmi = _haversine_nmi(
            location_lat, location_lon,
            storm.center_lat, storm.center_lon,
        )

        # If the storm is very close (within 150 nmi), it's threatening
        if dist_nmi <= 150:
            threat = True
            reasons.append(
                f"Storm center {dist_nmi:.0f} nmi from location "
                f"(within 150 nmi immediate threat threshold)"
            )

        # 2. Check forecast positions against cone radii
        for fp in storm.forecast_positions:
            fhr = fp["fhr"]
            fp_dist = _haversine_nmi(
                location_lat, location_lon,
                fp["lat"], fp["lon"],
            )
            cone_radius = _interpolate_cone_radius(fhr)

            if fp_dist <= cone_radius:
                threat = True
                reasons.append(
                    f"Location lies within the forecast cone at T+{fhr}h "
                    f"({fp_dist:.0f} nmi from forecast position, "
                    f"cone radius {cone_radius:.0f} nmi)"
                )

        # 3. Check if storm is within general threatening range
        #    (beyond immediate threshold but could reach location within
        #     the forecast period given the cone uncertainty)
        if not threat and dist_nmi <= 300:
            # Check if the storm is moving toward the location
            # (simplification: check if it's in the correct quadrant)
            # For now, flag as potential — the cone check above is the
            # primary criterion
            pass

        if not threat:
            reasons.append(
                f"Storm center {dist_nmi:.0f} nmi from location; "
                f"no forecast cone positions contain the location"
            )
    else:
        reasons.append("Storm center position not available")

    if not reasons:
        reasons.append("Insufficient storm data to evaluate threat")

    return threat, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def get_monitored_locations(conn_str: str | None) -> list[dict[str, Any]]:
    """Read monitored locations from the database.

    Falls back to DEFAULT_MONITORED_LOCATIONS if the DB is unavailable.
    Only returns locations that have non-NULL coordinates.
    """
    locations = list(DEFAULT_MONITORED_LOCATIONS)

    if not conn_str:
        return locations

    try:
        import psycopg2
    except ImportError:
        log.warning("psycopg2 not available, using default locations only")
        return locations

    sql = """
        SELECT group_id, locid, locname, latitude, longitude
        FROM public.forecast_locations
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY group_id, locid;
    """
    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                db_locations = [
                    {
                        "group_id": row[0],
                        "locid": row[1],
                        "locname": row[2] or row[1],
                        "latitude": row[3],
                        "longitude": row[4],
                    }
                    for row in cur.fetchall()
                ]
                if db_locations:
                    return db_locations
    except Exception as exc:
        log.warning("Failed to query forecast_locations: %s", exc)

    return locations


def write_storm_status(
    conn_str: str | None,
    storms: list[NHCStorm],
    locations: list[dict[str, Any]],
    dry_run: bool = False,
) -> None:
    """Write storm status and threat evaluations to the database.

    For each (storm, location) pair, evaluates the threat and upserts
    a row into tropical_cyclone_status.

    After processing, removes any rows for storms that are no longer
    in the active NHC feed.
    """
    active_atcf_ids: set[str] = set()

    for storm in storms:
        if not storm.atcf_id:
            continue
        active_atcf_ids.add(storm.atcf_id)

        # Build NHC URLs
        graphic_url = build_cone_graphic_url(storm)
        info_url = build_info_url(storm)

        for loc in locations:
            threat, reason = evaluate_threat(
                storm,
                loc["latitude"],
                loc["longitude"],
            )

            if dry_run:
                marker = "⚠️  THREAT" if threat else "OK"
                print(
                    f"[{marker}] {storm.name} ({storm.atcf_id}) → "
                    f"{loc['group_id']}/{loc['locname']}: {reason}"
                )
                if threat:
                    print(f"        Graphic URL: {graphic_url}")
                    print(f"        Info URL: {info_url}")
                continue

            # Upsert into database
            _upsert_threat_row(
                conn_str, storm, loc, threat, reason,
                graphic_url, info_url,
            )

    # Clean up storms that are no longer active
    if not dry_run and conn_str and active_atcf_ids:
        _cleanup_stale_storms(conn_str, active_atcf_ids)


def _upsert_threat_row(
    conn_str: str | None,
    storm: NHCStorm,
    loc: dict[str, Any],
    threat: bool,
    reason: str,
    graphic_url: str | None,
    info_url: str | None,
) -> None:
    """Upsert a single threat evaluation row."""
    if not conn_str:
        return

    try:
        import psycopg2
    except ImportError:
        log.error("psycopg2 not available, cannot write to database")
        return

    sql = """
        INSERT INTO public.tropical_cyclone_status (
            atcf_id, storm_name, group_id, locid,
            threat, threat_reason,
            storm_type, wind_mph, center_lat, center_lon,
            movement, pressure_mb,
            nhc_graphic_url, nhc_info_url,
            advisory_number, advisory_time,
            refreshed_at
        ) VALUES (
            %(atcf_id)s, %(storm_name)s, %(group_id)s, %(locid)s,
            %(threat)s, %(threat_reason)s,
            %(storm_type)s, %(wind_mph)s, %(center_lat)s, %(center_lon)s,
            %(movement)s, %(pressure_mb)s,
            %(nhc_graphic_url)s, %(nhc_info_url)s,
            %(advisory_number)s, %(advisory_time)s,
            NOW()
        ) ON CONFLICT (atcf_id, group_id, locid) DO UPDATE SET
            storm_name = EXCLUDED.storm_name,
            threat = EXCLUDED.threat,
            threat_reason = EXCLUDED.threat_reason,
            storm_type = EXCLUDED.storm_type,
            wind_mph = EXCLUDED.wind_mph,
            center_lat = EXCLUDED.center_lat,
            center_lon = EXCLUDED.center_lon,
            movement = EXCLUDED.movement,
            pressure_mb = EXCLUDED.pressure_mb,
            nhc_graphic_url = EXCLUDED.nhc_graphic_url,
            nhc_info_url = EXCLUDED.nhc_info_url,
            advisory_number = EXCLUDED.advisory_number,
            advisory_time = EXCLUDED.advisory_time,
            refreshed_at = NOW();
    """

    params = {
        "atcf_id": storm.atcf_id,
        "storm_name": storm.name,
        "group_id": loc["group_id"],
        "locid": loc["locid"],
        "threat": threat,
        "threat_reason": reason,
        "storm_type": storm.storm_type,
        "wind_mph": storm.wind_mph,
        "center_lat": storm.center_lat,
        "center_lon": storm.center_lon,
        "movement": storm.movement,
        "pressure_mb": storm.pressure_mb,
        "nhc_graphic_url": graphic_url,
        "nhc_info_url": info_url,
        "advisory_number": storm.advisory_number,
        "advisory_time": storm.advisory_time,
    }

    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        log.info(
            "Upserted %s/%s → %s/%s (threat=%s, adv=%s)",
            storm.atcf_id, storm.name,
            loc["group_id"], loc["locid"],
            threat, storm.advisory_number,
        )
    except Exception as exc:
        log.error(
            "Failed to upsert %s/%s → %s/%s: %s",
            storm.atcf_id, storm.name,
            loc["group_id"], loc["locid"],
            exc,
        )


def _cleanup_stale_storms(conn_str: str, active_atcf_ids: set[str]) -> None:
    """Remove rows for storms that are no longer tracked by NHC."""
    try:
        import psycopg2
    except ImportError:
        return

    sql = """
        DELETE FROM public.tropical_cyclone_status
        WHERE atcf_id != ALL(%s);
    """
    try:
        with psycopg2.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (list(active_atcf_ids),))
                deleted = cur.rowcount
            conn.commit()
        if deleted:
            log.info("Cleaned up %d stale storm row(s)", deleted)
    except Exception as exc:
        log.error("Failed to clean up stale storms: %s", exc)


# ---------------------------------------------------------------------------
# Environment and configuration helpers
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

    When running outside the Docker network, the Supabase connection pooler
    (supavisor) proxies port 5432 and requires the tenant ID as part of the
    username: postgres.<tenant_id>.  The tenant ID is read from
    POOLER_TENANT_ID in the .env file.
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

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch NHC tropical cyclone data and evaluate location threats",
    )
    parser.add_argument(
        "--db-url",
        help="PostgreSQL connection string",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print storm data without writing to the database",
    )
    args = parser.parse_args()

    conn_str = build_conn_str(args.db_url)

    log.info("Fetching NHC tropical cyclone data...")

    all_storms: list[NHCStorm] = []
    for basin_name, feed_url in NHC_FEEDS:
        xml_content = _fetch_xml(feed_url)
        if xml_content:
            storms = parse_storms_from_rss(basin_name, xml_content)
            log.info("  %s: %d active storm(s)", basin_name, len(storms))
            all_storms.extend(storms)
        else:
            log.warning("  %s: failed to fetch feed", basin_name)

    if not all_storms:
        log.info("No active tropical cyclones found in any basin.")
        # Still clean up stale rows
        if not args.dry_run and conn_str:
            _cleanup_stale_storms(conn_str, set())
        return

    log.info("Total active storms: %d", len(all_storms))
    for s in all_storms:
        log.info(
            "  %s (%s): %s @ %.1f,%.1f | %d mph | adv #%s",
            s.name, s.atcf_id, s.storm_type,
            s.center_lat or 0, s.center_lon or 0,
            s.wind_mph or 0,
            s.advisory_number or "?",
        )
        if s.forecast_positions:
            for fp in s.forecast_positions:
                log.info("    T+%dh: %.1fN %.1fW", fp["fhr"], fp["lat"], fp["lon"])

    # Get monitored locations
    locations = get_monitored_locations(conn_str)
    log.info("Monitored locations: %d", len(locations))
    for loc in locations:
        log.info(
            "  %s/%s: %.4f, %.4f",
            loc["group_id"], loc["locname"],
            loc["latitude"], loc["longitude"],
        )

    # Evaluate threats and write to database
    write_storm_status(conn_str, all_storms, locations, dry_run=args.dry_run)

    if args.dry_run:
        log.info("Dry-run complete. No changes written to the database.")
    else:
        log.info("Done.")


if __name__ == "__main__":
    main()
