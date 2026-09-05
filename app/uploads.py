"""Storage for uploaded audio/image/IQ files used by transmissions."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import BinaryIO

from app.config import settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def upload_root() -> Path:
    root = Path(settings.upload_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(filename: str | None) -> str:
    """Reduce an arbitrary client filename to a safe basename."""
    base = os.path.basename(filename or "").strip()
    base = _SAFE.sub("_", base)
    base = base.lstrip(".") or "upload.bin"
    return base


def _unique(root: Path, name: str) -> Path:
    """Never overwrite: a live TX may be reading the existing file."""
    dest = root / name
    if not dest.exists():
        return dest
    stem, dot, ext = name.partition(".")
    for i in range(1, 10_000):
        cand = root / f"{stem}-{i}{dot}{ext}"
        if not cand.exists():
            return cand
    raise ValueError("too many uploads with the same name")


def save_upload(filename: str | None, src: BinaryIO) -> Path:
    """Persist an uploaded stream under the upload root, return its full path."""
    root = upload_root()
    dest = _unique(root, _safe_name(filename)).resolve()
    # Defense in depth: never let a crafted name escape the upload root.
    if root not in dest.parents and dest != root:
        raise ValueError("resolved path escapes upload directory")
    with dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return dest


def is_allowed_file(path: str) -> bool:
    """A TX file param must be an existing file inside the upload root.

    The service runs as root on the Pi; without this, any host file (e.g.
    /etc/shadow) could be handed to sendiq and put on the air.
    """
    p = Path(path).resolve()
    if not p.is_file():
        return False
    if settings.allow_any_path:
        return True
    return upload_root() in p.parents


def list_uploads() -> list[dict]:
    root = upload_root()
    return [
        {"filename": p.name, "path": str(p), "size": p.stat().st_size}
        for p in sorted(root.iterdir())
        if p.is_file()
    ]
