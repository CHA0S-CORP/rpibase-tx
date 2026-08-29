"""HTMX dashboard routes (server-rendered)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import settings
from app.tx.manager import TxBusyError, manager
from app.tx.modes import REGISTRY, get_mode

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# Disable Jinja's LRUCache; it is broken under Python 3.14.
templates.env.cache = None


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "modes": REGISTRY,
            "mode": settings.rpitx_mode,
            "allowlist": settings.allowed_ranges,
            "max_seconds": settings.max_tx_seconds,
        },
    )


@router.get("/partials/form", response_class=HTMLResponse)
def mode_form(request: Request, mode: str = "tune") -> HTMLResponse:
    m = REGISTRY.get(mode)
    if m is None:
        return HTMLResponse("<p class='err'>unknown mode</p>", status_code=404)
    schema = m.model.model_json_schema()
    return templates.TemplateResponse(
        request, "_form.html", {"mode": mode, "schema": schema}
    )


@router.get("/partials/status", response_class=HTMLResponse)
def status_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_status.html", {"s": manager.status()}
    )


@router.post("/web/tx/{mode}", response_class=HTMLResponse)
async def web_start(request: Request, mode: str) -> HTMLResponse:
    form = dict(await request.form())
    authorized = form.pop("authorized", None) in ("on", "true", "1")

    def render(msg: str | None = None, err: str | None = None, code: int = 200):
        return templates.TemplateResponse(
            request,
            "_status.html",
            {"s": manager.status(), "msg": msg, "err": err},
            status_code=code,
        )

    try:
        get_mode(mode)
    except KeyError:
        return render(err=f"unknown mode '{mode}'", code=404)

    if not authorized:
        return render(err="Must confirm authorization before transmitting.", code=403)

    # Coerce numeric-looking form strings; pydantic handles the rest.
    payload = {k: v for k, v in form.items() if v != ""}
    try:
        await manager.start(mode, payload)
    except TxBusyError:
        return render(err="Already transmitting.", code=409)
    except ValidationError as e:
        first = e.errors(include_url=False)[0]
        loc = ".".join(str(x) for x in first.get("loc", []))
        return render(err=f"{loc}: {first.get('msg')}", code=400)
    return render(msg=f"Started {mode}.")


@router.post("/web/stop", response_class=HTMLResponse)
async def web_stop(request: Request) -> HTMLResponse:
    await manager.stop()
    return templates.TemplateResponse(
        request, "_status.html", {"s": manager.status(), "msg": "Stopped."}
    )
