"""HTMX dashboard routes (server-rendered)."""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import settings
from app.tx.manager import TxBusyError, manager
from app.tx.modes import REGISTRY, get_mode
from app.uploads import list_uploads, save_upload

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# Disable Jinja's LRUCache; it is broken under Python 3.14.
templates.env.cache = None


def _fmt_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(f"{lo / 1e6:.3f}–{hi / 1e6:.3f} MHz" for lo, hi in ranges)


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


templates.env.filters["fmt_ranges"] = _fmt_ranges
templates.env.filters["fmt_size"] = _fmt_size


def _base_ctx() -> dict:
    """Context every full page needs for the top bar."""
    return {
        "mode": settings.rpitx_mode,
        "hostname": socket.gethostname(),
        "allowlist": settings.allowed_ranges,
        "max_seconds": settings.max_tx_seconds,
        "bin_dir": settings.bin_dir,
        "modes": REGISTRY,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", _base_ctx())


@router.get("/partials/form", response_class=HTMLResponse)
def mode_form(request: Request, mode: str = "tune") -> HTMLResponse:
    m = REGISTRY.get(mode)
    if m is None:
        return HTMLResponse("<p class='banner err'>unknown mode</p>", status_code=404)
    return templates.TemplateResponse(
        request,
        "_form.html",
        {
            "mode": mode,
            "desc": m.desc,
            "schema": m.model.model_json_schema(),
            "max_seconds": settings.max_tx_seconds,
        },
    )


def _status(request: Request, msg: str | None = None, err: str | None = None, code: int = 200):
    return templates.TemplateResponse(
        request,
        "_status.html",
        {"s": manager.status(), "modes": REGISTRY, "msg": msg, "err": err},
        status_code=code,
    )


@router.get("/partials/status", response_class=HTMLResponse)
def status_partial(request: Request) -> HTMLResponse:
    return _status(request)


def _uploads(request: Request, err: str | None = None, code: int = 200):
    return templates.TemplateResponse(
        request, "_uploads.html", {"uploads": list_uploads(), "err": err}, status_code=code
    )


@router.get("/partials/uploads", response_class=HTMLResponse)
def uploads_partial(request: Request) -> HTMLResponse:
    return _uploads(request)


@router.post("/web/upload", response_class=HTMLResponse)
async def web_upload(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
    try:
        await asyncio.to_thread(save_upload, file.filename, file.file)
    except ValueError as e:
        return _uploads(request, err=str(e), code=400)
    finally:
        await file.close()
    return _uploads(request)


@router.post("/web/tx/{mode}", response_class=HTMLResponse)
async def web_start(request: Request, mode: str) -> HTMLResponse:
    form = dict(await request.form())
    authorized = form.pop("authorized", None) in ("on", "true", "1")

    try:
        get_mode(mode)
    except KeyError:
        return _status(request, err=f"unknown mode '{mode}'", code=404)

    if not authorized:
        return _status(request, err="Must confirm authorization before transmitting.", code=403)

    # Coerce numeric-looking form strings; pydantic handles the rest.
    payload = {k: v for k, v in form.items() if v != ""}
    try:
        await manager.start(mode, payload)
    except TxBusyError:
        return _status(request, err="Already transmitting.", code=409)
    except ValidationError as e:
        first = e.errors(include_url=False)[0]
        loc = ".".join(str(x) for x in first.get("loc", []))
        return _status(request, err=f"{loc}: {first.get('msg')}", code=400)
    return _status(request, msg=f"Started {mode}.")


@router.post("/web/stop", response_class=HTMLResponse)
async def web_stop(request: Request) -> HTMLResponse:
    await manager.stop()
    return _status(request, msg="Stopped.")
