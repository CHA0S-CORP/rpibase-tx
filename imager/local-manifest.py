#!/usr/bin/env python3
"""Make a Raspberry Pi Imager manifest for a locally downloaded rpibase-tx image.

Imager 2.x will not offer OS customisation (hostname, user, SSH, Wi-Fi) for a
local .img/.img.zst it knows nothing about. This writes a manifest next to the
image declaring init_format=systemd (what modules/headless.nix applies), so:

    ./imager/local-manifest.py ~/Downloads/nixos-image-sd-card-*.img.zst
    open ~/Downloads/os_list_local.rpi-imager-manifest      # macOS
    rpi-imager --repo ~/Downloads/os_list_local.rpi-imager-manifest

then pick "rpibase-tx" on the OS page and the Customisation step is enabled.

Needs `zstd` on PATH, or Python 3.14+ (compression.zstd), to hash the
uncompressed image. Only stdlib otherwise.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

CHUNK = 1 << 20


def _extract_stats(path: pathlib.Path) -> tuple[int, str]:
    """(uncompressed size, sha256) of the image inside a .zst, or of a raw .img."""
    h = hashlib.sha256()
    size = 0
    if path.suffix != ".zst":
        with path.open("rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
                size += len(chunk)
        return size, h.hexdigest()

    if shutil.which("zstd"):
        proc = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        while chunk := proc.stdout.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
        if proc.wait() != 0:
            sys.exit(f"zstd failed on {path}")
        return size, h.hexdigest()

    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        sys.exit("need `zstd` on PATH or Python 3.14+ to read a .zst image")
    with path.open("rb") as raw, zstd.ZstdFile(raw) as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image", type=pathlib.Path, help="the .img.zst (or .img) file")
    ap.add_argument("-o", "--output", type=pathlib.Path,
                    help="manifest path (default: os_list_local.rpi-imager-manifest next to the image)")
    ap.add_argument("--devices", nargs="+", default=["pi3-64bit"],
                    help="Imager device tags this image is offered for (default: pi3-64bit)")
    args = ap.parse_args()

    img = args.image.expanduser().resolve()
    if not img.is_file():
        sys.exit(f"not a file: {img}")
    out = args.output or img.with_name("os_list_local.rpi-imager-manifest")

    print(f"hashing uncompressed image (a minute or so)…", file=sys.stderr)
    extract_size, extract_sha = _extract_stats(img)

    manifest = {
        "os_list": [
            {
                "name": "rpibase-tx (rpitx + web dashboard, Raspberry Pi 3)",
                "description": (
                    "NixOS image with rpitx and the rpibase-tx control panel on port 8080. "
                    "Supports Imager OS customisation (hostname, user, SSH keys, Wi-Fi)."
                ),
                "url": img.as_uri(),
                "release_date": dt.datetime.fromtimestamp(img.stat().st_mtime).strftime("%Y-%m-%d"),
                "image_download_size": img.stat().st_size,
                "extract_size": extract_size,
                "extract_sha256": extract_sha,
                "init_format": "systemd",
                "devices": args.devices,
            }
        ]
    }
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    print("open it in Raspberry Pi Imager (double-click, or: rpi-imager --repo <that file>)")


if __name__ == "__main__":
    main()
