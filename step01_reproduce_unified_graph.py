# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Larry M. Arjomandi
"""Step 01: build the unified road network for each study site.

The OSM drive network within 2 km of each site is downloaded, and edges that
a truck cannot use are removed. Each remaining edge is matched to the open
reference roads from step 00b using geometric overlap, name, direction and
road type, and a confident match supplies the name, tunnel and bridge
structure, direction and posted speed. Reference tunnel segments missing
from OSM are added where they can be attached to the OSM network. The graph
is projected to the state's MGA zone, the data licence notices are added,
and the result is saved as GraphML with a short printed check.
"""

import osmnx as ox
import networkx as nx
import geopandas as gpd
import pandas as pd
import numpy as np
from scipy.spatial import KDTree
from shapely.geometry import LineString
from shapely.ops import unary_union
import re
import os
import argparse
import math
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from pipeline_config import (
    DATA_ATTRIBUTION,
    DATA_DISCLAIMER,
    DATA_LICENSE,
    DATA_LICENSE_URL,
    DATA_MODIFICATIONS,
    default_max_workers,
    ensure_project_dirs,
    reference_roads_dir,
    reference_roads_path,
    metric_crs_for_state,
    unified_graph_path,
)


def log(*args, **kwargs):
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)

# Ask OSMnx to keep the vertical tags (tunnel, layer, bridge, level).
if 'tunnel' not in ox.settings.useful_tags_way:
    ox.settings.useful_tags_way.extend(['tunnel', 'layer', 'bridge', 'level'])

# 1. Precise Core Locations
hard_case_locations = {
    "Rozelle_Interchange_NSW": {"coords": (-33.8702, 151.1722), "state": "nsw"},
    # Southern portal of the tunnel.
    "West_Gate_Tunnel_VIC": {"coords": (-37.824208, 144.871139), "state": "vic"},
    "NorthConnex_NSW": {"coords": (-33.7589, 151.0464), "state": "nsw"},
    "Light_Horse_Interchange_NSW": {"coords": (-33.7980, 150.8540), "state": "nsw"},
    "Domain_Tunnel_VIC": {"coords": (-37.8362, 144.9700), "state": "vic"},
    "M80_Princes_Freeway_VIC": {"coords": (-37.8276, 144.8169), "state": "vic"}
}

REFERENCE_ROADS_PATH = reference_roads_dir()

REFERENCE_MATCH_MAX_DISTANCE_M = 50.0
REFERENCE_OVERLAP_BUFFER_M = 8.0
REFERENCE_MATCH_MIN_SCORE = 0.50
REFERENCE_AMBIGUITY_MARGIN = 0.03
# Pure tunnel components retain a tight 30 m portal cap.  A component with a
# short, authoritative, same-name ROAD/RAMP approach may use at most 100 m for
# the final source-to-OSM topology gap; a global 500 m nearest-node rule
# would distort tunnel geometry.
PORTAL_SNAP_MAX_DISTANCE_M = 30.0
PORTAL_APPROACH_SNAP_MAX_DISTANCE_M = 100.0


def to_flag(value):
    """Parse booleans and GraphML numeric strings without treating NaN as true."""
    if value is None:
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(np.isfinite(value) and float(value) != 0.0)
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "on"}:
        return True
    if text in {"", "false", "no", "n", "off", "none", "nan", "null"}:
        return False
    try:
        number = float(text)
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(number) and number != 0.0)


def _flatten_values(value):
    """Flatten nested lists or arrays of OSM attribute values into one list."""
    if isinstance(value, (list, tuple, set, np.ndarray)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_values(item))
        return flattened
    return [value]


def _is_missing(value):
    """True for None or NaN-like values; False for anything pandas cannot test."""
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, (bool, np.bool_)) else False


def first_non_missing(*values):
    """First value, searching inside lists, that is not None or NaN."""
    for value in values:
        for candidate in _flatten_values(value):
            if not _is_missing(candidate):
                return candidate
    return None


def numeric_value(value, default, as_int=False):
    """First number found in an attribute value (as float, or int if `as_int`), or `default`."""
    candidate = first_non_missing(value)
    if candidate is None:
        return default
    if isinstance(candidate, str):
        match = re.search(r"-?\d+(?:\.\d+)?", candidate)
        if not match:
            return default
        candidate = match.group(0)
    try:
        number = float(candidate)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(number):
        return default
    return int(number) if as_int else number


def _all_numeric_values(value):
    """Every finite number found in an attribute value, as floats."""
    numbers = []
    for candidate in _flatten_values(value):
        parsed = numeric_value(candidate, np.nan)
        if np.isfinite(parsed):
            numbers.append(float(parsed))
    return numbers


def _any_flag(value):
    return any(to_flag(candidate) for candidate in _flatten_values(value))


def _attribute_text(value):
    """Join an attribute's values into one lower-case string."""
    return " ".join(
        str(candidate).strip().lower()
        for candidate in _flatten_values(value)
        if not _is_missing(candidate)
    )


def is_operational_truck_edge(data):
    """Filter construction, closed, and explicitly restricted road edges."""
    highway = _attribute_text(data.get("highway"))
    construction = _attribute_text(data.get("construction"))
    ref_used = to_flag(data.get("source_ref", 0))
    status = _attribute_text(data.get("status"))
    access = " ".join(
        _attribute_text(data.get(key))
        for key in ("access", "vehicle", "motor_vehicle", "hgv")
    )
    if ref_used:
        status += " " + _attribute_text(data.get("ref_status"))
        access += " " + _attribute_text(data.get("ref_access_type"))
        trafficable = _attribute_text(data.get("ref_trafficable"))
    else:
        trafficable = ""
    access_tokens = set(re.sub(r"[^a-z0-9]+", " ", access).split())
    if "construction" in highway or construction not in {"", "no", "none", "nan"}:
        return False
    if any(token in status for token in ("closed", "proposed", "under construction")):
        return False
    if access_tokens.intersection({"no", "private"}) or "management only" in access:
        return False
    if trafficable and trafficable not in {"2wd", "nan", "none"}:
        return False
    return True


def remove_nonoperational_truck_edges(G):
    """Drop the edges a truck cannot use, then any nodes left isolated.

    Returns the graph and the numbers of removed edges and nodes.
    """
    if G.is_multigraph():
        rejected = [
            (u, v, key)
            for u, v, key, data in G.edges(keys=True, data=True)
            if not is_operational_truck_edge(data)
        ]
    else:
        rejected = [
            (u, v)
            for u, v, data in G.edges(data=True)
            if not is_operational_truck_edge(data)
        ]
    G.remove_edges_from(rejected)
    isolates = list(nx.isolates(G))
    G.remove_nodes_from(isolates)
    return G, len(rejected), len(isolates)


def _normalised_name_tokens(value):
    """Token sets for each road name in a value, without generic road-type words."""
    names = []
    for candidate in _flatten_values(value):
        if _is_missing(candidate):
            continue
        text = re.sub(r"[^a-z0-9]+", " ", str(candidate).lower()).strip()
        if not text:
            continue
        tokens = {
            token for token in text.split()
            if token not in {
                "road", "rd", "street", "st", "avenue", "ave", "drive", "dr",
                "freeway", "motorway", "highway", "hwy", "tunnel", "bridge",
            }
        }
        names.append(tokens or set(text.split()))
    return names


def _name_compatibility(osm_name, ref_name):
    """Similarity of the OSM and reference road names (best token overlap); 0.5 if either is missing."""
    osm_names = _normalised_name_tokens(osm_name)
    geo_names = _normalised_name_tokens(ref_name)
    if not osm_names or not geo_names:
        return 0.5
    best = 0.0
    for left in osm_names:
        for right in geo_names:
            union = left | right
            if union:
                best = max(best, len(left & right) / len(union))
    return float(best)


def _osm_subtype(row):
    """Classify an OSM edge as TUNNEL, BRIDGE, RAMP or ROAD from its tags."""
    layers = _all_numeric_values(row.get("layer"))
    if _any_flag(row.get("tunnel")) or any(layer < 0 for layer in layers):
        return "TUNNEL"
    if _any_flag(row.get("bridge")) or any(layer > 0 for layer in layers):
        return "BRIDGE"
    highway = " ".join(str(v).lower() for v in _flatten_values(row.get("highway")))
    return "RAMP" if "link" in highway else "ROAD"


def _subtype_compatibility(osm_subtype, ref_subtype):
    """How well the OSM subtype agrees with the reference subtype (0.5 if the latter is unknown)."""
    gs = "" if _is_missing(ref_subtype) else str(ref_subtype).strip().upper()
    if not gs or gs == "NAN":
        return 0.5
    if osm_subtype == gs:
        return 1.0
    if {osm_subtype, gs} <= {"ROAD", "RAMP", "ROUNDABOUT"}:
        return 0.75
    # A missing OSM vertical tag is not strong evidence against an
    # authoritative reference TUNNEL/BRIDGE label.
    if osm_subtype == "ROAD" and gs in {"TUNNEL", "BRIDGE"}:
        return 0.65
    return 0.0


def _effective_reference_oneway(row, one_way_col):
    """True or False if the reference road is one-way or two-way, None if not recorded."""
    if one_way_col is None:
        return None
    value = row.get(one_way_col)
    if _is_missing(value):
        return None
    text = str(value).strip().lower()
    if "two way" in text or text in {"both", "two-way"}:
        return False
    if "one way" in text or text in {"one-way", "from to", "to from"}:
        return True
    return to_flag(value)


def _angular_difference_degrees(left, right):
    return abs((float(left) - float(right) + 180.0) % 360.0 - 180.0)


def _geometry_bearing(geometry):
    """Bearing in degrees from the first to the last point of the line (longest part if multi-part)."""
    if geometry is None or geometry.is_empty:
        return None
    part = max(geometry.geoms, key=lambda item: item.length) if geometry.geom_type == "MultiLineString" else geometry
    if not hasattr(part, "coords") or len(part.coords) < 2:
        return None
    start, end = part.coords[0], part.coords[-1]
    dx, dy = float(end[0] - start[0]), float(end[1] - start[1])
    if dx == 0.0 and dy == 0.0:
        return None
    return float((math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0)


def _direction_compatibility(osm_row, ref_row, one_way_col, tradir_col):
    """Agreement of one-way status and, for one-way roads, of travel bearing (0 to 1)."""
    gs_oneway = _effective_reference_oneway(ref_row, one_way_col)
    osm_oneway = _any_flag(osm_row.get("oneway"))
    if gs_oneway is None:
        return 0.5
    compatibility = 1.0 if osm_oneway == gs_oneway else 0.35
    if not (osm_oneway and gs_oneway):
        return compatibility

    osm_bearing = numeric_value(osm_row.get("bearing"), np.nan)
    gs_bearing = _geometry_bearing(ref_row.geometry)
    if not np.isfinite(osm_bearing) or gs_bearing is None:
        return compatibility
    tradir = str(ref_row.get(tradir_col, "") if tradir_col else "").strip().upper()
    if tradir == "TO FROM":
        gs_bearing = (gs_bearing + 180.0) % 360.0
    alignment = max(0.0, 1.0 - _angular_difference_degrees(osm_bearing, gs_bearing) / 90.0)
    return float(0.5 * compatibility + 0.5 * alignment)


def _line_overlap_score(left, right, buffer_m=REFERENCE_OVERLAP_BUFFER_M):
    """Overlap of two lines within `buffer_m`, mixing the larger and smaller coverage (0 to 1)."""
    if left is None or right is None or left.is_empty or right.is_empty:
        return 0.0
    left_length = max(float(left.length), 1e-6)
    right_length = max(float(right.length), 1e-6)
    try:
        left_coverage = float(left.intersection(right.buffer(buffer_m)).length) / left_length
        right_coverage = float(right.intersection(left.buffer(buffer_m)).length) / right_length
    except Exception:
        return 0.0
    # The reference layer is often more finely segmented than a simplified OSM way, so
    # retain a high score when either line substantially explains the other.
    return float(np.clip(0.65 * max(left_coverage, right_coverage) + 0.35 * min(left_coverage, right_coverage), 0.0, 1.0))


def match_reference_edges(
    edges,
    geo_df,
    subtype_col,
    lane_col,
    speed_col,
    max_distance_m=REFERENCE_MATCH_MAX_DISTANCE_M,
):
    """Choose one deterministic, auditable reference match per OSM edge."""
    road_pid_col = pick_column_name(geo_df, ["ROAD_PID", "road_pid", "ID", "id"])
    road_name_col = pick_column_name(geo_df, ["RD_NAME", "RD_NAM_LAB", "road_name", "name"])
    one_way_col = pick_column_name(geo_df, ["ONE_WAY", "one_way", "ONEWAY"])
    tradir_col = pick_column_name(geo_df, ["TRADIR", "travel_direction", "DIRECTION"])
    status_col = pick_column_name(geo_df, ["STATUS", "status"])
    access_col = pick_column_name(geo_df, ["ACCESS_TYP", "access_type", "ACCESS"])
    trafficable_col = pick_column_name(geo_df, ["TRFFCBL", "trafficable"])

    output_rows = []
    for edge_index, osm_row in edges.iterrows():
        geometry = osm_row.geometry
        candidate_positions = list(geo_df.sindex.query(geometry.buffer(float(max_distance_m)), predicate="intersects"))
        scored = []
        for position in candidate_positions:
            gs_row = geo_df.iloc[int(position)]
            if not is_operational_truck_edge(
                {
                    "source_ref": 1,
                    "ref_status": gs_row.get(status_col) if status_col else "",
                    "ref_access_type": gs_row.get(access_col) if access_col else "",
                    "ref_trafficable": gs_row.get(trafficable_col) if trafficable_col else "",
                }
            ):
                continue
            gs_geometry = gs_row.geometry
            if gs_geometry is None or gs_geometry.is_empty:
                continue
            distance = float(geometry.distance(gs_geometry))
            if distance > float(max_distance_m):
                continue
            overlap = _line_overlap_score(geometry, gs_geometry)
            name_score = _name_compatibility(osm_row.get("name"), gs_row.get(road_name_col) if road_name_col else None)
            subtype_score = _subtype_compatibility(
                _osm_subtype(osm_row),
                gs_row.get(subtype_col) if subtype_col else None,
            )
            direction_score = _direction_compatibility(osm_row, gs_row, one_way_col, tradir_col)
            distance_score = max(0.0, 1.0 - distance / float(max_distance_m))
            total = (
                0.35 * overlap
                + 0.20 * distance_score
                + 0.20 * name_score
                + 0.15 * direction_score
                + 0.10 * subtype_score
            )
            road_pid = str(gs_row.get(road_pid_col, "") if road_pid_col else gs_row.name)
            scored.append(
                {
                    "row": gs_row,
                    "road_pid": road_pid,
                    "distance": distance,
                    "overlap": overlap,
                    "name_score": name_score,
                    "subtype_score": subtype_score,
                    "direction_score": direction_score,
                    "total": float(total),
                }
            )

        scored.sort(
            key=lambda item: (
                -item["total"],
                -item["overlap"],
                item["distance"],
                item["road_pid"],
            )
        )
        if not scored:
            output_rows.append({"_edge_index": edge_index, "source_ref": 0, "ref_match_method": "no_candidate_within_50m"})
            continue

        best = scored[0]
        second = scored[1] if len(scored) > 1 else None
        margin = best["total"] - second["total"] if second else best["total"]
        best_subtype = str(best["row"].get(subtype_col, "") if subtype_col else "").strip().upper()
        # A low-margin match is not a unique source-identity match even when
        # the two candidates share a subtype.  Treat every such tie as
        # ambiguous so it cannot silently overwrite vertical semantics.
        semantic_ambiguity = bool(
            second is not None and margin < REFERENCE_AMBIGUITY_MARGIN
        )
        vertical_evidence = bool(
            best_subtype not in {"TUNNEL", "BRIDGE"}
            or best["overlap"] >= 0.15
            or (
                best["name_score"] >= 0.75
                and best["direction_score"] >= 0.5
            )
        )
        accepted = bool(
            best["total"] >= REFERENCE_MATCH_MIN_SCORE
            and (best["overlap"] >= 0.15 or best["distance"] <= 15.0)
        )
        confident = bool(accepted and not semantic_ambiguity and vertical_evidence)
        gs_row = best["row"]
        output_rows.append(
            {
                "_edge_index": edge_index,
                "source_ref": int(accepted),
                "ref_match_confident": int(confident),
                "ref_match_ambiguous": int(semantic_ambiguity or not vertical_evidence),
                "ref_match_method": "overlap_name_direction_subtype_v1",
                "ref_candidate_count": int(len(scored)),
                "ref_road_pid": best["road_pid"],
                "ref_match_distance_m": best["distance"],
                "ref_overlap_score": best["overlap"],
                "ref_name_score": best["name_score"],
                "ref_direction_score": best["direction_score"],
                "ref_subtype_score": best["subtype_score"],
                "ref_match_score": best["total"],
                "ref_match_margin": float(margin),
                "ref_subtype": str(gs_row.get(subtype_col, "") if subtype_col else ""),
                "ref_lane_count": gs_row.get(lane_col) if lane_col else np.nan,
                "ref_speed": gs_row.get(speed_col) if speed_col else np.nan,
                "ref_road_name": str(gs_row.get(road_name_col, "") if road_name_col else ""),
                "ref_one_way": str(gs_row.get(one_way_col, "") if one_way_col else ""),
                "ref_tradir": str(gs_row.get(tradir_col, "") if tradir_col else ""),
                "ref_status": str(gs_row.get(status_col, "") if status_col else ""),
                "ref_access_type": str(gs_row.get(access_col, "") if access_col else ""),
                "ref_trafficable": str(gs_row.get(trafficable_col, "") if trafficable_col else ""),
            }
        )

    matches = pd.DataFrame(output_rows).set_index("_edge_index")
    matches.index = edges.index
    return matches


def inject_missing_reference_tunnels(G, geo_df, centre_lat, centre_lon, metric_crs):
    """Insert authoritative tunnel corridors plus their named portal approaches.

    ROAD/RAMP features are admitted only when they share the name of a local
    TUNNEL feature.  They retain their own surface semantics; approach
    roads are never relabelled as tunnels.
    """
    centre = gpd.GeoSeries(
        [gpd.points_from_xy([centre_lon], [centre_lat], crs="EPSG:4326")[0]],
        crs="EPSG:4326",
    ).to_crs(metric_crs).iloc[0]
    subtype_col = pick_column_name(geo_df, ["SUBTYPE", "subtype", "ROAD_SUBTYPE"])
    if subtype_col is None:
        return G, 0
    local_window = geo_df[geo_df.geometry.intersects(centre.buffer(2500.0))].copy()
    road_name_col = pick_column_name(
        local_window, ["RD_NAME", "RD_NAM_LAB", "road_name", "name"]
    )
    subtype_values = local_window[subtype_col].fillna("").astype(str).str.upper()
    tunnel_rows = local_window[subtype_values.eq("TUNNEL")].copy()
    tunnel_names = set()
    if road_name_col is not None:
        tunnel_names = {
            name.strip().upper()
            for name in tunnel_rows[road_name_col].dropna().astype(str)
            if name.strip() and name.strip().upper() not in {"NONE", "NAN"}
        }
    connector_mask = pd.Series(False, index=local_window.index)
    if tunnel_names and road_name_col is not None:
        named_connector_candidates = (
            local_window[road_name_col].fillna("").astype(str).str.upper().isin(tunnel_names)
            & subtype_values.isin({"ROAD", "RAMP"})
        )
        connector_names = local_window.loc[
            named_connector_candidates, road_name_col
        ].fillna("").astype(str).str.upper()
        connector_counts = connector_names.value_counts()
        # A short, explicitly tunnel-named portal approach is useful (West
        # Gate has three). Broad road names such as CITYLINK otherwise pull
        # hundreds of unrelated surface features into the graph.
        allowed_connector_names = {
            name for name, count in connector_counts.items()
            if "TUNNEL" in name and int(count) <= 10
        }
        connector_mask = named_connector_candidates & (
            local_window[road_name_col].fillna("").astype(str).str.upper()
            .isin(allowed_connector_names)
        )
    local = local_window[subtype_values.eq("TUNNEL") | connector_mask].copy()
    status_col = pick_column_name(local, ["STATUS", "status"])
    access_col = pick_column_name(local, ["ACCESS_TYP", "access_type", "ACCESS"])
    trafficable_col = pick_column_name(local, ["TRFFCBL", "trafficable"])
    road_pid_col = pick_column_name(local, ["ROAD_PID", "road_pid", "ID", "id"])
    one_way_col = pick_column_name(local, ["ONE_WAY", "one_way", "ONEWAY"])
    tradir_col = pick_column_name(local, ["TRADIR", "travel_direction", "DIRECTION"])
    lane_col = pick_column_name(local, ["LANE_COUNT", "lane_count", "LANES", "lanes"])
    speed_col = pick_column_name(local, ["SPEED", "speed", "SPEED_LIMIT", "speed_limit", "MAXSPEED", "maxspeed"])
    if status_col:
        status = local[status_col].fillna("").astype(str).str.upper()
        local = local[status.isin({"", "OPERATIONAL"})]
    if access_col:
        access = local[access_col].fillna("").astype(str).str.upper()
        local = local[~access.isin({"PRIVATE", "MANAGEMENT ONLY", "NO ACCESS"})]
    if trafficable_col:
        trafficable = local[trafficable_col].fillna("").astype(str).str.upper()
        local = local[trafficable.isin({"", "2WD"})]
    if local.empty:
        return G, 0

    existing_tunnel_geometries = []
    for _u, _v, data in G.edges(data=True):
        layers = _all_numeric_values(data.get("layer"))
        if not (_any_flag(data.get("tunnel")) or any(layer < 0 for layer in layers)):
            continue
        geometry = data.get("geometry")
        if geometry is None:
            try:
                geometry = LineString([
                    (float(G.nodes[_u]["x"]), float(G.nodes[_u]["y"])),
                    (float(G.nodes[_v]["x"]), float(G.nodes[_v]["y"])),
                ])
            except Exception:
                geometry = None
        elif isinstance(geometry, str):
            try:
                geometry = gpd.GeoSeries.from_wkt([geometry], crs=metric_crs).iloc[0]
            except Exception:
                geometry = None
        if geometry is not None and not geometry.is_empty:
            existing_tunnel_geometries.append(geometry)
    # OSM commonly splits one physical tunnel carriageway into several edges.
    # Coverage must therefore be measured against their union; testing each
    # edge separately can inject a second, exact-edge-distinct copy of a
    # tunnel that is already represented across multiple OSM segments.
    existing_tunnel_coverage = (
        unary_union([
            geometry.buffer(REFERENCE_OVERLAP_BUFFER_M)
            for geometry in existing_tunnel_geometries
        ])
        if existing_tunnel_geometries
        else None
    )

    endpoint_counts = Counter()
    features = []
    for row_index, row in local.iterrows():
        geometry = row.geometry
        parts = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]
        for part_index, part in enumerate(parts):
            if part.is_empty or len(part.coords) < 2:
                continue
            source_subtype = str(row.get(subtype_col, "")).strip().upper()
            # Only compare authoritative tunnel pieces with already-modelled
            # tunnel geometry.  Named ROAD/RAMP portal approaches are kept in
            # the component even when they overlap an OSM edge, because they
            # provide the audited topological bridge to the portal.
            already_covered = (
                source_subtype == "TUNNEL"
                and existing_tunnel_coverage is not None
                and float(part.intersection(existing_tunnel_coverage).length)
                / max(float(part.length), 1e-6)
                >= 0.80
            )
            if already_covered:
                continue
            start = tuple(map(float, part.coords[0][:2]))
            end = tuple(map(float, part.coords[-1][:2]))
            start_key = (round(start[0], 3), round(start[1], 3))
            end_key = (round(end[0], 3), round(end[1], 3))
            endpoint_counts[start_key] += 1
            endpoint_counts[end_key] += 1
            features.append((row_index, part_index, row, part, start_key, end_key))

    existing_nodes = list(G.nodes)
    existing_xy = np.asarray([
        [float(G.nodes[node]["x"]), float(G.nodes[node]["y"])] for node in existing_nodes
    ])
    if not len(existing_xy):
        return G, 0
    node_tree = KDTree(existing_xy)

    feature_connectivity = nx.Graph()
    feature_connectivity.add_edges_from((feature[4], feature[5]) for feature in features)
    approach_endpoints = set()
    for _row_index, _part_index, row, _geometry, start_key, end_key in features:
        if str(row.get(subtype_col, "")).strip().upper() in {"ROAD", "RAMP"}:
            approach_endpoints.update({start_key, end_key})
    component_snap_caps = {}
    for component in nx.connected_components(feature_connectivity):
        cap = (
            PORTAL_APPROACH_SNAP_MAX_DISTANCE_M
            if any(key in approach_endpoints for key in component)
            else PORTAL_SNAP_MAX_DISTANCE_M
        )
        for key in component:
            component_snap_caps[key] = float(cap)

    node_ids = {}
    node_snap_distances = {}
    node_was_snapped = {}
    for key, count in endpoint_counts.items():
        x, y = key
        node_id = f"ref_tunnel_{x:.3f}_{y:.3f}"
        snap_distance = np.nan
        if count == 1:
            distance, idx = node_tree.query([x, y])
            snap_distance = float(distance)
            if snap_distance <= component_snap_caps.get(key, PORTAL_SNAP_MAX_DISTANCE_M):
                node_id = existing_nodes[int(idx)]
        node_ids[key] = node_id
        node_snap_distances[key] = snap_distance
        node_was_snapped[key] = bool(
            count == 1
            and np.isfinite(snap_distance)
            and snap_distance <= component_snap_caps.get(key, PORTAL_SNAP_MAX_DISTANCE_M)
        )

    allowed_endpoints = set()
    component_snap_counts = {}
    skipped_components = 0
    for component in nx.connected_components(feature_connectivity):
        snap_count = sum(int(node_was_snapped.get(key, False)) for key in component)
        if snap_count == 0:
            skipped_components += 1
            continue
        allowed_endpoints.update(component)
        for key in component:
            component_snap_counts[key] = snap_count
    if skipped_components:
        log(
            f"  WARNING: skipped {skipped_components} uncovered reference tunnel component(s) "
            "with no portal inside its audited 30/100 m OSM-attachment cap"
        )
    for key in allowed_endpoints:
        node_id = node_ids[key]
        if node_id not in G:
            x, y = key
            G.add_node(node_id, x=float(x), y=float(y), street_count=2)

    def has_same_source_direction(start_node, end_node, road_pid):
        """True when this authoritative feature/direction is already present."""
        if not G.has_edge(start_node, end_node):
            return False
        payload = G.get_edge_data(start_node, end_node) or {}
        candidates = payload.values() if G.is_multigraph() else [payload]
        return any(
            str(data.get("ref_road_pid", "")).strip() == str(road_pid).strip()
            for data in candidates
        )

    injected = 0
    for row_index, part_index, row, geometry, start_key, end_key in features:
        if start_key not in allowed_endpoints or end_key not in allowed_endpoints:
            continue
        u, v = node_ids[start_key], node_ids[end_key]
        if u == v:
            continue
        coords = list(geometry.coords)
        coords[0] = (float(G.nodes[u]["x"]), float(G.nodes[u]["y"]))
        coords[-1] = (float(G.nodes[v]["x"]), float(G.nodes[v]["y"]))
        geometry = type(geometry)(coords)
        dx = float(geometry.coords[-1][0] - geometry.coords[0][0])
        dy = float(geometry.coords[-1][1] - geometry.coords[0][1])
        bearing = float((math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0)
        road_name = str(row.get(road_name_col, "Unnamed tunnel") if road_name_col else "Unnamed tunnel")
        raw_lanes = pd.to_numeric(pd.Series([row.get(lane_col) if lane_col else None]), errors="coerce").iloc[0]
        raw_speed = pd.to_numeric(pd.Series([row.get(speed_col) if speed_col else None]), errors="coerce").iloc[0]
        lanes = int(raw_lanes) if pd.notna(raw_lanes) and raw_lanes > 0 else 3
        speed = float(raw_speed) if pd.notna(raw_speed) and raw_speed > 0 else 80.0
        road_pid = str(row.get(road_pid_col, row_index) if road_pid_col else row_index)
        source_subtype = str(row.get(subtype_col, "TUNNEL")).strip().upper()
        is_tunnel_segment = source_subtype == "TUNNEL"
        one_way = _effective_reference_oneway(row, one_way_col)
        tradir = str(row.get(tradir_col, "") if tradir_col else "").strip().upper()
        edge_start_key, edge_end_key = start_key, end_key
        if one_way and tradir == "TO FROM":
            u, v = v, u
            geometry = type(geometry)(list(geometry.coords)[::-1])
            bearing = (bearing + 180.0) % 360.0
            edge_start_key, edge_end_key = edge_end_key, edge_start_key
        attrs = {
            "osmid": f"ref_{road_pid}_{part_index}",
            "id": f"ref_{road_pid}_{part_index}",
            "name": road_name,
            "highway": "motorway" if is_tunnel_segment else "motorway_link",
            "length": float(geometry.length),
            "bearing": bearing,
            "geometry": geometry,
            "layer": -1 if is_tunnel_segment else 0,
            "maxspeed": speed,
            "lanes": lanes,
            "tunnel": int(is_tunnel_segment),
            "bridge": 0,
            "oneway": bool(one_way),
            "highway_idx": 1.0,
            "source_ref": 1,
            "ref_injected": 1,
            "ref_road_pid": road_pid,
            "ref_subtype": source_subtype,
            "ref_road_name": road_name,
            "ref_one_way": str(row.get(one_way_col, "") if one_way_col else ""),
            "ref_tradir": tradir,
            "ref_status": str(row.get(status_col, "") if status_col else ""),
            "ref_access_type": str(row.get(access_col, "") if access_col else ""),
            "ref_trafficable": str(row.get(trafficable_col, "") if trafficable_col else ""),
            "ref_match_method": (
                "authoritative_uncovered_tunnel_segment"
                if is_tunnel_segment
                else "authoritative_named_portal_approach"
            ),
            "ref_match_confident": 1,
            "ref_match_ambiguous": 0,
            "portal_snap_max_distance_m": float(
                component_snap_caps.get(edge_start_key, PORTAL_SNAP_MAX_DISTANCE_M)
            ),
            "portal_start_nearest_node_distance_m": float(node_snap_distances.get(edge_start_key, np.nan)),
            "portal_end_nearest_node_distance_m": float(node_snap_distances.get(edge_end_key, np.nan)),
            "portal_start_snapped": int(node_was_snapped.get(edge_start_key, False)),
            "portal_end_snapped": int(node_was_snapped.get(edge_end_key, False)),
            "ref_component_portal_snap_count": int(component_snap_counts.get(edge_start_key, 0)),
            "reversed": False,
        }
        if not has_same_source_direction(u, v, road_pid):
            G.add_edge(u, v, **attrs)
            injected += 1
        if not one_way:
            reverse_attrs = dict(attrs)
            reverse_attrs["id"] += "_reverse"
            reverse_attrs["osmid"] += "_reverse"
            reverse_attrs["bearing"] = (bearing + 180.0) % 360.0
            reverse_attrs["geometry"] = type(geometry)(list(geometry.coords)[::-1])
            reverse_attrs["portal_start_nearest_node_distance_m"] = attrs[
                "portal_end_nearest_node_distance_m"
            ]
            reverse_attrs["portal_end_nearest_node_distance_m"] = attrs[
                "portal_start_nearest_node_distance_m"
            ]
            reverse_attrs["portal_start_snapped"] = attrs["portal_end_snapped"]
            reverse_attrs["portal_end_snapped"] = attrs["portal_start_snapped"]
            reverse_attrs["reversed"] = True
            if not has_same_source_direction(v, u, road_pid):
                G.add_edge(v, u, **reverse_attrs)
                injected += 1
    return G, injected


def pick_column_name(df, preferred_names, fallback=None):
    """First of `preferred_names` found in `df` (case-insensitive), else `fallback`."""
    cols_lower = {c.lower(): c for c in df.columns}
    for name in preferred_names:
        hit = cols_lower.get(name.lower())
        if hit is not None:
            return hit
    return fallback

def reproduce_unified_graph(name, lat, lon, state_abbr):
    """Build, enrich and save the unified graph for one study site; return the GraphML path."""
    log(f"\n--- Fusing {name} for Quantum Paper ---")
    metric_crs = metric_crs_for_state(state_abbr)
    
    # Retain the established Overpass query so an existing OSMnx cache remains
    # reusable, then deterministically remove non-operational/access-restricted
    # edges before any candidate or ground-truth data can use them.
    cf = '["highway"~"motorway|motorway_link|trunk|primary|secondary|construction"]'
    G = ox.graph_from_point((lat, lon), dist=2000, custom_filter=cf, simplify=True)
    G, rejected_count, isolated_count = remove_nonoperational_truck_edges(G)
    log(
        f"  > Removed {rejected_count} non-operational/restricted OSM edges "
        f"and {isolated_count} isolated nodes"
    )
    if G.number_of_edges() == 0:
        raise RuntimeError(f"No operational road edges remain for {name}")
    
    # Calculate Bearing (Parameter #9)
    G = ox.add_edge_bearings(G)
    G = ox.project_graph(G, to_crs=metric_crs)
    
    nodes, edges = ox.graph_to_gdfs(G)

    # Load state-first reference GeoJSON
    ref_path = reference_roads_path(state_abbr)
    if ref_path is not None:
        log(f"  > Using reference source: {ref_path.name}")
        geo_df = gpd.read_file(ref_path).to_crs(metric_crs)

        subtype_col = pick_column_name(geo_df, ["SUBTYPE", "subtype", "ROAD_SUBTYPE"])
        lane_col = pick_column_name(geo_df, ["LANE_COUNT", "lane_count", "LANES", "lanes"])
        speed_col = pick_column_name(geo_df, ["SPEED", "speed", "SPEED_LIMIT", "speed_limit", "MAXSPEED", "maxspeed"])
        
        # Explicit verification that critical columns exist.
        if subtype_col is None:
            log("  WARNING: reference SUBTYPE column not found; vertical reference labels will not be fused.")

        matches = match_reference_edges(edges, geo_df, subtype_col, lane_col, speed_col)
        joined = edges.join(matches)
        for flag_col in ["source_ref", "ref_match_confident", "ref_match_ambiguous"]:
            raw_column = joined.get(flag_col, pd.Series(0, index=joined.index, dtype=int))
            joined[flag_col] = pd.to_numeric(raw_column, errors="coerce").fillna(0).astype(int)
        for text_col in [
            "ref_match_method", "ref_road_pid", "ref_subtype",
            "ref_road_name", "ref_one_way", "ref_tradir",
            "ref_status", "ref_access_type", "ref_trafficable",
        ]:
            joined[text_col] = joined.get(text_col, pd.Series(index=joined.index, dtype=object)).fillna("").astype(str)
        for numeric_col in [
            "ref_candidate_count", "ref_match_distance_m", "ref_overlap_score",
            "ref_name_score", "ref_direction_score", "ref_subtype_score",
            "ref_match_score", "ref_match_margin", "ref_lane_count",
            "ref_speed",
        ]:
            raw_column = joined.get(numeric_col, pd.Series(index=joined.index, dtype=float))
            joined[numeric_col] = pd.to_numeric(raw_column, errors="coerce").fillna(-1.0)

        def finalize_quantum_attrs(row):
            # OSMnx can consolidate differently tagged source ways into a
            # list. Vertical flags therefore use any/most-specific semantics
            # rather than only the first value.
            layers = _all_numeric_values(row.get("layer"))
            if any(value < 0 for value in layers):
                osm_layer = int(min(layers))
            elif any(value > 0 for value in layers):
                osm_layer = int(max(layers))
            else:
                osm_layer = int(layers[0]) if layers else 0
            osm_tunnel = _any_flag(row.get("tunnel"))
            osm_bridge = _any_flag(row.get("bridge"))
            ref_confident = to_flag(row.get("ref_match_confident", 0))
            gs_subtype = str(row.get("ref_subtype", "")).upper() if ref_confident else ""
            
            # 2. Re-introduce the Hybrid Logic
            layer = osm_layer
            tunnel = 0
            bridge = 0

            # If OSM says Tunnel OR reference says Tunnel OR negative layer -> It's Subterranean
            if osm_tunnel or gs_subtype == 'TUNNEL' or osm_layer < 0:
                tunnel = 1
                layer = min(osm_layer, -1) # Force to negative layer
            
            # If reference says Bridge or OSM says Bridge OR positive layer -> It's Elevated
            elif gs_subtype == 'BRIDGE' or osm_bridge or osm_layer > 0:
                bridge = 1
                layer = max(osm_layer, 1) # Force to positive layer
                
            # 3. Process the remaining Quantum parameters
            gs_speed_raw = numeric_value(row.get("ref_speed"), np.nan) if ref_confident else np.nan
            gs_lanes_raw = numeric_value(row.get("ref_lane_count"), np.nan) if ref_confident else np.nan
            gs_speed = gs_speed_raw if np.isfinite(gs_speed_raw) and gs_speed_raw > 0 else None
            gs_lanes = gs_lanes_raw if np.isfinite(gs_lanes_raw) and gs_lanes_raw > 0 else None
            # OSMnx merges differently tagged ways into an unordered list, so
            # taking its first element made speed/lanes depend on the Python
            # hash seed.  Multi-valued OSM tags resolve to their minimum.
            osm_speeds = _all_numeric_values(row.get('maxspeed'))
            osm_lanes = _all_numeric_values(row.get('lanes'))
            maxspeed = gs_speed if gs_speed is not None else (min(osm_speeds) if osm_speeds else 60.0)
            lanes = int(gs_lanes) if gs_lanes is not None else (int(min(osm_lanes)) if osm_lanes else 1)
            # OSM determines graph topology/travel direction. A spatial
            # Reference match may audit it but must never silently reverse it.
            oneway = int(_any_flag(row.get("oneway")))
            
            # Highway category mapping (Parameter #7)
            h_type = str(row.get('highway')).lower()
            highway_idx = 1.0 if 'motorway' in h_type else 0.5
            
            return pd.Series([layer, maxspeed, lanes, tunnel, bridge, oneway, highway_idx])

        # Inject the seven structured parameters and retain match provenance.
        quantum_attrs = joined.apply(finalize_quantum_attrs, axis=1)

        edges = joined.copy()
        # Delete conflicting columns that may be strings from OSM
        cols_to_replace = ['layer', 'maxspeed', 'lanes', 'tunnel', 'bridge', 'oneway', 'highway_idx']
        for col in cols_to_replace:
            if col in edges.columns:
                del edges[col]
        
        # Now assign the new numeric values
        edges['layer'] = quantum_attrs.iloc[:, 0].values
        edges['maxspeed'] = quantum_attrs.iloc[:, 1].values
        edges['lanes'] = quantum_attrs.iloc[:, 2].values
        edges['tunnel'] = quantum_attrs.iloc[:, 3].values
        edges['bridge'] = quantum_attrs.iloc[:, 4].values
        # Preserve OSMnx's boolean type so its GraphML loader does not receive
        # an invalid boolean literal such as "1.0".
        edges['oneway'] = quantum_attrs.iloc[:, 5].map(to_flag).astype(bool).values
        edges['highway_idx'] = quantum_attrs.iloc[:, 6].values
        
        # Ensure length and bearing are numeric (Parameters #1 and #9)
        edges['length'] = edges['length'].astype(float)
        edges['bearing'] = edges['bearing'].astype(float)

        G = ox.graph_from_gdfs(nodes, edges)
        G, injected_count = inject_missing_reference_tunnels(
            G, geo_df, lat, lon, metric_crs
        )
        if injected_count:
            log(
                f"  > Injected {injected_count} directed reference tunnel/portal-approach "
                "edges missing from OSM"
            )
    else:
        log(f"  > Reference file not found for state '{state_abbr}' in {REFERENCE_ROADS_PATH}. Saving OSM-only fused graph.")

    # Merged OSM tags (name, ref, osmid, ...) are unordered lists; sort them
    # so the saved GraphML is identical across runs and hash seeds.
    for _u, _v, data in G.edges(data=True):
        for attr, value in data.items():
            if isinstance(value, list):
                data[attr] = sorted(value, key=str)

    # The licence and attribution notices travel inside the network file.
    G.graph.update({
        "license": DATA_LICENSE,
        "license_url": DATA_LICENSE_URL,
        "attribution": DATA_ATTRIBUTION,
        "modifications": DATA_MODIFICATIONS,
        "disclaimer": DATA_DISCLAIMER,
    })

    ensure_project_dirs()
    output_path = unified_graph_path(name)
    ox.save_graphml(G, filepath=str(output_path))
    log(f"Generated Quantum-Ready GraphML: {output_path}")
    return str(output_path)

def run_verification_tests(filename):
    """Print a short summary of layers, tunnels, bridges and connectivity for a saved graph."""
    log(f"--- Running Validation for {filename} ---")
    G = nx.read_graphml(filename)
    edges = [d for u, v, d in G.edges(data=True)]
    df = pd.DataFrame(edges)

    # Test 1: Subterranean Integrity
    if 'layer' in df.columns:
        layers = pd.to_numeric(df['layer'], errors='coerce').fillna(0)
        neg_layers = (layers < 0).sum()
        pos_layers = (layers > 0).sum()
        log(f"  > Sub-surface (Tunnels) Found: {neg_layers}")
        log(f"  > Elevated (Bridges) Found: {pos_layers}")
    else:
        log("  > Sub-surface (Tunnels) Found: 0 (No 'layer' column present)")

    # Test 2: Metric Consistency
    if 'length' in df.columns:
        avg_len = pd.to_numeric(df['length'], errors='coerce').mean()
        if avg_len > 0.01: 
            log(f"  > Metric Check: PASS (Avg length {avg_len:.2f}m)")
        else:
            log(f"  > Metric Check: FAIL (Detected Lat/Lon units)")
    else:
        log("  > Metric Check: FAIL (No 'length' column present)")

    # Test 3: Reference Integrity Tagging
    if 'source_ref' in df.columns:
        verified = df['source_ref'].map(to_flag).sum()
        confident = df.get('ref_match_confident', pd.Series(dtype=object)).map(to_flag).sum()
        ambiguous = df.get('ref_match_ambiguous', pd.Series(dtype=object)).map(to_flag).sum()
        log(f"  > Reference Tagging: PASS ({verified} matched, {confident} confident, {ambiguous} ambiguous)")
    else:
        log("  > Reference Tagging: FAIL")


def _process_case(args):
    """Worker for one (name, info) pair: build the graph and run the quick checks."""
    name, info = args
    fname = reproduce_unified_graph(name, info['coords'][0], info['coords'][1], info['state'])
    run_verification_tests(fname)
    return name, fname

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reproduce unified graphs for all hard-case locations.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=default_max_workers(),
        help="Maximum parallel workers to use (default: all CPU cores).",
    )
    args = parser.parse_args()

    items = list(hard_case_locations.items())
    max_workers = max(1, min(int(args.max_workers), len(items)))

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_process_case, item) for item in items]
        for fut in as_completed(futures):
            name, fname = fut.result()
            log(f"Completed {name}: {fname}")
