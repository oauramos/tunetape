"""§4.3 and §4.5: daemon.info, daemon.log, history.list, ai.discover."""

import os


def test_daemon_info(daemon):
    conn = daemon.connect()
    info = conn.result("daemon.info")
    disc = daemon.discovery()
    assert info["protocol"] == 1
    assert info["pid"] == disc["pid"]
    assert info["clients"] >= 1
    assert os.path.samefile(info["data_dir"], daemon.data_dir)


def test_daemon_log(conn):
    entries = conn.result("daemon.log")["entries"]
    assert isinstance(entries, list)
    assert len(conn.result("daemon.log", {"limit": 1})["entries"]) <= 1


def test_history_starts_empty(conn):
    assert conn.result("history.list")["entries"] == []


def test_ai_discover_not_configured_fails_1020(conn):
    # The suite strips ANTHROPIC_API_KEY / OPENAI_API_KEY and the data dir is fresh.
    err = conn.error("ai.discover", {"request": "rainy day jazz"}, code=1020)
    assert err["message"]

