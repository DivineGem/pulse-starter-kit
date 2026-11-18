"""SQLite connection management, schema, and persistence helpers."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH
from .utils import now_iso

# SQLite allows concurrent readers but only one writer. This process-wide lock
# serializes short transactions shared by probe and API threads.
db_lock = threading.RLock()


@contextmanager
def connect():
    """Yield one transaction-scoped connection and always commit/rollback/close it."""
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
        # WAL supports dashboard reads while probe workers append new samples.
        db.execute("PRAGMA journal_mode=WAL")
        # Tables are created idempotently, so a restart can safely initialize storage.
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
          kind TEXT NOT NULL, at TEXT NOT NULL, message TEXT NOT NULL, author TEXT NOT NULL,
          edited_at TEXT
        );
        CREATE TABLE IF NOT EXISTS operator_sessions (
          token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
          id INTEGER PRIMARY KEY, feature TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL, sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS notification_outbox_pending ON notification_outbox(sent_at, next_attempt_at);
        """)
        # Lightweight forward migration for databases created before control_mode
        # was added; this avoids deleting a user's persisted incident history.
        incident_columns = {row["name"] for row in db.execute("PRAGMA table_info(incidents)")}
        if "control_mode" not in incident_columns:
            db.execute("ALTER TABLE incidents ADD COLUMN control_mode TEXT")
        event_columns = {row["name"] for row in db.execute("PRAGMA table_info(incident_events)")}
        if "edited_at" not in event_columns:
            db.execute("ALTER TABLE incident_events ADD COLUMN edited_at TEXT")


def event(db: sqlite3.Connection, incident_id: int, kind: str, message: str, author: str = "Pulse") -> None:
    """Append a timeline item in the caller's existing incident transaction."""
    db.execute("INSERT INTO incident_events(incident_id, kind, at, message, author) VALUES (?, ?, ?, ?, ?)",
               (incident_id, kind, now_iso(), message, author))


def get_open_incident(db: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Return the newest active incident, if this service currently has one."""
    return db.execute("SELECT * FROM incidents WHERE service=? AND state='OPEN' ORDER BY id DESC LIMIT 1", (name,)).fetchone()
