"""The Python client for the tunetape daemon (docs/protocol.md, docs/architecture.md).

The TUI, the CLI and the MCP server all talk to the daemon through
:class:`Client`; it is also the in-repo reference client for the protocol:

    with Client.connect() as client:          # starts the daemon if needed
        client.play("https://youtu.be/…")
        print(client.status()["state"])

One reader thread per connection routes responses to callers by ``id``, so
``call`` is safe from several threads at once. Event notifications go to the
``subscribe`` callback on a separate dispatcher thread, so a callback may itself
``call`` the daemon without deadlocking the reader. Standard library only.
"""

import itertools
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

from tunetape import paths

PROTOCOL = 1
_DEFAULT_TIMEOUT = 30.0
_WAIT_TIMEOUT = 300.0      # wait=True: an album page can take a while to fetch
_DISCOVER_TIMEOUT = 120.0  # the LLM call alone may take 45s


class DaemonError(Exception):
    """The daemon answered with an error (docs/protocol.md §5)."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self):
        return self.message


class DaemonUnavailable(DaemonError):
    """The daemon could not be reached or started, or the connection dropped. Code 0."""

    def __init__(self, message: str, data=None):
        super().__init__(0, message, data)


class DaemonTimeout(DaemonError):
    """A call got no answer within its timeout. Code -1; the connection stays usable."""

    def __init__(self, message: str = "The daemon did not answer in time.", data=None):
        super().__init__(-1, message, data)


# -- discovery (protocol §1.2) -------------------------------------------------

def discovery_path() -> str:
    return os.path.join(paths.data_dir(), "daemon.json")


def log_path() -> str:
    return os.path.join(paths.data_dir(), "daemon.log")


def read_discovery():
    """The parsed discovery file, or None when it is missing or not usable."""
    info = paths.read_json(discovery_path())
    if not isinstance(info, dict) or info.get("transport") not in ("unix", "tcp"):
        return None
    return info


def open_endpoint(info: dict, timeout: float) -> socket.socket:
    """A connected stream socket to the endpoint a discovery file names. Raises OSError."""
    if info.get("transport") == "unix":
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("Unix sockets are not available on this platform")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(info.get("path")))
        except OSError:
            sock.close()
            raise
    else:
        host = info.get("host")
        if host != "127.0.0.1":
            # The daemon only ever binds loopback; anything else is not ours.
            raise OSError(f"refusing non-loopback daemon address {host!r}")
        sock = socket.create_connection((host, int(info.get("port") or 0)), timeout)
    sock.settimeout(None)
    return sock


def _spawn_daemon():
    """Start ``python -m tunetape daemon`` detached from this process and terminal."""
    data_dir = paths.data_dir()
    log = None
    try:
        os.makedirs(data_dir, exist_ok=True)
        # Anything the daemon prints before its own log is up (an import error,
        # say) lands here; the daemon then appends to the same file.
        log = open(os.path.join(data_dir, "daemon.log"), "ab")
    except OSError:
        pass
    env = dict(os.environ)
    # Make the child import this same tunetape, even from a source checkout.
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_parent, env.get("PYTHONPATH")) if p)
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log if log is not None else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if log is not None else subprocess.DEVNULL,
        "env": env,
        "close_fds": True,
        # Not the caller's cwd: the daemon outlives it, and on Windows a process
        # sitting in a directory keeps it from being deleted.
        "cwd": data_dir if os.path.isdir(data_dir) else None,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([sys.executable, "-m", "tunetape", "daemon"], **kwargs)
    except OSError as e:
        raise DaemonUnavailable(f"Could not start the tunetape daemon: {e}")
    finally:
        if log is not None:
            log.close()
    # Reap it when it eventually exits, so a long-lived client leaves no zombie.
    threading.Thread(target=proc.wait, name="tunetape-daemon-reaper", daemon=True).start()
    return proc


# -- the client ---------------------------------------------------------------

class Client:
    """One connection to the daemon. Use :meth:`connect` to make one."""

    def __init__(self, sock: socket.socket, info: dict, name: str):
        self._sock = sock
        self._info = info
        self._name = name
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._ids = itertools.count(1)
        self._closed = False
        self._close_reason = "The connection to the tunetape daemon was lost."
        self._callback = None
        self._events = None
        self.hello = None  # the daemon's hello result: protocol, server, pid
        self._reader = threading.Thread(target=self._read_loop,
                                        name="tunetape-client-reader", daemon=True)
        self._reader.start()

    # connecting --------------------------------------------------------------

    @classmethod
    def connect(cls, *, spawn: bool = True, timeout: float = 10.0,
                name: str = "tunetape-python") -> "Client":
        """Connect to the daemon, starting it first when needed (protocol §1.3).

        Raises DaemonUnavailable when it can't be reached (and ``spawn`` is
        False, or starting it failed), DaemonError 1002 when it speaks another
        protocol version.
        """
        client, err = cls._attempt(name)
        if client is not None:
            return client
        if isinstance(err, DaemonError) and err.code == 1002:
            raise err
        if not spawn:
            raise DaemonUnavailable("The tunetape daemon is not running.")
        proc = _spawn_daemon()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.1)
            client, err = cls._attempt(name)
            if client is not None:
                return client
            if isinstance(err, DaemonError) and err.code == 1002:
                raise err
            code = proc.poll()
            # Status 0 means another daemon won the start-up race; keep looking.
            if code not in (None, 0):
                raise DaemonUnavailable(
                    f"The tunetape daemon failed to start (exit status {code}). "
                    f"See {log_path()}.")
        raise DaemonUnavailable(
            f"The tunetape daemon did not come up within {timeout:g}s. See {log_path()}.")

    @classmethod
    def _attempt(cls, name):
        """(client, None) on success, else (None, the error or None)."""
        info = read_discovery()
        if info is None:
            return None, None
        try:
            sock = open_endpoint(info, 2.0)
        except OSError as e:
            return None, e
        client = cls(sock, info, name)
        try:
            client.hello = client.call("hello", {
                "protocol": PROTOCOL, "client": name, "token": info.get("token"),
            }, timeout=5.0)
        except DaemonError as e:
            client.close()
            return None, e
        return client, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # the wire ------------------------------------------------------------------

    def _read_loop(self):
        buf = b""
        sock = self._sock  # close() clears the attribute; keep our own handle
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch(line)
        except OSError:
            pass
        finally:
            self._fail_all()

    def _dispatch(self, line: bytes):
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("method") == "event":
            params = msg.get("params") or {}
            events = self._events
            if events is not None and isinstance(params, dict):
                events.put((params.get("type"), params.get("data")))
            return
        with self._pending_lock:
            slot = self._pending.pop(msg.get("id"), None)
        if slot is not None:
            slot["msg"] = msg
            slot["done"].set()

    def _fail_all(self):
        with self._pending_lock:
            self._closed = True
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot["done"].set()  # msg stays None: the caller raises DaemonUnavailable
        events = self._events
        if events is not None:
            events.put(None)

    def call(self, method: str, params: dict = None, *, timeout: float = _DEFAULT_TIMEOUT):
        """Send one request and return its result. Thread-safe.

        Raises DaemonError for an error response, DaemonTimeout when there is no
        answer within ``timeout`` seconds (None waits forever), and
        DaemonUnavailable once the connection is gone.
        """
        slot = {"done": threading.Event(), "msg": None}
        with self._pending_lock:
            if self._closed:
                raise DaemonUnavailable(self._close_reason)
            request_id = next(self._ids)
            self._pending[request_id] = slot
        line = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": params or {}}) + "\n"
        try:
            with self._send_lock:
                sock = self._sock
                if sock is None:
                    raise OSError("closed")
                sock.sendall(line.encode("utf-8"))
        except OSError:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self.close()
            raise DaemonUnavailable(self._close_reason)
        if not slot["done"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise DaemonTimeout()
        msg = slot["msg"]
        if msg is None:
            raise DaemonUnavailable(self._close_reason)
        if "error" in msg:
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            raise DaemonError(err.get("code", -32603), err.get("message") or "Unknown error.",
                              err.get("data"))
        return msg.get("result")

    def subscribe(self, callback, types: list = None) -> None:
        """Have ``callback(type, data)`` called for each event (protocol §4.4).

        Callbacks run one at a time on a dispatcher thread, in arrival order.
        """
        if self._events is None:
            self._events = queue.Queue()
            threading.Thread(target=self._event_loop, name="tunetape-client-events",
                             daemon=True).start()
        self._callback = callback
        params = {} if types is None else {"types": list(types)}
        self.call("events.subscribe", params)

    def _event_loop(self):
        while True:
            item = self._events.get()
            if item is None:
                return
            callback = self._callback
            if callback is None:
                continue
            try:
                callback(*item)
            except Exception:
                pass  # a subscriber's bug must not stop event delivery

    def close(self) -> None:
        with self._pending_lock:
            if self._closed and self._sock is None:
                return
            self._close_reason = "The connection to the tunetape daemon is closed."
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._fail_all()

    # thin wrappers -------------------------------------------------------------
    # ``timeout=None`` means this method's usual timeout, which is longer for
    # the calls that wait on a fetch or an LLM.

    def _wrap(self, method, params, timeout, default=_DEFAULT_TIMEOUT):
        params = {k: v for k, v in params.items() if v is not None}
        return self.call(method, params, timeout=default if timeout is None else timeout)

    def play(self, input, *, title=None, start_index=0, wait=False, timeout=None) -> dict:
        return self._wrap("play", {"input": input, "title": title, "start_index": start_index,
                                   "wait": wait}, timeout,
                          _WAIT_TIMEOUT if wait else _DEFAULT_TIMEOUT)

    def add(self, input, *, at="end", title=None, wait=False, timeout=None) -> dict:
        return self._wrap("queue.add", {"input": input, "at": at, "title": title,
                                        "wait": wait}, timeout,
                          _WAIT_TIMEOUT if wait else _DEFAULT_TIMEOUT)

    def status(self, *, levels=False, stats=False, timeout=None) -> dict:
        return self._wrap("player.status", {"levels": levels, "stats": stats}, timeout)

    def queue(self, *, timeout=None) -> dict:
        return self._wrap("queue.list", {}, timeout)

    def jump(self, index, *, timeout=None) -> dict:
        return self._wrap("queue.jump", {"index": index}, timeout)

    def remove(self, item_id, *, timeout=None) -> dict:
        return self._wrap("queue.remove", {"id": item_id}, timeout)

    def clear(self, *, timeout=None) -> dict:
        return self._wrap("queue.clear", {}, timeout)

    def pause(self, *, timeout=None) -> dict:
        return self._wrap("player.pause", {}, timeout)

    def resume(self, *, timeout=None) -> dict:
        return self._wrap("player.resume", {}, timeout)

    def toggle(self, *, timeout=None) -> dict:
        return self._wrap("player.toggle", {}, timeout)

    def next(self, *, timeout=None) -> dict:
        return self._wrap("player.next", {}, timeout)

    def prev(self, *, timeout=None) -> dict:
        return self._wrap("player.prev", {}, timeout)

    def stop(self, *, timeout=None) -> dict:
        return self._wrap("player.stop", {}, timeout)

    def seek(self, seconds, *, relative=True, timeout=None) -> dict:
        return self._wrap("player.seek", {"seconds": seconds, "relative": relative}, timeout)

    def volume(self, *, value=None, delta=None, timeout=None) -> dict:
        return self._wrap("player.volume", {"value": value, "delta": delta}, timeout)

    def mute(self, muted=None, *, timeout=None) -> dict:
        return self._wrap("player.mute", {"muted": muted}, timeout)

    def requests(self, *, timeout=None) -> dict:
        return self._wrap("requests.list", {}, timeout)

    def request(self, request_id, *, timeout=None) -> dict:
        return self._wrap("requests.get", {"id": request_id}, timeout)

    def discover(self, request, *, timeout=None) -> dict:
        return self._wrap("ai.discover", {"request": request}, timeout, _DISCOVER_TIMEOUT)

    def history(self, limit=50, *, timeout=None) -> dict:
        return self._wrap("history.list", {"limit": limit}, timeout)

    def info(self, *, timeout=None) -> dict:
        return self._wrap("daemon.info", {}, timeout)

    def log(self, limit=200, *, timeout=None) -> dict:
        return self._wrap("daemon.log", {"limit": limit}, timeout)

    def shutdown(self, *, timeout=None) -> dict:
        return self._wrap("daemon.shutdown", {}, timeout)


__all__ = ["Client", "DaemonError", "DaemonTimeout", "DaemonUnavailable", "PROTOCOL"]
