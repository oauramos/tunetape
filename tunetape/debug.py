"""In-session debug log.

A tiny in-memory ring buffer of events and errors from the current run, surfaced
by the Debug/Logs screen (menu key ``d``). Nothing is written to disk — the log
lives only as long as the process. Capture is always on; it's cheap.
"""

from collections import deque
from datetime import datetime, timezone

_MAX = 500
_records = deque(maxlen=_MAX)


def log(message: str, level: str = "INFO") -> None:
    """Append a record (timestamp, level, message) to the in-memory log."""
    _records.append((datetime.now(timezone.utc), str(level).upper(), str(message)))


def exception(prefix: str, exc: BaseException) -> None:
    """Log an exception as an ERROR record, including its type and message."""
    log(f"{prefix}: {type(exc).__name__}: {exc}", "ERROR")


def entries() -> list:
    """Return a snapshot list of records, oldest first."""
    return list(_records)


def clear() -> None:
    """Drop all captured records."""
    _records.clear()
