"""``tunetape <command>``: output, --json and exit codes (docs/architecture.md)."""

import importlib.util
import json
import re
import time

import pytest

from conftest import needs_fake_mpv, pid_alive, run_tunetape
from tunetape import __version__, cli
from tunetape.client import read_discovery


@pytest.fixture
def tt(data_dir):
    """Run CLI commands against an isolated daemon; stop whatever they spawned."""
    yield run_tunetape
    info = read_discovery()
    run_tunetape("shutdown")
    if info:
        pid = info.get("pid")
        deadline = time.monotonic() + 10
        while pid and pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)


# -- pure helpers ---------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("90", (90.0, False)), ("+10", (10.0, True)), ("-10", (-10.0, True)),
    ("1:30", (90.0, False)), ("+1:30", (90.0, True)), ("1:02:03", (3723.0, False)),
    ("0.5", (0.5, False)),
])
def test_parse_seek(text, expected):
    assert cli.parse_seek(text) == expected


@pytest.mark.parametrize("text", ["abc", "1:99", "", "+"])
def test_parse_seek_rejects(text):
    with pytest.raises(cli._UsageError):
        cli.parse_seek(text)


def test_parse_volume():
    assert cli.parse_volume("70") == ("value", 70.0)
    assert cli.parse_volume("+5") == ("delta", 5.0)
    assert cli.parse_volume("-5") == ("delta", -5.0)
    with pytest.raises(cli._UsageError):
        cli.parse_volume("loud")


def test_format_status():
    item = {"title": "Corridors of Time", "input": "x",
            "collection": {"title": "Chrono Trigger OST", "index": 4, "count": 64}}
    line = cli.format_status({"state": "playing", "position": 83.4, "duration": 251.0,
                              "volume": 80.0, "muted": False, "current": item, "message": None})
    assert line == "▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%"
    assert cli.format_status({"state": "idle", "volume": 100, "current": None}) == \
        "■ Nothing playing  vol 100%"
    assert cli.format_status({"state": "loading", "message": "Loading track 5/64…",
                              "volume": 80, "current": item}).startswith("… Loading track 5/64…")
    paused = cli.format_status({"state": "paused", "position": 1, "duration": 0, "volume": 50,
                                "muted": True, "current": {"title": "T", "collection": None}})
    assert paused == "⏸ T  0:01  vol 50% (muted)"


# -- the command line, as a subprocess -----------------------------------------

def test_version(data_dir):
    r = run_tunetape("--version")
    assert r.returncode == 0 and r.stdout.strip() == f"tunetape {__version__}"


def test_usage_errors(tt):
    assert tt("frobnicate").returncode == 2
    assert tt("jump").returncode == 2
    r = tt("jump", "0")
    assert r.returncode == 2 and "start at 1" in r.stderr
    assert tt("seek", "abc").returncode == 2
    assert tt("volume", "loud").returncode == 2


def test_status_json_and_daemon_errors(tt):
    r = tt("status", "--json")
    assert r.returncode == 0, r.stderr
    status = json.loads(r.stdout)
    assert status["state"] == "idle" and status["index"] == -1
    assert tt("status").stdout.startswith("■ Nothing playing")

    r = tt("jump", "3")
    assert r.returncode == 1 and "no track 3" in r.stderr
    r = tt("play", "ftp://example.com/x.mp3")
    assert r.returncode == 1 and "Unsupported" in r.stderr
    r = tt("pause")
    assert r.returncode == 1 and "Nothing is playing" in r.stderr
    r = tt("queue")
    assert r.returncode == 0 and "empty" in r.stdout
    assert json.loads(tt("queue", "--json").stdout) == {"items": [], "index": -1}
    assert json.loads(tt("history", "--json").stdout) == {"entries": []}


@needs_fake_mpv
def test_play_add_queue(tt, audio):
    r = tt("play", audio("first-fake-dur-30.mp3"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Playing: first-fake-dur-30"
    r = tt("add", audio("second-fake-dur-30.mp3"))
    assert r.stdout.strip() == "Queued: second-fake-dur-30"
    r = tt("add", "daft", "punk", "--next", "--title", "Around the World")
    assert r.stdout.strip() == "Queued: Around the World"
    lines = tt("queue").stdout.splitlines()
    assert lines == ["▶ 1. first-fake-dur-30  0:30", "  2. Around the World",
                     "  3. second-fake-dur-30"]
    assert tt("volume", "40").stdout.strip() == "Volume 40%"
    assert tt("volume", "+5").stdout.strip() == "Volume 45%"
    assert tt("volume").stdout.strip() == "Volume 45%"
    assert tt("pause").stdout.startswith("⏸ first-fake-dur-30")
    assert tt("seek", "0:10").stdout.startswith("⏸ first-fake-dur-30  0:10")
    r = tt("jump", "3")  # 1-based: the third item
    assert r.returncode == 0 and "second-fake-dur-30" in r.stdout
    assert json.loads(tt("history", "--json").stdout)["entries"][0]["type"] == "file"
    assert re.match(r"^1\. (first|second)-fake-dur-30  \[file\]$",
                    tt("history").stdout.splitlines()[0])
    assert tt("clear").stdout.strip() == "Queue cleared"
    assert tt("queue").stdout.strip() == "The queue is empty."


@needs_fake_mpv
def test_play_reports_a_dead_link(tt, audio):
    r = tt("play", audio("fake-fail.mp3"))
    assert r.returncode == 1
    assert "Could not play" in r.stderr


def test_relative_paths_are_made_absolute(tmp_path, monkeypatch):
    (tmp_path / "song.mp3").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    assert cli._input_arg(["song.mp3"]) == str(tmp_path / "song.mp3")
    assert cli._input_arg(["daft", "punk"]) == "daft punk"


def test_unreachable_daemon_exits_3(tmp_path, monkeypatch, data_dir):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("TUNETAPE_DATA_DIR", str(blocker))  # the daemon can't start here
    r = run_tunetape("status")
    assert r.returncode == 3
    assert "daemon" in r.stderr


def test_shutdown_without_a_daemon(data_dir):
    r = run_tunetape("shutdown")
    assert r.returncode == 0 and r.stdout.strip() == "tunetape daemon is not running"
    assert read_discovery() is None  # and it did not start one


@pytest.mark.skipif(importlib.util.find_spec("tunetape.mcp_server") is not None,
                    reason="tunetape.mcp_server exists in this build")
def test_mcp_missing_module(data_dir):
    r = run_tunetape("mcp")
    assert r.returncode == 2 and "MCP server" in r.stderr
