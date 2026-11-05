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

import osmium
import zstandard

GEOFABRIK = "https://download.geofabrik.de/"
USER_AGENT = "bumpcar-osm-packages"
SCHEMA_VERSION = 1
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


def road_limits(tags):
    """(forward km/h, backward km/h). Directions win; zone tags are the fallback."""
    both = None
    for key in ("maxspeed", "maxspeed:type", "source:maxspeed", "zone:maxspeed"):
        both = parse_limit(tags.get(key))
        if both is not None:
            break
    forward = parse_limit(tags.get("maxspeed:forward"))
    backward = parse_limit(tags.get("maxspeed:backward"))
    return (forward if forward is not None else both, backward if backward is not None else both)


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
CREATE TABLE road (
    wayId INTEGER PRIMARY KEY NOT NULL, minLat INTEGER NOT NULL, maxLat INTEGER NOT NULL,
    minLon INTEGER NOT NULL, maxLon INTEGER NOT NULL, forwardKmh INTEGER, backwardKmh INTEGER,
    points BLOB NOT NULL
);
CREATE INDEX index_road_minLat ON road (minLat);
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


class RoadHandler(osmium.SimpleHandler):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.batch = []
        self.points_total = 0
        self.skipped = 0
        self.by_class = defaultdict(lambda: {"ways": 0, "km": 0.0, "ways_with_limit": 0, "km_with_limit": 0.0})

    def way(self, w):
        highway = w.tags.get("highway")
        if highway not in HIGHWAYS:
            return
        points = []
        for n in w.nodes:
            if n.location.valid():
                points.append(round(n.location.lat * 1e6))
                points.append(round(n.location.lon * 1e6))
        if len(points) < 4:
            self.skipped += 1
            return
        tags = {t.k: t.v for t in w.tags if "maxspeed" in t.k}
        forward, backward = road_limits(tags)
        lats, lons = points[0::2], points[1::2]
        self.batch.append((w.id, min(lats), max(lats), min(lons), max(lons), forward, backward,
                           struct.pack(f"<{len(points)}i", *points)))
        self.points_total += len(points) // 2
        km = length_km(points)
        c = self.by_class[highway]
        c["ways"] += 1
        c["km"] += km
        if forward is not None or backward is not None:
            c["ways_with_limit"] += 1
            c["km_with_limit"] += km
        if len(self.batch) >= 5000:
            self.flush()

    def flush(self):
        self.db.executemany("INSERT INTO road VALUES (?,?,?,?,?,?,?,?)", self.batch)
        self.batch.clear()


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

    header = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    data_date = header.header().get("osmosis_replication_timestamp")
    header.close()
    report["data_date"] = data_date
    meta = {"region": args.region, "schema_version": str(SCHEMA_VERSION), "data_date": data_date,
            "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors"}

    t = time.monotonic()
    road_path = args.out / f"{args.region}.limits.sqlite"
    db = new_database(road_path, ROAD_SCHEMA, {**meta, "layer": "limits"})
    roads = RoadHandler(db)
    roads.apply_file(str(pbf), locations=True, idx="flex_mem")
    roads.flush()
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
        "by_class": {k: {"ways": v["ways"], "km": round(v["km"]), "ways_with_limit": v["ways_with_limit"], "km_with_limit": round(v["km_with_limit"])}
                     for k, v in sorted(roads.by_class.items())},
    }
    report["cameras"] = {"cameras": len(cameras), "sections": len(sections), "enforcement_relations": len(relations.relations)}

    if road_count < region["min_roads"] or len(cameras) < region["min_cameras"]:
        sys.exit(f"self test failed: {road_count} roads, {len(cameras)} cameras")

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
