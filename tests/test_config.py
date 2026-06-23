import pytest

import tunetape.config as config


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the data dir (and thus config.json) at a fresh temp dir."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_volume_defaults_to_100_when_unset(isolated):
    assert config.get_volume() == 100.0


def test_volume_round_trips(isolated):
    config.set_volume(40)
    assert config.get_volume() == 40.0
    # Survives a fresh read from disk (no in-process caching).
    assert config.get_setting("volume") == 40


def test_volume_is_rounded_on_write(isolated):
    config.set_volume(42.7)
    assert config.get_setting("volume") == 43


def test_volume_clamped_to_range(isolated):
    config.set_volume(500)
    assert config.get_volume() == 130.0  # mpv's --volume-max
    config.set_volume(-20)
    assert config.get_volume() == 0.0


def test_corrupt_volume_falls_back_to_default(isolated):
    config.set_setting("volume", "loud")  # not a number
    assert config.get_volume() == 100.0


def test_set_volume_ignores_non_numeric(isolated):
    config.set_volume(55)
    config.set_volume("nope")  # best-effort: ignored, doesn't crash or clobber
    # A non-numeric write leaves the previously-saved value untouched.
    assert config.get_setting("volume") == 55


def test_volume_independent_of_other_settings(isolated):
    config.set_setting("accent_color", "magenta")
    config.set_volume(70)
    assert config.get_setting("accent_color") == "magenta"
    assert config.get_volume() == 70.0
