#!/usr/bin/env python3
"""SIGNING_KEY=<base64 seed> python sign.py <folder>"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

FORMAT_VERSION = 1
MIN_APP_VERSION = 0


def main():
    folder = Path(sys.argv[1])
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

    index = {
        "format_version": FORMAT_VERSION,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_date": min(r["data_date"] for r in reports),
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL 1.0",
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
    }
    (folder / "build-status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
    print(f"signed {len(files)} files, data {index['data_date']}")


if __name__ == "__main__":
    main()
