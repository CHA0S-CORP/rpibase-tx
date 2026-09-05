"""JSON API for scripting transmissions."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from pydantic import ValidationError

from app.config import settings
from app.tx.manager import TxBusyError, manager
from app.tx.modes import REGISTRY, get_mode
from app.uploads import list_uploads, save_upload

router = APIRouter(prefix="/api", tags=["tx"])


@router.get("/modes")
def list_modes() -> dict:
    return {
        name: {"description": m.desc, "schema": m.model.model_json_schema()}
        for name, m in REGISTRY.items()
    }


@router.get("/status")
def status() -> dict:
    return manager.status()


def _settings_view() -> dict:
    return {
        "max_tx_seconds": settings.max_tx_seconds,
        "max_tx_seconds_hard": settings.max_tx_seconds_hard,
        "watchdog": settings.watchdog_enabled,
        "freq_allowlist": settings.allowed_ranges,
        "mode": settings.rpitx_mode,
    }


@router.get("/settings")
def get_settings() -> dict:
    return _settings_view()


@router.put("/settings")
def put_settings(body: dict = Body(...)) -> dict:
    """Runtime-adjustable settings. Only the watchdog cap for now: seconds, or
    0 to disable it. Applies to the next transmission, never one already
    running. Resets on restart."""
    if "max_tx_seconds" in body:
        try:
            settings.set_max_tx_seconds(body["max_tx_seconds"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    return _settings_view()


@router.post("/tx/{mode}", status_code=202)
async def start_tx(mode: str, body: dict = Body(default_factory=dict)) -> dict:
    try:
        get_mode(mode)
    except KeyError:
        raise HTTPException(404, f"unknown mode '{mode}'")

    # Authorization gate — no real (or mock) TX without an explicit ack.
    # Strict: only JSON `true` counts — "false"/"no"/1 are all rejected.
    if body.pop("authorized", None) is not True:
        raise HTTPException(403, "transmission requires 'authorized': true")

    try:
        state = await manager.start(mode, body)
    except TxBusyError:
        raise HTTPException(409, "a transmission is already running")
    except ValidationError as e:
        raise HTTPException(400, e.errors(include_url=False, include_context=False))
    return {"started": True, "status": manager.status(), "argv": state.argv}


@router.post("/stop")
async def stop_tx() -> dict:
    stopped = await manager.stop()
    return {"stopped": stopped, "status": manager.status()}


@router.get("/uploads")
def get_uploads() -> dict:
    return {"uploads": list_uploads()}


@router.post("/upload", status_code=201)
async def upload_file(file: UploadFile = File(...)) -> dict:
    """Store an audio/image/IQ file and return the host path to transmit it.

    Feed the returned `path` back as `audio_file` / `image_file` / `iq_file`
    when starting a transmission.
    """
    try:
        # Copy off the event loop so a large file doesn't stall status polling.
        dest = await asyncio.to_thread(save_upload, file.filename, file.file)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        await file.close()
    return {"filename": dest.name, "path": str(dest), "size": dest.stat().st_size}
