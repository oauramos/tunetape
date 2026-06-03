import os

import pytest

import tunetape.history as history
import tunetape.paths as paths


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the data dir at a fresh temp dir for each test."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_empty_history_loads_as_list(isolated):
    assert history.load() == []


def test_record_and_load(isolated):
    history.record("youtube", "u1", "Title One")
    entries = history.load()
    assert len(entries) == 1
    assert entries[0]["url"] == "u1"
    assert entries[0]["title"] == "Title One"
    assert entries[0]["play_count"] == 1
    assert entries[0]["type"] == "youtube"
    assert "last_played" in entries[0]


def test_dedup_moves_to_front_and_bumps_play_count(isolated):
    history.record("youtube", "a", "A")
    history.record("youtube", "b", "B")
    history.record("youtube", "a", "A updated")
    entries = history.load()
    assert [e["url"] for e in entries] == ["a", "b"]
    assert entries[0]["play_count"] == 2
    assert entries[0]["title"] == "A updated"


def test_khinsider_fields(isolated):
    history.record("khinsider", "alb", "Album", track_count=10, last_index=3)
    entry = history.load()[0]
    assert entry["track_count"] == 10
    assert entry["last_index"] == 3


def test_set_last_index_does_not_bump_play_count(isolated):
    history.record("khinsider", "alb", "Album", track_count=5, last_index=0)
    history.set_last_index("alb", 4)
    entry = [e for e in history.load() if e["url"] == "alb"][0]
    assert entry["last_index"] == 4
    assert entry["play_count"] == 1


def test_cap_at_max_entries(isolated):
    for i in range(history.MAX_ENTRIES + 15):
        history.record("youtube", f"v{i}", f"t{i}")
    entries = history.load()
    assert len(entries) == history.MAX_ENTRIES
    # Most recent first.
    assert entries[0]["url"] == f"v{history.MAX_ENTRIES + 14}"


def test_remove(isolated):
    history.record("youtube", "x", "X")
    history.record("youtube", "y", "Y")
    history.remove("x")
    assert [e["url"] for e in history.load()] == ["y"]


def test_clear(isolated):
    history.record("youtube", "x", "X")
    history.clear()
    assert history.load() == []


def test_invalid_type_ignored(isolated):
    history.record("spotify", "z", "Z")
    assert history.load() == []


def test_empty_url_ignored(isolated):
    history.record("youtube", "", "no url")
    assert history.load() == []


def test_corrupt_file_returns_empty(isolated):
    paths.ensure_dir()
    with open(os.path.join(paths.data_dir(), "history.json"), "w") as f:
        f.write("{ this is not valid json")
    assert history.load() == []


def test_non_dict_json_returns_empty(isolated):
    paths.ensure_dir()
    with open(os.path.join(paths.data_dir(), "history.json"), "w") as f:
        f.write("[1, 2, 3]")
    assert history.load() == []


def test_corrupt_numeric_fields_are_healed(isolated):
    import json
    paths.ensure_dir()
    payload = {
        "version": 1,
        "entries": [
            {"type": "youtube", "url": "u", "title": "T", "play_count": "corrupt"},
            {"type": "khinsider", "url": "alb", "title": "A", "last_index": "nope",
             "track_count": 5, "play_count": None},
        ],
    }
    with open(os.path.join(paths.data_dir(), "history.json"), "w") as f:
        json.dump(payload, f)
    entries = history.load()
    yt = [e for e in entries if e["url"] == "u"][0]
    alb = [e for e in entries if e["url"] == "alb"][0]
    assert yt["play_count"] == 1
    assert alb["last_index"] == 0
    assert alb["play_count"] == 1
    # A corrupt existing entry must not block recording the same url again.
    history.record("youtube", "u", "T2")
    assert [e for e in history.load() if e["url"] == "u"][0]["play_count"] == 2
