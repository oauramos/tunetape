"""Filesystem locations and JSON helpers for tunetape's persistent state.

Data lives in an XDG-style data dir OUTSIDE ~/.tunetape (the install
checkout), so listening history and settings survive both updates and
uninstall.
"""

import json
import os
import tempfile


def data_dir() -> str:
    """Return tunetape's data directory, honoring $XDG_DATA_HOME.

    Per the XDG spec, a relative (or empty) XDG_DATA_HOME is ignored so the
    data dir stays stable regardless of the process's working directory.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "tunetape")


def ensure_dir() -> str:
    """Create the data directory if needed and return its path."""
    path = data_dir()
    os.makedirs(path, exist_ok=True)
    return path


def read_json(path: str):
    """Read JSON from ``path``; return None if missing, unreadable, or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None


def atomic_write_json(path: str, data) -> None:
    """Atomically write ``data`` as JSON to ``path`` (write-temp + os.replace).

    Raises OSError on failure; callers decide whether to swallow it.
    """
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    # Serialize first: a non-JSON-serializable value fails here, before any
    # temp file exists, so it can't leak an orphan .tmp_ file.
    text = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
