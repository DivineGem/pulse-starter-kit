"""HTTP routes for public status and authenticated operator operations."""
from __future__ import annotations

import sqlite3
import time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import ROOT, SERVICES, TIMEOUT_SECONDS
from .database import connect, db_lock, event, get_open_incident
from .metrics import monthly_metrics, operator_metrics
from .security import basic_operator, require_intent
from .state import BOOT_ID, runtime, state_lock
from .utils import now_iso

router = APIRouter()


@router.get("/healthz")
def healthz():
    """Simple liveness endpoint used by Docker's Pulse container healthcheck."""
    return {"status": "ok", "service": "pulse"}


@router.get("/")
def operator_page(_: str = Depends(basic_operator)):
    """Serve the internal dashboard only after operator authentication."""
    return FileResponse(ROOT / "static" / "operator.html")


@router.get("/status")
def public_page():
    """Serve the deliberately limited, login-free customer status page."""
    return FileResponse(ROOT / "static" / "status.html")


def customer_status(status: str) -> str:
    """Map internal state names to customer-safe wording."""
    return {"UNKNOWN": "Checking", "UP": "Operational", "DEGRADED": "Degraded performance", "DOWN": "Investigating"}.get(status, "Checking")


@router.get("/api/status")
def public_status():
    """Expose only product-facing names, public state, and update timestamps."""
    with state_lock:
        snapshot = {name: data.copy() for name, data in runtime.items()}
    return {"updated_at": now_iso(), "services": [
        {"name": SERVICES[name]["public_name"], "status": customer_status(data["status"]), "updated_at": data["last_checked"]}
        for name, data in snapshot.items()
    ]}


@router.get("/api/operator/status")
def operator_status(_: str = Depends(basic_operator)):
    """Return internal service diagnostics and reliability details to operators."""
    with state_lock:
        snapshot = {name: data.copy() for name, data in runtime.items()}
    services = []
    with db_lock, connect() as db:
        for name, conf in SERVICES.items():
            data = snapshot[name]
            incident = get_open_incident(db, name)
            services.append({
                "name": name, "label": conf["label"], "status": data["status"],
                "interval_seconds": conf["interval"], "last_checked": data["last_checked"],
                "latency_ms": data["latency_ms"], "last_success": data["last_success"],
                "last_error": data["last_error"], "control_mode": data["last_control_state"],
                "consecutive_failures": data["consecutive_failures"],
                "scheduler_delay_ms": data["scheduler_delay_ms"], "pulse_error": data["pulse_error"],
                "open_incident_id": incident["id"] if incident else None,
                "reliability": monthly_metrics(db, name, data["status"]),
            })
    # A scheduler is stale only after its normal cadence plus request timeout and
    # a grace interval, allowing brief OS scheduling delays without false alarms.
    stale = [name for name, data in snapshot.items() if data["last_check_monotonic"] is None or time.monotonic() - data["last_check_monotonic"] > SERVICES[name]["interval"] + TIMEOUT_SECONDS + 5]
    errors = [name for name, data in snapshot.items() if data["pulse_error"]]
    return {"pulse": {"status": "degraded" if stale or errors else "healthy", "boot_id": BOOT_ID,
                       "checked_at": now_iso(), "stale_schedulers": stale, "scheduler_errors": errors},
            "services": services}


@router.get("/api/operator/metrics")
def metrics(_: str = Depends(basic_operator)):
    """Combine persisted service metrics with current in-memory scheduler errors."""
    with db_lock, connect() as db:
        result = [operator_metrics(db, name) for name in SERVICES]
    with state_lock:
        errors = {name: data["pulse_error"] for name, data in runtime.items()}
    for service in result:
        service["pulse_error"] = errors[service["name"]]
    return {"measured_at": now_iso(), "services": result}


@router.get("/api/operator/incidents")
def incidents(_: str = Depends(basic_operator), state: str | None = None):
    """List recent incidents and ordered timeline events, optionally filtered."""
    with db_lock, connect() as db:
        if state not in (None, "OPEN", "RESOLVED"):
            raise HTTPException(400, "Invalid incident state")
        rows = db.execute(
            "SELECT * FROM incidents WHERE state=? ORDER BY detected_at DESC LIMIT 200" if state else
            "SELECT * FROM incidents ORDER BY detected_at DESC LIMIT 200", (state,) if state else (),
        ).fetchall()
        result = []
        for row in rows:
            events = db.execute("SELECT kind, at, message, author FROM incident_events WHERE incident_id=? ORDER BY id", (row["id"],)).fetchall()
            result.append({**dict(row), "events": [dict(item) for item in events]})
    return {"incidents": result}


class NoteBody(BaseModel):
    """Validated operator note payload; database insertion uses bound parameters."""
    note: str = Field(min_length=1, max_length=2000)


@router.post("/api/operator/incidents/{incident_id}/notes", dependencies=[Depends(require_intent)])
def add_note(incident_id: int, body: NoteBody, author: str = Depends(basic_operator)):
    """Append an attributed note; notes are operator-only and never publicized."""
    note = body.note.strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    with db_lock, connect() as db:
        if not db.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone():
            raise HTTPException(404, "Incident not found")
        event(db, incident_id, "note", note, author[:80])
    return {"ok": True}


@router.get("/api/operator/checks/{service}")
def check_history(service: str, _: str = Depends(basic_operator), limit: int = 100):
    """Return bounded probe history for one allowlisted service."""
    if service not in SERVICES:
        raise HTTPException(404, "Unknown service")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be 1 to 500")
    with db_lock, connect() as db:
        rows = db.execute(
            "SELECT checked_at, status, latency_ms, reason, scheduler_delay_ms FROM checks WHERE service=? ORDER BY id DESC LIMIT ?",
            (service, limit),
        ).fetchall()
    return {"service": service, "checks": [dict(row) for row in rows]}
