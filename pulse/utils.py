"""Shared stateless helpers."""
from datetime import datetime, timezone


def now_iso() -> str:
    """Return a timezone-aware UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def percentile(values: list[float], quantile: float) -> float | None:
    """Return a nearest-rank percentile; empty samples have no meaningful value."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))], 1)
