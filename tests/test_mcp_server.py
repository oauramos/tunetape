"""The MCP server, driven offline: a fake daemon client stands in for
tunetape.client, so neither a daemon nor stdio pipes are needed."""

import io
import json

import pytest

import tunetape
import tunetape.mcp_server as mcp


# ------------------------------------------------- protocol-shaped results --
# Built from docs/protocol.md §3.

def _item(item_id, title, index=None, count=None, status="queued"):
    collection = None
    if count is not None:
        collection = {"title": "Chrono Trigger OST", "index": index, "count": count}
    return {
        "id": item_id, "request_id": "r-7f3a2c", "title": title, "kind": "khinsider",
        "input": f"https://downloads.khinsider.com/x/{item_id}.mp3",
        "collection": collection, "status": status, "error": None, "duration": None,
    }


ALBUM_URL = "https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger"

REQUEST = {
    "id": "r-7f3a2c", "input": ALBUM_URL, "kind": "khinsider",
    "title": "Chrono Trigger OST", "status": "done", "error": None,
    "item_ids": ["i-01", "i-02", "i-03"], "created_at": "2026-10-23T14:05:00Z",
}

STATUS = {
    "state": "playing", "message": None, "position": 83.4, "duration": 251.0,
    "volume": 80.0, "muted": False,
    "current": _item("i-02", "Corridors of Time", index=1, count=3, status="playing"),
    "index": 1, "length": 3, "has_next": True, "has_prev": True,
}

QUEUE = {
    "items": [
        _item("i-01", "Peaceful Days", 0, 3, "played"),
        _item("i-02", "Corridors of Time", 1, 3, "playing"),
        _item("i-03", "Schala's Theme", 2, 3),
    ],
    "index": 1,
}

RESULTS = {
    "play": {"request": REQUEST},
    "queue.add": {"request": REQUEST},
    "queue.list": QUEUE,
    "queue.jump": STATUS,
    "queue.remove": {"items": QUEUE["items"][:2], "index": 1},
    "queue.clear": {"items": [], "index": -1},
    "ai.discover": {"tracks": [
        {"title": "Kavinsky - Nightcall", "input": "ytsearch1:Kavinsky Nightcall"},
        {"title": "M83 - Midnight City", "input": "ytsearch1:M83 Midnight City"},
    ]},
    "history.list": {"entries": [{
        "type": "khinsider", "url": ALBUM_URL, "title": "Chrono Trigger OST",
        "last_played": "2026-10-23T14:05:00Z", "play_count": 2,
        "track_count": 64, "last_index": 4,
    }]},
}


class FakeDaemonError(Exception):
    """Shaped like tunetape.client.DaemonError (architecture.md)."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class DaemonUnavailable(FakeDaemonError):
    def __init__(self, message="tunetape's daemon could not be started"):
        super().__init__(0, message)


class FakeClient:
    def __init__(self, raises=None):
        self.calls = []
        self.raises = dict(raises or {})  # method -> exception, raised once
        self.closed = False

    def call(self, method, params=None, *, timeout=30.0):
        self.calls.append((method, params, timeout))
        exc = self.raises.pop(method, None)
        if exc is not None:
            raise exc
        return RESULTS.get(method, STATUS)

    def close(self):
        self.closed = True


class Factory:
    """Hands out FakeClients and remembers them, like Client.connect would."""

    def __init__(self, *clients):
        self.queue = list(clients)
        self.made = []

    def __call__(self):
        client = self.queue.pop(0) if self.queue else FakeClient()
        self.made.append(client)
        return client


def _server(version="2025-11-25", factory=None):
    server = mcp.Server(factory or Factory())
    if version:
        server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": version, "capabilities": {},
                                  "clientInfo": {"name": "pytest", "version": "1"}}})
    return server


def _call_tool(server, name, arguments=None, msg_id=7):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
           "params": {"name": name}}
    if arguments is not None:
        msg["params"]["arguments"] = arguments
    return server.handle(msg)


def _last_call(factory):
    return factory.made[-1].calls[-1]


# ------------------------------------------------------------- lifecycle ----

@pytest.mark.parametrize("version", mcp.SUPPORTED_VERSIONS)
def test_initialize_echoes_supported_version(version):
    server = mcp.Server(Factory())
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": version}})
    result = response["result"]
    assert response["id"] == 1
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "tunetape", "version": tunetape.__version__}
    assert "URL" in result["instructions"] and "words" in result["instructions"]


def test_initialize_unknown_version_answers_newest():
    server = mcp.Server(Factory())
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "1999-01-01"}})["result"]
    assert result["protocolVersion"] == "2025-11-25"


def test_initialize_does_not_connect_to_daemon():
    factory = Factory()
    _server(factory=factory)
    assert factory.made == []


def test_initialized_notification_gets_no_response():
    server = _server()
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 3}}) is None


def test_ping():
    assert _server().handle({"jsonrpc": "2.0", "id": "p", "method": "ping"}) == {
        "jsonrpc": "2.0", "id": "p", "result": {}}


def test_unknown_method():
    response = _server().handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert response["error"]["code"] == -32601
    assert response["id"] == 4


def test_invalid_request_shapes():
    server = _server()
    assert server.handle(42)["error"]["code"] == -32600
    assert server.handle({"jsonrpc": "2.0", "id": 1})["error"]["code"] == -32600
    assert server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping",
                          "params": [1]})["error"]["code"] == -32602
    # A response from the client to something we never asked: ignored.
    assert server.handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None


# ------------------------------------------------------------ tools/list ----

def test_tools_list_shape():
    tools = _server().handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "play", "enqueue", "now_playing", "list_queue", "pause", "resume",
        "next_track", "previous_track", "stop", "set_volume", "seek", "jump_to",
        "remove_from_queue", "clear_queue", "discover", "history",
    ]
    for tool in tools:
        assert tool["description"].strip()
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        for required in schema.get("required", ()):
            assert required in schema["properties"]
        assert isinstance(tool["annotations"]["readOnlyHint"], bool)
    json.dumps(tools)  # must be plain JSON


def test_read_only_tools_are_annotated():
    by_name = {tool["name"]: tool for tool in mcp.TOOLS}
    for name in ("now_playing", "list_queue", "history"):
        assert by_name[name]["annotations"]["readOnlyHint"] is True
    for name in ("play", "clear_queue", "remove_from_queue"):
        assert by_name[name]["annotations"]["destructiveHint"] is True


# ------------------------------------------------------------ tool calls ----

@pytest.mark.parametrize("name, arguments, method, params", [
    ("play", {"input": "lofi hip hop"}, "play", {"input": "lofi hip hop", "wait": True}),
    ("play", {"input": "ytsearch1:x", "title": "X"}, "play",
     {"input": "ytsearch1:x", "wait": True, "title": "X"}),
    ("enqueue", {"input": ALBUM_URL}, "queue.add",
     {"input": ALBUM_URL, "wait": True, "at": "end"}),
    ("enqueue", {"input": ALBUM_URL, "play_next": True}, "queue.add",
     {"input": ALBUM_URL, "wait": True, "at": "next"}),
    ("enqueue", {"input": "x", "play_next": False, "title": "T"}, "queue.add",
     {"input": "x", "wait": True, "at": "end", "title": "T"}),
    ("now_playing", None, "player.status", {}),
    ("list_queue", {}, "queue.list", {}),
    ("pause", {}, "player.pause", {}),
    ("resume", {}, "player.resume", {}),
    ("next_track", {}, "player.next", {}),
    ("previous_track", {}, "player.prev", {}),
    ("stop", {}, "player.stop", {}),
    ("set_volume", {"value": 55}, "player.volume", {"value": 55}),
    ("set_volume", {"delta": -10}, "player.volume", {"delta": -10}),
    ("seek", {"seconds": 30}, "player.seek", {"seconds": 30, "relative": True}),
    ("seek", {"seconds": 90, "relative": False}, "player.seek",
     {"seconds": 90, "relative": False}),
    ("jump_to", {"position": 3}, "queue.jump", {"index": 2}),
    ("jump_to", {"position": 1.0}, "queue.jump", {"index": 0}),
    ("remove_from_queue", {"item_id": "i-03"}, "queue.remove", {"id": "i-03"}),
    ("clear_queue", {}, "queue.clear", {}),
    ("discover", {"request": "80s synthwave"}, "ai.discover", {"request": "80s synthwave"}),
    ("history", {}, "history.list", {}),
    ("history", {"limit": 5}, "history.list", {"limit": 5}),
])
def test_tool_maps_onto_daemon_call(name, arguments, method, params):
    factory = Factory()
    response = _call_tool(_server(factory=factory), name, arguments)
    result = response["result"]
    assert result["isError"] is False, result
    assert result["content"][0]["type"] == "text" and result["content"][0]["text"]
    called_method, called_params, timeout = _last_call(factory)
    assert (called_method, called_params) == (method, params)
    assert 0 < timeout <= 120


def test_posting_gets_a_longer_timeout_than_controls():
    factory = Factory()
    server = _server(factory=factory)
    _call_tool(server, "enqueue", {"input": ALBUM_URL})
    _call_tool(server, "pause", {})
    (_, _, post_timeout), (_, _, pause_timeout) = factory.made[0].calls
    assert post_timeout >= 60 > pause_timeout


def test_connects_lazily_once_with_spawn():
    factory = Factory()
    server = _server(factory=factory)
    _call_tool(server, "pause", {})
    _call_tool(server, "resume", {})
    assert len(factory.made) == 1
    assert [c[0] for c in factory.made[0].calls] == ["player.pause", "player.resume"]


def test_default_factory_connects_with_spawn_and_name(monkeypatch):
    import sys
    import types

    seen = {}

    class Client:
        @classmethod
        def connect(cls, **kwargs):
            seen.update(kwargs)
            return FakeClient()

    monkeypatch.setitem(sys.modules, "tunetape.client", types.SimpleNamespace(Client=Client))
    mcp._connect_daemon()
    assert seen == {"spawn": True, "name": f"tunetape-mcp/{tunetape.__version__}"}


def test_reconnects_once_when_connection_dropped():
    first = FakeClient(raises={"player.pause": DaemonUnavailable("connection lost")})
    factory = Factory(first, FakeClient())
    result = _call_tool(_server(factory=factory), "pause", {})["result"]
    assert result["isError"] is False
    assert first.closed
    assert len(factory.made) == 2
    assert factory.made[1].calls[0][0] == "player.pause"


def test_gives_up_after_second_drop():
    factory = Factory(
        FakeClient(raises={"player.pause": DaemonUnavailable("gone")}),
        FakeClient(raises={"player.pause": DaemonUnavailable("still gone")}),
    )
    result = _call_tool(_server(factory=factory), "pause", {})["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "still gone"
    assert len(factory.made) == 2


def test_daemon_that_cannot_start_is_a_tool_error():
    def factory():
        raise DaemonUnavailable("Could not start the tunetape daemon (see daemon.log)")

    result = _call_tool(_server(factory=factory), "now_playing")["result"]
    assert result["isError"] is True
    assert "Could not start" in result["content"][0]["text"]


def test_daemon_error_becomes_is_error_result():
    failed = dict(REQUEST, status="failed", error="This KHInsider album has no tracks")
    client = FakeClient(raises={"queue.add": FakeDaemonError(
        1014, "This KHInsider album has no tracks", {"request": failed})})
    response = _call_tool(_server(factory=Factory(client)), "enqueue", {"input": ALBUM_URL})
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "This KHInsider album has no tracks"}]
    assert "structuredContent" not in result


def test_nothing_playing_is_a_tool_error():
    client = FakeClient(raises={"player.pause": FakeDaemonError(1012, "Nothing is playing")})
    result = _call_tool(_server(factory=Factory(client)), "pause", {})["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Nothing is playing"


def test_timeout_is_a_tool_error():
    client = FakeClient(raises={"queue.add": TimeoutError("timed out")})
    result = _call_tool(_server(factory=Factory(client)), "enqueue", {"input": "x"})["result"]
    assert result["isError"] is True
    assert "did not answer in time" in result["content"][0]["text"]


@pytest.mark.parametrize("name, arguments", [
    ("play", {}),
    ("play", {"input": "   "}),
    ("play", {"input": 5}),
    ("enqueue", {"input": "x", "play_next": "yes"}),
    ("set_volume", {}),
    ("set_volume", {"value": 50, "delta": 5}),
    ("set_volume", {"value": 500}),
    ("seek", {"seconds": "ten"}),
    ("seek", {"seconds": True}),
    ("jump_to", {"position": 0}),
    ("jump_to", {"position": 2.5}),
    ("remove_from_queue", {}),
    ("history", {"limit": 0}),
    ("pause", ["not", "an", "object"]),
])
def test_invalid_arguments_are_tool_errors(name, arguments):
    factory = Factory()
    response = _call_tool(_server(factory=factory), name, arguments)
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"]
    assert all(not client.calls for client in factory.made)


def test_unknown_tool_is_invalid_params():
    response = _call_tool(_server(), "self_destruct", {})
    assert response["error"]["code"] == -32602
    assert "self_destruct" in response["error"]["message"]


# ------------------------------------------------------ result contents ----

@pytest.mark.parametrize("version, structured", [
    ("2025-11-25", True), ("2025-06-18", True), ("2025-03-26", False), ("2024-11-05", False),
])
def test_structured_content_by_version(version, structured):
    result = _call_tool(_server(version), "now_playing")["result"]
    if structured:
        assert result["structuredContent"] == STATUS
    else:
        assert "structuredContent" not in result


def test_summaries_are_human_readable():
    server = _server()
    text = lambda name, args=None: _call_tool(server, name, args)["result"]["content"][0]["text"]
    assert text("now_playing") == (
        "Playing: Corridors of Time · Chrono Trigger OST (2/3)  1:23 / 4:11  vol 80%")
    assert text("enqueue", {"input": ALBUM_URL}) == "Queued: Chrono Trigger OST (3 tracks)"
    queue = text("list_queue")
    assert "▶ 2. Corridors of Time" in queue and "[i-03]" in queue
    discover = text("discover", {"request": "synthwave"})
    assert "input: ytsearch1:Kavinsky Nightcall" in discover
    assert ALBUM_URL in text("history")


def test_idle_status_summary():
    idle = dict(STATUS, state="idle", current=None, index=-1, length=0)
    assert mcp._status_text(idle) == "Nothing is playing and the queue is empty."


# ----------------------------------------------------------------- stdio ----

def _run(lines, factory=None):
    stdin = io.BytesIO("".join(line + "\n" for line in lines).encode("utf-8"))
    stdout = io.BytesIO()
    assert mcp.main([], stdin=stdin, stdout=stdout, client_factory=factory or Factory()) == 0
    raw = stdout.getvalue()
    assert b"\r\n" not in raw
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def test_malformed_line_answers_parse_error_and_keeps_going():
    responses = _run([
        "{not json",
        "",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
    ])
    assert responses[0]["error"]["code"] == -32700
    assert responses[0]["id"] is None
    assert responses[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_full_stdio_round_trip():
    factory = Factory()
    responses = _run([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "pytest", "version": "1"}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "play", "arguments": {"input": "lofi hip hop"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "now_playing", "arguments": {}}}),
    ], factory)
    # Tool calls run on worker threads, so their order may vary.
    by_id = {response["id"]: response for response in responses}
    assert sorted(by_id) == [1, 2, 3, 4]
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == len(mcp.TOOLS)
    assert by_id[3]["result"]["structuredContent"] == {"request": REQUEST}
    assert "Corridors of Time" in by_id[4]["result"]["content"][0]["text"]
    assert sorted(c[0] for c in factory.made[0].calls) == ["play", "player.status"]
    assert factory.made[0].closed  # closed on EOF


def test_non_ascii_survives_the_round_trip():
    responses = _run([json.dumps({"jsonrpc": "2.0", "id": "çã", "method": "ping"},
                                 ensure_ascii=False)])
    assert responses == [{"jsonrpc": "2.0", "id": "çã", "result": {}}]


def test_main_accepts_text_streams_with_a_buffer():
    stdin = io.TextIOWrapper(io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
                             encoding="utf-8")
    out = io.BytesIO()
    stdout = io.TextIOWrapper(out, encoding="utf-8")
    mcp.main([], stdin=stdin, stdout=stdout, client_factory=Factory())
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}
