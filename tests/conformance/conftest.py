"""Fixtures for the protocol conformance suite (see protocol/README.md).

Every test here starts a real daemon process in a temp TUNETAPE_DATA_DIR with
the fake mpv, and talks raw NDJSON to it. The daemon under test is
$TUNETAPE_CONFORMANCE_DAEMON (split shell-style), by default
``<python> -m tunetape daemon --idle-timeout 0``.

The suite skips as a whole, with the reason, when it cannot run: no
jsonschema, no tests/fakes/fake_mpv.py, or no tunetape.daemon to test (and no
other daemon named in $TUNETAPE_CONFORMANCE_DAEMON).
"""

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from .wire import FAKE_MPV, WINDOWS, Daemon, Schema, short_temp_dir, using_default_daemon

HERE = Path(__file__).resolve().parent


def _skip_reason():
    if importlib.util.find_spec("jsonschema") is None:
        return "conformance suite needs jsonschema (pip install -e '.[test]')"
    if not FAKE_MPV.is_file():
        return "conformance suite needs tests/fakes/fake_mpv.py, which is not on this branch yet"
    if using_default_daemon():
        try:
            found = importlib.util.find_spec("tunetape.daemon") is not None
        except ImportError:
            found = False
        if not found:
            return ("tunetape.daemon is not on this branch yet; set TUNETAPE_CONFORMANCE_DAEMON "
                    "to run the suite against another daemon")
    return None


def _playback_skip_reason():
    # fake_mpv.py serves its IPC on a named pipe on Windows, which has not been
    # exercised there yet; the transport, handshake and framing tests still run.
    if WINDOWS and os.environ.get("TUNETAPE_CONFORMANCE_PLAYBACK") != "1":
        return ("playback through fake_mpv.py is unverified on Windows; "
                "set TUNETAPE_CONFORMANCE_PLAYBACK=1 to run it")
    return None


def pytest_collection_modifyitems(config, items):
    reason = _skip_reason()
    playback = _playback_skip_reason()
    if reason is None and playback is None:
        return
    for item in items:
        if HERE not in Path(str(item.fspath)).resolve().parents:
            continue
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))
        elif "media" in item.fixturenames:
            item.add_marker(pytest.mark.skip(reason=playback))


@pytest.fixture(scope="session")
def schema():
    return Schema()


@pytest.fixture
def daemon_factory(schema):
    """``start(fake_duration=30, data_dir=None, cmd=None, env=None, wait=True)`` -> Daemon."""
    started = []
    dirs = []

    def start(fake_duration=30, data_dir=None, cmd=None, env=None, wait=True):
        if data_dir is None:
            data_dir = short_temp_dir("ttc-")
            dirs.append(data_dir)
        d = Daemon(schema, data_dir, cmd=cmd, fake_duration=fake_duration, env=env)
        started.append(d)
        if wait:
            d.start()
        return d

    yield start
    for d in reversed(started):
        d.stop()
    for path in dirs:
        shutil.rmtree(path, ignore_errors=True)
        try:
            os.unlink(path + ".out")
        except OSError:
            pass


@pytest.fixture
def daemon(daemon_factory):
    return daemon_factory()


@pytest.fixture
def conn(daemon):
    return daemon.connect()


@pytest.fixture
def media(tmp_path):
    """``media(name)`` -> absolute path of a small local "audio" file."""
    folder = tmp_path / "media"
    folder.mkdir()

    def make(name="track.mp3"):
        path = folder / name
        path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 512)
        return str(path)

    return make
