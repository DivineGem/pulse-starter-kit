"""Pulse monitoring, incident tracking, and audience-separated status APIs."""
from __future__ import annotations

import hmac
import json
import math
import os
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DB_PATH = os.environ.get("PULSE_DB", "/data/pulse.sqlite3")
USERNAME = os.environ.get("OPERATOR_USERNAME", "operator")
PASSWORD = os.environ.get("OPERATOR_PASSWORD", "pulse-local-change-me")
try:
    OPERATOR_USERS = json.loads(os.environ.get("OPERATOR_USERS") or "{}")
except ValueError as exc:
    raise RuntimeError("OPERATOR_USERS must be a JSON object of usernames to passwords") from exc
if not isinstance(OPERATOR_USERS, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) < 12 for k, v in OPERATOR_USERS.items()):
    raise RuntimeError("OPERATOR_USERS must map usernames to passwords of at least 12 characters")
if not OPERATOR_USERS:
    OPERATOR_USERS = {USERNAME: PASSWORD}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://webhook-receiver:9000/pulse")


def positive_number(name: str, default: str, minimum: float = 0.1, maximum: float = 120.0) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


if len(PASSWORD) < 12 and os.environ.get("OPERATOR_USERS") is None:
    raise RuntimeError("OPERATOR_PASSWORD must be at least 12 characters")
TIMEOUT_SECONDS = positive_number("PROBE_TIMEOUT_SECONDS", "4", 0.2, 30.0)
FAILURE_THRESHOLD = int(positive_number("FAILURE_THRESHOLD", "2", 1, 10))
RECOVERY_THRESHOLD = int(positive_number("RECOVERY_THRESHOLD", "2", 1, 10))
SLOW_THRESHOLD_SECONDS = positive_number("SLOW_THRESHOLD_SECONDS", "1.5", 0.2, 60.0)

SERVICES: dict[str, dict[str, Any]] = {
    "payments": {"url": "http://payments:8080/health", "state_url": "http://payments:8080/control/state", "interval": 10, "target": 99.9, "label": "Payments", "feature": "Payments", "public_name": "Making a payment"},
    "claims": {"url": "http://claims:8080/health", "state_url": "http://claims:8080/control/state", "interval": 15, "target": 99.9, "label": "Claims", "feature": "Claims", "public_name": "Managing a claim"},
    "quotes": {"url": "http://quotes:8080/health", "state_url": "http://quotes:8080/control/state", "interval": 30, "target": 99.0, "label": "Quotes", "feature": "Quotes", "public_name": "Getting a quote"},
    "notifications": {"url": "http://notifications:8080/health", "state_url": "http://notifications:8080/control/state", "interval": 60, "target": 99.0, "label": "Notifications", "feature": "Notifications", "public_name": "Receiving updates"},
}

security = HTTPBasic()
db_lock = threading.RLock()
state_lock = threading.RLock()
runtime: dict[str, dict[str, Any]] = {
    name: {"status": "UNKNOWN", "last_checked": None, "latency_ms": None,
           "last_success": None, "consecutive_failures": 0, "consecutive_successes": 0,
           "last_error": None, "last_control_state": None, "last_scheduled": None,
           "scheduler_delay_ms": 0.0, "pulse_error": None, "last_check_monotonic": None}
    for name in SERVICES
}
stop_event = threading.Event()
threads: list[threading.Thread] = []
notification_thread: threading.Thread | None = None
BOOT_ID = str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db_lock, connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS checks (
          id INTEGER PRIMARY KEY, service TEXT NOT NULL, checked_at TEXT NOT NULL,
          status TEXT NOT NULL, latency_ms REAL, reason TEXT, scheduler_delay_ms REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS checks_service_time ON checks(service, checked_at);
        CREATE TABLE IF NOT EXISTS incidents (
          id INTEGER PRIMARY KEY, service TEXT NOT NULL, state TEXT NOT NULL,
          started_at TEXT NOT NULL, detected_at TEXT NOT NULL, recovered_at TEXT,
          detection_seconds REAL, recovery_seconds REAL, reason TEXT NOT NULL, control_mode TEXT
        );
        CREATE INDEX IF NOT EXISTS incidents_service_state ON incidents(service, state);
        CREATE TABLE IF NOT EXISTS incident_events (
          id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL REFERENCES incidents(id),
          kind TEXT NOT NULL, at TEXT NOT NULL, message TEXT NOT NULL, author TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
          id INTEGER PRIMARY KEY, feature TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL, sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS notification_outbox_pending ON notification_outbox(sent_at, next_attempt_at);
        """)
        incident_columns = {row["name"] for row in db.execute("PRAGMA table_info(incidents)")}
        if "control_mode" not in incident_columns:
            db.execute("ALTER TABLE incidents ADD COLUMN control_mode TEXT")


def restore_runtime() -> None:
    """Restore the latest known service states so restarts do not erase incidents."""
    with db_lock, connect() as db, state_lock:
        for name in SERVICES:
            latest = db.execute("SELECT * FROM checks WHERE service=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            if latest is None:
                continue
            data = runtime[name]
            data["status"] = latest["status"]
            data["last_checked"] = latest["checked_at"]
            data["latency_ms"] = latest["latency_ms"]
            data["last_error"] = latest["reason"]
            data["scheduler_delay_ms"] = latest["scheduler_delay_ms"]
            last_success = db.execute("SELECT checked_at FROM checks WHERE service=? AND status='UP' ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            data["last_success"] = last_success["checked_at"] if last_success else None
            recent = db.execute("SELECT status, reason FROM checks WHERE service=? ORDER BY id DESC LIMIT ?", (name, max(FAILURE_THRESHOLD, RECOVERY_THRESHOLD))).fetchall()
            if latest["status"] == "UP":
                data["consecutive_successes"] = sum(1 for row in recent if row["status"] == "UP" and row["reason"] is None)
            elif latest["status"] == "DOWN":
                data["consecutive_failures"] = sum(1 for row in recent if row["reason"] is not None)
            elif latest["status"] == "DEGRADED" and latest["reason"]:
                data["consecutive_failures"] = sum(1 for row in recent if row["reason"] is not None)
            elif latest["status"] == "DEGRADED":
                data["consecutive_successes"] = sum(1 for row in recent if row["status"] == "UP" and row["reason"] is None)
            incident = get_open_incident(db, name)
            if incident is not None:
                data["consecutive_failures"] = max(1, data["consecutive_failures"])
                try:
                    code, body = request_json(SERVICES[name]["state_url"], 2)
                    state_mode = body.get("mode") if code == 200 and isinstance(body, dict) else None
                    data["last_control_state"] = state_mode
                    db.execute("UPDATE incidents SET control_mode=? WHERE id=?", (state_mode, incident["id"]))
                except Exception:
                    data["last_control_state"] = None


def basic_operator(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    expected_password = OPERATOR_USERS.get(credentials.username, "invalid-operator-password")
    user_ok = credentials.username in OPERATOR_USERS
    pass_ok = hmac.compare_digest(credentials.password.encode(), expected_password.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Operator authentication required", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def require_intent(x_pulse_request: str | None = Header(default=None)) -> None:
    if x_pulse_request != "1":
        raise HTTPException(status_code=403, detail="Missing request intent header")


def request_json(url: str, timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Pulse-Monitor/1.0", "Accept": "application/json"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, new_url):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as resp:
            raw = resp.read(16_384)
            try:
                body = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                body = None
            return resp.status, body
    except urllib.error.HTTPError as exc:
        # Deliberately discard error response bodies: mock errors contain internal details.
        return exc.code, None
    except urllib.error.URLError as exc:
        raise ConnectionError("probe request failed") from exc


def notify_public(feature: str, status: str) -> bool:
    if status == "DOWN":
        text = f"Service disruption: Some users may have trouble using {feature}. We’re investigating. Status: Investigating. Updated: {now_iso()}"
    elif status == "DEGRADED":
        text = f"Service update: Some users may experience delays using {feature}. We’re monitoring the situation. Status: Degraded. Updated: {now_iso()}"
    else:
        text = f"Service restored: {feature} is operating normally again. Updated: {now_iso()}"
    payload = json.dumps({"message": text}).encode()
    req = urllib.request.Request(WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json", "User-Agent": "Pulse-Notifier/1.0"}, method="POST")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, new_url):
            return None
    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=3) as response:
                response.read(1024)
            return True
        except Exception as exc:
            # Public notifications contain no exception text or internal service diagnostics.
            print(f"notification delivery failed (attempt {attempt + 1}, {type(exc).__name__})", flush=True)
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
    return False


def notification_worker() -> None:
    while not stop_event.is_set():
        with db_lock, connect() as db:
            row = db.execute("SELECT * FROM notification_outbox WHERE sent_at IS NULL AND next_attempt_at<=? ORDER BY id LIMIT 1", (now_iso(),)).fetchone()
            if row is not None:
                db.execute("UPDATE notification_outbox SET attempts=attempts+1 WHERE id=?", (row["id"],))
        if row is None:
            stop_event.wait(0.5)
            continue
        delivered = notify_public(row["feature"], row["status"])
        with db_lock, connect() as db:
            if delivered:
                db.execute("UPDATE notification_outbox SET sent_at=? WHERE id=?", (now_iso(), row["id"]))
            else:
                delay = min(300, 2 ** min(row["attempts"], 8))
                next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
                db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE id=?", (next_attempt, row["id"]))


def event(db: sqlite3.Connection, incident_id: int, kind: str, message: str, author: str = "Pulse") -> None:
    db.execute("INSERT INTO incident_events(incident_id, kind, at, message, author) VALUES (?, ?, ?, ?, ?)",
               (incident_id, kind, now_iso(), message, author))


def get_open_incident(db: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM incidents WHERE service=? AND state='OPEN' ORDER BY id DESC LIMIT 1", (name,)).fetchone()


def check_service(name: str, expected_due: float) -> None:
    conf = SERVICES[name]
    started = time.monotonic()
    scheduled = time.monotonic()
    with state_lock:
        scheduler_delay_ms = max(0.0, (scheduled - expected_due) * 1000)
        runtime[name]["last_scheduled"] = now_iso()
        runtime[name]["scheduler_delay_ms"] = round(scheduler_delay_ms, 2)
    try:
        code, body = request_json(conf["url"], TIMEOUT_SECONDS)
        latency = (time.monotonic() - started) * 1000
        ok = code == 200 and isinstance(body, dict) and body.get("status") == "ok" and body.get("service") == name
        reason = None if ok else f"http_{code}" if code != 200 else "invalid_response"
    except Exception as exc:
        latency = (time.monotonic() - started) * 1000
        ok = False
        reason = "timeout" if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower() else "unreachable"

    checked_at = now_iso()
    with state_lock:
        data = runtime[name]
        old_status = data["status"]
        prior_success_at = data["last_success"]
        data["last_checked"] = checked_at
        data["last_check_monotonic"] = time.monotonic()
        data["latency_ms"] = round(latency, 2)
        data["pulse_error"] = None
        slow_response = ok and latency >= SLOW_THRESHOLD_SECONDS * 1000
        if ok:
            data["last_success"] = checked_at
            data["consecutive_successes"] = 0 if slow_response else data["consecutive_successes"] + 1
            data["consecutive_failures"] = 0
            data["last_error"] = None
            if old_status == "DOWN" and data["consecutive_successes"] < RECOVERY_THRESHOLD:
                new_status = "DOWN"
            elif slow_response:
                new_status = "DEGRADED"
            elif data["consecutive_successes"] >= RECOVERY_THRESHOLD:
                new_status = "UP"
            else:
                new_status = old_status if old_status != "UNKNOWN" else "UP"
        else:
            data["consecutive_failures"] += 1
            data["consecutive_successes"] = 0
            data["last_error"] = reason
            # A single transient failed probe does not flap state or page everyone.
            new_status = "DOWN" if data["consecutive_failures"] >= FAILURE_THRESHOLD else old_status
        data["status"] = new_status

    state_mode = None
    if new_status in ("DOWN", "DEGRADED") and new_status != old_status:
        # Optional diagnostic lookup does not hold locks or block another probe worker.
        try:
            state_code, state_body = request_json(conf["state_url"], 2)
            state_mode = state_body.get("mode") if state_code == 200 and isinstance(state_body, dict) else None
        except Exception:
            state_mode = None

    with db_lock, connect() as db:
        db.execute("INSERT INTO checks(service, checked_at, status, latency_ms, reason, scheduler_delay_ms) VALUES (?, ?, ?, ?, ?, ?)",
                   (name, checked_at, new_status, round(latency, 2), reason, round(scheduler_delay_ms, 2)))
        incident = get_open_incident(db, name)
        new_incident_id = None
        if new_status != old_status:
            if new_status in ("DOWN", "DEGRADED") and incident is None:
                last_ok = prior_success_at
                if last_ok:
                    detection_seconds = max(0.0, (datetime.fromisoformat(checked_at) - datetime.fromisoformat(last_ok)).total_seconds())
                else:
                    detection_seconds = None
                # Read-only control-state context is operator-only; never copy raw health errors.
                with state_lock:
                    runtime[name]["last_control_state"] = state_mode
                incident_reason = reason or ("high_latency" if slow_response else "unavailable")
                cursor = db.execute("INSERT INTO incidents(service, state, started_at, detected_at, detection_seconds, reason, control_mode) VALUES (?, 'OPEN', ?, ?, ?, ?, ?)",
                                    (name, last_ok or checked_at, checked_at, detection_seconds, incident_reason, state_mode))
                event(db, cursor.lastrowid, "opened", f"Incident detected ({incident_reason}). Control mode: {state_mode or 'unknown'}.")
                new_incident_id = cursor.lastrowid
            elif incident is not None and new_status == "UP":
                recovered_at = checked_at
                recovery_seconds = max(0.0, (datetime.fromisoformat(recovered_at) - datetime.fromisoformat(incident["detected_at"])).total_seconds())
                db.execute("UPDATE incidents SET state='RESOLVED', recovered_at=?, recovery_seconds=? WHERE id=?", (recovered_at, recovery_seconds, incident["id"]))
                event(db, incident["id"], "recovered", "Service health checks recovered; incident resolved automatically.")
            elif incident is not None and new_status in ("DEGRADED", "DOWN"):
                detail = "Service is responding but remains degraded." if new_status == "DEGRADED" else f"Service failure threshold reached ({reason or 'unavailable'})."
                event(db, incident["id"], "status", detail)
        should_notify = new_status in ("DOWN", "UP") or (new_status == "DEGRADED" and slow_response)
        notify_transition = old_status != "UNKNOWN" or new_status in ("DOWN", "DEGRADED")
        if notify_transition and new_status != old_status and should_notify and (new_status != "DEGRADED" or new_incident_id is not None or incident is not None):
            db.execute("INSERT INTO notification_outbox(feature, status, created_at, next_attempt_at) VALUES (?, ?, ?, ?)",
                       (conf["public_name"], new_status, now_iso(), now_iso()))


def scheduler(name: str) -> None:
    interval = SERVICES[name]["interval"]
    due = time.monotonic() + random.uniform(0, min(2.0, interval * 0.1))
    while not stop_event.is_set():
        wait = due - time.monotonic()
        if wait > 0 and stop_event.wait(wait):
            return
        try:
            check_service(name, due)
        except Exception as exc:
            with state_lock:
                runtime[name]["pulse_error"] = type(exc).__name__
            print(f"probe processing failed for {name} ({type(exc).__name__})", flush=True)
        due += interval
        if due < time.monotonic():
            due = time.monotonic() + interval


@asynccontextmanager
async def lifespan(_: FastAPI):
    global notification_thread
    init_db()
    restore_runtime()
    stop_event.clear()
    notification_thread = threading.Thread(target=notification_worker, name="pulse-notifications", daemon=True)
    notification_thread.start()
    for name in SERVICES:
        thread = threading.Thread(target=scheduler, args=(name,), name=f"probe-{name}", daemon=True)
        threads.append(thread)
        thread.start()
    yield
    stop_event.set()
    if notification_thread:
        notification_thread.join(timeout=1)
    for thread in threads:
        thread.join(timeout=1)


app = FastAPI(title="Pulse", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "pulse"}


@app.get("/")
def operator_page(_: str = Depends(basic_operator)):
    return FileResponse(ROOT / "static" / "operator.html")


@app.get("/status")
def public_page():
    return FileResponse(ROOT / "static" / "status.html")


def public_state(status: str) -> str:
    return {"UNKNOWN": "Checking", "UP": "Operational", "DEGRADED": "Degraded performance", "DOWN": "Investigating"}.get(status, "Checking")


@app.get("/api/status")
def public_status():
    with state_lock:
        snapshot = {key: value.copy() for key, value in runtime.items()}
    return {"updated_at": now_iso(), "services": [
        {"name": SERVICES[name]["public_name"], "status": public_state(data["status"]), "updated_at": data["last_checked"]}
        for name, data in snapshot.items()
    ]}


def recent_checks(db: sqlite3.Connection, name: str, since: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM checks WHERE service=? AND checked_at>=? ORDER BY checked_at", (name, since)).fetchall()


def monthly_metrics(db: sqlite3.Connection, name: str, status: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_s = month_start.isoformat()
    rows = recent_checks(db, name, month_start_s)
    first_check = datetime.fromisoformat(rows[0]["checked_at"]) if rows else now
    measured_seconds = max(0.0, (now - max(month_start, first_check)).total_seconds())
    target = SERVICES[name]["target"]
    allowed_downtime_seconds = measured_seconds * (100.0 - target) / 100.0
    bad_seconds = 0.0
    events = db.execute("SELECT checked_at, status, reason FROM checks WHERE service=? AND checked_at>=? ORDER BY checked_at", (name, month_start_s)).fetchall()
    if events:
        cur_status = "UP"
        last_t = first_check
        for row in events:
            at = datetime.fromisoformat(row["checked_at"])
            sample_failed = row["status"] != "UP" or row["reason"] is not None
            if cur_status != "UP":
                bad_seconds += max(0, (at - last_t).total_seconds())
            cur_status = "DOWN" if sample_failed else "UP"
            last_t = at
        if cur_status != "UP" or status == "DOWN":
            bad_seconds += max(0, (now - last_t).total_seconds())
    remaining = allowed_downtime_seconds - bad_seconds
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    p95 = percentile(latencies, 0.95)
    availability = max(0.0, 100.0 * (1.0 - bad_seconds / measured_seconds)) if measured_seconds > 0 else None
    return {"target_percent": target, "observed_percent": round(availability, 4) if availability is not None else None,
            "remaining_error_budget_seconds": round(remaining, 1), "window": "calendar month",
            "error_budget_exhausted": remaining < 0,
            "checks": len(rows), "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "p95_latency_ms": round(p95, 2) if p95 is not None else None,
            "failed_checks": sum(1 for r in rows if r["status"] != "UP" or r["reason"] is not None)}


@app.get("/api/operator/status")
def operator_status(_: str = Depends(basic_operator)):
    with state_lock:
        snapshot = {key: value.copy() for key, value in runtime.items()}
    results = []
    with db_lock, connect() as db:
        for name, conf in SERVICES.items():
            data = snapshot[name]
            incident = get_open_incident(db, name)
            results.append({"name": name, "label": conf["label"], "status": data["status"],
                "interval_seconds": conf["interval"], "last_checked": data["last_checked"],
                "latency_ms": data["latency_ms"], "last_success": data["last_success"],
                "last_error": data["last_error"], "control_mode": data["last_control_state"],
                "consecutive_failures": data["consecutive_failures"], "scheduler_delay_ms": data["scheduler_delay_ms"],
                "pulse_error": data["pulse_error"], "open_incident_id": incident["id"] if incident else None,
                "reliability": monthly_metrics(db, name, data["status"])})
    stale = [name for name, data in snapshot.items() if data["last_check_monotonic"] is None or time.monotonic() - data["last_check_monotonic"] > SERVICES[name]["interval"] + TIMEOUT_SECONDS + 5]
    errors = [name for name, data in snapshot.items() if data["pulse_error"]]
    pulse_status = "degraded" if stale or errors else "healthy"
    return {"pulse": {"status": pulse_status, "boot_id": BOOT_ID, "checked_at": now_iso(),
        "stale_schedulers": stale, "scheduler_errors": errors}, "services": results}


@app.get("/api/operator/metrics")
def operator_metrics(_: str = Depends(basic_operator)):
    with state_lock:
        snapshot = {key: value.copy() for key, value in runtime.items()}
    result = []
    with db_lock, connect() as db:
        for name, conf in SERVICES.items():
            data = snapshot[name]
            since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            rows = recent_checks(db, name, since)
            incidents = db.execute("SELECT detection_seconds, recovery_seconds FROM incidents WHERE service=? AND detected_at>=?", (name, since)).fetchall()
            detection = [r["detection_seconds"] for r in incidents if r["detection_seconds"] is not None]
            recovery = [r["recovery_seconds"] for r in incidents if r["recovery_seconds"] is not None]
            started_ago = (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["checked_at"])).total_seconds() if rows else 0.0
            delays = [r["scheduler_delay_ms"] for r in rows]
            result.append({"name": name, "interval_seconds": conf["interval"],
                "probe_count_30d": len(rows), "failed_checks_30d": sum(1 for r in rows if r["reason"]),
                "avg_scheduler_delay_ms": round(sum(delays) / len(delays), 2) if delays else 0,
                "max_scheduler_delay_ms": round(max(delays), 2) if delays else 0,
                "detection_seconds": sorted(detection), "recovery_seconds": sorted(recovery),
                "median_detection_seconds": percentile(detection, 0.5), "p95_detection_seconds": percentile(detection, 0.95),
                "median_recovery_seconds": percentile(recovery, 0.5), "p95_recovery_seconds": percentile(recovery, 0.95),
                "estimated_observation_coverage_percent": round(min(100.0, 100.0 * len(rows) * conf["interval"] / started_ago), 1) if started_ago > 0 else 0.0,
                "open_incident_duration_seconds": round((datetime.now(timezone.utc) - datetime.fromisoformat(get_open_incident(db, name)["detected_at"])).total_seconds(), 1) if get_open_incident(db, name) else None,
                "pulse_error": data["pulse_error"]})
    return {"measured_at": now_iso(), "services": result}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))], 1)


@app.get("/api/operator/incidents")
def incidents(_: str = Depends(basic_operator), state: str | None = None):
    with db_lock, connect() as db:
        if state not in (None, "OPEN", "RESOLVED"):
            raise HTTPException(400, "Invalid incident state")
        if state:
            rows = db.execute("SELECT * FROM incidents WHERE state=? ORDER BY detected_at DESC LIMIT 200", (state,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM incidents ORDER BY detected_at DESC LIMIT 200").fetchall()
        out = []
        for row in rows:
            events = db.execute("SELECT kind, at, message, author FROM incident_events WHERE incident_id=? ORDER BY id", (row["id"],)).fetchall()
            out.append({**dict(row), "events": [dict(e) for e in events]})
    return {"incidents": out}


class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@app.post("/api/operator/incidents/{incident_id}/notes", dependencies=[Depends(require_intent)])
def add_note(incident_id: int, body: NoteBody, author: str = Depends(basic_operator)):
    note = body.note.strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    with db_lock, connect() as db:
        incident = db.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not incident:
            raise HTTPException(404, "Incident not found")
        event(db, incident_id, "note", note, author[:80])
    return {"ok": True}


@app.get("/api/operator/checks/{service}")
def check_history(service: str, _: str = Depends(basic_operator), limit: int = 100):
    if service not in SERVICES:
        raise HTTPException(404, "Unknown service")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be 1 to 500")
    with db_lock, connect() as db:
        rows = db.execute("SELECT checked_at, status, latency_ms, reason, scheduler_delay_ms FROM checks WHERE service=? ORDER BY id DESC LIMIT ?", (service, limit)).fetchall()
    return {"service": service, "checks": [dict(row) for row in rows]}


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_: Request, __: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "Invalid request"})


@app.exception_handler(Exception)
async def safe_internal_error(_: Request, __: Exception):
    return JSONResponse(status_code=500, content={"detail": "Internal Pulse error"})
