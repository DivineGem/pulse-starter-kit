"""Validated application configuration and monitored service definitions."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
# The database path is mounted as a named volume in Compose so incident data
# survives container rebuilds and restarts.
DB_PATH = os.environ.get("PULSE_DB", "/data/pulse.sqlite3")
USERNAME = os.environ.get("OPERATOR_USERNAME", "operator")
PASSWORD = os.environ.get("OPERATOR_PASSWORD", "pulse-local-change-me")
# Multiple operators can be configured as JSON so incident notes can be
# attributed to the person who added them. An empty value selects single-user mode.
try:
    OPERATOR_USERS = json.loads(os.environ.get("OPERATOR_USERS") or "{}")
except ValueError as exc:
    raise RuntimeError("OPERATOR_USERS must be a JSON object of usernames to passwords") from exc
if not isinstance(OPERATOR_USERS, dict) or any(
    not isinstance(k, str) or not isinstance(v, str) or len(v) < 12
    for k, v in OPERATOR_USERS.items()
):
    raise RuntimeError("OPERATOR_USERS must map usernames to passwords of at least 12 characters")
if not OPERATOR_USERS:
    OPERATOR_USERS = {USERNAME: PASSWORD}
if len(PASSWORD) < 12 and os.environ.get("OPERATOR_USERS") is None:
    raise RuntimeError("OPERATOR_PASSWORD must be at least 12 characters")

# Secure cookies are opt-in for local HTTP; enable this behind HTTPS in production.
COOKIE_SECURE = os.environ.get("OPERATOR_COOKIE_SECURE", "false").lower() == "true"

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://webhook-receiver:9000/pulse")


def positive_number(name: str, default: str, minimum: float = 0.1, maximum: float = 120.0) -> float:
    """Parse a bounded numeric setting and fail early on unsafe values."""
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


TIMEOUT_SECONDS = positive_number("PROBE_TIMEOUT_SECONDS", "4", 0.2, 30.0)
FAILURE_THRESHOLD = int(positive_number("FAILURE_THRESHOLD", "2", 1, 10))
RECOVERY_THRESHOLD = int(positive_number("RECOVERY_THRESHOLD", "2", 1, 10))
SLOW_THRESHOLD_SECONDS = positive_number("SLOW_THRESHOLD_SECONDS", "1.5", 0.2, 60.0)
SESSION_TTL_SECONDS = positive_number("OPERATOR_SESSION_TTL_SECONDS", "43200", 300, 604800)

SERVICES: dict[str, dict[str, Any]] = {
    # Probe cadences are independent; ordering here also defines dashboard order.
    # Keep internal endpoints separate from user-facing product labels.
    "payments": {"url": "http://payments:8080/health", "state_url": "http://payments:8080/control/state", "interval": 10, "target": 99.9, "label": "Payments", "feature": "Payments", "public_name": "Making a payment"},
    "claims": {"url": "http://claims:8080/health", "state_url": "http://claims:8080/control/state", "interval": 15, "target": 99.9, "label": "Claims", "feature": "Claims", "public_name": "Managing a claim"},
    "quotes": {"url": "http://quotes:8080/health", "state_url": "http://quotes:8080/control/state", "interval": 30, "target": 99.0, "label": "Quotes", "feature": "Quotes", "public_name": "Getting a quote"},
    "notifications": {"url": "http://notifications:8080/health", "state_url": "http://notifications:8080/control/state", "interval": 60, "target": 99.0, "label": "Notifications", "feature": "Notifications", "public_name": "Receiving updates"},
}
