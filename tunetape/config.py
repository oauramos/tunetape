"""Persistent user settings (stored alongside listening history).

Holds the volume-normalization toggle, accent color, and the last
playback volume. Reads/writes are best-effort and corruption-safe: a bad
or missing file falls back to defaults rather than crashing startup.
"""

import os

from tunetape import paths

# mpv's default --volume-max; software volume can exceed 100% up to this.
_VOLUME_MIN = 0.0
_VOLUME_MAX = 130.0

_DEFAULTS = {
    # The user asked for even volume across sources, so normalization is on
    # by default; toggle it from the Settings menu.
    "normalize_volume": True,
    # Accent color for the whole interface; change it in Settings → Accent color.
    "accent_color": "cyan",
    # Last playback volume (percent), restored on the next launch.
    "volume": 100,
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


def _clamp_volume(value) -> float:
    """Coerce ``value`` to a float volume in [_VOLUME_MIN, _VOLUME_MAX]."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(_DEFAULTS["volume"])
    return max(_VOLUME_MIN, min(v, _VOLUME_MAX))


def get_volume() -> float:
    """Return the saved playback volume (percent), clamped to a sane range.

    A missing or corrupt value falls back to the default rather than
    handing mpv something it would reject.
    """
    return _clamp_volume(get_setting("volume"))


def set_volume(value) -> None:
    """Persist the playback volume (percent), clamped and rounded. Best-effort.

    A non-numeric value is ignored rather than clobbering a good saved one.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    v = max(_VOLUME_MIN, min(v, _VOLUME_MAX))
    set_setting("volume", round(v))
