#!/usr/bin/env python3
"""python verify.py <folder or URL base>"""
import base64
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PUBLIC_KEY = Path(__file__).resolve().parent.parent / "keys" / "public-key.txt"


def read(base, name):
    if base.startswith("http"):
        request = urllib.request.Request(f"{base}/{name}", headers={"User-Agent": "bumpcar-osm-verify"})
        with urllib.request.urlopen(request) as response:
            return response.read()
    return (Path(base) / name).read_bytes()


def fetch_signed_index(base):
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY.read_text().strip()))
    index = read(base, "index.json")
    try:
        key.verify(read(base, "index.json.sig"), index)
    except InvalidSignature:
        sys.exit("BAD SIGNATURE")
    return json.loads(index)


def main():
    base = sys.argv[1].rstrip("/")
    index = fetch_signed_index(base)
    print("signature ok")
    for entry in index["files"]:
        data = read(base, entry["file"])
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            sys.exit(f"BAD CHECKSUM: {entry['file']}")
        print(f"ok {entry['file']} {len(data)} bytes, data {entry['data_date']}")


if __name__ == "__main__":
    main()
