#!/usr/bin/env python3
"""GH_TOKEN=... python bandwidth.py <owner/repo> <status file>

Adds up downloads x size of every release file and keeps one line per run, so the
difference between two runs is the traffic in between. Run it before old releases are deleted."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def releases(repo):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "bumpcar-osm-build"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    request = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases?per_page=100", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main():
    repo, status = sys.argv[1], Path(sys.argv[2])
    data = json.loads(status.read_text(encoding="utf-8")) if status.exists() else {"releases": {}, "runs": []}
    for release in releases(repo):
        assets = release["assets"]
        data["releases"][release["tag_name"]] = {
            "downloads": sum(a["download_count"] for a in assets),
            "bytes": sum(a["download_count"] * a["size"] for a in assets),
            "index_downloads": sum(a["download_count"] for a in assets if a["name"] == "index.json"),
        }
    total = sum(r["bytes"] for r in data["releases"].values())
    runs = data["runs"]
    since = total - runs[-1]["total_bytes"] if runs else total
    runs.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_bytes": total,
        "since_last_gb": round(since / 1e9, 2),
    })
    status.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"since last run {since / 1e9:.2f} GB, all time {total / 1e9:.2f} GB")
    if since > 5e12:
        print("::warning::more than 5 TB since the last run, time for the second host (R2)")


if __name__ == "__main__":
    main()
