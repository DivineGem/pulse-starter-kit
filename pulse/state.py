"""In-memory, lock-protected current state and worker lifecycle signals."""
import threading
import uuid
from typing import Any

from .config import SERVICES

# Probe workers and HTTP handlers share this lock when reading or mutating the
# in-memory snapshot. Reentrant locking permits helper calls under a lock.
state_lock = threading.RLock()
# Shutdown signal is shared by schedulers and the durable notification worker.
stop_event = threading.Event()
# This cache is for current status only; check history and incidents are persisted
# in SQLite. Monotonic timestamps are process-local and used only to detect stale workers.
runtime: dict[str, dict[str, Any]] = {
    name: {"status": "UNKNOWN", "last_checked": None, "latency_ms": None,
           "last_success": None, "consecutive_failures": 0, "consecutive_successes": 0,
           "last_error": None, "last_control_state": None, "last_scheduled": None,
           "scheduler_delay_ms": 0.0, "pulse_error": None, "last_check_monotonic": None}
    for name in SERVICES
}
# Lets the dashboard distinguish a fresh Pulse process from data persisted earlier.
BOOT_ID = str(uuid.uuid4())
