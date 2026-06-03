import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time


def check_dependencies(require_ytdlp: bool = True):
    """Verify mpv (always) and yt-dlp (when required) exist in PATH."""
    if not shutil.which("mpv"):
        raise RuntimeError("mpv is not installed. Install it with: brew install mpv")
    if require_ytdlp and not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed. Install it with: brew install yt-dlp")


# [#9] Broadened regex: supports youtube.com, youtu.be, music/m subdomains, /live/, /embed/, /shorts/
_YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|music\.|m\.)?"
    r"(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w\-]+"
)


def get_stream_info(url: str) -> dict:
    """Extract audio stream URL and title from a YouTube URL."""
    url = url.strip()
    if not _YOUTUBE_RE.match(url):
        raise ValueError("Invalid URL. Please paste a valid youtube.com or youtu.be link.")

    try:
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "--get-url", "--get-title", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ConnectionError("Network error. Check your internet connection and try again.")
    except OSError:
        raise RuntimeError("yt-dlp is not installed. Install it with: brew install yt-dlp")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "urlopen error" in stderr or "getaddrinfo" in stderr or "timed out" in stderr:
            raise ConnectionError("Network error. Check your internet connection and try again.")
        raise RuntimeError(
            f"Could not load video. It may be private, unavailable, or region-blocked.\n\nDetails: {stderr}"
        )

    # `--get-title --get-url` prints the title first, then the URL. Take the
    # title as the first line and the stream URL as the LAST http line — robust
    # to a title that itself starts with "http" and to blank lines.
    lines = [ln.strip() for ln in result.stdout.strip().split("\n") if ln.strip()]
    if not lines:
        raise RuntimeError("Could not extract stream information from yt-dlp output.")
    stream_url = next((ln for ln in reversed(lines) if ln.startswith("http")), None)
    if stream_url is None:
        raise RuntimeError("Could not extract stream information from yt-dlp output.")
    title = lines[0] if lines[0] != stream_url else "Unknown title"

    return {"title": title, "stream_url": stream_url}


class MPVController:
    """Controls an mpv instance via IPC socket."""

    def __init__(self, stream_url: str, normalize: bool = False):
        # [#5] Secure socket path: private temp directory instead of predictable /tmp path
        self._sock_dir = tempfile.mkdtemp(prefix="tunetape_")
        self._sock_path = os.path.join(self._sock_dir, "ipc.sock")
        # [#2] Use RLock to prevent deadlock when signal handler calls quit() while lock is held
        self._lock = threading.RLock()
        self._sock = None
        self._proc = None
        self._closed = False  # [#16] Idempotent quit guard
        self._buf = b""  # [#3] Persistent read buffer for IPC
        self._req_id = 0  # Monotonic IPC request id for response matching
        self._last_volume = 100.0  # last good readings, returned on IPC error
        self._last_muted = False

        if not self._spawn_and_connect(stream_url, normalize):
            # The audio filter may be unavailable on a minimal mpv build; retry
            # once without it before giving up so playback isn't blocked.
            if not (normalize and self._spawn_and_connect(stream_url, False)):
                self._cleanup_socket()
                raise RuntimeError("Could not connect to the player. Try again.")

    def _spawn_and_connect(self, stream_url: str, normalize: bool) -> bool:
        """Launch mpv and connect to its IPC socket. Returns True on success.

        On failure, reaps the spawned process so a retry can start cleanly.
        """
        # Start from a clean socket path (a prior failed attempt may have left one).
        try:
            if os.path.exists(self._sock_path):
                os.unlink(self._sock_path)
        except OSError:
            pass

        args = [
            "mpv",
            "--no-video",
            "--no-terminal",
            f"--input-ipc-server={self._sock_path}",
        ]
        if normalize:
            # Even out loudness across sources. Uses mpv's built-in libavfilter
            # (no separate ffmpeg binary needed); dynaudnorm is single-pass and
            # real-time friendly, which suits streaming.
            args.append("--af=dynaudnorm")
        # [#11] Use -- separator so stream_url can't be interpreted as mpv flag
        args += ["--", stream_url]

        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Reap this child even if SIGINT/exit fires before the caller stores us
        # (the socket-wait below can block for up to 5s). quit() is idempotent
        # and unregisters itself, so this never accumulates or double-reaps.
        atexit.register(self.quit)

        # Wait for socket to appear
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(self._sock_path):
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self._sock_path)
                    sock.settimeout(2.0)
                    self._sock = sock
                    return True
                except (ConnectionRefusedError, FileNotFoundError):
                    # [#1] Close socket on failed connect to prevent FD leak
                    if sock is not None:
                        sock.close()
            time.sleep(0.1)

        # [#10] Reap zombie on this attempt's failure
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        atexit.unregister(self.quit)
        return False

    def _cleanup_socket(self):
        try:
            if os.path.exists(self._sock_path):
                os.unlink(self._sock_path)
        except OSError:
            pass
        try:
            if os.path.exists(self._sock_dir):
                os.rmdir(self._sock_dir)
        except OSError:
            pass

    def _send(self, command: list) -> dict:
        with self._lock:
            if self._closed:
                return {"error": "closed"}
            try:
                # [#3] Monotonic request_id so we return the reply to THIS command,
                # never an async event or a late reply to an earlier command.
                self._req_id += 1
                request_id = self._req_id
                msg = json.dumps({"command": command, "request_id": request_id}) + "\n"
                self._sock.sendall(msg.encode())

                # [#4] Overall deadline for response (5s)
                send_deadline = time.monotonic() + 5.0
                while time.monotonic() < send_deadline:
                    # Process lines from buffer
                    while b"\n" in self._buf:
                        line, self._buf = self._buf.split(b"\n", 1)
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Only the reply echoing our request_id matches; skip
                        # async events and stale replies to earlier commands.
                        if isinstance(parsed, dict) and parsed.get("request_id") == request_id:
                            return parsed

                    # [#17] Cap buffer size
                    if len(self._buf) > 65536:
                        self._buf = b""
                        return {"error": "response too large"}

                    chunk = self._sock.recv(4096)
                    if not chunk:
                        return {"error": "connection closed"}
                    self._buf += chunk

                return {"error": "timeout"}
            except Exception:
                return {"error": "send failed"}

    def toggle_pause(self):
        self._send(["cycle", "pause"])

    def seek(self, seconds: float):
        self._send(["seek", str(seconds), "relative"])

    def set_volume_relative(self, delta: float):
        self._send(["add", "volume", str(delta)])

    def toggle_mute(self):
        self._send(["cycle", "mute"])

    def get_volume(self) -> float:
        # mpv replies carry error="success" on success; our own failure dicts
        # use other error values. Update the cache only on a real mpv success,
        # otherwise hold the last good reading (avoids a 100%/unmuted flicker).
        resp = self._send(["get_property", "volume"])
        if resp.get("error") == "success":
            try:
                self._last_volume = float(resp.get("data"))
            except (TypeError, ValueError):
                pass
        return self._last_volume

    def is_muted(self) -> bool:
        resp = self._send(["get_property", "mute"])
        if resp.get("error") == "success":
            self._last_muted = bool(resp.get("data", False))
        return self._last_muted

    def get_position(self) -> float:
        resp = self._send(["get_property", "playback-time"])
        try:
            return float(resp.get("data", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def get_duration(self) -> float:
        resp = self._send(["get_property", "duration"])
        try:
            return float(resp.get("data", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def is_paused(self) -> bool:
        resp = self._send(["get_property", "pause"])
        return bool(resp.get("data", False))

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def quit(self):
        # [#16] Idempotent quit — safe to call multiple times / from signal handler
        with self._lock:
            if self._closed:
                return
            self._closed = True

        try:
            if self._sock:
                msg = json.dumps({"command": ["quit"]}) + "\n"
                self._sock.sendall(msg.encode())
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.terminate()
                self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
                self._proc.wait()
            except Exception:
                pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._cleanup_socket()
        try:
            atexit.unregister(self.quit)
        except Exception:
            pass
