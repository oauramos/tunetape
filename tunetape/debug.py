"""In-session debug log.

A tiny in-memory ring buffer of events and errors from the current run, surfaced
by the Debug/Logs screen (menu key ``d``). Nothing is written to disk — the log
lives only as long as the process. Capture is always on; it's cheap.
"""

from collections import deque
from datetime import datetime, timezone

_MAX = 500
_records = deque(maxlen=_MAX)
# Extra destinations for each record; the daemon adds its log file here so a
# background process with no Debug screen still leaves a trail on disk.
_sinks = []


def log(message: str, level: str = "INFO") -> None:
    """Append a record (timestamp, level, message) to the in-memory log."""
    record = (datetime.now(timezone.utc), str(level).upper(), str(message))
    _records.append(record)
    for sink in list(_sinks):
        try:
            sink(record)
        except Exception:
            pass  # a broken sink must never break the caller


def add_sink(fn) -> None:
    """Also hand every new record to ``fn(record)``."""
    _sinks.append(fn)


def remove_sink(fn) -> None:
    try:
        _sinks.remove(fn)
    except ValueError:
        pass


def exception(prefix: str, exc: BaseException) -> None:
    """Log an exception as an ERROR record, including its type and message."""
    log(f"{prefix}: {type(exc).__name__}: {exc}", "ERROR")


def entries() -> list:
    """Return a snapshot list of records, oldest first."""
    return list(_records)


def clear() -> None:
    """Drop all captured records."""
    _records.clear()
