#!/usr/bin/env python3
"""Builds data packages for one region from a Geofabrik extract.

    python build.py DE-BY [--input file.osm.pbf] [--out out] [--work work]

Writes <region>.limits.sqlite.zst, <region>.cameras.sqlite.zst and <region>.report.json.
sign.py then creates the signed index.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import struct
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy
import osmium
import shapely
import zstandard

GEOFABRIK = "https://download.geofabrik.de/"
USER_AGENT = "bumpcar-osm-packages"
SCHEMA_VERSION = 3
TOLERANCE_M = 5.0
OUTLINE_TOLERANCE_M = 2000.0
CELL_E6 = 20_000  # 0.02 degrees
COORD_STEP = 10  # stored in 1e-5 degrees, about 1 m
ZSTD_LEVEL = 19

# Minimum counts per region: a build below them is broken and stops instead of being published.
REGIONS = json.loads((Path(__file__).resolve().parent.parent / "regions.json").read_text(encoding="utf-8"))

HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}

ZONES = {
    "IT:urban": 50, "IT:rural": 90, "IT:trunk": 110, "IT:motorway": 130,
    "AT:urban": 50, "AT:rural": 100, "AT:motorway": 130,
    "DE:urban": 50, "DE:rural": 100,
    "CH:urban": 50, "CH:rural": 80, "CH:trunk": 100, "CH:motorway": 120,
}
# Limit bytes per direction: 0 unknown, 5..150 tagged km/h, 151..254 default km/h as 151 + km/h / 2, 255 no limit.
UNLIMITED = 255
DEFAULT_BASE = 151

# km/h by country, side and road class where no limit is tagged. Other classes stay unknown.
DEFAULTS = {
    "DE": {
        "urban": {"primary": 50, "secondary": 50, "tertiary": 50, "residential": 30},
        "rural": {"primary": 100, "secondary": 100, "tertiary": 100},
    },
}
SETTLEMENT = {"residential", "commercial", "retail", "industrial"}
SIDEWALK_KEYS = ("sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right")
SIDEWALK_VALUES = {"both", "left", "right", "separate", "yes"}
ALWAYS_URBAN = {"residential", "living_street"}
SIGNED_KEYS = ("maxspeed", "maxspeed:forward", "maxspeed:backward", "maxspeed:conditional")
NUMBER = re.compile(r"(\d{1,3})\s*(mph|km/h|kmh)?")
ZONE_NUMBER = re.compile(r"[A-Z]{2}:(?:zone:?)?(\d{1,3})")
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def parse_limit(value):
    """km/h, or None if it is not one clear number."""
    if value is None:
        return None
    v = value.strip()
    if v in ZONES:
        return ZONES[v]
    m = ZONE_NUMBER.fullmatch(v)
    if m:
        n = int(m.group(1))
        return n if 5 <= n <= 150 else None
    m = NUMBER.fullmatch(v)
    if not m:
        return None
    n = int(m.group(1))
    kmh = math.floor(n * 1.609344 + 0.5) if m.group(2) == "mph" else n
    return kmh if 5 <= kmh <= 150 else None


def direction_limit(value):
    return UNLIMITED if value is not None and value.strip() == "none" else parse_limit(value)


def road_limits(tags):
    """(forward, backward): km/h, UNLIMITED or None. Directions win; zone tags are the fallback."""
    both = direction_limit(tags.get("maxspeed"))
    if both is None:
        for key in ("maxspeed:type", "source:maxspeed", "zone:maxspeed"):
            both = parse_limit(tags.get(key))
            if both is not None:
                break
    forward = direction_limit(tags.get("maxspeed:forward"))
    backward = direction_limit(tags.get("maxspeed:backward"))
    return (forward if forward is not None else both, backward if backward is not None else both)


def default_byte(kmh):
    return DEFAULT_BASE + (kmh + 1) // 2


def side_from_tags(highway, tags):
    """True urban, False rural, None when only the settlement areas can tell."""
    zone = tags.get("zone:traffic", "")
    if zone.endswith("urban"):
        return True
    if zone.endswith(("rural", "motorway", "motorroad")):
        return False
    if highway in ALWAYS_URBAN or tags.get("lit") == "yes":
        return True
    return True if any(tags.get(k) in SIDEWALK_VALUES for k in SIDEWALK_KEYS) else None


def default_for(rules, highway, urban, signed):
    """The default byte of a road, 0 without one. A value that could not be read is no gap to fill."""
    if signed:
        return 0
    kmh = rules.get("urban" if urban else "rural", {}).get(highway)
    return default_byte(kmh) if kmh else 0


def camera_limit(value):
    """km/h from "50", "50 km/h", "30 mph"; zone values give None."""
    if value is None:
        return None
    v = value.strip().lower()
    m = re.match(r"\d+", v)
    if not m:
        return None
    n = int(m.group())
    return int(n * 1.609) if v.endswith("mph") else n


def camera_direction(value):
    """Degrees or compass points; "forward" and "backward" are ignored."""
    if value is None:
        return None
    v = value.strip().upper()
    try:
        return float(v) % 360
    except ValueError:
        return COMPASS.index(v) * 22.5 if v in COMPASS else None


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = math.radians(lon2 - lon1)
    y = math.sin(d) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(d)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def length_km(points):
    total = 0.0
    for i in range(0, len(points) - 2, 2):
        lat = math.radians((points[i] + points[i + 2]) / 2e6)
        dy = (points[i + 2] - points[i]) / 1e6
        dx = (points[i + 3] - points[i + 1]) / 1e6 * math.cos(lat)
        total += math.hypot(dx, dy) * 111.195
    return total


def new_database(path, schema, meta):
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(schema)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    return db


ROAD_SCHEMA = """
CREATE TABLE cell (id INTEGER PRIMARY KEY NOT NULL, data BLOB NOT NULL);
"""

CAMERA_SCHEMA = """
CREATE TABLE speed_camera (
    id TEXT PRIMARY KEY NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, maxspeed INTEGER,
    direction REAL, source TEXT NOT NULL, kind TEXT NOT NULL, dormant INTEGER NOT NULL,
    hidden INTEGER NOT NULL
);
CREATE INDEX index_speed_camera_lat_lon ON speed_camera (lat, lon);
CREATE TABLE speed_section (
    id TEXT PRIMARY KEY NOT NULL, startLat REAL NOT NULL, startLon REAL NOT NULL, endLat REAL NOT NULL,
    endLon REAL NOT NULL, maxspeed INTEGER
);
CREATE INDEX index_speed_section_startLat_startLon ON speed_section (startLat, startLon);
"""


def simplify(points, tolerance_m=TOLERANCE_M):
    """Douglas-Peucker on (lat, lon) in micro degrees."""
    if len(points) < 3:
        return points
    metres = 0.111195
    cos_lat = math.cos(math.radians(points[0][0] / 1e6))
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    limit = tolerance_m * tolerance_m
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first][1] * cos_lat * metres, points[first][0] * metres
        dx, dy = points[last][1] * cos_lat * metres - ax, points[last][0] * metres - ay
        length = dx * dx + dy * dy
        worst, at = -1.0, -1
        for i in range(first + 1, last):
            px, py = points[i][1] * cos_lat * metres - ax, points[i][0] * metres - ay
            u = 0.0 if length == 0 else max(0.0, min(1.0, (px * dx + py * dy) / length))
            ex, ey = px - u * dx, py - u * dy
            error = ex * ex + ey * ey
            if error > worst:
                worst, at = error, i
        if worst > limit:
            keep[at] = True
            stack += [(first, at), (at, last)]
    return [point for point, kept in zip(points, keep) if kept]


def cell_id(lat_index, lon_index):
    return (lat_index << 20) | (lon_index & 0xFFFFF)


def cut_into_cells(points):
    """Pieces of a road per grid cell: {(lat index, lon index): [[point, ...], ...]}."""
    cells = defaultdict(list)
    current = {}
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        for la in range(min(a[0], b[0]) // CELL_E6, max(a[0], b[0]) // CELL_E6 + 1):
            for lo in range(min(a[1], b[1]) // CELL_E6, max(a[1], b[1]) // CELL_E6 + 1):
                piece = current.get((la, lo))
                if piece is not None and piece[1] == i:
                    piece[0].append(b)
                    piece[1] = i + 1
                else:
                    piece = [[a, b], i + 1]
                    current[(la, lo)] = piece
                    cells[(la, lo)].append(piece[0])
    return cells


def write_varint(value, out):
    while value >= 128:
        out.append(value & 127 | 128)
        value >>= 7
    out.append(value)


def encode_cell(lat_index, lon_index, pieces):
    """pieces: (forward byte or None, backward byte or None, points). Differences from the cell corner."""
    out = bytearray()
    write_varint(len(pieces), out)
    origin_lat = lat_index * CELL_E6 // COORD_STEP
    origin_lon = lon_index * CELL_E6 // COORD_STEP
    for forward, backward, points in pieces:
        out.append(forward or 0)
        out.append(backward or 0)
        write_varint(len(points), out)
        last_lat, last_lon = origin_lat, origin_lon
        for lat, lon in points:
            lat, lon = round(lat / COORD_STEP), round(lon / COORD_STEP)
            for delta in (lat - last_lat, lon - last_lon):
                write_varint((delta << 1) ^ (delta >> 63), out)
            last_lat, last_lon = lat, lon
    return bytes(out)


def inside_settlement(polygons, middles):
    """Per way index with middles: does at least half of its middles lie in a settlement area?"""
    if not middles or not polygons:
        return {}
    owners, xs, ys = [], [], []
    for owner, points in middles.items():
        for lat, lon in points:
            owners.append(owner)
            xs.append(lon)
            ys.append(lat)
    points = shapely.points(numpy.array(xs), numpy.array(ys))
    hits = shapely.STRtree(shapely.from_wkb(polygons)).query(points, predicate="within")[0]
    inside = numpy.zeros(len(points), dtype=bool)
    inside[hits] = True
    count = defaultdict(int)
    for owner, is_inside in zip(owners, inside):
        if is_inside:
            count[owner] += 1
    return {owner: count[owner] * 2 >= len(way) for owner, way in middles.items()}


def empty_class():
    return {"ways": 0, "km": 0.0, "ways_with_limit": 0, "km_with_limit": 0.0, "km_unlimited": 0.0, "km_default": 0.0}


class Roads:
    def __init__(self):
        self.cells = defaultdict(list)
        self.points_total = 0
        self.skipped = 0
        self.by_class = defaultdict(empty_class)


def read_roads(pbf, country):
    """All drivable roads of an extract, cut into cells, with their limit bytes."""
    rules = DEFAULTS.get(country, {})
    classes = set(rules.get("urban", {})) | set(rules.get("rural", {}))
    roads = Roads()
    polygons, ways, middles = [], [], {}
    wkb = osmium.geom.WKBFactory()
    for o in osmium.FileProcessor(str(pbf)).with_locations().with_areas(osmium.filter.KeyFilter("landuse")):
        if o.is_area():
            if o.tags.get("landuse") in SETTLEMENT:
                try:
                    polygons.append(wkb.create_multipolygon(o))
                except RuntimeError:
                    pass
            continue
        if not o.is_way() or o.tags.get("highway") not in HIGHWAYS:
            continue
        highway = o.tags.get("highway")
        points = [(round(n.location.lat * 1e6), round(n.location.lon * 1e6)) for n in o.nodes if n.location.valid()]
        if len(points) < 2:
            roads.skipped += 1
            continue
        tags = {t.k: t.v for t in o.tags}
        forward, backward = road_limits(tags)
        signed = any(k in tags for k in SIGNED_KEYS)
        side = None
        if highway in classes and not signed and (forward is None or backward is None):
            side = side_from_tags(highway, tags)
            if side is None:
                middles[len(ways)] = [((a[0] + b[0]) / 2e6, (a[1] + b[1]) / 2e6) for a, b in zip(points, points[1:])]
        km = length_km([value for point in points for value in point])
        ways.append((highway, forward, backward, signed, side, km, simplify(points)))

    urban_by_area = inside_settlement(polygons, middles)
    del polygons, middles
    for i, (highway, forward, backward, signed, side, km, simple) in enumerate(ways):
        urban = urban_by_area.get(i, False) if side is None else side
        fill = default_for(rules, highway, urban, signed)
        forward_byte = forward if forward is not None else fill
        backward_byte = backward if backward is not None else fill
        roads.points_total += len(simple)
        for key, pieces in cut_into_cells(simple).items():
            roads.cells[key].extend((forward_byte, backward_byte, piece) for piece in pieces)
        c = roads.by_class[highway]
        c["ways"] += 1
        c["km"] += km
        if any(v is not None and v != UNLIMITED for v in (forward, backward)):
            c["ways_with_limit"] += 1
            c["km_with_limit"] += km
        elif UNLIMITED in (forward, backward):
            c["km_unlimited"] += km
        elif fill:
            c["km_default"] += km
    return roads


class RelationHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.relations = []
        self.wanted = set()

    def relation(self, r):
        if r.tags.get("type") != "enforcement":
            return
        members = [(m.ref, m.role) for m in r.members if m.type == "n"]
        self.relations.append((r.id, {t.k: t.v for t in r.tags}, members))
        self.wanted.update(ref for ref, _ in members)


class NodeHandler(osmium.SimpleHandler):
    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted
        self.nodes = {}

    def node(self, n):
        if (n.id in self.wanted or n.tags.get("highway") == "speed_camera") and n.location.valid():
            self.nodes[n.id] = (n.location.lat, n.location.lon, {t.k: t.v for t in n.tags})


def build_cameras(relations, nodes):
    """Cameras and average speed sections from nodes and enforcement relations."""
    sections, section_devices, rel_limit, rel_direction = [], set(), {}, {}
    for rel_id, tags, members in relations:
        def role(name):
            return [ref for ref, r in members if r == name and ref in nodes]

        kind, limit = tags.get("enforcement"), camera_limit(tags.get("maxspeed"))
        devices = role("device")
        from_ids, to_ids = role("from"), role("to")
        first_from = from_ids[0] if from_ids else None
        first_to = to_ids[0] if to_ids else None
        if kind == "average_speed":
            start = first_from if first_from is not None else (devices[0] if devices else None)
            if start is None:
                continue
            end = first_to
            if end is None and devices and devices[-1] != start:
                end = devices[-1]
            if end is None:
                continue
            section_limit = limit
            if section_limit is None:
                section_limit = next((v for v in (camera_limit(nodes[d][2].get("maxspeed")) for d in devices) if v is not None), None)
            sections.append((f"osm:{rel_id}", nodes[start][0], nodes[start][1], nodes[end][0], nodes[end][1], section_limit))
            section_devices.update(devices)
            section_devices.update(x for x in (first_from, first_to) if x is not None)
        elif kind == "maxspeed":
            direction = None
            if first_from is not None and first_to is not None:
                a, b = nodes[first_from], nodes[first_to]
                direction = bearing(a[0], a[1], b[0], b[1])
            for d in devices:
                if limit is not None:
                    rel_limit[d] = limit
                if direction is not None:
                    rel_direction[d] = direction

    cameras = []
    for node_id, (lat, lon, tags) in nodes.items():
        if tags.get("highway") != "speed_camera" or node_id in section_devices:
            continue
        limit = camera_limit(tags.get("maxspeed"))
        direction = camera_direction(tags.get("direction"))
        cameras.append((f"osm:{node_id}", lat, lon,
                        limit if limit is not None else rel_limit.get(node_id),
                        direction if direction is not None else rel_direction.get(node_id),
                        "OSM", "FIXED", 0, 0))
    return cameras, sections


def parse_outline(text, tolerance_m=OUTLINE_TOLERANCE_M):
    """Geofabrik .poly to simplified rings of flat lat, lon values. Holes are left out."""
    rings, ring = [], None
    for line in text.splitlines()[1:]:
        line = line.strip()
        if ring is None:
            if line and line != "END":
                ring = [] if not line.startswith("!") else False
        elif line == "END":
            if ring:
                points = simplify(ring, tolerance_m)
                rings.append([value for lat, lon in points for value in (lat / 1e6, lon / 1e6)])
            ring = None
        elif ring is not False:
            lon, lat = line.split()[:2]
            ring.append((round(float(lat) * 1e6), round(float(lon) * 1e6)))
    return [[round(value, 3) for value in ring] for ring in rings]


def fetch_outline(path):
    url = GEOFABRIK + path.replace("-latest.osm.pbf", ".poly")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return parse_outline(response.read().decode("utf-8"))


def compress(path):
    """Writes path.zst and returns its description."""
    data = path.read_bytes()
    packed = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(data)
    target = path.with_name(path.name + ".zst")
    target.write_bytes(packed)
    return {"file": target.name, "bytes": len(packed), "sqlite_bytes": len(data), "sha256": hashlib.sha256(packed).hexdigest()}


def download(url, target):
    if target.exists():
        print(f"using {target}")
        return
    print(f"downloading {url}")
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response, partial.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    partial.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("region", choices=sorted(REGIONS))
    parser.add_argument("--input", type=Path, help="local .osm.pbf instead of downloading")
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--work", type=Path, default=Path("work"))
    args = parser.parse_args()

    region = REGIONS[args.region]
    args.out.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    report = {"region": args.region, "schema_version": SCHEMA_VERSION, "zstd_level": ZSTD_LEVEL, "seconds": {}}

    def step(name, started):
        report["seconds"][name] = round(time.monotonic() - started, 1)
        print(f"{name}: {report['seconds'][name]} s", flush=True)

    t = time.monotonic()
    pbf = args.input
    if pbf is None:
        pbf = args.work / f"{args.region}.osm.pbf"
        download(GEOFABRIK + region["path"], pbf)
    step("download", t)
    report["source_bytes"] = pbf.stat().st_size
    report["outline"] = fetch_outline(region["path"])

    header = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    data_date = header.header().get("osmosis_replication_timestamp")
    header.close()
    report["data_date"] = data_date
    meta = {"region": args.region, "schema_version": str(SCHEMA_VERSION), "data_date": data_date,
            "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors"}

    t = time.monotonic()
    road_path = args.out / f"{args.region}.limits.sqlite"
    db = new_database(road_path, ROAD_SCHEMA, {**meta, "layer": "limits"})
    roads = read_roads(pbf, region["country"])
    db.executemany("INSERT INTO cell VALUES (?, ?)", ((cell_id(la, lo), encode_cell(la, lo, pieces)) for (la, lo), pieces in roads.cells.items()))
    db.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("cell_e6", str(CELL_E6)),
        ("coord_step", str(COORD_STEP)),
        ("road_count", str(sum(c["ways"] for c in roads.by_class.values()))),
        ("road_count_with_limit", str(sum(c["ways_with_limit"] for c in roads.by_class.values()))),
    ])
    db.commit()
    db.execute("VACUUM")
    db.close()
    step("roads", t)

    t = time.monotonic()
    relations = RelationHandler()
    relations.apply_file(str(pbf))
    nodes = NodeHandler(relations.wanted)
    nodes.apply_file(str(pbf))
    cameras, sections = build_cameras(relations.relations, nodes.nodes)
    camera_path = args.out / f"{args.region}.cameras.sqlite"
    db = new_database(camera_path, CAMERA_SCHEMA, {**meta, "layer": "cameras"})
    db.executemany("INSERT INTO speed_camera VALUES (?,?,?,?,?,?,?,?,?)", cameras)
    db.executemany("INSERT INTO speed_section VALUES (?,?,?,?,?,?)", sections)
    db.commit()
    db.execute("VACUUM")
    db.close()
    step("cameras", t)

    road_count = sum(c["ways"] for c in roads.by_class.values())
    report["roads"] = {
        "ways": road_count,
        "points": roads.points_total,
        "skipped_no_geometry": roads.skipped,
        "ways_with_limit": sum(c["ways_with_limit"] for c in roads.by_class.values()),
        "km": round(sum(c["km"] for c in roads.by_class.values())),
        "km_with_limit": round(sum(c["km_with_limit"] for c in roads.by_class.values())),
        "km_unlimited": round(sum(c["km_unlimited"] for c in roads.by_class.values())),
        "km_default": round(sum(c["km_default"] for c in roads.by_class.values())),
        "by_class": {k: {"ways": v["ways"], "km": round(v["km"]), "ways_with_limit": v["ways_with_limit"], "km_with_limit": round(v["km_with_limit"]),
                         "km_unlimited": round(v["km_unlimited"]), "km_default": round(v["km_default"])}
                     for k, v in sorted(roads.by_class.items())},
    }
    report["cameras"] = {"cameras": len(cameras), "sections": len(sections), "enforcement_relations": len(relations.relations)}

    if road_count < region["min_roads"] or len(cameras) < region["min_cameras"]:
        sys.exit(f"self test failed: {road_count} roads, {len(cameras)} cameras")
    if region["country"] in DEFAULTS and not report["roads"]["km_default"]:
        sys.exit("self test failed: no default limits")

    t = time.monotonic()
    report["limits"] = compress(road_path)
    step("compress_limits", t)
    report["cameras_file"] = compress(camera_path)
    road_path.unlink()
    camera_path.unlink()

    report_path = args.out / f"{args.region}.report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
