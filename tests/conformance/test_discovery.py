"""§1.2 endpoint discovery and §1.4 lifecycle: daemon.json, the socket, single instance."""

import json
import os
import shutil
import signal
import socket
import stat

import pytest

from .wire import WINDOWS, Conn, default_daemon_cmd, short_temp_dir, using_default_daemon, wait_for

posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX file modes and unix sockets")


def test_discovery_file_matches_schema(daemon, schema):
    disc = daemon.discovery()
    schema.check("/$defs/Discovery", disc, "daemon.json")
    assert disc["protocol"] == 1
    assert disc["transport"] == ("tcp" if WINDOWS else "unix")


def test_discovery_agrees_with_hello_and_info(daemon):
    disc = daemon.discovery()
    c = daemon.connect(hello=False)
    hello = c.hello()["result"]
    assert hello["protocol"] == 1
    assert hello["pid"] == disc["pid"]
    info = c.result("daemon.info")
    assert info["pid"] == disc["pid"]
    assert info["version"] == disc["version"]
    assert info["started_at"] == disc["started_at"]
    if using_default_daemon():
        assert disc["pid"] == daemon.proc.pid


@posix_only
def test_discovery_file_is_private(daemon):
    mode = stat.S_IMODE(os.stat(daemon.discovery_path).st_mode)
    assert mode == 0o600, oct(mode)


@posix_only
def test_socket_lives_in_short_data_dir_and_is_private(daemon):
    disc = daemon.discovery()
    # The suite's data dir is short, so the daemon must use <data_dir>/daemon.sock.
    assert os.path.samefile(os.path.dirname(disc["path"]), daemon.data_dir)
    assert os.path.basename(disc["path"]) == "daemon.sock"
    st = os.stat(disc["path"])
    assert stat.S_ISSOCK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600, oct(stat.S_IMODE(st.st_mode))


@pytest.mark.skipif(not WINDOWS, reason="tcp transport is Windows-only")
def test_tcp_binds_loopback_only(daemon):
    disc = daemon.discovery()
    assert disc["host"] == "127.0.0.1"
    assert len(disc["token"]) == 43


def test_daemon_writes_its_log(daemon):
    assert os.path.isfile(os.path.join(daemon.data_dir, "daemon.log"))


def test_shutdown_removes_socket_and_discovery_file(daemon):
    disc = daemon.discovery()
    c = daemon.connect()
    assert isinstance(c.result("daemon.shutdown"), dict)
    assert daemon.wait_exit(10) == 0
    assert not os.path.exists(daemon.discovery_path)
    if disc["transport"] == "unix":
        assert not os.path.exists(disc["path"])


@posix_only
def test_sigterm_is_a_clean_exit(daemon):
    disc = daemon.discovery()
    daemon.proc.send_signal(signal.SIGTERM)
    daemon.wait_exit(10)
    assert not os.path.exists(daemon.discovery_path)
    assert not os.path.exists(disc["path"])


def test_single_instance(daemon, daemon_factory):
    first = daemon.discovery()
    second = daemon_factory(data_dir=daemon.data_dir, wait=False)
    proc = second.spawn()
    try:
        assert proc.wait(timeout=15) == 0, "a second daemon on the same data dir must exit 0"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert daemon.discovery()["pid"] == first["pid"]
    assert daemon.proc.poll() is None
    c = daemon.connect()
    assert c.result("daemon.info")["pid"] == first["pid"]


def test_stale_discovery_file_is_replaced(daemon_factory, schema):
    data_dir = short_temp_dir("ttc-")
    try:
        stale = {"protocol": 1, "version": "0.0.0", "pid": 999999, "started_at": "2026-01-01T00:00:00Z"}
        if WINDOWS:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()  # nothing listens there any more
            stale.update(transport="tcp", host="127.0.0.1", port=port, token="A" * 43)
        else:
            sock_path = os.path.join(data_dir, "daemon.sock")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(sock_path)
            s.close()  # leaves a dead socket file behind
            stale.update(transport="unix", path=sock_path)
        with open(os.path.join(data_dir, "daemon.json"), "w", encoding="utf-8") as f:
            json.dump(stale, f)

        d = daemon_factory(data_dir=data_dir)
        disc = d.discovery()
        assert disc["pid"] != 999999
        c = d.connect()
        assert c.result("daemon.info")["pid"] == disc["pid"]
        d.stop()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.mark.skipif(not using_default_daemon(), reason="needs the default command to pass --idle-timeout")
def test_idle_timeout_exits_cleanly(daemon_factory):
    cmd = default_daemon_cmd()
    cmd[cmd.index("--idle-timeout") + 1] = "1"
    d = daemon_factory(cmd=cmd)
    disc = d.discovery()
    d.wait_exit(15)
    assert not os.path.exists(d.discovery_path)
    if disc["transport"] == "unix":
        assert not os.path.exists(disc["path"])


def test_connected_client_keeps_idle_daemon_alive_and_is_counted(daemon):
    a = daemon.connect()
    b = daemon.connect()
    assert a.result("daemon.info")["clients"] >= 2
    b.close()
    wait_for(lambda: a.result("daemon.info")["clients"] < 2 or None, timeout=5,
             what="clients to drop after a disconnect")


def test_refuses_connections_after_shutdown(daemon, schema):
    disc = daemon.discovery()
    daemon.connect().result("daemon.shutdown")
    daemon.wait_exit(10)
    with pytest.raises(OSError):
        Conn(disc, schema, timeout=2).hello()
