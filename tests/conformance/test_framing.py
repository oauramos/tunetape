"""§1.1 framing and §5 errors: NDJSON lines, ids, pipelining, error codes."""

import json
import time

import pytest

from .wire import add, queue_items


def _line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def test_pipelined_requests_are_all_answered_by_id(conn, media):
    path = media("pipelined.mp3")
    reqs = [
        (10, "player.status", {}),
        (11, "queue.list", None),
        ("s-12", "daemon.info", {}),
        (13, "queue.add", {"input": path, "wait": True}),
        (14, "requests.list", {}),
        ("s-15", "player.status", {"levels": True}),
    ]
    blob = b""
    for rid, method, params in reqs:
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        conn._methods[rid] = method
        blob += _line(msg)
    conn.send_raw(blob)  # one write: several requests in flight at once
    for rid, method, _ in reqs:
        msg = conn.response(rid, timeout=20)
        assert msg["id"] == rid
        assert "result" in msg, f"{method}: {msg}"


def test_responses_may_arrive_in_any_order(conn, media):
    # A slow-ish request followed by fast ones: the client matches by id, so
    # whatever order the daemon picks must still pair every answer correctly.
    path = media("order.mp3")
    slow = conn.request("queue.add", {"input": path, "wait": True})
    fast = [conn.request("player.status") for _ in range(5)]
    for rid in [slow] + fast:
        assert "result" in conn.response(rid, timeout=20)
    assert not conn.unmatched


def test_request_split_across_writes(conn):
    line = _line({"jsonrpc": "2.0", "id": 77, "method": "player.status", "params": {}})
    conn._methods[77] = "player.status"
    conn.send_raw(line[:9])
    time.sleep(0.2)
    conn.send_raw(line[9:20])
    time.sleep(0.2)
    conn.send_raw(line[20:])
    assert "result" in conn.response(77)


def test_utf8_round_trip(conn, media):
    title = "Café ☕ — 東京 \U0001F3B5 \"quoted\" \\ back"
    req = add(conn, media("utf8.mp3"), title=title)
    items = {it["id"]: it for it in queue_items(conn)}
    assert items[req["item_ids"][0]]["title"] == title


def test_accepts_long_lines(conn):
    # Receivers must accept lines up to 1 MiB; unknown params are ignored.
    pad = "x" * (900 * 1024)
    msg = conn.call("player.status", {"padding": pad}, timeout=20)
    assert "result" in msg, msg


def test_malformed_line_is_a_parse_error_and_closes(conn):
    conn.send_raw(b'{"jsonrpc":"2.0","id":5,"method":"player.status"\n')
    msg = conn.recv(10)
    assert msg is not None, "expected a -32700 response before the connection closes"
    assert msg["error"]["code"] == -32700
    assert msg["id"] is None
    assert conn.closed_by_daemon(5), "the daemon closes the connection after a parse error"


def test_garbage_line_is_a_parse_error(conn):
    conn.send_raw(b"this is not json\n")
    msg = conn.recv(10)
    assert msg is not None and msg["error"]["code"] == -32700


@pytest.mark.parametrize("payload", [
    {"jsonrpc": "2.0", "id": 21},                                # no method
    {"jsonrpc": "2.0", "id": 22, "method": 42},                  # method not a string
    [{"jsonrpc": "2.0", "id": 23, "method": "player.status"}],   # a batch: not supported
    42,                                                          # not an object
])
def test_invalid_request(conn, payload):
    conn.send_raw(_line(payload))
    msg = conn.recv(10)
    assert msg is not None, "expected an error response"
    assert msg["error"]["code"] == -32600, msg


def test_unknown_method(conn):
    err = conn.error("player.dance", code=-32601)
    assert err["message"]


@pytest.mark.parametrize("method,params", [
    ("queue.add", {}),
    ("queue.add", {"input": 5}),
    ("queue.add", {"input": "/x.mp3", "at": "sideways"}),
    ("queue.add", {"input": "/x.mp3", "wait": "yes"}),
    ("play", {}),
    ("queue.jump", {}),
    ("queue.jump", {"index": "two"}),
    ("queue.remove", {}),
    ("requests.get", {}),
    ("history.list", {"limit": "ten"}),
])
def test_invalid_params(conn, method, params):
    conn.error(method, params, code=-32602)


def test_params_must_be_an_object(conn):
    msg = conn.call("player.status", [1, 2])
    assert msg["error"]["code"] in (-32600, -32602), msg


def test_error_codes_are_known(conn, schema):
    known = schema.error_codes()
    for method, params in [("player.dance", None), ("queue.add", {}), ("queue.jump", {"index": 99})]:
        assert conn.error(method, params)["code"] in known


def test_connection_survives_method_errors(conn):
    conn.error("player.dance", code=-32601)
    conn.error("queue.add", {}, code=-32602)
    conn.error("requests.get", {"id": "r-nope"}, code=1013)
    assert "result" in conn.call("player.status")
