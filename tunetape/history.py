"""Persistent listening history ("Recently played").

Each entry is keyed by the ORIGINAL pasted URL (YouTube watch URL or
KHInsider album page URL) — never the resolved stream/CDN URL, which is
signed and expires. Replay re-resolves the original URL.

All writes are best-effort: a persistence failure must never break
playback, so the public functions swallow their own errors. Writes are
last-writer-wins and assume a single tunetape instance; two instances
running at once may clobber each other's most recent update (acceptable
for a single-user terminal tool).
"""

from datetime import datetime, timezone

import os

from tunetape import paths

MAX_ENTRIES = 50
_VERSION = 1
VALID_TYPES = ("youtube", "khinsider")


def _history_path() -> str:
    return os.path.join(paths.data_dir(), "history.json")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load() -> list:
    """Return history entries, most-recent first. [] if missing/corrupt.

    Numeric fields are healed on read so a hand-edited or partially-written
    value can't poison record()/replay.
    """
    data = paths.read_json(_history_path())
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    healed = []
    for e in entries:
        if not (isinstance(e, dict) and e.get("url")):
            continue
        e["play_count"] = _as_int(e.get("play_count", 1), 1)
        if e.get("type") == "khinsider":
            e["last_index"] = _as_int(e.get("last_index", 0), 0)
        healed.append(e)
    return healed


def _save(entries: list) -> None:
    try:
        payload = {"version": _VERSION, "entries": entries[:MAX_ENTRIES]}
        paths.atomic_write_json(_history_path(), payload)
    except OSError:
        pass


def record(entry_type: str, url: str, title: str, *, track_count=None, last_index=0) -> None:
    """Record a played item; dedup by url (move to front), bump play_count."""
    try:
        if entry_type not in VALID_TYPES or not url:
            return
        play_count = 1
        kept = []
        for e in load():
            if e.get("url") == url:
                play_count = int(e.get("play_count", 1) or 1) + 1
            else:
                kept.append(e)
        entry = {
            "type": entry_type,
            "url": url,
            "title": title or url,
            "last_played": datetime.now(timezone.utc).isoformat(),
            "play_count": play_count,
        }
        if entry_type == "khinsider":
            entry["track_count"] = track_count
            entry["last_index"] = int(last_index or 0)
        kept.insert(0, entry)
        _save(kept)
    except Exception:
        pass


def set_last_index(url: str, last_index: int) -> None:
    """Update a KHInsider entry's resume position without bumping play_count."""
    try:
        entries = load()
        changed = False
        for e in entries:
            if e.get("url") == url and e.get("type") == "khinsider":
                e["last_index"] = int(last_index or 0)
                e["last_played"] = datetime.now(timezone.utc).isoformat()
                changed = True
                break
        if changed:
            _save(entries)
    except Exception:
        pass


def remove(url: str) -> None:
    """Remove a single entry by url. Best-effort."""
    try:
        _save([e for e in load() if e.get("url") != url])
    except Exception:
        pass


def clear() -> None:
    """Remove all history. Best-effort."""
    try:
        _save([])
    except Exception:
        pass
