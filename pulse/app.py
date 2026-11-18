"""Pulse application entry point: lifecycle, middleware, and route assembly."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api
from .config import ROOT
from .database import init_db
from .monitor import restore_runtime, start_workers, stop_workers
from .notifications import notification_worker
from .state import stop_event


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize storage and start/stop workers with the web application."""
    init_db()
    restore_runtime()
    stop_event.clear()
    # Delivery is isolated from probes: a slow webhook can never pause monitoring.
    notifier = threading.Thread(target=notification_worker, name="pulse-notifications", daemon=True)
    notifier.start()
    workers = start_workers()
    try:
        yield
    finally:
        stop_event.set()
        notifier.join(timeout=1)
        stop_workers(workers)
        notifier.join(timeout=1)


app = FastAPI(title="Pulse", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.include_router(api.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply browser security headers consistently to pages and API responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_: Request, __: RequestValidationError):
    """Avoid reflecting request content or model internals in validation errors."""
    return JSONResponse(status_code=422, content={"detail": "Invalid request"})


@app.exception_handler(Exception)
async def safe_internal_error(_: Request, __: Exception):
    """Keep exception details in server logs rather than returning them to clients."""
    return JSONResponse(status_code=500, content={"detail": "Internal Pulse error"})
