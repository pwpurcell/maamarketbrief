"""FastAPI app entry point.

Phase 5: dashboard reads exclusively from the SQLite cache. APScheduler
refreshes the cache every 15 minutes in the background — no request hits
AEMO live anymore. 7-day sparklines are built from cache history.

Snapshot composition (cache reads + per-section formatting) lives in
`app.snapshot.build_snapshot` so the email sender (Phase 6) can reuse it."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import cache, scheduler, sparkline
from app.snapshot import build_snapshot


log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["sparkline"] = sparkline.render

app = FastAPI(title="Markets brief")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


_scheduler = None


@app.on_event("startup")
def _on_startup():
    global _scheduler
    cache.init()
    _scheduler = scheduler.start()
    log.info("startup: cache initialised, scheduler running")


@app.on_event("shutdown")
def _on_shutdown():
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", build_snapshot())


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/cache/status")
def cache_status() -> JSONResponse:
    """Diagnostic endpoint: latest gas_day + fetched_at per cached source."""
    return JSONResponse(cache.status())


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
