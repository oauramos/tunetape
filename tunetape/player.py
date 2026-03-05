import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time


def check_dependencies():
    """Verify mpv and yt-dlp exist in PATH."""
    if not shutil.which("mpv"):
        raise RuntimeError("mpv is not installed. Install it with: brew install mpv")
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed. Install it with: brew install yt-dlp")


_YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-]+"
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

    lines = result.stdout.strip().split("\n")
    if len(lines) < 2:
        raise RuntimeError("Could not extract stream information from yt-dlp output.")

    return {"title": lines[0], "stream_url": lines[1]}


class MPVController:
    """Controls an mpv instance via IPC socket."""

    def __init__(self, stream_url: str):
        self._sock_path = f"/tmp/tunetape_{os.getpid()}.sock"
        self._lock = threading.Lock()
        self._sock = None

        # Remove stale socket
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)

        self._proc = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--no-terminal",
                f"--input-ipc-server={self._sock_path}",
                stream_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for socket to appear
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(self._sock_path):
                try:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self._sock.connect(self._sock_path)
                    self._sock.settimeout(2.0)
                    return
                except (ConnectionRefusedError, FileNotFoundError):
                    self._sock = None
            time.sleep(0.1)

        # Cleanup on failure
        self._proc.terminate()
        raise RuntimeError("Could not connect to the player. Try again.")

    def _send(self, command: list) -> dict:
        with self._lock:
            try:
                msg = json.dumps({"command": command}) + "\n"
                self._sock.sendall(msg.encode())
                data = b""
                while b"\n" not in data:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        return {"error": "connection closed"}
                    data += chunk
                return json.loads(data.split(b"\n")[0])
            except Exception:
                return {"error": "send failed"}

    def toggle_pause(self):
        self._send(["cycle", "pause"])

    def seek(self, seconds: float):
        self._send(["seek", str(seconds), "relative"])

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
        return self._proc.poll() is None

    def quit(self):
        try:
            self._send(["quit"])
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        try:
            if os.path.exists(self._sock_path):
                os.unlink(self._sock_path)
        except Exception:
            pass
