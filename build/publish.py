#!/usr/bin/env python3
"""python publish.py <folder>: draft, upload with retries, check, publish, check from outside."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import recall

ATTEMPTS = 5
WAIT_SECONDS = 30
NOTES = "© OpenStreetMap contributors, ODbL 1.0"
LATEST = "https://github.com/bumpcar/osm-data/releases/latest/download"


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def release_tag(data_date, run):
    return f"data-{data_date[:10]}-r{run}"


def existing_tag(releases, run):
    """A restart may see another data date; the run number still finds its draft."""
    suffix = f"-r{run}"
    return next((r["tagName"] for r in releases if r["tagName"].endswith(suffix)), None)


def missing(files, assets):
    """Local files that are not there in full. An interrupted upload has the state 'starter'."""
    there = {a["name"]: a for a in assets}
    result = []
    for file in files:
        asset = there.get(file.name)
        if asset is None or asset["size"] != file.stat().st_size or asset.get("state", "uploaded") != "uploaded":
            result.append(file)
    return result


def remote_assets(tag):
    return json.loads(gh("release", "view", tag, "--json", "assets"))["assets"]


def upload(tag, files, assets=remote_assets, send=None, attempts=ATTEMPTS, wait=WAIT_SECONDS, sleep=time.sleep):
    send = send or (lambda todo: gh("release", "upload", tag, "--clobber", *map(str, todo)))
    for attempt in range(1, attempts + 1):
        todo = missing(files, assets(tag))
        if not todo:
            return
        print(f"attempt {attempt}: {len(todo)} files", flush=True)
        try:
            send(todo)
        except subprocess.CalledProcessError as e:
            print(e.stderr, flush=True)
            if attempt < attempts:
                sleep(wait * attempt)
    todo = missing(files, assets(tag))
    if todo:
        sys.exit(f"not uploaded: {[f.name for f in todo]}")


def release_files(folder):
    names = [*sorted(p.name for p in folder.glob("*.zst")), "index.json", "index.json.sig", "LICENSE-ODbL.txt", "README.md"]
    if (folder / recall.FILE).exists():
        names.append(recall.FILE)
    return [folder / name for name in names]


def check_from_outside():
    script = str(Path(__file__).with_name("verify.py"))
    for attempt in range(1, ATTEMPTS + 1):
        result = subprocess.run([sys.executable, script, LATEST], capture_output=True, text=True)
        if result.returncode == 0:
            print("checked from outside", flush=True)
            return
        print(result.stdout[-500:], result.stderr[-500:], flush=True)
        time.sleep(WAIT_SECONDS * attempt)
    sys.exit("check from outside failed")


def main():
    folder = Path(sys.argv[1])
    data_date = json.loads((folder / "index.json").read_text(encoding="utf-8"))["data_date"]
    run = os.environ["GITHUB_RUN_NUMBER"]
    releases = json.loads(gh("release", "list", "--limit", "100", "--json", "tagName"))
    tag = existing_tag(releases, run)
    if tag is None:
        tag = release_tag(data_date, run)
        gh("release", "create", tag, "--draft", "--title", data_date[:10], "--notes", NOTES)
    print(f"release {tag}", flush=True)
    upload(tag, release_files(folder))
    gh("release", "edit", tag, "--draft=false", "--latest", "--title", data_date[:10])
    check_from_outside()


if __name__ == "__main__":
    main()
