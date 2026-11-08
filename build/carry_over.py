#!/usr/bin/env python3
"""python carry_over.py <folder>

Regions that did not build keep last month's files: load the old index and those files
from the latest release into <folder>. sign.py then signs them with the new ones.
Exits with an error when too many regions are missing."""
import json
import sys
import urllib.request
from pathlib import Path

LATEST = "https://github.com/bumpcar/osm-data/releases/latest/download/"
REGIONS = json.loads((Path(__file__).resolve().parent.parent / "regions.json").read_text(encoding="utf-8"))
MAX_SHARE = 0.5


def fetch(name, target):
    request = urllib.request.Request(LATEST + name, headers={"User-Agent": "bumpcar-osm-build"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                target.write_bytes(response.read())
            return
        except OSError as error:
            print(f"retry {name}: {error}")
    sys.exit(f"could not load {name}")


def main():
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    built = {json.loads(p.read_text(encoding="utf-8"))["region"] for p in folder.glob("*.report.json")}
    missing = sorted(set(REGIONS) - built)
    print(f"built {len(built)} of {len(REGIONS)}")
    if not missing:
        return
    if len(missing) > len(REGIONS) * MAX_SHARE:
        sys.exit(f"too many regions failed: {', '.join(missing)}")
    old_index = folder.parent / "previous-index.json"
    fetch("index.json", old_index)
    files = [f for f in json.loads(old_index.read_text(encoding="utf-8"))["files"] if f["region"] in missing]
    for entry in files:
        fetch(entry["file"], folder / entry["file"])
    print(f"carried over: {', '.join(missing)} ({len(files)} files)")
    Path(folder.parent / "carried-over.txt").write_text(" ".join(missing), encoding="utf-8")


if __name__ == "__main__":
    main()
