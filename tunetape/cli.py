"""``tunetape <command>``: drive the daemon from the shell (docs/architecture.md).

Every command is one or two protocol calls through :class:`tunetape.client.Client`,
which starts the daemon on demand. Output is one human line (or a short list)
by default and the method's raw result with ``--json``.

Exit status: 0 ok, 1 the daemon returned an error (its message goes to stderr),
2 usage error, 3 the daemon could not be reached or started.

The output formats are shared with the Rust ``tunetape-cli``, so scripts can
use either; change them in both or in neither.
"""

import argparse
import json
import os
import re
import sys
import time

from tunetape.client import Client, DaemonError, DaemonTimeout, DaemonUnavailable

EXIT_OK = 0
EXIT_DAEMON_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

# How long `play` waits for the first track to actually start before it
# reports "Loading" and returns: long enough for a yt-dlp lookup.
_START_WAIT = 45.0


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


# -- formatting ---------------------------------------------------------------

def _clock(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_status(status: dict) -> str:
    """One line, e.g. ``▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%``."""
    state = status.get("state")
    icon = {"playing": "▶", "paused": "⏸", "loading": "…"}.get(state, "■")
    volume = f"vol {round(status.get('volume') or 0)}%" + (" (muted)" if status.get("muted") else "")
    current = status.get("current")
    if state == "loading" and status.get("message"):
        return f"{icon} {status['message']}  {volume}"
    if not current:
        return f"{icon} Nothing playing  {volume}"
    title = current.get("title") or current.get("input") or "?"
    coll = current.get("collection")
    if coll:
        title += f" · {coll.get('title')} ({coll.get('index', 0) + 1}/{coll.get('count')})"
    parts = [f"{icon} {title}"]
    if state in ("playing", "paused"):
        clock = _clock(status.get("position"))
        if status.get("duration"):
            clock += f" / {_clock(status['duration'])}"
        parts.append(clock)
    elif status.get("message"):
        parts.append(status["message"])
    parts.append(volume)
    return "  ".join(parts)


def _request_label(request: dict, items=None) -> str:
    count = len(request.get("item_ids") or [])
    title = request.get("title")
    if not title and items:
        title = items[0].get("title")
    title = title or request.get("input")
    return f"{title} ({count} tracks)" if request.get("kind") in (
        "khinsider", "spotify", "youtube_playlist") else title


def _print_json(value) -> None:
    # Two-space indent, keys in the daemon's order (json keeps dict order).
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _volume_line(status: dict) -> str:
    return f"Volume {round(status.get('volume') or 0)}%" + (" (muted)" if status.get("muted") else "")


# -- argument helpers ---------------------------------------------------------

def _input_arg(words) -> str:
    text = " ".join(words).strip()
    # The daemon runs elsewhere (its own cwd), so a relative path given here
    # must become absolute before it is sent.
    if text and not os.path.isabs(text) and os.path.isfile(text):
        return os.path.abspath(text)
    return text


_TIME_RE = re.compile(r"^(\d+(?:\.\d+)?)$|^(\d+):([0-5]?\d(?:\.\d+)?)$|^(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)$")


def parse_seek(text: str):
    """``(seconds, relative)`` for ``s``, ``+s``, ``-s``, ``mm:ss`` or ``h:mm:ss``."""
    text = text.strip()
    sign = 0
    if text and text[0] in "+-":
        sign = 1 if text[0] == "+" else -1
        text = text[1:]
    m = _TIME_RE.match(text)
    if not m:
        raise _UsageError(f"invalid time {text!r}: use seconds, +s, -s or mm:ss")
    if m.group(1) is not None:
        seconds = float(m.group(1))
    elif m.group(2) is not None:
        seconds = int(m.group(2)) * 60 + float(m.group(3))
    else:
        seconds = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + float(m.group(6))
    return (sign * seconds, True) if sign else (seconds, False)


def parse_volume(text: str):
    """``("value", n)`` for ``n`` or ``("delta", ±n)`` for ``+n`` / ``-n``."""
    try:
        number = float(text)
    except ValueError:
        raise _UsageError(f"invalid volume {text!r}: use n, +n or -n")
    return ("delta", number) if text.strip()[0] in "+-" else ("value", number)


# -- commands -----------------------------------------------------------------

def _wait_started(client, request: dict):
    """Wait until one of ``play``'s items is playing; returns (item, error).

    Resolution is lazy, so a dead link only shows up once the daemon tries to
    play it; waiting here is what lets ``play`` report it right away. A
    collection fails forward on its own, so only "nothing of it played" is an error.
    """
    ids = set(request.get("item_ids") or [])
    deadline = time.monotonic() + _START_WAIT
    while ids and time.monotonic() < deadline:
        items = [it for it in client.queue()["items"] if it["id"] in ids]
        if not items:
            return None, None  # replaced by a later play
        playing = next((it for it in items if it["status"] in ("playing", "played")), None)
        if playing:
            return playing, None
        failed = [it for it in items if it["status"] == "failed"]
        if failed and (len(failed) == len(items) or client.status()["state"] == "idle"):
            return failed[-1], failed[-1].get("error") or "Playback failed."
        time.sleep(0.2)
    return None, None


def _cmd_play(client, args):
    text = _input_arg(args.input)
    if not text:
        raise _UsageError("play needs something to play")
    result = client.play(text, title=args.title, wait=True)
    request = result["request"]
    item, error = _wait_started(client, request)
    if error:
        raise DaemonError(1014, f"Could not play {_request_label(request)}: {error}")
    if args.json:
        _print_json(result)
        return EXIT_OK
    if request.get("kind") in ("khinsider", "spotify", "youtube_playlist"):
        label = _request_label(request)
        if item:
            label = f"{item['title']} · {label}"
    else:
        label = (item or {}).get("title") or _request_label(request)
    verb = "Playing" if item and item["status"] == "playing" else "Loading"
    print(f"{verb}: {label}")
    return EXIT_OK


def _cmd_add(client, args):
    text = _input_arg(args.input)
    if not text:
        raise _UsageError("add needs something to queue")
    result = client.add(text, at="next" if args.next else "end", title=args.title, wait=True)
    if args.json:
        _print_json(result)
        return EXIT_OK
    items = [it for it in client.queue()["items"]
             if it["id"] in set(result["request"].get("item_ids") or [])]
    print(f"Queued: {_request_label(result['request'], items)}")
    return EXIT_OK


def _simple(method):
    def run(client, args):
        result = getattr(client, method)()
        if args.json:
            _print_json(result)
        elif method == "clear":
            print("Queue cleared")
        else:
            print(format_status(result))
        return EXIT_OK
    return run


def _cmd_status(client, args):
    status = client.status()
    if args.json:
        _print_json(status)
    else:
        print(format_status(status))
    return EXIT_OK


def _cmd_queue(client, args):
    result = client.queue()
    if args.json:
        _print_json(result)
        return EXIT_OK
    items = result["items"]
    if not items:
        print("The queue is empty.")
        return EXIT_OK
    for n, item in enumerate(items):
        mark = "▶" if n == result["index"] else " "
        line = f"{mark} {n + 1}. {item.get('title') or item.get('input')}"
        if item.get("duration"):
            line += f"  {_clock(item['duration'])}"
        if item["status"] == "failed":
            line += f"  (failed: {item.get('error')})"
        print(line)
    return EXIT_OK


def _cmd_jump(client, args):
    if args.n < 1:
        raise _UsageError("track numbers start at 1")
    status = client.jump(args.n - 1)
    if args.json:
        _print_json(status)
    else:
        print(format_status(status))
    return EXIT_OK


def _cmd_volume(client, args):
    if args.level is None:
        status = client.status()
    else:
        kind, number = parse_volume(args.level)
        status = client.volume(**{kind: number})
    if args.json:
        _print_json(status)
    else:
        print(_volume_line(status))
    return EXIT_OK


def _cmd_seek(client, args):
    seconds, relative = parse_seek(args.time)
    status = client.seek(seconds, relative=relative)
    if args.json:
        _print_json(status)
    else:
        print(format_status(status))
    return EXIT_OK


def _cmd_mute(client, args):
    status = client.mute()
    if args.json:
        _print_json(status)
    else:
        print(format_status(status))
    return EXIT_OK


def _cmd_history(client, args):
    if args.limit < 0:
        raise _UsageError("--limit must be >= 0")
    result = client.history(args.limit)
    if args.json:
        _print_json(result)
        return EXIT_OK
    entries = result["entries"]
    if not entries:
        print("No listening history yet.")
    for n, entry in enumerate(entries, 1):
        tag = entry.get("type") or "?"
        if entry.get("track_count"):
            tag += f", {int(entry.get('last_index') or 0) + 1}/{entry['track_count']}"
        print(f"{n}. {entry.get('title') or entry.get('url')}  [{tag}]")
    return EXIT_OK


def _cmd_discover(client, args):
    words = " ".join(args.request).strip()
    if not words:
        raise _UsageError("discover needs a description of the music you want")
    result = client.discover(words)
    if args.json:
        _print_json(result)
        return EXIT_OK
    for n, track in enumerate(result["tracks"], 1):
        print(f"{n}. {track['title']}  ({track['input']})")
    return EXIT_OK


def _cmd_shutdown(args):
    try:
        client = Client.connect(spawn=False)  # never start one just to stop it
    except DaemonUnavailable:
        print("tunetape daemon is not running")
        return EXIT_OK
    with client:
        client.shutdown()
    print("tunetape daemon stopped.")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="tunetape", description="Terminal audio player. Run with no "
                     "arguments for the interactive player.")
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)

    def command(name, func, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def with_json(p):
        p.add_argument("--json", action="store_true", help="print the raw result as JSON")
        return p

    p = with_json(command("play", _cmd_play, "replace the queue and play"))
    p.add_argument("input", nargs="+", help="a URL, a local file, or words to search for")
    p.add_argument("--title", help="display title for a single track")
    p = with_json(command("add", _cmd_add, "add to the queue"))
    p.add_argument("input", nargs="+", help="a URL, a local file, or words to search for")
    p.add_argument("--next", action="store_true", help="play it after the current track")
    p.add_argument("--title", help="display title for a single track")
    for name, help_text in (("pause", "pause playback"), ("resume", "resume playback"),
                            ("toggle", "pause or resume"), ("next", "skip to the next track"),
                            ("prev", "go back to the previous track"),
                            ("stop", "stop playback, keep the queue"),
                            ("clear", "empty the queue and stop")):
        with_json(command(name, _simple(name), help_text))
    with_json(command("mute", _cmd_mute, "mute or unmute"))
    with_json(command("status", _cmd_status, "show what is playing"))
    with_json(command("queue", _cmd_queue, "list the queue"))
    p = with_json(command("jump", _cmd_jump, "play track n of the queue (1-based)"))
    p.add_argument("n", type=int)
    p = with_json(command("volume", _cmd_volume, "show or set the volume"))
    p.add_argument("level", nargs="?", help="n, +n or -n (percent)")
    p = with_json(command("seek", _cmd_seek, "seek: s, +s, -s or mm:ss"))
    p.add_argument("time")
    p = with_json(command("history", _cmd_history, "list recently played"))
    p.add_argument("--limit", type=int, default=50)
    p = with_json(command("discover", _cmd_discover, "ask the AI for songs"))
    p.add_argument("request", nargs="+")
    command("shutdown", None, "stop the daemon")
    p = command("daemon", None, "run the daemon in the foreground")
    p.add_argument("--idle-timeout", metavar="S")
    command("mcp", None, "MCP server on stdio")
    return parser


def _reconfigure_output() -> None:
    # The status icons (▶ ⏸ ■) don't exist in every console code page; a
    # missing glyph must not turn into a traceback.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv) -> int:
    """Run one CLI command; returns the exit status."""
    argv = list(argv)
    _reconfigure_output()
    # These two own their arguments, so hand them over untouched.
    if argv and argv[0] == "daemon":
        from tunetape import daemon
        return daemon.main(argv[1:])
    if argv and argv[0] == "mcp":
        try:
            from tunetape.mcp_server import main as mcp_main
        except ModuleNotFoundError as e:
            if e.name != "tunetape.mcp_server":
                raise
            print("tunetape: the MCP server (tunetape.mcp_server) is not part of this build.",
                  file=sys.stderr)
            return EXIT_USAGE
        return mcp_main(argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    try:
        if args.command == "shutdown":
            return _cmd_shutdown(args)
        try:
            client = Client.connect(name="tunetape-cli")
        except DaemonError as e:
            # A daemon that speaks another protocol (1002) can't be used; it
            # counts as unreachable, like a daemon that would not start.
            print(f"tunetape: {e}", file=sys.stderr)
            return EXIT_UNAVAILABLE
        with client:
            return args.func(client, args)
    except _UsageError as e:
        print(f"tunetape {args.command}: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (DaemonUnavailable, DaemonTimeout) as e:
        print(f"tunetape: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except DaemonError as e:
        print(f"tunetape: {e}", file=sys.stderr)
        return EXIT_DAEMON_ERROR
    except KeyboardInterrupt:
        return 130
