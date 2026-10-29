"""A stand-in for mpv that needs no audio device (docs/architecture.md, "Test seams").

Point ``TUNETAPE_MPV`` at ``<python> tests/fakes/fake_mpv.py`` and tunetape
launches this instead of mpv. It accepts mpv's command line, serves mpv's JSON
IPC on the ``--input-ipc-server`` endpoint, "plays" for ``FAKE_MPV_DURATION``
seconds (default 2) and then exits 0, like mpv at the end of a file.

Knobs, all optional:

- a media argument containing ``fake-fail`` exits with status 2 before the
  IPC endpoint exists (mpv failing to start);
- ``fake-dur-<seconds>`` in the media argument overrides FAKE_MPV_DURATION
  for that one launch, e.g. ``/tmp/song-fake-dur-30.mp3``;
- ``FAKE_MPV_LOG=<path>`` appends one JSON line per launch with the argv, so a
  test can check the flags tunetape passed (``--volume``, ``--af``);
- ``FAKE_MPV_SLOW_QUIT=<seconds>`` plays a wedged mpv: it ignores SIGTERM and
  lingers that long after a ``quit`` command.

POSIX serves a Unix socket. Windows serves a named pipe through pywin32 (a
tunetape dependency there); that path has not been exercised on a real Windows
machine, so the tests that need it skip on Windows.
"""

import json
import os
import re
import signal
import sys
import threading
import time


class FakePlayer:
    def __init__(self, argv):
        self.ipc_path = None
        self.volume = 100.0
        self.filters = []
        self.media = None
        rest = list(argv)
        while rest:
            arg = rest.pop(0)
            if arg == "--":
                if rest:
                    self.media = rest[0]
                break
            if arg.startswith("--input-ipc-server="):
                self.ipc_path = arg.split("=", 1)[1]
            elif arg.startswith("--volume="):
                self.volume = float(arg.split("=", 1)[1])
            elif arg.startswith("--af="):
                self.filters = [f for f in arg.split("=", 1)[1].split(",") if f]
            elif not arg.startswith("--") and self.media is None:
                self.media = arg
        media = self.media or ""
        m = re.search(r"fake-dur-([0-9.]+)", media)
        self.duration = float(m.group(1).rstrip(".")) if m else float(
            os.environ.get("FAKE_MPV_DURATION", "2"))
        self.paused = False
        self.muted = False
        self.position = 0.0
        self._clock = time.monotonic()
        self.lock = threading.Lock()
        self.done = threading.Event()

    # -- the play clock -----------------------------------------------------

    def tick(self):
        with self.lock:
            now = time.monotonic()
            if not self.paused:
                self.position += now - self._clock
            self._clock = now
            if self.position >= self.duration:
                self.position = self.duration
                self.done.set()
            return self.position

    # -- properties -----------------------------------------------------------

    def _af_list(self):
        out = []
        for f in self.filters:
            label = None
            if f.startswith("@") and ":" in f:
                label, f = f[1:].split(":", 1)
            name = f.split("=", 1)[0]
            entry = {"name": name, "enabled": True, "params": {}}
            if label:
                entry["label"] = label
            out.append(entry)
        return out

    def get(self, name):
        """(found, value) for an mpv property name."""
        pos = self.tick()
        if name in ("time-pos", "playback-time"):
            return True, pos
        if name == "duration":
            return True, self.duration
        if name == "pause":
            return True, self.paused
        if name == "volume":
            return True, self.volume
        if name == "mute":
            return True, self.muted
        if name == "path" or name == "filename":
            return True, self.media
        if name.startswith("af-metadata/"):
            label = name.split("/", 1)[1]
            if not any(f.get("label") == label for f in self._af_list()):
                return False, None
            # Channel 2 is silent: mpv reports that as the string "-inf".
            return True, {
                "lavfi.astats.1.RMS_level": "-20.500000",
                "lavfi.astats.1.Peak_level": "-6.000000",
                "lavfi.astats.2.RMS_level": "-inf",
                "lavfi.astats.2.Peak_level": "-inf",
            }
        if name == "audio-params":
            return True, {"format": "floatp", "samplerate": 44100,
                          "channels": "stereo", "hr-channels": "stereo",
                          "channel-count": 2}
        if name == "audio-codec-name":
            return True, "mp3"
        if name == "audio-bitrate":
            return True, 128000
        if name == "file-format":
            return True, "mp3"
        if name == "demuxer-cache-duration":
            return True, 10.0
        if name == "demuxer-cache-state":
            return True, {"total-bytes": 163840, "cache-end": 10.0}
        if name == "cache-speed":
            return True, 0
        if name == "af":
            return True, self._af_list()
        return False, None

    def set(self, name, value):
        with self.lock:
            if name == "pause":
                self.paused = bool(value)
            elif name == "mute":
                self.muted = bool(value)
            elif name == "volume":
                self.volume = max(0.0, min(float(value), 130.0))
            else:
                return False
        return True

    def handle(self, msg):
        """Run one IPC command; returns the reply dict (without request_id)."""
        cmd = msg.get("command") or []
        if not cmd:
            return {"error": "invalid parameter"}
        op, args = cmd[0], cmd[1:]
        if op == "get_property" and args:
            found, value = self.get(args[0])
            return {"data": value, "error": "success"} if found else {"error": "property unavailable"}
        if op == "set_property" and len(args) >= 2:
            return {"error": "success" if self.set(args[0], args[1]) else "property not found"}
        if op == "cycle" and args:
            found, value = self.get(args[0])
            if not found or not isinstance(value, bool):
                return {"error": "property not found"}
            self.set(args[0], not value)
            return {"error": "success"}
        if op == "add" and args and args[0] == "volume":
            delta = float(args[1]) if len(args) > 1 else 1.0
            self.set("volume", self.volume + delta)
            return {"error": "success"}
        if op == "seek" and args:
            mode = args[1] if len(args) > 1 else "relative"
            self.tick()
            with self.lock:
                target = float(args[0]) + (self.position if mode == "relative" else 0.0)
                self.position = max(0.0, min(target, self.duration))
            return {"error": "success"}
        if op in ("quit", "stop"):
            slow = float(os.environ.get("FAKE_MPV_SLOW_QUIT", "0") or 0)
            if slow:
                threading.Timer(slow, self.done.set).start()
            else:
                self.done.set()
            return {"error": "success"}
        return {"error": "invalid parameter"}

    def serve_line(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        reply = self.handle(msg)
        if "request_id" in msg:
            reply["request_id"] = msg["request_id"]
        return (json.dumps(reply) + "\n").encode()


def _serve_unix(player):
    import socket

    try:
        os.unlink(player.ipc_path)
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(player.ipc_path)
    server.listen(4)
    server.settimeout(0.05)

    def client(conn):
        buf = b""
        conn.settimeout(0.1)
        while not player.done.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                out = player.serve_line(line)
                if out:
                    try:
                        conn.sendall(out)
                    except OSError:
                        return
        conn.close()

    while not player.done.is_set():
        player.tick()
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        threading.Thread(target=client, args=(conn,), daemon=True).start()
    server.close()
    try:
        os.unlink(player.ipc_path)
    except OSError:
        pass


def _serve_pipe(player):  # pragma: no cover - Windows only, see the module docstring
    import pywintypes
    import win32file
    import win32pipe

    def make():
        return win32pipe.CreateNamedPipe(
            player.ipc_path, win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, None)

    def client(handle):
        buf = b""
        while not player.done.is_set():
            try:
                _, avail, _ = win32pipe.PeekNamedPipe(handle, 0)
                if not avail:
                    time.sleep(0.01)
                    continue
                _, data = win32file.ReadFile(handle, avail)
            except pywintypes.error:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                out = player.serve_line(line)
                if out:
                    win32file.WriteFile(handle, out)
        win32file.CloseHandle(handle)

    def accept_loop():
        while not player.done.is_set():
            handle = make()
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error:
                win32file.CloseHandle(handle)
                continue
            threading.Thread(target=client, args=(handle,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    while not player.done.is_set():
        player.tick()
        time.sleep(0.05)


def main(argv):
    player = FakePlayer(argv)
    log = os.environ.get("FAKE_MPV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": argv, "pid": os.getpid()}) + "\n")
    if os.environ.get("FAKE_MPV_SLOW_QUIT") and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "fake-fail" in (player.media or ""):
        sys.stderr.write("fake mpv: refusing to open %s\n" % player.media)
        return 2
    if not player.ipc_path:
        player.done.wait(player.duration)
        return 0
    if sys.platform == "win32":
        _serve_pipe(player)
    else:
        _serve_unix(player)
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    # Skip interpreter teardown: client threads may still be blocked in recv.
    os._exit(code)
