#!/usr/bin/env python3
"""python check.py <folder> [--previous URL] [--accept all|A,B]

Compares each new package with the one of the last release. A package that changed too much, or a
region whose build failed, keeps the old file. Writes held.json with what was kept.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import urllib.error
from pathlib import Path

import zstandard

import recall
from verify import fetch_signed_index, read

REGIONS = json.loads((Path(__file__).resolve().parent.parent / "regions.json").read_text(encoding="utf-8"))
LATEST = "https://github.com/bumpcar/osm-data/releases/latest/download"
HELD = "held.json"

MAX_SIZE_CHANGE = 0.10
MAX_ROADS_CHANGE = 0.08
MAX_SHARE_POINTS = 3.0
MAX_CAMERA_CHANGE = 0.20
CAMERA_SLACK = 10


def limit_bucket(value):
    if value == 0:
        return "none"
    if value == 255:
        return "unlimited"
    if value > 150:
        return f"default {(value - 151) * 2}"
    return str(value)


def cell_counts(data, counts):
    """Adds the limit bytes of one cell. Same layout as encode_cell in build.py."""
    at = 0

    def varint():
        nonlocal at
        result = shift = 0
        while True:
            byte = data[at]
            at += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
            shift += 7

    for _ in range(varint()):
        for value in data[at:at + 2]:
            bucket = limit_bucket(value)
            counts[bucket] = counts.get(bucket, 0) + 1
        at += 2
        for _ in range(varint() * 2):
            while data[at] >= 128:
                at += 1
            at += 1


def unpack(path, target):
    with open(path, "rb") as packed, open(target, "wb") as out:
        zstandard.ZstdDecompressor().copy_stream(packed, out)


def limits_stats(path):
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "p.sqlite"
        unpack(path, database)
        db = sqlite3.connect(database)
        counts = {}
        for (data,) in db.execute("SELECT data FROM cell"):
            cell_counts(data, counts)
        db.close()
    return {"bytes": Path(path).stat().st_size, "directions": sum(counts.values()), "limits": counts}


def cameras_stats(path):
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "p.sqlite"
        unpack(path, database)
        db = sqlite3.connect(database)
        cameras = db.execute("SELECT COUNT(*) FROM speed_camera").fetchone()[0]
        sections = db.execute("SELECT COUNT(*) FROM speed_section").fetchone()[0]
        db.close()
    return {"cameras": cameras, "sections": sections}


def hazards_stats(path):
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "p.sqlite"
        unpack(path, database)
        db = sqlite3.connect(database)
        counts = dict(db.execute("SELECT kind, COUNT(*) FROM hazard GROUP BY kind").fetchall())
        db.close()
    return counts


def clearance_stats(path):
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "p.sqlite"
        unpack(path, database)
        db = sqlite3.connect(database)
        counts = {name: db.execute(f"SELECT COUNT({name}) FROM clearance").fetchone()[0] for name in ("maxheight", "maxweight", "maxwidth")}
        db.close()
    return counts


def change(new, old):
    return abs(new - old) / old if old else (1.0 if new else 0.0)


def compare_limits(new, old):
    """Empty when the new package looks like the old one, else the reasons."""
    reasons = []
    if change(new["bytes"], old["bytes"]) > MAX_SIZE_CHANGE:
        reasons.append(f"size {old['bytes']} -> {new['bytes']}")
    if change(new["directions"], old["directions"]) > MAX_ROADS_CHANGE:
        reasons.append(f"roads {old['directions']} -> {new['directions']}")
    for bucket in sorted(set(new["limits"]) | set(old["limits"])):
        before = 100 * old["limits"].get(bucket, 0) / max(old["directions"], 1)
        after = 100 * new["limits"].get(bucket, 0) / max(new["directions"], 1)
        if abs(after - before) > MAX_SHARE_POINTS:
            reasons.append(f"limit {bucket} {before:.1f} % -> {after:.1f} %")
    return reasons


def compare_cameras(new, old):
    reasons = []
    for name in ("cameras", "sections"):
        difference = abs(new[name] - old[name])
        if difference > CAMERA_SLACK and change(new[name], old[name]) > MAX_CAMERA_CHANGE:
            reasons.append(f"{name} {old[name]} -> {new[name]}")
    return reasons


def compare_counts(new, old):
    """Point layers: one reason per kind whose count moved a lot. Same rule as the cameras."""
    reasons = []
    for name in sorted(set(new) | set(old)):
        before, after = old.get(name, 0), new.get(name, 0)
        if abs(after - before) > CAMERA_SLACK and change(after, before) > MAX_CAMERA_CHANGE:
            reasons.append(f"{name} {before} -> {after}")
    return reasons


def parse_accept(text):
    """None: all regions."""
    text = (text or "").strip()
    if text == "all":
        return None
    return {part.strip() for part in text.split(",") if part.strip()}


LAYERS = (("limits", "limits", limits_stats, compare_limits), ("cameras", "cameras_file", cameras_stats, compare_cameras),
          ("hazards", "hazards_file", hazards_stats, compare_counts), ("clearance", "clearance_file", clearance_stats, compare_counts))
# Layers older releases do not have yet: a kept region goes out without them.
NEW_LAYERS = {"hazards", "clearance"}


def fetch_old(base, entry, folder):
    data = read(base, entry["file"])
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        sys.exit(f"bad checksum in the last release: {entry['file']}")
    (folder / entry["file"]).write_bytes(data)


def old_values(entry):
    return {key: entry[key] for key in ("file", "bytes", "sha256", "data_date", "schema_version")}


def check_built(folder, base, previous, recalls, reports, accept, held):
    """Puts the old file back for every package that changed too much."""
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        for region, (path, report) in reports.items():
            changed = False
            for layer, key, stats, compare in LAYERS:
                old = previous.get((region, layer))
                if old is None or key not in report or old["schema_version"] != report["schema_version"]:
                    continue
                # A recalled package is no measure: the repaired one is meant to differ from it.
                if recall.is_recalled(recalls, region, layer, old["data_date"]):
                    print(f"{region} {layer}: old package recalled, not compared")
                    continue
                fetch_old(base, old, temp)
                reasons = compare(stats(folder / report[key]["file"]), stats(temp / old["file"]))
                if not reasons:
                    continue
                if accept is None or region in accept:
                    print(f"{region} {layer}: accepted: {'; '.join(reasons)}")
                    continue
                print(f"{region} {layer}: HELD: {'; '.join(reasons)}")
                held[f"{region} {layer}"] = reasons
                (folder / report[key]["file"]).unlink()
                (folder / old["file"]).write_bytes((temp / old["file"]).read_bytes())
                report[key] = old_values(old)
                changed = True
            if changed:
                path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def keep_unbuilt(folder, base, index, previous, reports, held):
    """Regions without a new build go out with their old packages."""
    outlines = {r["code"]: r.get("outline", []) for r in index.get("regions", [])}
    for region in sorted({r for r, _ in previous} - set(reports)):
        if region not in REGIONS:
            continue
        report = {"region": region, "outline": outlines.get(region, []), "kept": True}
        for layer, key, _, _ in LAYERS:
            old = previous.get((region, layer))
            if old is None and layer in NEW_LAYERS:
                continue
            if old is None:
                sys.exit(f"{region}: {layer} missing in the last release")
            fetch_old(base, old, folder)
            report[key] = old_values(old)
        (folder / f"{region}.report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        held[region] = ["no new build"]
        print(f"{region}: HELD: no new build")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--previous", default=LATEST)
    parser.add_argument("--accept", default="")
    args = parser.parse_args()

    reports = {}
    for path in sorted(args.folder.glob("*.report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        reports[report["region"]] = (path, report)
    if not reports:
        sys.exit("no region was built")

    try:
        index = fetch_signed_index(args.previous)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        print("no last release")
        return
    recalls = recall.fetch_current(args.previous)
    previous = {(e["region"], e["layer"]): e for e in index["files"]}
    held = {}
    check_built(args.folder, args.previous, previous, recalls, reports, parse_accept(args.accept), held)
    keep_unbuilt(args.folder, args.previous, index, previous, reports, held)
    (args.folder / HELD).write_text(json.dumps(held, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
