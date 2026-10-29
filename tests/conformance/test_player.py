"""§3.4 Status and §4.2 player methods."""

import pytest

from .wire import add, first_item, status, wait_for, wait_idle, wait_playing


@pytest.fixture
def playing(conn, media):
    """A connection with one track playing; returns (conn, item_id)."""
    req = add(conn, media("playing.mp3"))
    wait_playing(conn, first_item(req))
    return conn, first_item(req)


def test_idle_status_shape(conn):
    s = status(conn)
    assert s["state"] == "idle"
    assert s["current"] is None
    assert s["index"] == -1
    assert s["length"] == 0
    assert s["has_next"] is False and s["has_prev"] is False
    assert 0 <= s["volume"] <= 130


def test_playing_status_shape(playing):
    conn, item_id = playing
    s = status(conn)
    assert s["state"] == "playing"
    assert s["current"]["id"] == item_id
    assert s["index"] == 0 and s["length"] == 1
    assert s["position"] >= 0


def test_status_with_levels_and_stats(playing):
    conn, _ = playing
    s = status(conn, levels=True, stats=True)
    assert "levels" in s
    assert "stats" in s


@pytest.mark.parametrize("method,params", [
    ("player.pause", None),
    ("player.resume", None),
    ("player.toggle", None),
    ("player.seek", {"seconds": 10}),
    ("player.mute", {"muted": True}),
    ("player.mute", None),
])
def test_nothing_playing_fails_1012(conn, method, params):
    conn.error(method, params, code=1012)


def test_stopped_counts_as_nothing_playing(playing):
    conn, _ = playing
    s = conn.result("player.stop")
    assert s["state"] == "idle"
    conn.error("player.pause", code=1012)


def test_stop_keeps_the_queue(playing, media):
    conn, item_id = playing
    add(conn, media("second.mp3"))
    s = conn.result("player.stop")
    assert s["state"] == "idle"
    assert s["length"] == 2
    assert wait_idle(conn)["length"] == 2


def test_pause_resume(playing):
    conn, _ = playing
    assert conn.result("player.pause")["state"] == "paused"
    assert status(conn)["state"] == "paused"
    assert conn.result("player.resume")["state"] == "playing"
    assert status(conn)["state"] == "playing"


def test_toggle(playing):
    conn, _ = playing
    assert conn.result("player.toggle")["state"] == "paused"
    assert conn.result("player.toggle")["state"] == "playing"


def test_seek_absolute_and_relative(playing):
    conn, _ = playing
    conn.result("player.seek", {"seconds": 10, "relative": False})
    wait_for(lambda: status(conn)["position"] >= 9.5, timeout=5, what="absolute seek to 10s")
    conn.result("player.seek", {"seconds": 5})  # relative is the default
    wait_for(lambda: status(conn)["position"] >= 14.5, timeout=5, what="relative seek +5s")
    conn.result("player.seek", {"seconds": -8, "relative": True})
    wait_for(lambda: status(conn)["position"] < 14, timeout=5, what="relative seek -8s")


@pytest.mark.parametrize("params", [{}, {"seconds": "ten"}, {"seconds": 5, "relative": "yes"}])
def test_seek_invalid_params(playing, params):
    conn, _ = playing
    conn.error("player.seek", params, code=-32602)


def test_volume_absolute_delta_and_clamp(playing):
    conn, _ = playing
    assert conn.result("player.volume", {"value": 50})["volume"] == pytest.approx(50)
    assert conn.result("player.volume", {"delta": 5})["volume"] == pytest.approx(55)
    assert conn.result("player.volume", {"delta": -15})["volume"] == pytest.approx(40)
    assert conn.result("player.volume", {"value": 500})["volume"] == pytest.approx(130)
    assert conn.result("player.volume", {"delta": 10})["volume"] == pytest.approx(130)
    assert conn.result("player.volume", {"value": -20})["volume"] == pytest.approx(0)
    assert status(conn)["volume"] == pytest.approx(0)


@pytest.mark.parametrize("params", [{}, {"value": "loud"}, {"delta": "up"}])
def test_volume_invalid_params(playing, params):
    conn, _ = playing
    conn.error("player.volume", params, code=-32602)


def test_volume_works_when_idle(conn):
    assert conn.result("player.volume", {"value": 33})["volume"] == pytest.approx(33)
    assert status(conn)["volume"] == pytest.approx(33)


def test_volume_is_persisted(daemon_factory):
    d = daemon_factory()
    conn = d.connect()
    conn.result("player.volume", {"value": 42})
    conn.result("daemon.shutdown")
    d.wait_exit(10)
    again = daemon_factory(data_dir=d.data_dir)
    assert status(again.connect())["volume"] == pytest.approx(42)


def test_mute_set_and_toggle(playing):
    conn, _ = playing
    assert conn.result("player.mute", {"muted": True})["muted"] is True
    assert conn.result("player.mute", {"muted": True})["muted"] is True
    assert conn.result("player.mute")["muted"] is False
    assert conn.result("player.mute", {})["muted"] is True
    assert status(conn)["muted"] is True


def test_next_prev_on_single_item_fail_1013(playing):
    conn, _ = playing
    conn.error("player.next", code=1013)
    conn.error("player.prev", code=1013)


def test_next_prev_walk_and_boundaries(conn, media):
    a = add(conn, media("a.mp3"))
    b = add(conn, media("b.mp3"))
    s = wait_playing(conn, first_item(a))
    assert s["has_prev"] is False and s["has_next"] is True
    conn.error("player.prev", code=1013)

    s = conn.result("player.next")
    assert s["index"] == 1
    s = wait_playing(conn, first_item(b))
    assert s["has_prev"] is True and s["has_next"] is False
    conn.error("player.next", code=1013)

    s = conn.result("player.prev")
    assert s["index"] == 0
    wait_playing(conn, first_item(a))
