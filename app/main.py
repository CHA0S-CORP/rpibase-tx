"""rpibase-tx — FastAPI app entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.config import settings
from app.tx.manager import manager
from app.web.routes import router as web_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Safety: never leave the transmitter keyed on shutdown.
    await manager.stop()


app = FastAPI(title="rpibase-tx", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
app.include_router(web_router)

_static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "mode": settings.rpitx_mode}
