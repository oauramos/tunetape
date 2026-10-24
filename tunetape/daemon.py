"""``tunetape daemon``: the background server every client talks to.

It owns the :class:`~tunetape.engine.Engine` (queue, mpv, resolvers, history)
and exposes it over a local stream socket as NDJSON JSON-RPC 2.0, exactly as
docs/protocol.md describes: a Unix socket on POSIX, loopback TCP plus a token
on Windows, a discovery file so clients can find it, one instance per data dir,
and a clean exit on ``daemon.shutdown``, SIGTERM/SIGINT or idle timeout.

Threads: the main thread accepts connections and watches the idle timer; each
connection has a reader (which runs most methods inline, so one client's
commands apply in the order sent) and a writer draining a bounded outbox, so a
client that stops reading can't stall playback — it is dropped instead. The
methods that can block for long run on a thread of their own.

Standard library only.
"""

import argparse
import hashlib
import json
import math
import os
import queue
import secrets
import signal
import socket
import stat
import sys
import tempfile
import threading
import time

from tunetape import __version__, debug, paths
from tunetape.client import discovery_path, open_endpoint, read_discovery
from tunetape.engine import Engine, EngineError, now_iso

PROTOCOL = 1
SUPPORTED_PROTOCOLS = [1]
DEFAULT_IDLE_TIMEOUT = 600
MAX_LINE = 1 << 20
EVENT_TYPES = ("state", "track", "queue", "request", "error", "shutdown")
_OUTBOX_MAX = 1000
_SOCKET_PATH_MAX = 100  # sun_path is 104 bytes on macOS, 108 on Linux

# JSON-RPC and tunetape error codes (protocol §5).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_AUTHENTICATED = 1001
UNSUPPORTED_PROTOCOL = 1002


# -- endpoint and files -------------------------------------------------------

def _transport() -> str:
    """``unix`` on POSIX, ``tcp`` on Windows.

    ``$TUNETAPE_DAEMON_TRANSPORT=tcp`` forces TCP on POSIX too, so the token
    path that Windows uses is exercised by the POSIX test runs.
    """
    if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
        return "tcp"
    return "tcp" if os.environ.get("TUNETAPE_DAEMON_TRANSPORT") == "tcp" else "unix"


def socket_path(data_dir: str) -> str:
    """Where the Unix socket goes (protocol §1.2).

    ``<data_dir>/daemon.sock`` when that fits in sun_path; otherwise
    ``<tmpdir>/tunetape-<uid>/<sha256(data_dir)[:12]>.sock``, the hash keeping
    two isolated instances (tests, TUNETAPE_DATA_DIR) from colliding.
    """
    path = os.path.join(data_dir, "daemon.sock")
    if len(path.encode("utf-8")) < _SOCKET_PATH_MAX:
        return path
    digest = hashlib.sha256(data_dir.encode("utf-8")).hexdigest()[:12]
    name = os.path.join(f"tunetape-{os.getuid()}", f"{digest}.sock")
    path = os.path.join(tempfile.gettempdir(), name)
    if len(path.encode("utf-8")) >= _SOCKET_PATH_MAX:
        path = os.path.join("/tmp", name)  # a long $TMPDIR: /tmp always fits
    return path


def _private_dir(path: str) -> None:
    """Create ``path`` as a 0700 directory we own, refusing anyone else's."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"{path} is not a directory owned by this user; refusing to use it.")
    os.chmod(path, 0o700)


def _remove(path) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def probe(info: dict, timeout: float = 1.0) -> bool:
    """True when a live daemon answers ``hello`` at the endpoint ``info`` names.

    Any well-formed JSON-RPC answer counts — even an error (a wrong token, another
    protocol version) means something is alive and listening there.
    """
    try:
        sock = open_endpoint(info, timeout)
    except (OSError, ValueError, TypeError):
        return False
    try:
        sock.settimeout(timeout)
        hello = {"jsonrpc": "2.0", "id": 0, "method": "hello",
                 "params": {"protocol": PROTOCOL, "client": "tunetape-daemon-probe",
                            "token": info.get("token")}}
        sock.sendall((json.dumps(hello) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < MAX_LINE:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buf += chunk
        msg = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        return isinstance(msg, dict) and ("result" in msg or "error" in msg)
    except (OSError, ValueError):
        return False
    finally:
        sock.close()


class _InstanceLock:
    """An exclusive OS lock on ``<data_dir>/daemon.lock`` held for the daemon's life.

    The discovery-file probe alone can't stop two daemons that start at the same
    moment (both find no file, both bind); the lock makes the check atomic. The
    OS drops it when the process dies, so a crash never leaves it stuck.
    """

    def __init__(self, path: str):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        f = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._file.close()
        self._file = None


# -- JSON-RPC helpers ---------------------------------------------------------

def _json_safe(value):
    """Replace non-finite floats (JSON has no Infinity/NaN) with None, recursively."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _encode(msg: dict) -> bytes:
    try:
        text = json.dumps(msg, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError:
        text = json.dumps(_json_safe(msg), ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _error(request_id, code, message, data=None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message, "data": data}}


def _valid_id(value) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _bool(params, name, default):
    value = params.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise EngineError(INVALID_PARAMS, f"Invalid params: '{name}' must be a boolean.")
    return value


def _string(params, name):
    value = params.get(name)
    if not isinstance(value, str):
        raise EngineError(INVALID_PARAMS, f"Invalid params: '{name}' must be a string.")
    return value


# -- methods (protocol §4) ----------------------------------------------------

def _queue_add(server, conn, params, at=None):
    return server.engine.add(
        params.get("input"),
        at=at or params.get("at", "end") or "end",
        title=params.get("title"),
        start_index=params.get("start_index", 0),
        wait=_bool(params, "wait", False),
    )


def _subscribe(server, conn, params):
    types = params.get("types")
    if types is not None and not (isinstance(types, list)
                                  and all(isinstance(t, str) for t in types)):
        raise EngineError(INVALID_PARAMS, "Invalid params: 'types' must be a list of strings.")
    # Unknown types are dropped, not refused: a newer client may ask for more.
    subscribed = [t for t in EVENT_TYPES if types is None or t in types]
    conn.subscribed = frozenset(subscribed)
    return {"subscribed": subscribed}


def _info(server, conn, params):
    return {"protocol": PROTOCOL, "version": __version__, "pid": os.getpid(),
            "started_at": server.started_at, "clients": server.client_count(),
            "data_dir": server.data_dir}


def _log(server, conn, params):
    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise EngineError(INVALID_PARAMS, "Invalid params: 'limit' must be a non-negative integer.")
    records = debug.entries()[-limit:] if limit else []
    return {"entries": [{"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "level": level,
                         "message": message} for t, level, message in records]}


def _shutdown(server, conn, params):
    return {}


METHODS = {
    "queue.add": _queue_add,
    "play": lambda s, c, p: _queue_add(s, c, p, at="now"),
    "queue.list": lambda s, c, p: s.engine.queue_list(),
    "queue.jump": lambda s, c, p: s.engine.jump(p.get("index")),
    "queue.remove": lambda s, c, p: s.engine.remove(_string(p, "id")),
    "queue.clear": lambda s, c, p: s.engine.clear(),
    "requests.list": lambda s, c, p: s.engine.requests_list(),
    "requests.get": lambda s, c, p: s.engine.request_get(_string(p, "id")),
    "player.status": lambda s, c, p: s.engine.status(levels=_bool(p, "levels", False),
                                                      stats=_bool(p, "stats", False)),
    "player.pause": lambda s, c, p: s.engine.pause(),
    "player.resume": lambda s, c, p: s.engine.resume(),
    "player.toggle": lambda s, c, p: s.engine.toggle(),
    "player.next": lambda s, c, p: s.engine.next(),
    "player.prev": lambda s, c, p: s.engine.prev(),
    "player.stop": lambda s, c, p: s.engine.stop(),
    "player.seek": lambda s, c, p: s.engine.seek(p.get("seconds"),
                                                 relative=_bool(p, "relative", True)),
    "player.volume": lambda s, c, p: s.engine.set_volume(value=p.get("value"),
                                                         delta=p.get("delta")),
    "player.mute": lambda s, c, p: s.engine.mute(p.get("muted")),
    "ai.discover": lambda s, c, p: s.engine.discover(p.get("request")),
    "history.list": lambda s, c, p: s.engine.history_list(p.get("limit", 50)),
    "events.subscribe": _subscribe,
    "daemon.info": _info,
    "daemon.log": _log,
    "daemon.shutdown": _shutdown,
}

# Methods that may block for long (an expansion with wait, an LLM call, mpv
# teardown) get a thread of their own instead of holding up the connection.
_THREADED = {"queue.add", "play", "ai.discover", "player.stop", "queue.clear",
             "queue.remove", "history.list", "daemon.shutdown"}


# -- connections --------------------------------------------------------------

class _Connection:
    def __init__(self, server, sock):
        self.server = server
        self.sock = sock
        self.authed = False
        self.subscribed = frozenset()
        self._outbox = queue.Queue(maxsize=_OUTBOX_MAX)
        self._lock = threading.Lock()
        self._closing = False
        self._aborted = False
        self.done = threading.Event()

    def start(self) -> None:
        # Separate from __init__ so the server registers the connection first;
        # otherwise a fast client's daemon.info could count itself out.
        threading.Thread(target=self._read_loop, name="tunetape-conn-reader", daemon=True).start()
        threading.Thread(target=self._write_loop, name="tunetape-conn-writer", daemon=True).start()

    # output ------------------------------------------------------------------

    def send(self, msg: dict) -> None:
        self.send_raw(_encode(msg))

    def send_raw(self, line: bytes) -> None:
        if self._closing:
            return
        try:
            self._outbox.put_nowait(line)
        except queue.Full:
            debug.log("Dropping a client that stopped reading its events", "WARN")
            self.close(flush=False)

    def _write_loop(self):
        try:
            while True:
                line = self._outbox.get()
                if line is None or self._aborted:
                    break
                self.sock.sendall(line)
        except OSError:
            pass
        finally:
            self._shutdown_socket()
            try:
                self.sock.close()
            except OSError:
                pass
            self.close(flush=False)
            self.done.set()

    def _shutdown_socket(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self, flush=True) -> None:
        """Stop this connection; with ``flush``, after sending what is queued."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
        self.server.forget(self)
        if flush:
            try:
                self._outbox.put_nowait(None)
                return
            except queue.Full:
                pass
        self._aborted = True
        self._shutdown_socket()
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass

    # input -------------------------------------------------------------------

    def _read_loop(self):
        buf = b""
        try:
            while not self._closing:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf and not self._closing:
                    line, buf = buf.split(b"\n", 1)
                    if len(line) > MAX_LINE:
                        return
                    if line.strip():
                        self._handle_line(line)
                if len(buf) > MAX_LINE:
                    return  # protocol §1.1: no line longer than 1 MiB
        except OSError:
            pass
        finally:
            self.close()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send(_error(None, PARSE_ERROR, "Parse error: not valid JSON."))
            self.close()
            return
        if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0"
                or not isinstance(msg.get("method"), str)
                or ("id" in msg and not _valid_id(msg["id"]))):
            request_id = msg.get("id") if isinstance(msg, dict) and _valid_id(msg.get("id")) else None
            self.send(_error(request_id, INVALID_REQUEST, "Invalid request."))
            return
        request_id = msg.get("id")
        notify = "id" not in msg  # a JSON-RPC notification gets no answer
        method = msg["method"]
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if not notify:
                self.send(_error(request_id, INVALID_PARAMS, "Invalid params: 'params' must be an object."))
            return
        if method == "hello":
            self._hello(request_id, params, notify)
            return
        if not self.authed:
            if not notify:
                self.send(_error(request_id, NOT_AUTHENTICATED,
                                 "Not authenticated: the first request must be hello."))
            self.close()
            return
        handler = METHODS.get(method)
        if handler is None:
            if not notify:
                self.send(_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}"))
            return
        if method in _THREADED:
            threading.Thread(target=self._run, args=(request_id, method, handler, params, notify),
                             name=f"tunetape-{method}", daemon=True).start()
        else:
            self._run(request_id, method, handler, params, notify)

    def _hello(self, request_id, params, notify):
        protocol = params.get("protocol")
        if isinstance(protocol, bool) or not isinstance(protocol, int):
            if not notify:
                self.send(_error(request_id, INVALID_PARAMS,
                                 "Invalid params: 'protocol' must be an integer."))
            return
        if protocol not in SUPPORTED_PROTOCOLS:
            if not notify:
                self.send(_error(request_id, UNSUPPORTED_PROTOCOL,
                                 f"Unsupported protocol version {protocol}.",
                                 {"supported": SUPPORTED_PROTOCOLS}))
            return
        token = self.server.token
        if token is not None:
            given = params.get("token")
            if not isinstance(given, str) or not secrets.compare_digest(given, token):
                if not notify:
                    self.send(_error(request_id, NOT_AUTHENTICATED,
                                     "Not authenticated: missing or wrong token."))
                self.close()
                return
        self.authed = True
        debug.log(f"Client connected: {str(params.get('client') or 'unknown')[:80]}")
        if not notify:
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocol": PROTOCOL, "server": f"tunetape/{__version__}", "pid": os.getpid()}})

    def _run(self, request_id, method, handler, params, notify):
        try:
            result = handler(self.server, self, params)
        except EngineError as e:
            if not notify:
                self.send(_error(request_id, e.code, e.message, e.data))
            return
        except Exception as e:
            debug.exception(f"{method} failed", e)
            if not notify:
                self.send(_error(request_id, INTERNAL_ERROR, f"Internal error: {e}"))
            return
        if not notify:
            self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
        if method == "daemon.shutdown":
            self.server.request_stop("daemon.shutdown")


# -- the server ---------------------------------------------------------------

class Server:
    def __init__(self, data_dir: str, idle_timeout: float):
        self.data_dir = data_dir
        self.idle_timeout = idle_timeout
        self.started_at = now_iso()
        self.token = None
        self.engine = Engine(listener=self._broadcast)
        self._conns = set()
        self._conns_lock = threading.Lock()
        self._stop = threading.Event()
        self._stop_reason = None
        self._listener = None
        self._socket_path = None
        self._discovery = None

    # bookkeeping -------------------------------------------------------------

    def client_count(self) -> int:
        with self._conns_lock:
            return len(self._conns)

    def forget(self, conn) -> None:
        with self._conns_lock:
            self._conns.discard(conn)

    def request_stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stop.set()

    def _broadcast(self, type_, data) -> None:
        line = None
        with self._conns_lock:
            targets = [c for c in self._conns if c.authed and type_ in c.subscribed]
        for conn in targets:
            if line is None:
                line = _encode({"jsonrpc": "2.0", "method": "event",
                                "params": {"type": type_, "data": data}})
            conn.send_raw(line)

    # listening ---------------------------------------------------------------

    def listen(self) -> dict:
        """Bind the endpoint and return the discovery record for it."""
        record = {"protocol": PROTOCOL, "version": __version__, "pid": os.getpid()}
        if _transport() == "unix":
            path = socket_path(self.data_dir)
            if os.path.dirname(path) != self.data_dir:
                _private_dir(os.path.dirname(path))
            _remove(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Born 0600: no window in which another user could connect.
            old_umask = os.umask(0o177)
            try:
                sock.bind(path)
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            self._socket_path = path
            record.update(transport="unix", path=path)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            self.token = secrets.token_urlsafe(32)
            record.update(transport="tcp", host="127.0.0.1", port=sock.getsockname()[1],
                          token=self.token)
        sock.listen(16)
        # Short, so a stop request is noticed within a tenth of a second.
        sock.settimeout(0.1)
        self._listener = sock
        record["started_at"] = self.started_at
        return record

    def publish(self, record: dict) -> None:
        path = discovery_path()
        paths.atomic_write_json(path, record)  # mkstemp: already 0600
        if sys.platform != "win32":
            os.chmod(path, 0o600)
        self._discovery = path

    def serve(self) -> None:
        idle_since = None
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:
                pass
            except OSError as e:
                if self._stop.is_set():
                    break
                debug.exception("accept failed", e)
                time.sleep(0.1)
            else:
                sock.settimeout(None)
                conn = _Connection(self, sock)
                with self._conns_lock:
                    self._conns.add(conn)
                conn.start()
            if self.idle_timeout > 0:
                if self.client_count() == 0 and self.engine.is_idle():
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= self.idle_timeout:
                        self.request_stop("idle timeout")
                else:
                    idle_since = None

    def close(self) -> None:
        """Clean exit (protocol §1.4): mpv, shutdown event, connections, files."""
        debug.log(f"Shutting down ({self._stop_reason or 'stop'})")
        try:
            self.engine.shutdown()
        except Exception as e:
            debug.exception("Engine shutdown failed", e)
        self._broadcast("shutdown", {})
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            conn.close(flush=True)
        deadline = time.monotonic() + 1.0
        for conn in conns:
            conn.done.wait(max(0.0, deadline - time.monotonic()))
        if self._listener is not None:
            self._listener.close()
        # Only remove what is ours: a newer daemon may already have replaced it.
        info = paths.read_json(self._discovery) if self._discovery else None
        if isinstance(info, dict) and info.get("pid") == os.getpid():
            _remove(self._discovery)
        _remove(self._socket_path)


# -- entry point --------------------------------------------------------------

class _LogFile:
    """Mirror debug records into ``<data_dir>/daemon.log``."""

    def __init__(self, path: str):
        open(path, "w").close()  # truncated at start (protocol §1.4)
        # Append mode, like the stdio a spawning client hands us, so the two
        # writers interleave instead of overwriting each other.
        self._file = open(path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def __call__(self, record) -> None:
        t, level, message = record
        with self._lock:
            self._file.write(f"{t.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z {level:<5} {message}\n")

    def close(self) -> None:
        with self._lock:
            self._file.close()


def _wait_for_lock(lock: _InstanceLock, timeout: float = 15.0) -> bool:
    """Take the instance lock, or return False if a live daemon already answers.

    A held lock means a daemon is starting (it will answer soon) or shutting
    down (it will let go soon); waiting tells the two apart.
    """
    deadline = time.monotonic() + timeout
    while True:
        if lock.acquire():
            return True
        info = read_discovery()
        if info is not None and probe(info):
            return False
        if time.monotonic() >= deadline:
            raise RuntimeError("another tunetape daemon holds the lock but does not answer")
        time.sleep(0.1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tunetape daemon",
                                     description="Run the tunetape daemon in the foreground.")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
                        metavar="SECONDS",
                        help="exit after this long idle with no clients (0 disables; default 600)")
    args = parser.parse_args(argv)
    if args.idle_timeout < 0:
        parser.error("--idle-timeout must be >= 0")

    try:
        data_dir = paths.ensure_dir()
    except OSError as e:
        print(f"tunetape daemon: cannot create the data directory: {e}", file=sys.stderr)
        return 1

    lock = _InstanceLock(os.path.join(data_dir, "daemon.lock"))
    try:
        if not _wait_for_lock(lock):
            print("tunetape daemon is already running.")
            return 0
    except (OSError, RuntimeError) as e:
        print(f"tunetape daemon: {e}", file=sys.stderr)
        return 1

    log = None
    server = None
    try:
        # Single instance (protocol §1.4): a daemon that predates the lock, or
        # a stale file from one that crashed.
        info = read_discovery()
        if info is not None:
            if probe(info):
                print(f"tunetape daemon is already running (pid {info.get('pid')}).")
                return 0
            if info.get("transport") == "unix":
                _remove(info.get("path"))
            _remove(discovery_path())

        log = _LogFile(os.path.join(data_dir, "daemon.log"))
        debug.add_sink(log)
        debug.log(f"tunetape daemon {__version__} starting (pid {os.getpid()}, "
                  f"data dir {data_dir})")
        threading.excepthook = lambda a: debug.log(
            f"Uncaught {a.exc_type.__name__} in {a.thread.name if a.thread else '?'}: "
            f"{a.exc_value}", "ERROR")

        server = Server(data_dir, args.idle_timeout)
        record = server.listen()
        server.publish(record)
        where = record.get("path") or f"{record['host']}:{record['port']}"
        debug.log(f"Listening on {record['transport']} {where}")
        print(f"tunetape daemon listening on {where} (pid {os.getpid()})", flush=True)

        def on_signal(signum, frame):
            server.request_stop(signal.Signals(signum).name)

        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), on_signal)
                except (OSError, ValueError):
                    pass
        server.serve()
        return 0
    except Exception as e:
        debug.exception("Daemon failed", e)
        print(f"tunetape daemon: {e}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.close()
        lock.release()
        if log is not None:
            debug.remove_sink(log)
            log.close()


if __name__ == "__main__":
    sys.exit(main())
