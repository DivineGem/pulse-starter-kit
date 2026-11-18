"""HTTP routes for public status and authenticated operator operations."""
from __future__ import annotations

import time
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import COOKIE_SECURE, ROOT, SERVICES, SESSION_TTL_SECONDS, TIMEOUT_SECONDS
from .database import connect, db_lock, event, get_open_incident
from .metrics import monthly_metrics, operator_metrics
from .security import (SESSION_COOKIE, clear_session_cookie, current_operator,
                       issue_session, require_intent,
                       revoke_session, verify_credentials)
from .state import BOOT_ID, runtime, state_lock
from .utils import now_iso

router = APIRouter()


class LoginBody(BaseModel):
    """Credentials submitted by the dedicated operator login form."""
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


@router.get("/healthz")
def healthz():
    """Simple liveness endpoint used by Docker's Pulse container healthcheck."""
    return {"status": "ok", "service": "pulse"}


@router.get("/")
def operator_page(token: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    """Serve the internal dashboard after session login, or redirect to login."""
    try:
        current_operator(token)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(ROOT / "static" / "operator.html")


@router.get("/login")
def login_page():
    """Serve a normal login page rather than triggering the browser auth popup."""
    return FileResponse(ROOT / "static" / "login.html")


@router.post("/api/login", dependencies=[Depends(require_intent)])
def login(body: LoginBody, response: Response):
    """Exchange posted credentials for a random HttpOnly operator session cookie."""
    username = verify_credentials(body.username, body.password)
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = issue_session(username)
    response.set_cookie(SESSION_COOKIE, token, max_age=int(SESSION_TTL_SECONDS), path="/",
                        secure=COOKIE_SECURE, httponly=True, samesite="strict")
    return {"ok": True, "username": username}


@router.post("/api/logout", dependencies=[Depends(require_intent)])
def logout(response: Response, token: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    """Revoke the current server-side session and remove its browser cookie."""
    revoke_session(token)
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/api/operator/me")
def operator_me(username: str = Depends(current_operator)):
    """Return the current logged-in operator name for the dashboard header."""
    return {"username": username}


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
def operator_status(_: str = Depends(current_operator)):
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
def metrics(_: str = Depends(current_operator)):
    """Combine persisted service metrics with current in-memory scheduler errors."""
    with db_lock, connect() as db:
        result = [operator_metrics(db, name) for name in SERVICES]
    with state_lock:
        errors = {name: data["pulse_error"] for name, data in runtime.items()}
    for service in result:
        service["pulse_error"] = errors[service["name"]]
    return {"measured_at": now_iso(), "services": result}


@router.get("/api/operator/incidents")
def incidents(_: str = Depends(current_operator), state: str | None = None):
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
            events = db.execute("SELECT id, kind, at, message, author, edited_at FROM incident_events WHERE incident_id=? ORDER BY id", (row["id"],)).fetchall()
            result.append({**dict(row), "events": [dict(item) for item in events]})
    return {"incidents": result}


class NoteBody(BaseModel):
    """Validated operator note payload; database insertion uses bound parameters."""
    note: str = Field(min_length=1, max_length=2000)


@router.post("/api/operator/incidents/{incident_id}/notes", dependencies=[Depends(require_intent)])
def add_note(incident_id: int, body: NoteBody, author: str = Depends(current_operator)):
    """Append an attributed note; notes are operator-only and never publicized."""
    note = body.note.strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    with db_lock, connect() as db:
        if not db.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone():
            raise HTTPException(404, "Incident not found")
        event(db, incident_id, "note", note, author[:80])
    return {"ok": True}


@router.patch("/api/operator/incidents/{incident_id}/notes/{event_id}", dependencies=[Depends(require_intent)])
def edit_note(incident_id: int, event_id: int, body: NoteBody, author: str = Depends(current_operator)):
    """Edit only a note authored by the current operator; original metadata remains."""
    note = body.note.strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    with db_lock, connect() as db:
        event_row = db.execute(
            "SELECT e.id, e.kind, e.author, i.state FROM incident_events e JOIN incidents i ON i.id=e.incident_id WHERE e.id=? AND i.id=?",
            (event_id, incident_id),
        ).fetchone()
        if event_row is None:
            raise HTTPException(404, "Incident note not found")
        if event_row["kind"] != "note":
            raise HTTPException(400, "Only operator notes can be edited")
        if event_row["author"] != author:
            raise HTTPException(403, "You can only edit notes you authored")
        db.execute("UPDATE incident_events SET message=?, edited_at=? WHERE id=?",
                   (note, now_iso(), event_id))
        event(db, incident_id, "note_edited", f"An existing note was edited by {author}.", author)
    return {"ok": True}


@router.get("/api/operator/checks/{service}")
def check_history(service: str, _: str = Depends(current_operator), limit: int = 100):
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
