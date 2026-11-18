"""Customer-safe notification formatting and durable webhook delivery."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import WEBHOOK_URL
from .database import connect, db_lock, init_db
from .state import stop_event
from .utils import now_iso


def notify_public(feature: str, status: str) -> bool:
    """Send one generic customer-facing message to the shared demo webhook."""
    # Keep copy audience-neutral: the provided receiver is shared by engineers,
    # support, and consumers, and has no separate client-specific destinations.
    if status == "DOWN":
        text = f"Service disruption: Some users may have trouble using {feature}. We’re investigating. Status: Investigating. Updated: {now_iso()}"
    elif status == "DEGRADED":
        text = f"Service update: Some users may experience delays using {feature}. We’re monitoring the situation. Status: Degraded. Updated: {now_iso()}"
    else:
        text = f"Service restored: {feature} is operating normally again. Updated: {now_iso()}"
    payload = json.dumps({"message": text}).encode()
    req = urllib.request.Request(WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Pulse-Notifier/1.0"}, method="POST")

    # Do not follow redirects with the POST body; this avoids forwarding content
    # to an unexpected host if the configured receiver redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, new_url):
            return None

    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=3) as response:
                response.read(1024)
            return True
        except Exception as exc:
            # Avoid logging exception details that might contain sensitive data.
            print(f"notification delivery failed (attempt {attempt + 1}, {type(exc).__name__})", flush=True)
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
    return False


def notification_worker() -> None:
    """Deliver pending persisted notifications and retry with capped backoff."""
    init_db()
    while not stop_event.is_set():
        # Claim one pending outbox row before network I/O. Network delivery happens
        # outside the database lock so it cannot stall probes or API requests.
        with db_lock, connect() as db:
            row = db.execute(
                "SELECT * FROM notification_outbox WHERE sent_at IS NULL AND next_attempt_at<=? ORDER BY id LIMIT 1",
                (now_iso(),),
            ).fetchone()
            if row is not None:
                db.execute("UPDATE notification_outbox SET attempts=attempts+1 WHERE id=?", (row["id"],))
        if row is None:
            stop_event.wait(0.5)
            continue

        delivered = notify_public(row["feature"], row["status"])
        # Mark success or schedule a capped retry; persisted rows survive restarts.
        with db_lock, connect() as db:
            if delivered:
                db.execute("UPDATE notification_outbox SET sent_at=? WHERE id=?", (now_iso(), row["id"]))
            else:
                delay = min(300, 2 ** min(row["attempts"], 8))
                next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
                db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE id=?", (next_attempt, row["id"]))
