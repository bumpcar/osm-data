#!/usr/bin/env python3
"""recalls.txt: line 1 the signature, the rest the signed JSON.

SIGNING_KEY=<base64 seed> python recall.py add <region> <layer> <valid from> <folder>
SIGNING_KEY=<base64 seed> python recall.py remove <region> <layer> <folder>
python recall.py carry <folder>
python recall.py confirm <folder>
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from verify import PUBLIC_KEY, fetch_signed_index, read

REGIONS = json.loads((Path(__file__).resolve().parent.parent / "regions.json").read_text(encoding="utf-8"))
LATEST = "https://github.com/bumpcar/osm-data/releases/latest/download"
FILE = "recalls.txt"
FORMAT_VERSION = 1
LAYERS = ("limits", "cameras")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def empty():
    return {"format_version": FORMAT_VERSION, "issued": "", "recalls": []}


def is_recalled(recalls, region, layer, data_date):
    return any(r["region"] == region and r["layer"] == layer and data_date < r["valid_from"] for r in recalls["recalls"])


def next_issued(recalls, now):
    """Always later than the last one: devices ignore a list that is not newer than theirs."""
    issued = now.strftime(TIME_FORMAT)
    if recalls["issued"] and issued <= recalls["issued"]:
        last = datetime.strptime(recalls["issued"], TIME_FORMAT).replace(tzinfo=timezone.utc)
        issued = (last + timedelta(seconds=1)).strftime(TIME_FORMAT)
    return issued


def added(recalls, region, layer, valid_from, now):
    if region not in REGIONS:
        sys.exit(f"unknown region: {region}")
    if layer not in LAYERS:
        sys.exit(f"unknown layer: {layer}")
    if not DATE.match(valid_from):
        sys.exit(f"wrong date: {valid_from}")
    rest = [r for r in recalls["recalls"] if (r["region"], r["layer"]) != (region, layer)]
    entry = {"region": region, "layer": layer, "valid_from": valid_from}
    return {"format_version": FORMAT_VERSION, "issued": next_issued(recalls, now), "recalls": rest + [entry]}


def removed(recalls, region, layer, now):
    rest = [r for r in recalls["recalls"] if (r["region"], r["layer"]) != (region, layer)]
    if len(rest) == len(recalls["recalls"]):
        sys.exit(f"not recalled: {region} {layer}")
    return {"format_version": FORMAT_VERSION, "issued": next_issued(recalls, now), "recalls": rest}


def sign(recalls, key):
    payload = json.dumps(recalls, indent=1).encode("utf-8")
    return base64.b64encode(key.sign(payload)) + b"\n" + payload


def parse(data, key):
    """The list, or None when the signature is wrong."""
    signature, _, payload = data.partition(b"\n")
    try:
        key.verify(base64.b64decode(signature), payload)
    except (InvalidSignature, ValueError):
        return None
    return json.loads(payload)


def public_key():
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY.read_text().strip()))


def fetch_raw(base):
    """The bytes of the published list, None when there is none yet."""
    try:
        return read(base, FILE)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return None


def fetch_current(base):
    data = fetch_raw(base)
    if data is None:
        return empty()
    recalls = parse(data, public_key())
    if recalls is None:
        sys.exit("bad signature on the published recalls")
    return recalls


def main():
    command = sys.argv[1]
    folder = Path(sys.argv[-1])
    folder.mkdir(parents=True, exist_ok=True)
    if command == "carry":
        data = fetch_raw(LATEST)
        if data is None:
            print("no recalls yet")
            return
        if parse(data, public_key()) is None:
            sys.exit("bad signature on the published recalls")
        (folder / FILE).write_bytes(data)
        return
    if command == "confirm":
        for attempt in range(1, 6):
            if fetch_raw(LATEST) == (folder / FILE).read_bytes():
                print("checked from outside")
                return
            time.sleep(30 * attempt)
        sys.exit("check from outside failed")

    now = datetime.now(timezone.utc)
    current = fetch_current(LATEST)
    if command == "add":
        new = added(current, sys.argv[2], sys.argv[3], sys.argv[4], now)
    elif command == "remove":
        new = removed(current, sys.argv[2], sys.argv[3], now)
    else:
        sys.exit(f"unknown command: {command}")
    seed = os.environ.get("SIGNING_KEY", "").strip()
    if not seed:
        sys.exit("SIGNING_KEY is not set")
    (folder / FILE).write_bytes(sign(new, Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed))))
    print(json.dumps(new, indent=1))
    if command == "add":
        files = fetch_signed_index(LATEST)["files"]
        hit = [e for e in files if e["region"] == sys.argv[2] and e["layer"] == sys.argv[3] and e["data_date"] < sys.argv[4]]
        print("latest release affected" if hit else "latest release not affected, only older packages")


if __name__ == "__main__":
    main()
