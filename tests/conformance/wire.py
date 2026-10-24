"""Raw NDJSON / JSON-RPC plumbing for the protocol conformance suite.

Deliberately independent of ``tunetape.client``: the suite tests the wire, so
it opens the socket named in ``daemon.json`` itself and checks every message
the daemon sends against ``protocol/v1.schema.json``.
"""

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "protocol" / "v1.schema.json"
FAKE_MPV = REPO / "tests" / "fakes" / "fake_mpv.py"
WINDOWS = sys.platform == "win32"

# Event types protocol 1 defines (§4.4).
EVENT_TYPES = ("state", "track", "queue", "request", "error", "shutdown")


def default_daemon_cmd():
    return [sys.executable, "-m", "tunetape", "daemon", "--idle-timeout", "0"]


def daemon_cmd():
    """The daemon under test: $TUNETAPE_CONFORMANCE_DAEMON, else the Python one."""
    raw = os.environ.get("TUNETAPE_CONFORMANCE_DAEMON", "").strip()
    if not raw:
        return default_daemon_cmd()
    return shlex.split(raw, posix=not WINDOWS)


def using_default_daemon():
    return not os.environ.get("TUNETAPE_CONFORMANCE_DAEMON", "").strip()


def command_line(argv):
    """Join argv into one string that the daemon will split back (architecture.md)."""
    if WINDOWS:
        return subprocess.list2cmdline([str(a) for a in argv])
    return " ".join(shlex.quote(str(a)) for a in argv)


def wait_for(fn, timeout=10.0, interval=0.05, what="condition"):
    """Poll ``fn`` until it returns something truthy; return that value."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = fn()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}; last value: {last!r}")
        time.sleep(interval)


# --------------------------------------------------------------------------- schema


class Schema:
    """protocol/v1.schema.json with validators addressed by JSON pointer."""

    def __init__(self, path=SCHEMA_PATH):
        import jsonschema
        from referencing import Registry, Resource

        self._jsonschema = jsonschema
        with open(path, encoding="utf-8") as f:
            self.doc = json.load(f)
        jsonschema.Draft202012Validator.check_schema(self.doc)
        self.id = self.doc["$id"]
        self._registry = Registry().with_resource(self.id, Resource.from_contents(self.doc))
        self._validators = {}
        self._format = jsonschema.FormatChecker()

    def has_method(self, method):
        return method in self.doc["$defs"]["methods"]

    def has_event(self, etype):
        return etype in self.doc["$defs"]["events"]

    def error_codes(self):
        return {c["const"] for c in self.doc["$defs"]["ErrorCode"]["oneOf"]}

    def check(self, pointer, instance, context=""):
        v = self._validators.get(pointer)
        if v is None:
            v = self._jsonschema.Draft202012Validator(
                {"$ref": self.id + "#" + pointer},
                registry=self._registry,
                format_checker=self._format,
            )
            self._validators[pointer] = v
        errors = list(v.iter_errors(instance))
        if errors:
            best = self._jsonschema.exceptions.best_match(errors)
            where = "/".join(str(p) for p in best.absolute_path) or "(root)"
            text = json.dumps(instance, ensure_ascii=False)
            if len(text) > 3000:
                text = text[:3000] + "…"
            raise AssertionError(
                f"{context or pointer} does not match {pointer}: {best.message} at {where}\n{text}"
            )

    def check_server_message(self, msg):
        """Validate a message from the daemon against the envelope it claims to be."""
        if not isinstance(msg, dict):
            raise AssertionError(f"daemon sent a non-object message: {msg!r}")
        if "id" not in msg and msg.get("method") == "event":
            self.check("/$defs/envelope/event", msg, "event")
        elif "error" in msg:
            self.check("/$defs/envelope/error", msg, "error response")
        elif "result" in msg:
            self.check("/$defs/envelope/success", msg, "response")
        else:
            self.check("/$defs/serverMessage", msg, "message")


# --------------------------------------------------------------------------- connection


class RpcError(AssertionError):
    pass


AUTO = object()


def _reject_constant(name):
    # NaN / Infinity / -Infinity are not JSON; levels send silence as -200.0 (§4.2).
    raise ValueError(f"{name} is not valid JSON")


class Conn:
    """One raw NDJSON connection to the daemon."""

    def __init__(self, discovery, schema, timeout=10.0):
        self.schema = schema
        self.discovery = discovery
        self.timeout = timeout
        if discovery["transport"] == "unix":
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(discovery["path"])
        else:
            self.sock = socket.create_connection((discovery["host"], discovery["port"]), timeout=timeout)
        self._buf = b""
        self._eof = False
        self._next_id = 0
        self._methods = {}  # id -> method, to validate results
        self.responses = {}  # id -> message
        self.unmatched = []  # responses with an id we never sent (e.g. null)
        self.events = []  # every event received, in order

    # -- sending

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, obj, ascii_only=False):
        line = json.dumps(obj, ensure_ascii=ascii_only, separators=(",", ":"))
        self.send_raw(line.encode("utf-8") + b"\n")

    def new_id(self):
        self._next_id += 1
        return self._next_id

    def request(self, method, params=None, id=None):
        """Send a request; return its id."""
        rid = self.new_id() if id is None else id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._methods[rid] = method
        self.send(msg)
        return rid

    # -- receiving

    def recv(self, timeout=None):
        """Next message from the daemon, or None at EOF. Raises TimeoutError."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while b"\n" not in self._buf:
            if self._eof:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no message from the daemon")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise TimeoutError("no message from the daemon") from None
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                chunk = b""
            if not chunk:
                self._eof = True
                if self._buf.strip():
                    raise AssertionError(f"connection closed mid-line: {self._buf[:200]!r}")
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            msg = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except ValueError as e:
            raise AssertionError(f"daemon sent a line that is not JSON ({e}): {line[:300]!r}") from None
        self.schema.check_server_message(msg)
        self._route(msg)
        return msg

    def _route(self, msg):
        if "id" not in msg:
            etype = msg["params"]["type"]
            self.events.append(msg["params"])
            assert etype, "event without a type"
            return
        rid = msg["id"]
        if rid not in self._methods:
            self.unmatched.append(msg)
            return
        if "result" in msg:
            method = self._methods[rid]
            if self.schema.has_method(method):
                self.schema.check(f"/$defs/methods/{method}/result", msg["result"], f"{method} result")
        self.responses[rid] = msg

    def response(self, rid, timeout=None):
        """Wait for the response to ``rid``, routing anything else that arrives."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while rid not in self.responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response to request {rid!r}")
            if self.recv(remaining) is None:
                raise AssertionError(f"connection closed before the response to {rid!r}")
        return self.responses.pop(rid)

    def call(self, method, params=None, timeout=None, id=None):
        return self.response(self.request(method, params, id=id), timeout)

    def result(self, method, params=None, timeout=None):
        msg = self.call(method, params, timeout)
        if "error" in msg:
            raise RpcError(f"{method} {params!r} failed: {msg['error']}")
        return msg["result"]

    def error(self, method, params=None, code=None, timeout=None):
        msg = self.call(method, params, timeout)
        assert "error" in msg, f"{method} {params!r} should fail with {code}, got {msg.get('result')!r}"
        err = msg["error"]
        if code is not None:
            assert err["code"] == code, f"{method} {params!r}: expected error {code}, got {err}"
        return err

    def hello(self, protocol=1, token=AUTO, **extra):
        """Send hello. ``token`` defaults to the discovery token; None omits it."""
        params = {"protocol": protocol, "client": "tunetape-conformance/1"}
        if token is AUTO:
            token = self.discovery.get("token")
        if token is not None:
            params["token"] = token
        params.update(extra)
        return self.call("hello", params)

    def wait_event(self, etype, pred=None, timeout=10.0):
        """Return the data of the first (unconsumed) ``etype`` event matching ``pred``."""
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            for i in range(seen, len(self.events)):
                ev = self.events[i]
                if ev["type"] == etype and (pred is None or pred(ev["data"])):
                    del self.events[i]
                    return ev["data"]
            seen = len(self.events)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"no matching {etype!r} event within {timeout}s; got types "
                    f"{[e['type'] for e in self.events]}"
                )
            try:
                if self.recv(remaining) is None:
                    raise AssertionError(f"connection closed while waiting for a {etype!r} event")
            except TimeoutError:
                pass

    def pump(self, seconds):
        """Read whatever arrives for ``seconds``; return False if the daemon hung up."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            try:
                if self.recv(remaining) is None:
                    return False
            except TimeoutError:
                return True

    def closed_by_daemon(self, timeout=5.0):
        """True once the daemon closes the connection within ``timeout``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.recv(deadline - time.monotonic()) is None:
                    return True
            except TimeoutError:
                return False
            except OSError:
                return True
        return False

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- daemon process


def short_temp_dir(prefix):
    """A temp dir short enough that <dir>/daemon.sock stays under 100 bytes (§1.2)."""
    path = tempfile.mkdtemp(prefix=prefix)
    if not WINDOWS and len(os.path.join(path, "daemon.sock").encode()) >= 100 and os.path.isdir("/tmp"):
        shutil.rmtree(path, ignore_errors=True)
        path = tempfile.mkdtemp(prefix=prefix, dir="/tmp")
    return path


class Daemon:
    """A daemon process under test, isolated in its own TUNETAPE_DATA_DIR."""

    def __init__(self, schema, data_dir, cmd=None, fake_duration=30, env=None):
        self.schema = schema
        self.data_dir = data_dir
        self.cmd = list(cmd or daemon_cmd())
        self.discovery_path = os.path.join(data_dir, "daemon.json")
        self.output_path = os.path.join(os.path.dirname(data_dir), os.path.basename(data_dir) + ".out")
        self.env = self._env(fake_duration, env or {})
        self.proc = None
        self.conns = []

    def _env(self, fake_duration, extra):
        env = dict(os.environ)
        # The daemon must never see the user's AI keys or real data dir.
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TUNETAPE_CONFORMANCE_DAEMON",
                    "TUNETAPE_DAEMON_TRANSPORT"):
            env.pop(key, None)
        env["TUNETAPE_DATA_DIR"] = self.data_dir
        env["XDG_DATA_HOME"] = os.path.join(self.data_dir, "xdg")
        env["TUNETAPE_MPV"] = command_line([sys.executable, str(FAKE_MPV)])
        # No yt-dlp at all: nothing in the suite may reach the network.
        env["TUNETAPE_YTDLP"] = os.path.join(self.data_dir, "no-such-yt-dlp")
        env["FAKE_MPV_DURATION"] = str(fake_duration)
        env["PYTHONUNBUFFERED"] = "1"
        env.update(extra)
        return env

    def spawn(self):
        """Start the process without waiting for it to listen."""
        kwargs = {}
        if WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        out = open(self.output_path, "ab")
        try:
            proc = subprocess.Popen(
                self.cmd, env=self.env, cwd=str(REPO), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, **kwargs,
            )
        finally:
            out.close()
        return proc

    def start(self, timeout=20.0):
        self.proc = self.spawn()
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"daemon exited with {self.proc.returncode} before listening; output:\n{self.output()}"
                )
            if self._answers():
                return self
            if time.monotonic() >= deadline:
                raise AssertionError(f"daemon did not start within {timeout}s; output:\n{self.output()}")
            time.sleep(0.05)

    def _answers(self):
        disc = self.read_discovery()
        if not disc:
            return False
        try:
            c = Conn(disc, self.schema, timeout=2.0)
        except OSError:
            return False
        try:
            msg = c.hello()
            return "result" in msg
        except (OSError, TimeoutError, AssertionError):
            return False
        finally:
            c.close()

    def output(self):
        try:
            with open(self.output_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        log = os.path.join(self.data_dir, "daemon.log")
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                text += "\n--- daemon.log ---\n" + f.read()[-4000:]
        except OSError:
            pass
        return text[-8000:]

    def read_discovery(self):
        try:
            with open(self.discovery_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def discovery(self):
        disc = self.read_discovery()
        assert disc is not None, f"{self.discovery_path} is missing or not JSON"
        return disc

    def connect(self, hello=True, timeout=10.0):
        conn = Conn(self.discovery(), self.schema, timeout=timeout)
        self.conns.append(conn)
        if hello:
            msg = conn.hello()
            assert "result" in msg, f"hello failed: {msg}"
        return conn

    def wait_exit(self, timeout=10.0):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"daemon still running after {timeout}s; output:\n{self.output()}") from None

    def stop(self):
        for c in self.conns:
            c.close()
        self.conns = []
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            disc = self.read_discovery()
            if disc:
                try:
                    c = Conn(disc, self.schema, timeout=2.0)
                    c.hello()
                    c.call("daemon.shutdown", timeout=3.0)
                    c.close()
                except (OSError, TimeoutError, AssertionError):
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        _reap_group(proc.pid)


def _reap_group(pid):
    """Kill anything the daemon left behind in its process group (fake mpv)."""
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# --------------------------------------------------------------------------- shorthands


def add(conn, input, wait=True, **params):
    """queue.add ``input`` and return the Request; asserts a ``done`` request when waiting."""
    params = dict(params, input=input, wait=wait)
    request = conn.result("queue.add", params, timeout=20.0)["request"]
    if wait:
        assert request["status"] == "done", f"queue.add {input!r} with wait: {request}"
        assert request["item_ids"], f"done request without items: {request}"
    return request


def first_item(request):
    return request["item_ids"][0]


def status(conn, **params):
    return conn.result("player.status", params or None)


def queue_items(conn):
    return conn.result("queue.list")["items"]


def queue_ids(conn):
    return [it["id"] for it in queue_items(conn)]


def item_status(conn):
    return {it["id"]: it["status"] for it in queue_items(conn)}


def wait_status(conn, pred, timeout=10.0, what="player status"):
    box = {}

    def probe():
        box["s"] = status(conn)
        return pred(box["s"])

    try:
        wait_for(probe, timeout=timeout, what=what)
    except AssertionError as e:
        raise AssertionError(f"{e}\nlast status: {json.dumps(box.get('s'), ensure_ascii=False)}") from None
    return box["s"]


def wait_playing(conn, item_id=None, timeout=10.0):
    """Wait until the player is ``playing`` (``item_id`` if given)."""
    def ok(s):
        return s["state"] == "playing" and s["current"] is not None and (
            item_id is None or s["current"]["id"] == item_id)
    return wait_status(conn, ok, timeout, what=f"playing {item_id or 'anything'}")


def wait_idle(conn, timeout=15.0):
    return wait_status(conn, lambda s: s["state"] == "idle", timeout, what="idle")
