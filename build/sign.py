#!/usr/bin/env python3
"""SIGNING_KEY=<base64 seed> python sign.py <folder> [--previous <old index.json>]

With --previous, regions without a new report keep their files from the old index.
Those files must already be in <folder>."""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REGIONS = json.loads((Path(__file__).resolve().parent.parent / "regions.json").read_text(encoding="utf-8"))
FORMAT_VERSION = 1
MIN_APP_VERSION = 0


def main():
    folder = Path(sys.argv[1])
    previous = None
    if "--previous" in sys.argv:
        old = Path(sys.argv[sys.argv.index("--previous") + 1])
        if old.exists():
            previous = json.loads(old.read_text(encoding="utf-8"))
    seed = os.environ.get("SIGNING_KEY", "").strip()
    if not seed:
        sys.exit("SIGNING_KEY is not set")
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed))

    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.report.json"))]
    if not reports:
        sys.exit("no reports found")

    files = []
    for report in reports:
        for layer, entry in (("limits", report["limits"]), ("cameras", report["cameras_file"])):
            path = folder / entry["file"]
            if path.stat().st_size != entry["bytes"]:
                sys.exit(f"wrong size: {path.name}")
            files.append({
                "region": report["region"],
                "layer": layer,
                "file": entry["file"],
                "bytes": entry["bytes"],
                "sha256": entry["sha256"],
                "schema_version": report["schema_version"],
                "data_date": report["data_date"],
                "min_app_version": MIN_APP_VERSION,
            })

    regions = [
        {
            "code": report["region"],
            "name": REGIONS[report["region"]]["name"],
            "country": REGIONS[report["region"]]["country"],
            "outline": report["outline"],
        }
        for report in reports
    ]
    built = {r["region"] for r in reports}
    carried = {}
    if previous:
        for region in previous["regions"]:
            code = region["code"]
            if code in built or code not in REGIONS:
                continue
            old_files = [f for f in previous["files"] if f["region"] == code]
            for entry in old_files:
                path = folder / entry["file"]
                if not path.exists() or path.stat().st_size != entry["bytes"]:
                    sys.exit(f"old file missing: {entry['file']}")
            regions.append(region)
            files.extend(old_files)
            carried[code] = min(f["data_date"] for f in old_files)
    missing = sorted(set(REGIONS) - built - set(carried))
    if missing:
        print(f"without package: {', '.join(missing)}")

    index = {
        "format_version": FORMAT_VERSION,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_date": min(f["data_date"] for f in files),
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL 1.0",
        "regions": regions,
        "files": files,
    }
    data = json.dumps(index, indent=1, ensure_ascii=False).encode("utf-8")
    (folder / "index.json").write_bytes(data)
    (folder / "index.json.sig").write_bytes(key.sign(data))

    status = {
        "created": index["created"],
        "data_date": index["data_date"],
        "regions": {
            r["region"]: {
                "roads": r["roads"]["ways"],
                "cameras": r["cameras"]["cameras"],
                "limits_bytes": r["limits"]["bytes"],
                "seconds": r["seconds"],
            }
            for r in reports
        },
        "carried_over": carried,
    }
    (folder / "build-status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
    print(f"signed {len(files)} files, data {index['data_date']}, carried over {len(carried)}")


if __name__ == "__main__":
    main()
