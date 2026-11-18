"""Reliability/error-budget and scheduler effectiveness calculations."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import SERVICES
from .database import get_open_incident
from .utils import percentile


def recent_checks(db: sqlite3.Connection, name: str, since: str) -> list[sqlite3.Row]:
    """Load samples in chronological order for one service and one reporting window."""
    return db.execute("SELECT * FROM checks WHERE service=? AND checked_at>=? ORDER BY checked_at", (name, since)).fetchall()


def monthly_metrics(db: sqlite3.Connection, name: str, status: str) -> dict[str, Any]:
    """Estimate monthly availability and remaining target error budget from probes.

    Downtime is approximated between probe timestamps, so the displayed value is an
    operational estimate rather than an exact customer-impact/SLO accounting.
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = recent_checks(db, name, month_start.isoformat())
    first_check = datetime.fromisoformat(rows[0]["checked_at"]) if rows else now
    measured_seconds = max(0.0, (now - max(month_start, first_check)).total_seconds())
    target = SERVICES[name]["target"]
    allowed_downtime = measured_seconds * (100.0 - target) / 100.0
    bad_seconds = 0.0
    events = db.execute(
        "SELECT checked_at, status, reason FROM checks WHERE service=? AND checked_at>=? ORDER BY checked_at",
        (name, month_start.isoformat()),
    ).fetchall()
    if events:
        # Carry each observed failure until the next sample. This makes the estimate
        # duration-based rather than treating every individual check as equal time.
        current_status = "UP"
        previous_time = first_check
        for row in events:
            checked_at = datetime.fromisoformat(row["checked_at"])
            failed = row["status"] != "UP" or row["reason"] is not None
            if current_status != "UP":
                bad_seconds += max(0, (checked_at - previous_time).total_seconds())
            current_status = "DOWN" if failed else "UP"
            previous_time = checked_at
        if current_status != "UP" or status == "DOWN":
            bad_seconds += max(0, (now - previous_time).total_seconds())
    remaining = allowed_downtime - bad_seconds
    latencies = [row["latency_ms"] for row in rows if row["latency_ms"] is not None]
    observed = max(0.0, 100.0 * (1.0 - bad_seconds / measured_seconds)) if measured_seconds > 0 else None
    return {
        "target_percent": target,
        "observed_percent": round(observed, 4) if observed is not None else None,
        "remaining_error_budget_seconds": round(remaining, 1),
        "window": "calendar month",
        "error_budget_exhausted": remaining < 0,
        "checks": len(rows),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "p95_latency_ms": round(percentile(latencies, 0.95), 2) if latencies else None,
        "failed_checks": sum(1 for row in rows if row["status"] != "UP" or row["reason"] is not None),
    }


def operator_metrics(db: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Summarize the last 30 days of monitoring cadence and incident response."""
    conf = SERVICES[name]
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = recent_checks(db, name, since)
    incidents = db.execute(
        "SELECT detection_seconds, recovery_seconds FROM incidents WHERE service=? AND detected_at>=?",
        (name, since),
    ).fetchall()
    detections = [row["detection_seconds"] for row in incidents if row["detection_seconds"] is not None]
    recoveries = [row["recovery_seconds"] for row in incidents if row["recovery_seconds"] is not None]
    delays = [row["scheduler_delay_ms"] for row in rows]
    first_check_age = (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["checked_at"])).total_seconds() if rows else 0
    open_incident = get_open_incident(db, name)
    return {
        "name": name,
        "interval_seconds": conf["interval"],
        "probe_count_30d": len(rows),
        "failed_checks_30d": sum(1 for row in rows if row["reason"]),
        "avg_scheduler_delay_ms": round(sum(delays) / len(delays), 2) if delays else 0,
        "max_scheduler_delay_ms": round(max(delays), 2) if delays else 0,
        "median_detection_seconds": percentile(detections, 0.5),
        "p95_detection_seconds": percentile(detections, 0.95),
        "median_recovery_seconds": percentile(recoveries, 0.5),
        "p95_recovery_seconds": percentile(recoveries, 0.95),
        "estimated_observation_coverage_percent": round(min(100.0, 100.0 * len(rows) * conf["interval"] / first_check_age), 1) if first_check_age > 0 else 0.0,
        "open_incident_duration_seconds": round((datetime.now(timezone.utc) - datetime.fromisoformat(open_incident["detected_at"])).total_seconds(), 1) if open_incident else None,
    }
