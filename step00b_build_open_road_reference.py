# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Larry M. Arjomandi
"""Build the open-licensed reference road layer used by step01.

The layer is built from openly licensed government data for the six study
windows:

  VIC geometry, structure, direction, status:
      Vicmap Transport Road Line (tr_road), State of Victoria (DEECA), CC BY 4.0
  NSW geometry, structure, status, surface:
      NSW FSDF Transport Theme Road Segment, State of New South Wales
      (Spatial Services, DCS), CC BY 4.0
  VIC posted speed:
      Speed Zones, State of Victoria (Department of Transport and Planning), CC BY 4.0
  NSW posted speed:
      Speed Zones, Transport for NSW, CC BY 4.0

The output uses the field names step01 reads (SUBTYPE, RD_NAME, SPEED,
ONE_WAY, ...) for its deterministic OSM matching and tunnel injection.
Each state file is written to Roads/Open/Standard/<state>_roads.geojson
together with a SOURCES.json manifest recording URLs, licences, retrieval
time, and SHA-256 digests of the downloaded inputs.

Raw downloads are cached in Roads/Open/raw/.  The statewide speed-zone files
must be present there (see SPEED_ZONE_SOURCES); road geometry is fetched per
study window from the publishers' WFS / ArcGIS services.

The outputs are adapted CC BY 4.0 material.  Anyone sharing them, or data
derived from them, must keep the attributions recorded in SOURCES.json, link
the licence, and state that the material was modified.
"""

import argparse
import hashlib
import json
import math
import re
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio

from pipeline_config import OPEN_ROADS_DIR, PROJECT_DIR
from step01_reproduce_unified_graph import hard_case_locations

RAW_DIR = PROJECT_DIR / "Roads" / "Open" / "raw"

# Half-width of each downloaded window.  step01 builds a 2 km OSM graph and
# searches reference tunnels within 2.5 km, so 3.5 km leaves margin for the
# 50 m matching buffer and segments that straddle the boundary.
WINDOW_HALF_WIDTH_M = 3500.0
# A speed zone must overlap at least this share of a segment to be assigned.
SPEED_MIN_COVERAGE = 0.30
SPEED_MATCH_BUFFER_M = 10.0
# Zone pieces more than this far from the segment's direction are crossing
# streets, not the segment's own zone.
SPEED_MAX_AXIS_DIFFERENCE_DEG = 20.0
METRIC_CRS = {"vic": "EPSG:7855", "nsw": "EPSG:7856"}

VICMAP_WFS = "https://opendata.maps.vic.gov.au/geoserver/wfs"
VICMAP_LAYER = "open-data-platform:tr_road"
NSW_ROAD_SEGMENT = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/"
    "NSW_Transport_Theme/FeatureServer/5/query"
)
NSW_FIELDS = [
    "objectid", "topoid", "roadnamebase", "roadnametype", "functionhierarchy",
    "roadontype", "operationalstatus", "surface", "lanecount",
]

SPEED_ZONE_SOURCES = {
    "vic": {
        "file": "vic_speed_zones_september_2026.geojson",
        "url": (
            "https://opendata.transport.vic.gov.au/dataset/"
            "975b80b9-e530-46e2-80a5-54002765e81a/resource/"
            "47697ba7-5105-4d50-b28e-bdab06d2aed5/download/"
            "speed_zones_september_2026.geojson"
        ),
        "licence": "CC BY 4.0",
        "attribution": "State of Victoria (Department of Transport and Planning)",
    },
    "nsw": {
        "file": "nsw_speed_zones.geojson",
        "url": (
            "https://opendata.transport.nsw.gov.au/data/dataset/"
            "4253a054-b377-4b5b-83d1-71385bb6ff33/resource/"
            "bc2da977-65d2-4caa-a73a-48d0c8bf1100/download/speed_zones.geojson"
        ),
        "licence": "CC BY 4.0",
        "attribution": "Transport for NSW",
    },
}
ROAD_SOURCES = {
    "vic": {
        "url": f"{VICMAP_WFS}?typeNames={VICMAP_LAYER}",
        "licence": "CC BY 4.0",
        "attribution": "State of Victoria (Department of Energy, Environment and Climate Action)",
    },
    "nsw": {
        "url": NSW_ROAD_SEGMENT.rsplit("/query", 1)[0],
        "licence": "CC BY 4.0",
        "attribution": (
            "State of New South Wales (Spatial Services, a business unit of "
            "the Department of Customer Service NSW)"
        ),
    },
}
# Time-limited NSW zones are not the posted limit a truck normally sees.
NSW_EXCLUDED_ZONE_TYPES = {"SCHOOL", "SCHOOL BUS", "WET WEATHER"}


def log(*args):
    print(*args, flush=True)


def window_bbox(lat, lon, half_width_m=WINDOW_HALF_WIDTH_M):
    """Lon/lat bounding box of a square window centred on (lat, lon)."""
    dlat = half_width_m / 111_320.0
    dlon = half_width_m / (111_320.0 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def fetch_json(url, timeout=180, attempts=4):
    """GET JSON with a hard total-time limit per attempt.

    A socket timeout alone does not stop a response that trickles in
    indefinitely, which stalled an earlier build, so curl's --max-time
    bounds each attempt.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            ["curl", "-sS", "--fail", "--max-time", str(int(timeout)), url],
            capture_output=True,
        )
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError as error:
                last_error = error
        else:
            last_error = RuntimeError(result.stderr.decode(errors="replace").strip())
        log(f"    fetch attempt {attempt}/{attempts} failed: {last_error}")
        time.sleep(5 * attempt)
    raise RuntimeError(f"Failed to fetch {url}") from last_error


def cached_fetch(cache_path, fetcher):
    """Read a cached GeoJSON window if present; otherwise fetch it and cache it."""
    if cache_path.exists():
        return gpd.read_file(cache_path)
    frame = fetcher()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_file(cache_path, driver="GeoJSON")
    return frame


def fetch_vicmap_window(bbox, page_size=5000):
    """Download the Vicmap road lines inside `bbox` from the WFS service, page by page."""
    features = []
    start = 0
    while True:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": VICMAP_LAYER, "outputFormat": "application/json",
            "srsName": "EPSG:4326", "sortBy": "ufi",
            "count": page_size, "startIndex": start,
            "bbox": ",".join(f"{value:.6f}" for value in bbox) + ",EPSG:4326",
        }
        payload = fetch_json(f"{VICMAP_WFS}?{urllib.parse.urlencode(params)}")
        page = payload.get("features", [])
        features.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")


def fetch_nsw_window(bbox, page_size=2000):
    """Download the NSW Road Segment features inside `bbox` from the ArcGIS service, page by page."""
    features = []
    offset = 0
    while True:
        params = {
            "where": "1=1", "outFields": ",".join(NSW_FIELDS),
            "geometry": ",".join(f"{value:.6f}" for value in bbox),
            "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outSR": 4326,
            "orderByFields": "objectid", "resultOffset": offset,
            "resultRecordCount": page_size, "f": "geojson",
        }
        payload = fetch_json(f"{NSW_ROAD_SEGMENT}?{urllib.parse.urlencode(params)}")
        page = payload.get("features", [])
        features.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")


def _clean_name(value):
    """Upper-case a road name and map empty or placeholder names to ''."""
    text = "" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value)
    text = text.strip().upper()
    return "" if text in {"", "UNNAMED", "NONE", "NAN"} else text


def normalise_vicmap(frame):
    """Map Vicmap tr_road codes onto the reference schema used by step01.

    Codes were checked against the study windows: class_code 9 carries only
    trails and pedestrian underpasses (vehicular_access 4) and 14 carries
    ferry routes, so both are non-vehicular.  direction_code is B (both) or
    F (digitised direction); R is handled as the reverse.
    """
    feature_type = frame["feature_type_code"].fillna("").str.lower()
    class_code = pd.to_numeric(frame["class_code"], errors="coerce")
    frame = frame[~feature_type.eq("ferry_route") & ~class_code.eq(14)].copy()
    feature_type = frame["feature_type_code"].fillna("").str.lower()
    class_code = pd.to_numeric(frame["class_code"], errors="coerce")
    non_vehicular = (
        feature_type.isin({"trail", "foot_bridge"})
        | class_code.eq(9)
        | frame["vehicular_access"].astype(str).eq("4")
    )
    subtype = np.select(
        [non_vehicular, feature_type.eq("tunnel"), feature_type.eq("bridge")],
        ["PATHWAY", "TUNNEL", "BRIDGE"],
        default="ROAD",
    )
    direction = frame["direction_code"].fillna("").str.upper()
    status = frame["road_status"].fillna("").str.upper()
    access = frame["vehicular_access"].astype(str)
    return gpd.GeoDataFrame(
        {
            "ROAD_PID": "VICMAP_" + frame["ufi"].astype(str),
            "RD_NAME": frame["road_name"].map(_clean_name),
            "RD_NAM_LAB": frame["ezi_road_name_label"].map(_clean_name),
            "SUBTYPE": subtype,
            "LANE_COUNT": np.nan,
            "ONE_WAY": direction.map({"B": "TWO WAY", "F": "ONE WAY", "R": "ONE WAY"}),
            "TRADIR": direction.map({"B": "BOTH", "F": "FROM TO", "R": "TO FROM"}),
            "STATUS": status.map({"O": "OPERATIONAL", "P": "PROPOSED"}).fillna(
                "UNKNOWN_" + status
            ).where(status.ne(""), ""),
            "ACCESS_TYP": np.where(non_vehicular, "NO ACCESS", np.where(access.eq("1"), "PUBLIC", "")),
            "TRFFCBL": "",
            "SOURCE": "Vicmap Transport tr_road",
        },
        geometry=frame.geometry.values,
        crs=frame.crs,
    )


def normalise_nsw(frame):
    """Map NSW Road Segment coded values onto the reference schema.

    The NSW layer has no one-way or travel-direction attribute, and lanecount
    only separates one lane from two or more, so only the unambiguous
    single-lane value is carried forward.
    """
    on_type = pd.to_numeric(frame["roadontype"], errors="coerce")
    hierarchy = pd.to_numeric(frame["functionhierarchy"], errors="coerce")
    status = pd.to_numeric(frame["operationalstatus"], errors="coerce")
    surface = pd.to_numeric(frame["surface"], errors="coerce")
    lanes = pd.to_numeric(frame["lanecount"], errors="coerce")
    is_path = hierarchy.eq(9)
    subtype = np.select(
        [is_path, on_type.eq(3), on_type.eq(2)],
        ["PATHWAY", "TUNNEL", "BRIDGE"],
        default="ROAD",
    )
    status_text = status.map({
        1: "OPERATIONAL", 2: "UNDER CONSTRUCTION", 3: "PROPOSED",
        4: "CLOSED", 5: "CLOSED", 6: "CLOSED", 7: "", 8: "NOTIONAL",
    }).fillna("")
    trafficable = np.select(
        [surface.isin([3, 4]), surface.isin([1, 2])], ["4WD", "2WD"], default=""
    )
    topoid = pd.to_numeric(frame["topoid"], errors="coerce")
    pid = np.where(
        topoid.notna(),
        "NSWRS_" + topoid.fillna(0).astype("int64").astype(str),
        "NSWRS_OID_" + frame["objectid"].astype(str),
    )
    return gpd.GeoDataFrame(
        {
            "ROAD_PID": pid,
            "RD_NAME": frame["roadnamebase"].map(_clean_name),
            "RD_NAM_LAB": (
                frame["roadnamebase"].fillna("").astype(str) + " "
                + frame["roadnametype"].fillna("").astype(str)
            ).map(_clean_name),
            "SUBTYPE": subtype,
            "LANE_COUNT": np.where(lanes.eq(1), 1.0, np.nan),
            "ONE_WAY": None,
            "TRADIR": None,
            "STATUS": status_text,
            "ACCESS_TYP": np.where(is_path, "NO ACCESS", ""),
            "TRFFCBL": trafficable,
            "SOURCE": "NSW FSDF Road Segment",
        },
        geometry=frame.geometry.values,
        crs=frame.crs,
    )


def _speed_value(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else np.nan


def load_speed_zones(state, bbox):
    """Load the state speed-zone file within `bbox`, with speeds in km/h and metric geometry."""
    source = SPEED_ZONE_SOURCES[state]
    path = RAW_DIR / source["file"]
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Download it from {source['url']} into {RAW_DIR}."
        )
    if state == "vic":
        zones = pyogrio.read_dataframe(path, bbox=bbox, columns=["speed_limit"])
        zones["speed"] = zones["speed_limit"].map(_speed_value)
    else:
        zones = pyogrio.read_dataframe(path, bbox=bbox, columns=["Type", "Speed"])
        zone_type = zones["Type"].fillna("").str.strip().str.upper()
        zones = zones[~zone_type.isin(NSW_EXCLUDED_ZONE_TYPES)].copy()
        zones["speed"] = zones["Speed"].map(_speed_value)
    zones = zones[zones["speed"].gt(0) & zones.geometry.notna()]
    return zones[["speed", "geometry"]].to_crs(METRIC_CRS[state]).reset_index(drop=True)


def _line_parts(geometry):
    """Split a (multi)line geometry into its LineString parts."""
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [part for item in geometry.geoms for part in _line_parts(item)]
    return []


def _axis_difference_degrees(first, second):
    """Undirected angle between two line directions, in [0, 90]."""
    difference = abs(first - second) % 180.0
    return min(difference, 180.0 - difference)


def _part_bearing(part):
    (x0, y0), (x1, y1) = part.coords[0][:2], part.coords[-1][:2]
    return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 180.0


def _local_bearing(line, distance, step=5.0):
    """Undirected bearing (0 to 180 degrees) of `line` around the point at `distance`."""
    start = line.interpolate(max(0.0, distance - step))
    end = line.interpolate(min(line.length, distance + step))
    return math.degrees(math.atan2(end.x - start.x, end.y - start.y)) % 180.0


def parallel_overlap(segment, zone_geometry, buffer):
    """Length of a zone line inside the buffer that runs along the segment.

    Cross streets and other non-parallel lines inside the buffer are not
    evidence for the segment's own limit.
    """
    total = 0.0
    for piece in _line_parts(zone_geometry.intersection(buffer)):
        if piece.length < 1.0:
            continue
        midpoint = piece.interpolate(0.5, normalized=True)
        segment_bearing = _local_bearing(segment, segment.project(midpoint))
        if _axis_difference_degrees(_part_bearing(piece), segment_bearing) <= SPEED_MAX_AXIS_DIFFERENCE_DEG:
            total += piece.length
    return total


def assign_speeds(roads, zones, state):
    """Assign a posted speed to each reference segment from the zone lines.

    A zone qualifies when its parallel overlap covers SPEED_MIN_COVERAGE of
    the segment.  Each zone line is scored on its own, so a two-way street
    published as one line per direction does not count double.  Surface
    segments take the qualifying zone with the longest overlap (ties to the
    lower limit).

    Speed-zone lines carry no vertical level, so a structure also overlaps
    the streets above or below it in plan view.  Tunnels are left without a
    speed: their own zone line is often absent (e.g. tolled NorthConnex
    ramps), leaving only the street above, so step01 falls back to OSM
    maxspeed or its injected-tunnel default.  Bridges take the highest qualifying limit,
    since streets passing beneath are local roads with lower limits.
    """
    metric = roads.to_crs(METRIC_CRS[state])
    tunnel = roads["SUBTYPE"].eq("TUNNEL").to_numpy()
    bridge = roads["SUBTYPE"].eq("BRIDGE").to_numpy()
    index = zones.sindex
    speeds = np.full(len(metric), np.nan)
    for position, geometry in enumerate(metric.geometry.values):
        if tunnel[position] or geometry is None or geometry.is_empty or geometry.length <= 0:
            continue
        segment = geometry if geometry.geom_type == "LineString" else max(
            _line_parts(geometry), key=lambda part: part.length
        )
        buffer = geometry.buffer(SPEED_MATCH_BUFFER_M)
        required = SPEED_MIN_COVERAGE * geometry.length
        qualifying = []
        for zone_position in index.query(buffer, predicate="intersects"):
            overlap = parallel_overlap(segment, zones.geometry.iloc[zone_position], buffer)
            if overlap >= required:
                qualifying.append((float(zones["speed"].iloc[zone_position]), overlap))
        if not qualifying:
            continue
        if bridge[position]:
            speeds[position] = max(speed for speed, _overlap in qualifying)
        else:
            speeds[position] = min(qualifying, key=lambda item: (-item[1], item[0]))[0]
    roads = roads.copy()
    roads["SPEED"] = speeds
    return roads


def sha256(path):
    """SHA-256 hex digest of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_state(state, cases, refresh=False):
    """Fetch, normalise and merge the reference roads of every case in one state.

    Returns the merged output and the number of segments kept per case.
    """
    fetch = fetch_vicmap_window if state == "vic" else fetch_nsw_window
    normalise = normalise_vicmap if state == "vic" else normalise_nsw
    parts = []
    case_counts = {}
    for case_name, case in cases.items():
        lat, lon = case["coords"]
        bbox = window_bbox(lat, lon)
        cache_path = RAW_DIR / "windows" / f"{case_name}_roads_raw.geojson"
        if refresh and cache_path.exists():
            cache_path.unlink()
        raw = cached_fetch(cache_path, lambda: fetch(bbox))
        roads = normalise(raw)
        roads = assign_speeds(roads, load_speed_zones(state, bbox), state)
        case_counts[case_name] = {
            "segments": int(len(roads)),
            "with_speed": int(roads["SPEED"].notna().sum()),
            "subtype_counts": {k: int(v) for k, v in roads["SUBTYPE"].value_counts().items()},
        }
        log(f"  {case_name}: {len(roads)} segments, "
            f"{case_counts[case_name]['with_speed']} with speed, "
            f"{case_counts[case_name]['subtype_counts']}")
        parts.append(roads)
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.drop_duplicates(subset="ROAD_PID", keep="first")
    combined = gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")
    OPEN_ROADS_DIR.mkdir(parents=True, exist_ok=True)
    output = OPEN_ROADS_DIR / f"{state}_roads.geojson"
    combined.to_file(output, driver="GeoJSON")
    log(f"  wrote {output.relative_to(PROJECT_DIR)} ({len(combined)} segments)")
    return output, case_counts


def main():
    """Build the reference-road layers for both states and write SOURCES.json."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true",
                        help="Re-download road geometry instead of using cached windows.")
    args = parser.parse_args()

    manifest = {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_half_width_m": WINDOW_HALF_WIDTH_M,
        "speed_min_coverage": SPEED_MIN_COVERAGE,
        "speed_match_buffer_m": SPEED_MATCH_BUFFER_M,
        "speed_max_axis_difference_deg": SPEED_MAX_AXIS_DIFFERENCE_DEG,
        "states": {},
    }
    for state in ("vic", "nsw"):
        log(f"Building {state.upper()} open reference roads")
        cases = {
            name: case for name, case in hard_case_locations.items()
            if case["state"] == state
        }
        output, counts = build_state(state, cases, refresh=args.refresh)
        speed_source = SPEED_ZONE_SOURCES[state]
        manifest["states"][state] = {
            "output": str(output.relative_to(PROJECT_DIR)),
            "roads": ROAD_SOURCES[state],
            "speed_zones": {
                **{k: v for k, v in speed_source.items() if k != "file"},
                "sha256": sha256(RAW_DIR / speed_source["file"]),
            },
            "road_windows_sha256": {
                name: sha256(RAW_DIR / "windows" / f"{name}_roads_raw.geojson")
                for name in cases
            },
            "cases": counts,
        }
    manifest_path = OPEN_ROADS_DIR / "SOURCES.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log(f"Wrote {manifest_path.relative_to(PROJECT_DIR)}")


if __name__ == "__main__":
    main()
