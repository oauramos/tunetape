"""The daemon end to end: a real ``tunetape daemon`` subprocess and the Client.

Every test runs against its own TUNETAPE_DATA_DIR, so it never meets the
user's daemon, and every daemon started here is stopped (or killed) after.
"""

import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from conftest import REPO, needs_fake_mpv, pid_alive, posix_only
from tunetape import daemon as daemon_mod
from tunetape.client import (Client, DaemonError, DaemonTimeout, DaemonUnavailable,
                             discovery_path, open_endpoint, read_discovery)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (REPO, env.get("PYTHONPATH")) if p)
    return env


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


class Daemon:
    def __init__(self, *args):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tunetape", "daemon", *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_env())
        self.info = wait_for(self._ready)

    def _ready(self):
        if self.proc.poll() is not None:
            raise AssertionError(f"daemon exited early: {self.proc.stdout.read()}")
        info = read_discovery()
        if info and info.get("pid") == self.proc.pid and daemon_mod.probe(info):
            return info
        return None

    def stop(self):
        if self.proc.poll() is None:
            try:
                with Client.connect(spawn=False, timeout=2) as c:
                    c.shutdown()
                self.proc.wait(10)
            except (DaemonError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(5)
        if self.proc.stdout:
            self.proc.stdout.close()


@pytest.fixture
def daemon(data_dir):
    d = Daemon("--idle-timeout", "0")
    yield d
    d.stop()


@pytest.fixture
def tcp_daemon(data_dir, monkeypatch):
    monkeypatch.setenv("TUNETAPE_DAEMON_TRANSPORT", "tcp")
    d = Daemon("--idle-timeout", "0")
    yield d
    d.stop()


class Raw:
    """A bare protocol connection, for what the Client never sends."""

    def __init__(self, info):
        self.sock = open_endpoint(info, 5)
        self.sock.settimeout(5)
        self.buf = b""

    def send(self, msg):
        data = msg if isinstance(msg, bytes) else (json.dumps(msg) + "\n").encode()
        self.sock.sendall(data)

    def recv(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, method, params=None, id=1):
        self.send({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})
        return self.recv()

    def hello(self, token=None, protocol=1):
        return self.call("hello", {"protocol": protocol, "client": "test", "token": token}, id=0)

    def close(self):
        self.sock.close()


# -- files and handshake -------------------------------------------------------

def test_discovery_file_and_handshake(daemon, data_dir):
    info = daemon.info
    assert info["protocol"] == 1 and info["pid"] == daemon.proc.pid
    assert info["started_at"].endswith("Z")
    if sys.platform != "win32":
        assert info["transport"] == "unix"
        assert stat.S_IMODE(os.stat(discovery_path()).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(info["path"]).st_mode) == 0o600
    with Client.connect(spawn=False) as c:
        assert c.hello["protocol"] == 1 and c.hello["pid"] == daemon.proc.pid
        assert c.hello["server"].startswith("tunetape/")
        info = c.info()
        assert info["clients"] == 1 and info["data_dir"] == str(data_dir)
        assert c.status()["state"] == "idle"
        assert any("listening" in e["message"].lower() for e in c.log()["entries"])
    assert (data_dir / "daemon.log").read_text().strip()


def test_first_request_must_be_hello(daemon):
    raw = Raw(daemon.info)
    reply = raw.call("player.status")
    assert reply["error"]["code"] == 1001
    assert raw.recv() is None  # and the daemon hangs up
    raw.close()


def test_unsupported_protocol(daemon):
    raw = Raw(daemon.info)
    reply = raw.hello(protocol=99)
    assert reply["error"]["code"] == 1002
    assert reply["error"]["data"] == {"supported": [1]}
    assert raw.hello()["result"]["protocol"] == 1  # may retry on the same connection
    raw.close()


def test_errors_on_an_authenticated_connection(daemon):
    raw = Raw(daemon.info)
    raw.hello()
    assert raw.call("no.such.method")["error"]["code"] == -32601
    assert raw.call("queue.jump", {"index": "two"})["error"]["code"] == -32602
    assert raw.call("queue.jump", {"index": 3})["error"]["code"] == 1013
    assert raw.call("player.pause")["error"]["code"] == 1012
    assert raw.call("queue.add", {"input": "lavfi:sine"})["error"]["code"] == 1010
    assert raw.call("queue.add", {})["error"]["code"] == -32602
    raw.send({"jsonrpc": "2.0", "id": 7, "method": "queue.list", "params": [1]})
    assert raw.recv()["error"]["code"] == -32602
    raw.send({"jsonrpc": "1.0", "id": 8, "method": "queue.list"})
    reply = raw.recv()
    assert reply["error"]["code"] == -32600 and reply["id"] == 8
    # A string id is echoed back as is.
    raw.send({"jsonrpc": "2.0", "id": "abc", "method": "queue.list"})
    assert raw.recv() == {"jsonrpc": "2.0", "id": "abc", "result": {"items": [], "index": -1}}
    raw.send(b"{not json\n")
    reply = raw.recv()
    assert reply["error"]["code"] == -32700 and reply["id"] is None
    assert raw.recv() is None
    raw.close()


def test_client_raises_daemon_error(daemon):
    with Client.connect(spawn=False) as c:
        with pytest.raises(DaemonError) as e:
            c.jump(5)
        assert e.value.code == 1013 and "track 6" in e.value.message
        with pytest.raises(DaemonError) as e:
            c.call("nope")
        assert e.value.code == -32601


def test_tcp_token(tcp_daemon):
    info = tcp_daemon.info
    assert info["transport"] == "tcp" and info["host"] == "127.0.0.1"
    assert len(info["token"]) == 43
    raw = Raw(info)
    assert raw.hello(token="wrong")["error"]["code"] == 1001
    assert raw.recv() is None
    raw.close()
    raw = Raw(info)
    assert raw.hello()["error"]["code"] == 1001  # missing token
    raw.close()
    with Client.connect(spawn=False) as c:  # reads the token from the file
        assert c.status()["state"] == "idle"


# -- events and playback ------------------------------------------------------

@needs_fake_mpv
def test_events_and_playback(daemon, audio):
    got = []
    seen = threading.Event()

    def on_event(type_, data):
        got.append((type_, data))
        if type_ == "state" and data["state"] == "playing":
            seen.set()

    with Client.connect(spawn=False) as c:
        c.subscribe(on_event)
        result = c.play(audio("song-fake-dur-30.mp3"), wait=True)
        assert result["request"]["status"] == "done"
        assert seen.wait(10)
        types = {t for t, _ in got}
        assert {"request", "queue", "track", "state"} <= types
        s = c.status(levels=True, stats=True)
        assert s["state"] == "playing" and s["current"]["kind"] == "file"
        assert s["levels"] is None or [-200.0, -200.0] in s["levels"]
        assert c.pause()["state"] == "paused"
        assert c.volume(value=42)["volume"] == 42
        assert c.queue()["index"] == 0
        assert c.requests()["requests"][0]["id"] == result["request"]["id"]
        assert c.history(limit=1)["entries"][0]["type"] == "file"


def test_subscribe_filters_types(daemon):
    with Client.connect(spawn=False) as c:
        reply = c.call("events.subscribe", {"types": ["state", "bogus"]})
        assert reply == {"subscribed": ["state"]}
        assert c.call("events.subscribe")["subscribed"] == list(daemon_mod.EVENT_TYPES)


def test_ai_discover_not_configured(daemon):
    with Client.connect(spawn=False) as c:
        with pytest.raises(DaemonError) as e:
            c.discover("rainy jazz")
        assert e.value.code == 1020


# -- lifecycle ----------------------------------------------------------------

def test_shutdown_cleans_up(daemon):
    info = daemon.info
    events = []
    with Client.connect(spawn=False) as c:
        c.subscribe(lambda t, d: events.append(t), ["shutdown"])
        assert c.shutdown() == {}
        assert daemon.proc.wait(10) == 0
        wait_for(lambda: "shutdown" in events, 2)
        with pytest.raises(DaemonUnavailable):
            c.status()
    assert not os.path.exists(discovery_path())
    if info["transport"] == "unix":
        assert not os.path.exists(info["path"])


@posix_only
def test_sigterm_cleans_up(daemon):
    info = daemon.info
    daemon.proc.send_signal(signal.SIGTERM)
    assert daemon.proc.wait(10) == 0
    assert not os.path.exists(discovery_path())
    assert not os.path.exists(info["path"])


def test_idle_exit(data_dir):
    d = Daemon("--idle-timeout", "0.5")
    try:
        assert d.proc.wait(10) == 0
        assert not os.path.exists(discovery_path())
    finally:
        d.stop()


def test_a_connected_client_keeps_it_alive(data_dir):
    d = Daemon("--idle-timeout", "0.5")
    try:
        with Client.connect(spawn=False) as c:
            time.sleep(1.2)
            assert c.status()["state"] == "idle"
        assert d.proc.wait(10) == 0
    finally:
        d.stop()


def test_single_instance(daemon):
    second = subprocess.run([sys.executable, "-m", "tunetape", "daemon"],
                            capture_output=True, text=True, env=_env(), timeout=30)
    assert second.returncode == 0
    assert "already running" in second.stdout
    assert read_discovery()["pid"] == daemon.proc.pid
    assert daemon_mod.probe(read_discovery())


def test_stale_discovery_file_is_replaced(data_dir):
    os.makedirs(str(data_dir), exist_ok=True)
    stale = {"protocol": 1, "version": "0", "pid": 999999, "started_at": "2026-01-01T00:00:00Z"}
    stale_sock = sock_dir = None
    if sys.platform == "win32":
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        stale.update(transport="tcp", host="127.0.0.1", port=s.getsockname()[1], token="x")
        s.close()  # nothing listens there any more
    else:
        sock_dir = tempfile.mkdtemp(prefix="tt", dir="/tmp")  # short enough for sun_path
        stale_sock = os.path.join(sock_dir, "old.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(stale_sock)  # a socket file nobody listens on
        s.close()
        stale.update(transport="unix", path=stale_sock)
    with open(discovery_path(), "w") as f:
        json.dump(stale, f)
    d = Daemon("--idle-timeout", "0")
    try:
        assert d.info["pid"] == d.proc.pid
        if stale_sock:
            assert not os.path.exists(stale_sock)
        with Client.connect(spawn=False) as c:
            assert c.status()["state"] == "idle"
    finally:
        d.stop()
        if sock_dir:
            shutil.rmtree(sock_dir, ignore_errors=True)


def test_connect_without_a_daemon(data_dir):
    with pytest.raises(DaemonUnavailable) as e:
        Client.connect(spawn=False)
    assert e.value.code == 0


def test_spawn_on_demand(data_dir):
    with Client.connect(timeout=20) as c:
        pid = c.hello["pid"]
        assert c.status()["state"] == "idle"
        c.shutdown()
    wait_for(lambda: not pid_alive(pid), 10)
    assert not os.path.exists(discovery_path())


def test_parallel_spawns_end_up_with_one_daemon(data_dir):
    clients, errors = [], []

    def go():
        try:
            clients.append(Client.connect(timeout=20))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert not errors
        assert len({c.hello["pid"] for c in clients}) == 1
    finally:
        pid = clients[0].hello["pid"] if clients else None
        if clients:
            clients[0].shutdown()
        for c in clients:
            c.close()
        if pid:
            wait_for(lambda: not pid_alive(pid), 10)
        _stop_late_losers()


def _stop_late_losers(settle=3.0):
    """Shut down a spawned loser that got the lock after the winner exited.

    A loser that never saw the winner answer (the winner lived only a moment)
    takes over once the lock is free, as it would after a real shutdown; it
    would then idle out after 600 s. Don't leave it running past the test.
    """
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        try:
            with Client.connect(spawn=False, timeout=2) as c:
                late = c.hello["pid"]
                c.shutdown()
            wait_for(lambda: not pid_alive(late), 10)
        except DaemonError:
            pass
        time.sleep(0.1)


def test_call_timeout(data_dir):
    """A daemon that answers hello and then goes quiet -> DaemonTimeout (-1)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    os.makedirs(str(data_dir), exist_ok=True)
    with open(discovery_path(), "w") as f:
        json.dump({"protocol": 1, "pid": 1, "transport": "tcp", "host": "127.0.0.1",
                   "port": server.getsockname()[1], "token": "t"}, f)

    def serve():
        conn, _ = server.accept()
        line = conn.makefile("rb").readline()
        request = json.loads(line)
        conn.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocol": 1, "server": "fake", "pid": 1}}) + "\n").encode())
        time.sleep(2)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    with Client.connect(spawn=False) as c:
        with pytest.raises(DaemonTimeout) as e:
            c.status(timeout=0.3)
        assert e.value.code == -1 and isinstance(e.value, DaemonError)
    server.close()


def test_socket_path_rules(tmp_path):
    if not hasattr(os, "getuid"):
        pytest.skip("Unix sockets only")
    short = "/tmp/tt"
    assert daemon_mod.socket_path(short) == "/tmp/tt/daemon.sock"
    long_dir = "/tmp/" + "x" * 120
    a = daemon_mod.socket_path(long_dir)
    b = daemon_mod.socket_path(long_dir + "y")
    assert len(a.encode()) < 100 and a != b
    assert os.path.basename(os.path.dirname(a)) == f"tunetape-{os.getuid()}"
    digest = hashlib.sha256(long_dir.encode()).hexdigest()[:12]
    assert os.path.basename(a) == f"{digest}.sock"
