import os

import pytest

import tunetape.paths as paths


def test_data_dir_uses_absolute_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.data_dir() == os.path.join(str(tmp_path), "tunetape")


def test_data_dir_ignores_relative_xdg(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    # Relative value must be ignored (XDG spec) -> falls back to ~/.local/share.
    assert paths.data_dir().endswith(os.path.join(".local", "share", "tunetape"))
    assert os.path.isabs(paths.data_dir())


def test_data_dir_ignores_empty_xdg(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert paths.data_dir().endswith(os.path.join(".local", "share", "tunetape"))


def test_atomic_write_round_trip(tmp_path):
    p = os.path.join(str(tmp_path), "sub", "data.json")
    paths.atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert paths.read_json(p) == {"a": 1, "b": [1, 2, 3]}


def test_atomic_write_no_temp_leak_on_unserializable(tmp_path):
    p = os.path.join(str(tmp_path), "data.json")
    with pytest.raises(TypeError):
        paths.atomic_write_json(p, {"bad": {1, 2, 3}})  # a set is not JSON-serializable
    # No target file and no orphaned temp file left behind.
    assert not os.path.exists(p)
    assert not any(n.startswith(".tmp_") for n in os.listdir(str(tmp_path)))


def test_read_json_missing_and_corrupt(tmp_path):
    assert paths.read_json(os.path.join(str(tmp_path), "nope.json")) is None
    bad = os.path.join(str(tmp_path), "bad.json")
    with open(bad, "w") as f:
        f.write("{not json")
    assert paths.read_json(bad) is None
