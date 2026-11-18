"""Isolated service probe workers, status transitions, and incident lifecycle."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .config import FAILURE_THRESHOLD, RECOVERY_THRESHOLD, SERVICES, SLOW_THRESHOLD_SECONDS, TIMEOUT_SECONDS
from .database import connect, db_lock, event, get_open_incident
from .state import runtime, state_lock, stop_event
from .utils import now_iso


def request_json(url: str, timeout: float) -> tuple[int, Any]:
    """GET a bounded JSON response without following redirects or exposing error bodies."""
    request = urllib.request.Request(url, headers={"User-Agent": "Pulse-Monitor/1.0", "Accept": "application/json"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, new_url):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(16_384)
            try:
                body = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                body = None
            return response.status, body
    except urllib.error.HTTPError as exc:
        # Error payloads from mock endpoints intentionally contain internal details.
        return exc.code, None
    except urllib.error.URLError as exc:
        raise ConnectionError("probe request failed") from exc


def restore_runtime() -> None:
    """Restore persisted state and leave any active incidents open on restart."""
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
            last_success = db.execute(
                "SELECT checked_at FROM checks WHERE service=? AND status='UP' ORDER BY id DESC LIMIT 1", (name,)
            ).fetchone()
            data["last_success"] = last_success["checked_at"] if last_success else None
            # Rebuild enough consecutive samples to continue threshold decisions
            # after a restart rather than treating every restart as a fresh incident.
            recent = db.execute(
                "SELECT status, reason FROM checks WHERE service=? ORDER BY id DESC LIMIT ?",
                (name, max(FAILURE_THRESHOLD, RECOVERY_THRESHOLD)),
            ).fetchall()
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
                # The incident remains open across restarts. Control mode is refreshed
                # for operator diagnostics only and is never copied to public output.
                data["consecutive_failures"] = max(1, data["consecutive_failures"])
                try:
                    code, body = request_json(SERVICES[name]["state_url"], 2)
                    mode = body.get("mode") if code == 200 and isinstance(body, dict) else None
                    data["last_control_state"] = mode
                    db.execute("UPDATE incidents SET control_mode=? WHERE id=?", (mode, incident["id"]))
                except Exception:
                    data["last_control_state"] = None


def _next_state(data: dict[str, Any], old_status: str, ok: bool, slow: bool, reason: str) -> str:
    """Apply hysteresis thresholds to one sample to reduce status flapping."""
    if ok:
        data["last_success"] = data["last_checked"]
        data["consecutive_successes"] = 0 if slow else data["consecutive_successes"] + 1
        data["consecutive_failures"] = 0
        data["last_error"] = None
        # Going DOWN requires repeated failures; recovery similarly needs a run of
        # successful checks before marking an incident resolved.
        if old_status == "DOWN" and data["consecutive_successes"] < RECOVERY_THRESHOLD:
            return "DOWN"
        if slow:
            return "DEGRADED"
        if data["consecutive_successes"] >= RECOVERY_THRESHOLD:
            return "UP"
        return old_status if old_status != "UNKNOWN" else "UP"

    data["consecutive_failures"] += 1
    data["consecutive_successes"] = 0
    data["last_error"] = reason
    # A single transient failure does not flap status or open an incident.
    return "DOWN" if data["consecutive_failures"] >= FAILURE_THRESHOLD else old_status


def check_service(name: str, expected_due: float) -> None:
    """Run and persist one service check without sharing its network wait with peers."""
    conf = SERVICES[name]
    started = time.monotonic()
    scheduler_delay_ms = max(0.0, (started - expected_due) * 1000)
    with state_lock:
        runtime[name]["last_scheduled"] = now_iso()
        runtime[name]["scheduler_delay_ms"] = round(scheduler_delay_ms, 2)
    # This worker has its own bounded request, so timeout/latency affects only itself.
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
        previous_success = data["last_success"]
        data["last_checked"] = checked_at
        data["last_check_monotonic"] = time.monotonic()
        data["latency_ms"] = round(latency, 2)
        data["pulse_error"] = None
        slow = ok and latency >= SLOW_THRESHOLD_SECONDS * 1000
        new_status = _next_state(data, old_status, ok, slow, reason or "")
        data["status"] = new_status

    # Fetch diagnostic control state only on a transition, not on every poll.
    # It is a read-only GET and its result is kept on the internal incident record.
    control_mode = None
    if new_status in ("DOWN", "DEGRADED") and new_status != old_status:
        try:
            state_code, state_body = request_json(conf["state_url"], 2)
            control_mode = state_body.get("mode") if state_code == 200 and isinstance(state_body, dict) else None
        except Exception:
            control_mode = None

    # Persist the probe and related incident/outbox updates in a short transaction,
    # ensuring a transition is not recorded without its corresponding notification.
    with db_lock, connect() as db:
        db.execute(
            "INSERT INTO checks(service, checked_at, status, latency_ms, reason, scheduler_delay_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (name, checked_at, new_status, round(latency, 2), reason, round(scheduler_delay_ms, 2)),
        )
        incident = get_open_incident(db, name)
        new_incident_id = None
        if new_status != old_status:
            if new_status in ("DOWN", "DEGRADED") and incident is None:
                detection = max(0.0, (datetime.fromisoformat(checked_at) - datetime.fromisoformat(previous_success)).total_seconds()) if previous_success else None
                with state_lock:
                    runtime[name]["last_control_state"] = control_mode
                incident_reason = reason or ("high_latency" if slow else "unavailable")
                cursor = db.execute(
                    "INSERT INTO incidents(service, state, started_at, detected_at, detection_seconds, reason, control_mode) VALUES (?, 'OPEN', ?, ?, ?, ?, ?)",
                    (name, previous_success or checked_at, checked_at, detection, incident_reason, control_mode),
                )
                event(db, cursor.lastrowid, "opened", f"Incident detected ({incident_reason}). Control mode: {control_mode or 'unknown'}.")
                new_incident_id = cursor.lastrowid
            elif incident is not None and new_status == "UP":
                recovery = max(0.0, (datetime.fromisoformat(checked_at) - datetime.fromisoformat(incident["detected_at"])).total_seconds())
                db.execute("UPDATE incidents SET state='RESOLVED', recovered_at=?, recovery_seconds=? WHERE id=?", (checked_at, recovery, incident["id"]))
                event(db, incident["id"], "recovered", "Service health checks recovered; incident resolved automatically.")
            elif incident is not None and new_status in ("DEGRADED", "DOWN"):
                message = "Service is responding but remains degraded." if new_status == "DEGRADED" else f"Service failure threshold reached ({reason or 'unavailable'})."
                event(db, incident["id"], "status", message)

        # Ordinary transient failures do not alert; sustained failures and slow
        # successful probes do. Recovery alerts are emitted only on a state change.
        send_alert = new_status in ("DOWN", "UP") or (new_status == "DEGRADED" and slow)
        first_failure_alert = old_status == "UNKNOWN" and new_status in ("DOWN", "DEGRADED")
        if (old_status != "UNKNOWN" or first_failure_alert) and new_status != old_status and send_alert:
            if new_status != "DEGRADED" or new_incident_id is not None or incident is not None:
                db.execute(
                    "INSERT INTO notification_outbox(feature, status, created_at, next_attempt_at) VALUES (?, ?, ?, ?)",
                    (conf["public_name"], new_status, now_iso(), now_iso()),
                )


def scheduler(name: str) -> None:
    """Own one service's cadence; never block another service's worker thread."""
    interval = SERVICES[name]["interval"]
    # A small initial jitter prevents every service from probing at once on startup.
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
        # Keep a stable cadence. If a probe overruns its next slot, skip the missed
        # slot instead of launching overlapping requests for this same service.
        due += interval
        if due < time.monotonic():
            due = time.monotonic() + interval


def start_workers() -> list[threading.Thread]:
    """Start exactly one independent scheduler per configured service."""
    stop_event.clear()
    workers = []
    for name in SERVICES:
        worker = threading.Thread(target=scheduler, args=(name,), name=f"probe-{name}", daemon=True)
        workers.append(worker)
        worker.start()
    return workers


def stop_workers(workers: list[threading.Thread]) -> None:
    """Signal all schedulers and briefly join them during application shutdown."""
    stop_event.set()
    for worker in workers:
        worker.join(timeout=1)
