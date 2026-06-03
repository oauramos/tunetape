"""Persistent user settings (stored alongside listening history).

Currently just the volume-normalization toggle. Reads/writes are
best-effort and corruption-safe: a bad or missing file falls back to
defaults rather than crashing startup.
"""

import os

from tunetape import paths

_DEFAULTS = {
    # The user asked for even volume across sources, so normalization is on
    # by default; toggle it from the Settings menu.
    "normalize_volume": True,
    # Accent color for the whole interface; change it in Settings → Accent color.
    "accent_color": "cyan",
}


def _config_path() -> str:
    return os.path.join(paths.data_dir(), "config.json")


def _load() -> dict:
    data = paths.read_json(_config_path())
    return data if isinstance(data, dict) else {}


def get_setting(key: str):
    """Return a setting value, falling back to its default."""
    return _load().get(key, _DEFAULTS.get(key))


def set_setting(key: str, value) -> None:
    """Persist a single setting. Best-effort."""
    try:
        data = _load()
        data[key] = value
        paths.atomic_write_json(_config_path(), data)
    except OSError:
        pass
