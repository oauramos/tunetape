"""`tunetape mcp`: a Model Context Protocol server on stdio.

Agents such as Claude Code start this process and talk MCP to it over
stdin/stdout; it turns their tool calls into daemon calls through
`tunetape.client.Client` (docs/architecture.md) and nothing else, so it never
touches mpv, resolvers or history itself.

The official `mcp` SDK needs Python 3.10, and tunetape supports 3.9, so this is
JSON-RPC 2.0 by hand: one JSON object per line in each direction. stdout carries
protocol messages only; anything meant for a human goes to stderr, which MCP
clients show as the server's log.

`Server.handle()` is the whole protocol and takes a parsed message, so tests
drive it without pipes or a daemon by passing a fake `client_factory`.
"""

import argparse
import json
import os
import sys
import threading
import traceback

from tunetape import __version__

# Newest first; the first entry is what we answer when the client asks for a
# version we do not know.
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# structuredContent arrived in 2025-06-18. ISO dates compare as strings.
_STRUCTURED_SINCE = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Seconds to wait on the daemon. Controls answer at once; posting with
# wait=True blocks until a KHInsider album or a playlist is expanded, which
# can take ~30 s on a slow page, and an AI provider can be just as slow.
CONTROL_TIMEOUT = 10.0
POST_TIMEOUT = 90.0
DISCOVER_TIMEOUT = 90.0

# list_queue / history text is for the model to read; past this it is noise.
# The full lists still go out as structuredContent.
_MAX_LINES = 100

INSTRUCTIONS = (
    "tunetape is a terminal music player running in the background on the "
    "user's computer. Use these tools to play audio, build and inspect the "
    "queue, control playback and get AI suggestions. An `input` can be a "
    "YouTube video or playlist URL, a Spotify track/album/playlist link, a "
    "KHInsider album page, a direct audio URL, a local file path, or plain "
    "words, which are searched on YouTube and the first result is played "
    "(e.g. \"lofi hip hop radio\"). Albums and playlists expand into several "
    "queue items. Queue positions shown to you are 1-based."
)


class ToolError(Exception):
    """A failure the model should see as a tool result (`isError: true`)."""


# ---------------------------------------------------------------- tools ------

def _schema(properties=None, required=()):
    schema = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = list(required)
    return schema


_INPUT = {
    "type": "string",
    "minLength": 1,
    "description": (
        "What to play: a YouTube, Spotify, KHInsider or direct audio URL, a "
        "local file path, or plain search words such as \"daft punk around "
        "the world\"."
    ),
}
_TITLE = {
    "type": "string",
    "description": (
        "Optional display title for a single track, e.g. the title of a "
        "`discover` pick. Ignored for albums and playlists."
    ),
}


def _annotations(title, read_only=False, destructive=False, idempotent=False,
                 open_world=False):
    hints = {"title": title, "readOnlyHint": read_only}
    if not read_only:
        # Only meaningful for tools that change something (MCP spec).
        hints["destructiveHint"] = destructive
        hints["idempotentHint"] = idempotent
    hints["openWorldHint"] = open_world
    return hints


TOOLS = [
    {
        "name": "play",
        "description": (
            "Replace the queue with `input` and start playing it now. Use this "
            "when the user asks to play something; use `enqueue` when they "
            "want to keep what is queued. Waits until the link is resolved, so "
            "a bad link or failed search is reported right away. Returns the "
            "posted request (title, kind, number of tracks)."
        ),
        "inputSchema": _schema({"input": _INPUT, "title": _TITLE}, ["input"]),
        "annotations": _annotations("Play now", destructive=True, open_world=True),
    },
    {
        "name": "enqueue",
        "description": (
            "Add `input` to the queue without interrupting what is playing: at "
            "the end, or right after the current track with `play_next`. If "
            "nothing is playing, playback starts with it. Waits until the link "
            "is resolved and reports how many tracks were added."
        ),
        "inputSchema": _schema({
            "input": _INPUT,
            "play_next": {
                "type": "boolean",
                "default": False,
                "description": "Insert right after the current track instead of at the end.",
            },
            "title": _TITLE,
        }, ["input"]),
        "annotations": _annotations("Add to queue", open_world=True),
    },
    {
        "name": "now_playing",
        "description": (
            "What is playing right now: player state (playing, paused, "
            "loading, idle), the current track and its album or playlist, "
            "position and duration in seconds, volume, and where it sits in "
            "the queue."
        ),
        "inputSchema": _schema(),
        "annotations": _annotations("Now playing", read_only=True),
    },
    {
        "name": "list_queue",
        "description": (
            "List the queue in order with 1-based positions, item ids, titles "
            "and status, marking the current track. Use the positions with "
            "`jump_to` and the item ids with `remove_from_queue`."
        ),
        "inputSchema": _schema(),
        "annotations": _annotations("List queue", read_only=True),
    },
    {
        "name": "pause",
        "description": "Pause playback. Fails if nothing is playing.",
        "inputSchema": _schema(),
        "annotations": _annotations("Pause", idempotent=True),
    },
    {
        "name": "resume",
        "description": "Resume paused playback. Fails if nothing is playing.",
        "inputSchema": _schema(),
        "annotations": _annotations("Resume", idempotent=True),
    },
    {
        "name": "next_track",
        "description": "Skip to the next track in the queue. Fails on the last track.",
        "inputSchema": _schema(),
        "annotations": _annotations("Next track"),
    },
    {
        "name": "previous_track",
        "description": "Go back to the previous track in the queue. Fails on the first track.",
        "inputSchema": _schema(),
        "annotations": _annotations("Previous track"),
    },
    {
        "name": "stop",
        "description": (
            "Stop playback but keep the queue, so `jump_to` or `play` can "
            "start again later. Use `clear_queue` to empty it."
        ),
        "inputSchema": _schema(),
        "annotations": _annotations("Stop", idempotent=True),
    },
    {
        "name": "set_volume",
        "description": (
            "Set the volume in percent (0-130, 100 is unamplified) with "
            "`value`, or change it relative to the current level with `delta` "
            "(e.g. -10 for \"a bit quieter\"). Give exactly one of them. The "
            "volume is remembered for later sessions."
        ),
        "inputSchema": _schema({
            "value": {"type": "number", "minimum": 0, "maximum": 130,
                      "description": "Absolute volume in percent."},
            "delta": {"type": "number", "minimum": -130, "maximum": 130,
                      "description": "Change in percentage points, negative is quieter."},
        }),
        "annotations": _annotations("Set volume"),
    },
    {
        "name": "seek",
        "description": (
            "Move within the current track. By default `seconds` is relative "
            "(30 skips ahead, -10 goes back); with `relative: false` it is the "
            "absolute position from the start of the track."
        ),
        "inputSchema": _schema({
            "seconds": {"type": "number", "description": "Seconds to move, or the target position."},
            "relative": {"type": "boolean", "default": True,
                         "description": "false to seek to an absolute position."},
        }, ["seconds"]),
        "annotations": _annotations("Seek"),
    },
    {
        "name": "jump_to",
        "description": (
            "Play the track at a 1-based queue `position`, as shown by "
            "`list_queue` (1 is the first track)."
        ),
        "inputSchema": _schema({
            "position": {"type": "integer", "minimum": 1,
                         "description": "1-based position in the queue."},
        }, ["position"]),
        "annotations": _annotations("Jump to track", idempotent=True),
    },
    {
        "name": "remove_from_queue",
        "description": (
            "Remove one item from the queue by its item id (from "
            "`list_queue`, e.g. \"i-01a9\"). Removing the current track skips "
            "to the next one."
        ),
        "inputSchema": _schema({
            "item_id": {"type": "string", "minLength": 1, "description": "Queue item id."},
        }, ["item_id"]),
        "annotations": _annotations("Remove from queue", destructive=True, idempotent=True),
    },
    {
        "name": "clear_queue",
        "description": "Stop playback and empty the whole queue.",
        "inputSchema": _schema(),
        "annotations": _annotations("Clear queue", destructive=True, idempotent=True),
    },
    {
        "name": "discover",
        "description": (
            "Ask tunetape's AI for track suggestions matching a mood, genre or "
            "description (\"upbeat 80s synthwave for coding\"). Plays nothing: "
            "it returns picks, each with an `input` and a `title` to pass to "
            "`play` or `enqueue`. Needs AI discovery configured in tunetape. "
            "For a plain search, pass the words to `play` directly instead."
        ),
        "inputSchema": _schema({
            "request": {"type": "string", "minLength": 1,
                        "description": "What the user is in the mood for, in their words."},
        }, ["request"]),
        "annotations": _annotations("Discover music", open_world=True),
    },
    {
        "name": "history",
        "description": (
            "Recently played tracks, albums and playlists, most recent first, "
            "with their original URL (pass it to `play` to replay), type and "
            "play count."
        ),
        "inputSchema": _schema({
            "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                      "description": "How many entries to return (default 50)."},
        }),
        "annotations": _annotations("Listening history", read_only=True),
    },
]

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}

_JSON_TYPES = {
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}
_TYPE_NAMES = {
    "string": "a string",
    "boolean": "true or false",
    "integer": "a whole number",
    "number": "a number",
}


def _validate(tool, args):
    """Check `args` against the tool's inputSchema, returning cleaned args.

    Just enough JSON Schema for the schemas above. Unknown properties are
    dropped rather than refused: models add stray fields now and then, and the
    call is still clear.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("Tool arguments must be a JSON object.")
    schema = tool["inputSchema"]
    for name in schema.get("required", ()):
        if args.get(name) is None:
            raise ToolError(f"Missing required argument `{name}`.")
    clean = {}
    for name, spec in schema["properties"].items():
        value = args.get(name)
        if value is None:
            continue
        kind = spec["type"]
        # bool is an int in Python, and 3.0 is a fine integer in JSON.
        if kind == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, _JSON_TYPES[kind]) or (
                kind != "boolean" and isinstance(value, bool)):
            raise ToolError(f"Argument `{name}` must be {_TYPE_NAMES[kind]}.")
        if kind == "string":
            value = value.strip()
            if len(value) < spec.get("minLength", 0):
                raise ToolError(f"Argument `{name}` must not be empty.")
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolError(f"Argument `{name}` must be at least {spec['minimum']}.")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolError(f"Argument `{name}` must be at most {spec['maximum']}.")
        clean[name] = value
    return clean


# ------------------------------------------------------------- summaries ----

def _clock(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "?"
    minutes, secs = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _track_label(item):
    title = item.get("title") or item.get("input") or "(untitled)"
    collection = item.get("collection")
    if collection and collection.get("title"):
        title += f" · {collection['title']}"
    return title


def _status_text(status):
    state = status.get("state")
    current = status.get("current")
    if state == "idle" or not current:
        length = status.get("length") or 0
        if not current or not length:
            return "Nothing is playing and the queue is empty."
        return f"Stopped. {length} track(s) in the queue."
    marks = {"playing": "Playing", "paused": "Paused", "loading": "Loading"}
    text = f"{marks.get(state, state)}: {_track_label(current)}"
    if (status.get("length") or 0) > 1:
        text += f" ({status.get('index', 0) + 1}/{status['length']})"
    duration = status.get("duration") or 0
    clock = _clock(status.get("position"))
    text += f"  {clock} / {_clock(duration)}" if duration else f"  {clock} (live)"
    text += f"  vol {round(status.get('volume') or 0)}%"
    if status.get("muted"):
        text += " (muted)"
    if state == "loading" and status.get("message"):
        text += f"  {status['message']}"
    return text


def _request_text(verb, result):
    request = result.get("request") or {}
    title = request.get("title") or request.get("input") or "(untitled)"
    count = len(request.get("item_ids") or ())
    text = f"{verb}: {title}"
    if count > 1:
        text += f" ({count} tracks)"
    if request.get("status") == "failed" and request.get("error"):
        text += f"\nFailed: {request['error']}"
    return text


def _queue_text(result):
    items = result.get("items") or []
    if not items:
        return "The queue is empty."
    index = result.get("index", -1)
    lines = [f"{len(items)} track(s) in the queue:"]
    for position, item in enumerate(items[:_MAX_LINES]):
        mark = "▶" if position == index else " "
        line = f"{mark} {position + 1}. {_track_label(item)}  [{item.get('id')}]"
        if item.get("status") not in (None, "queued", "playing"):
            line += f"  ({item['status']})"
        if item.get("error"):
            line += f"  {item['error']}"
        lines.append(line)
    if len(items) > _MAX_LINES:
        lines.append(f"… and {len(items) - _MAX_LINES} more.")
    return "\n".join(lines)


def _discover_text(result):
    tracks = result.get("tracks") or []
    if not tracks:
        return "No suggestions came back. Try describing it differently."
    lines = [f"{len(tracks)} suggestion(s); pass `input` and `title` to play or enqueue:"]
    for number, track in enumerate(tracks, 1):
        lines.append(f"{number}. {track.get('title')}  input: {track.get('input')}")
    return "\n".join(lines)


def _history_text(result):
    entries = result.get("entries") or []
    if not entries:
        return "Nothing has been played yet."
    lines = []
    for entry in entries[:_MAX_LINES]:
        line = f"- {entry.get('title') or entry.get('url')} ({entry.get('type')}"
        if entry.get("track_count"):
            line += f", {entry['track_count']} tracks"
        line += f", played {entry.get('play_count', 1)}x)  {entry.get('url')}"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------- server -----

def _is_daemon_error(exc):
    # Duck-typed so this module never has to import tunetape.client up front
    # (and tests can raise their own): DaemonError carries an int `code`.
    return isinstance(getattr(exc, "code", None), int) and hasattr(exc, "message")


def _is_unavailable(exc):
    # DaemonUnavailable is the DaemonError with code 0 (architecture.md).
    return type(exc).__name__ == "DaemonUnavailable" or (
        _is_daemon_error(exc) and exc.code == 0)


def _is_timeout(exc):
    return isinstance(exc, TimeoutError) or type(exc).__name__ in ("timeout", "TimeoutError")


def _connect_daemon():
    """Default client factory: reach the daemon, starting it if needed."""
    from tunetape.client import Client  # lazy: the daemon client is only needed on a tool call
    return Client.connect(spawn=True, name=f"tunetape-mcp/{__version__}")


def _log(message):
    print(f"tunetape-mcp: {message}", file=sys.stderr, flush=True)


class Server:
    """The MCP protocol state for one stdio session."""

    def __init__(self, client_factory=None):
        self._client_factory = client_factory or _connect_daemon
        self._client = None
        self._client_lock = threading.Lock()
        self.protocol_version = None

    # --- daemon connection -------------------------------------------------

    def _get_client(self):
        with self._client_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _drop_client(self, client):
        with self._client_lock:
            if self._client is client:
                self._client = None
        try:
            client.close()
        except Exception:
            pass

    def _call(self, method, params=None, timeout=CONTROL_TIMEOUT):
        """`Client.call`, reconnecting once if the daemon went away under us.

        The daemon exits after a while idle (protocol §1.4) while an agent
        session can sit open for hours, so a dropped connection is routine.
        A failure to connect at all is not retried: Client.connect has already
        spent its own timeout trying.
        """
        for attempt in (1, 2):
            client = self._get_client()
            try:
                return client.call(method, params or {}, timeout=timeout)
            except Exception as exc:
                if attempt == 1 and _is_unavailable(exc):
                    _log("lost the daemon connection; reconnecting")
                    self._drop_client(client)
                    continue
                raise

    def close(self):
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # --- tools -------------------------------------------------------------

    def _post(self, method, args, at=None):
        params = {"input": args["input"], "wait": True}
        if at is not None:
            params["at"] = at
        if "title" in args:
            params["title"] = args["title"]
        return self._call(method, params, timeout=POST_TIMEOUT)

    def _run_tool(self, name, args):
        """Run one tool; returns (summary text, raw daemon result)."""
        if name == "play":
            result = self._post("play", args)
            return _request_text("Playing", result), result
        if name == "enqueue":
            at = "next" if args.get("play_next") else "end"
            result = self._post("queue.add", args, at=at)
            verb = "Queued to play next" if at == "next" else "Queued"
            return _request_text(verb, result), result
        if name == "now_playing":
            result = self._call("player.status")
            return _status_text(result), result
        if name == "list_queue":
            result = self._call("queue.list")
            return _queue_text(result), result
        if name in ("remove_from_queue", "clear_queue"):
            if name == "clear_queue":
                result = self._call("queue.clear")
            else:
                result = self._call("queue.remove", {"id": args["item_id"]})
            return _queue_text(result), result
        if name == "discover":
            result = self._call("ai.discover", {"request": args["request"]},
                                timeout=DISCOVER_TIMEOUT)
            return _discover_text(result), result
        if name == "history":
            params = {"limit": args["limit"]} if "limit" in args else {}
            result = self._call("history.list", params)
            return _history_text(result), result

        # Everything else answers with a Status.
        if name == "set_volume":
            if ("value" in args) == ("delta" in args):
                raise ToolError("Give exactly one of `value` or `delta`.")
            method, params = "player.volume", args
        elif name == "seek":
            method = "player.seek"
            params = {"seconds": args["seconds"], "relative": args.get("relative", True)}
        elif name == "jump_to":
            method, params = "queue.jump", {"index": args["position"] - 1}
        else:
            method = {
                "pause": "player.pause",
                "resume": "player.resume",
                "next_track": "player.next",
                "previous_track": "player.prev",
                "stop": "player.stop",
            }[name]
            params = {}
        result = self._call(method, params)
        return _status_text(result), result

    def _tool_result(self, text, structured=None, is_error=False):
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if (structured is not None and self.protocol_version
                and self.protocol_version >= _STRUCTURED_SINCE):
            result["structuredContent"] = structured
        return result

    def _tools_call(self, params):
        name = params.get("name")
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            args = _validate(tool, params.get("arguments"))
            text, raw = self._run_tool(name, args)
        except ToolError as exc:
            return self._tool_result(str(exc), is_error=True)
        except Exception as exc:
            if _is_daemon_error(exc):
                return self._tool_result(exc.message or str(exc), is_error=True)
            if _is_timeout(exc):
                hint = (" It may still be loading; check `list_queue` in a moment."
                        if name in ("play", "enqueue") else "")
                return self._tool_result("tunetape did not answer in time." + hint,
                                         is_error=True)
            _log(f"tool {name} failed:\n{traceback.format_exc()}")
            return self._tool_result(f"tunetape hit an unexpected error: {exc}",
                                     is_error=True)
        return self._tool_result(text, raw)

    # --- JSON-RPC ----------------------------------------------------------

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        self.protocol_version = version
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tunetape", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _dispatch(self, method, params):
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._tools_call(params)
        raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def handle(self, message):
        """Handle one parsed JSON-RPC message; returns the response or None.

        Notifications (no `id`) and responses from the client get None: the
        only notifications we receive (`initialized`, `cancelled`) need no
        action, and we never send requests that would be answered.
        """
        if isinstance(message, list):
            # JSON-RPC batches: only protocol 2025-03-26 allowed them.
            if not message:
                return _error(None, INVALID_REQUEST, "Empty batch")
            responses = [r for r in (self.handle(m) for m in message) if r is not None]
            return responses or None
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid request")
        method = message.get("method")
        is_request = "id" in message
        if not isinstance(method, str):
            if is_request and "result" not in message and "error" not in message:
                return _error(message.get("id"), INVALID_REQUEST, "Invalid request")
            return None
        if not is_request:
            return None
        msg_id = message["id"]
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except _RpcError as exc:
            return _error(msg_id, exc.code, exc.message)
        except Exception as exc:
            _log(f"{method} failed:\n{traceback.format_exc()}")
            return _error(msg_id, INTERNAL_ERROR, f"Internal error: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------- stdio -----

def _binary(stream):
    # The real std streams are text wrappers; JSON goes through their bytes
    # buffer so Windows neither re-encodes it (cp1252) nor turns \n into \r\n.
    return getattr(stream, "buffer", stream)


def _windows_binary_stdio():
    if os.name != "nt":
        return
    try:
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    except Exception:
        pass


def serve(server, stdin, stdout):
    """Read NDJSON from `stdin` and write responses to `stdout` until EOF.

    Tool calls run on their own threads: posting an album can take half a
    minute, and the client must still get answers to `ping` meanwhile.
    Responses may therefore come back out of order, which JSON-RPC allows.
    """
    write_lock = threading.Lock()
    workers = []

    def send(response):
        if response is None:
            return
        data = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with write_lock:
            stdout.write(data.encode("utf-8") + b"\n")
            stdout.flush()

    def run(message):
        try:
            send(server.handle(message))
        except Exception:
            _log(f"failed to answer:\n{traceback.format_exc()}")

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError alike
            send(_error(None, PARSE_ERROR, f"Parse error: {exc}"))
            continue
        if (isinstance(message, dict) and message.get("method") == "tools/call"
                and "id" in message):
            worker = threading.Thread(target=run, args=(message,), daemon=True,
                                      name="tunetape-mcp-tool")
            worker.start()
            workers.append(worker)
            workers = [w for w in workers if w.is_alive()]
        else:
            run(message)
    # stdin closed: let calls in flight answer before we go. Each is bounded
    # by its daemon timeout.
    for worker in workers:
        worker.join()


def main(argv=None, *, stdin=None, stdout=None, client_factory=None) -> int:
    """Entry point for `tunetape mcp`. Streams and factory are test seams."""
    parser = argparse.ArgumentParser(
        prog="tunetape mcp",
        description="Run tunetape's MCP server on stdio, for agents such as Claude Code.",
    )
    parser.parse_args(argv if argv is not None else [])

    real_stdio = stdin is None and stdout is None
    if real_stdio:
        _windows_binary_stdio()
    in_stream = _binary(stdin if stdin is not None else sys.stdin)
    out_stream = _binary(stdout if stdout is not None else sys.stdout)
    saved_stdout = sys.stdout
    if real_stdio:
        # A stray print anywhere would corrupt the protocol stream.
        sys.stdout = sys.stderr
    server = Server(client_factory)
    try:
        serve(server, in_stream, out_stream)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        sys.stdout = saved_stdout
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
