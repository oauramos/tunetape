"""§2 handshake: hello first, protocol negotiation, the tcp token."""

import pytest


def test_hello_result(daemon):
    c = daemon.connect(hello=False)
    msg = c.hello()
    assert "result" in msg, msg
    res = msg["result"]
    assert res["protocol"] == 1
    assert res["server"]
    assert res["pid"] == daemon.discovery()["pid"]


def test_hello_echoes_string_ids(daemon):
    c = daemon.connect(hello=False)
    params = {"protocol": 1, "client": "conformance"}
    if daemon.discovery()["transport"] == "tcp":
        params["token"] = daemon.discovery()["token"]
    msg = c.call("hello", params, id="hello-1")
    assert msg["id"] == "hello-1"
    assert "result" in msg


@pytest.mark.parametrize("method", ["player.status", "queue.list", "daemon.info"])
def test_method_before_hello_fails_1001_and_closes(daemon, method):
    c = daemon.connect(hello=False)
    err = c.error(method, code=1001)
    assert err["message"]
    assert c.closed_by_daemon(5), "the daemon must close the connection after 1001"


def test_unknown_method_before_hello_is_still_1001(daemon):
    c = daemon.connect(hello=False)
    c.error("no.such.method", code=1001)


def test_unsupported_protocol_fails_1002(daemon):
    c = daemon.connect(hello=False)
    msg = c.hello(protocol=999)
    assert "error" in msg, msg
    err = msg["error"]
    assert err["code"] == 1002
    assert 1 in err["data"]["supported"]


def test_commands_work_after_hello(conn):
    assert conn.result("player.status")["state"] in ("idle", "loading", "playing", "paused")


def _require_transport(daemon, transport):
    if daemon.discovery()["transport"] != transport:
        pytest.skip(f"only on the {transport} transport")


def test_tcp_hello_without_token_fails_1001(daemon):
    _require_transport(daemon, "tcp")
    c = daemon.connect(hello=False)
    msg = c.hello(token=None)
    assert msg.get("error", {}).get("code") == 1001, msg


def test_tcp_hello_with_wrong_token_fails_1001(daemon):
    _require_transport(daemon, "tcp")
    c = daemon.connect(hello=False)
    msg = c.hello(token="B" * 43)
    assert msg.get("error", {}).get("code") == 1001, msg


def test_tcp_failed_hello_does_not_authenticate(daemon):
    _require_transport(daemon, "tcp")
    c = daemon.connect(hello=False)
    c.hello(token="B" * 43)
    if not c.closed_by_daemon(1):
        c.error("player.status", code=1001)


def test_unix_ignores_token(daemon):
    _require_transport(daemon, "unix")
    c = daemon.connect(hello=False)
    msg = c.hello(token="not-a-real-token")
    assert "result" in msg, msg
