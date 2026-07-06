import atexit
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from tunetape.ipc import create_ipc
from tunetape.models import Album, Track


# Cache resolved executable paths so we probe the filesystem at most once each.
_resolved_exe = {}


def _windows_exe_candidates(name):
    """Well-known install dirs winget/scoop use but may not add to PATH.

    winget's ``shinchiro.mpv`` extracts to ``C:\\Program Files\\MPV Player``
    without touching PATH, so ``shutil.which`` reports mpv missing even when it
    is installed. Return absolute paths to probe, most-specific first.
    """
    exe = f"{name}.exe"
    home = os.path.expanduser("~")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        # scoop normally keeps its shim dir on PATH; probe it anyway as a fallback.
        os.path.join(home, "scoop", "shims", exe),
        os.path.join(home, "scoop", "apps", name, "current", exe),
    ]
    if name == "mpv":
        candidates += [
            r"C:\Program Files\MPV Player\mpv.exe",  # winget: shinchiro.mpv
            r"C:\Program Files\mpv\mpv.exe",
        ]
    if localappdata:
        if name == "mpv":
            candidates.append(os.path.join(localappdata, "Programs", "mpv", "mpv.exe"))
        # winget extracts each package under a versioned subfolder — glob across
        # them and probe the most recently modified first, so a fresh upgrade
        # wins over a leftover older version left behind in a sibling folder.
        matches = glob.glob(
            os.path.join(localappdata, "Microsoft", "WinGet", "Packages",
                         f"*{name}*", "**", exe),
            recursive=True,
        )

        def _mtime(path):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        matches.sort(key=_mtime, reverse=True)
        candidates += matches
    return candidates


def find_executable(name):
    """Locate a CLI tool, honoring PATH first, then known Windows install dirs.

    Returns an absolute path suitable as ``argv[0]``, or None if the tool can't
    be found anywhere.
    """
    if name in _resolved_exe:
        return _resolved_exe[name]

    found = shutil.which(name)
    if found is None and sys.platform == "win32":
        for path in _windows_exe_candidates(name):
            if os.path.isfile(path):
                found = path
                break
    # Cache only successful resolutions: a tool the user installs mid-session
    # should be re-probed and picked up rather than staying "missing" for the
    # life of the process.
    if found is not None:
        _resolved_exe[name] = found
    return found


def _install_hint(pkg: str) -> str:
    """Platform-appropriate one-liner for installing a missing dependency."""
    if sys.platform == "win32":
        return f"Install it with: winget install {pkg}  (or: scoop install {pkg})"
    if sys.platform == "darwin":
        return f"Install it with: brew install {pkg}"
    return f"Install it with your package manager, e.g. sudo apt install {pkg}"


def check_dependencies(require_ytdlp: bool = True):
    """Verify mpv (always) and yt-dlp (when required) exist in PATH."""
    if not find_executable("mpv"):
        raise RuntimeError(f"mpv is not installed. {_install_hint('mpv')}")
    if require_ytdlp and not find_executable("yt-dlp"):
        raise RuntimeError(f"yt-dlp is not installed. {_install_hint('yt-dlp')}")


# [#9] Broadened regex: supports youtube.com, youtu.be, music/m subdomains, /live/, /embed/, /shorts/
_YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|music\.|m\.)?"
    r"(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w\-]+"
)


def is_youtube_url(url: str) -> bool:
    """True for a single YouTube video URL (not a playlist — see is_youtube_playlist_url)."""
    return bool(_YOUTUBE_RE.match(url.strip()))


def get_stream_info(url: str) -> dict:
    """Extract audio stream URL and title from a YouTube URL."""
    url = url.strip()
    if not _YOUTUBE_RE.match(url):
        raise ValueError("Invalid URL. Please paste a valid youtube.com or youtu.be link.")
    return resolve_with_ytdlp(url)


def resolve_with_ytdlp(arg: str) -> dict:
    """Resolve a yt-dlp input to a playable audio stream URL + title.

    ``arg`` is anything yt-dlp accepts as input: a YouTube watch URL or a
    ``ytsearch1:<query>`` search (used to find the YouTube audio for a Spotify
    track). Shared by the YouTube, YouTube-playlist, and Spotify resolvers so
    the network/error handling lives in one place.
    """
    try:
        result = subprocess.run(
            [find_executable("yt-dlp") or "yt-dlp",
             "-f", "bestaudio", "--get-url", "--get-title", arg],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ConnectionError("Network error. Check your internet connection and try again.")
    except OSError:
        raise RuntimeError(f"yt-dlp is not installed. {_install_hint('yt-dlp')}")

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


# Matches a YouTube playlist URL — either the dedicated playlist page or a
# watch URL carrying a `list=` param. Kept separate from _YOUTUBE_RE because the
# bare `playlist?list=…` form (no v=) is not a single-video URL.
_YT_PLAYLIST_RE = re.compile(
    r"^(https?://)?(www\.|music\.|m\.)?youtube\.com/"
    r"(playlist|watch)\?(\S*&)?list=[\w\-]+"
)


def is_youtube_playlist_url(url: str) -> bool:
    return bool(_YT_PLAYLIST_RE.match(url.strip()))


def _parse_flat_playlist(stdout: str) -> list:
    """Parse `yt-dlp --flat-playlist --print "%(id)s|%(title)s"` output to Tracks.

    Split on the FIRST `|` only, so a `|` inside a video title is preserved.
    """
    tracks = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        vid, title = line.split("|", 1)
        vid = vid.strip()
        if not vid:
            continue
        tracks.append(Track(
            name=title.strip() or vid,
            resolve_hint=f"https://www.youtube.com/watch?v={vid}",
        ))
    return tracks


def fetch_youtube_playlist(url: str) -> Album:
    """Enumerate a YouTube playlist (no download) and return an Album of Tracks."""
    url = url.strip()
    if not is_youtube_playlist_url(url):
        raise ValueError("Invalid YouTube playlist URL.")

    try:
        result = subprocess.run(
            [find_executable("yt-dlp") or "yt-dlp", "--flat-playlist", "--no-warnings",
             "--print", "%(id)s|%(title)s", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ConnectionError("Network error. Check your internet connection and try again.")
    except OSError:
        raise RuntimeError(f"yt-dlp is not installed. {_install_hint('yt-dlp')}")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "urlopen error" in stderr or "getaddrinfo" in stderr or "timed out" in stderr:
            raise ConnectionError("Network error. Check your internet connection and try again.")
        raise RuntimeError(
            f"Could not load playlist. It may be private or unavailable.\n\nDetails: {stderr}"
        )

    tracks = _parse_flat_playlist(result.stdout)
    if not tracks:
        raise ValueError(
            "No videos found in the playlist. Double-check the URL.\n\n"
            f"URL: {url}"
        )
    return Album(title="YouTube Playlist", tracks=tracks)


class MPVController:
    """Controls an mpv instance via IPC socket."""

    def __init__(self, stream_url: str, normalize: bool = False, volume: float = None):
        # [#5] IPC endpoint (Unix socket in a private temp dir, or a named pipe on
        # Windows) — platform detail lives in tunetape.ipc.
        self._ipc = create_ipc()
        # [#2] Use RLock to prevent deadlock when signal handler calls quit() while lock is held
        self._lock = threading.RLock()
        self._proc = None
        self._closed = False  # [#16] Idempotent quit guard
        self._buf = b""  # [#3] Persistent read buffer for IPC
        self._req_id = 0  # Monotonic IPC request id for response matching
        # Seed the cached reading with the launch volume so the meter shows the
        # restored level immediately, before the first IPC poll lands.
        self._last_volume = 100.0 if volume is None else float(volume)
        self._last_muted = False

        if not self._spawn_and_connect(stream_url, normalize, volume):
            # The audio filter may be unavailable on a minimal mpv build; retry
            # once without it before giving up so playback isn't blocked.
            if not (normalize and self._spawn_and_connect(stream_url, False, volume)):
                self._ipc.cleanup()
                raise RuntimeError("Could not connect to the player. Try again.")

    def _spawn_and_connect(self, stream_url: str, normalize: bool, volume: float = None) -> bool:
        """Launch mpv and connect to its IPC socket. Returns True on success.

        On failure, reaps the spawned process so a retry can start cleanly.
        """
        # Start from a clean endpoint (a prior failed attempt may have left one).
        self._ipc.reset()

        args = [
            find_executable("mpv") or "mpv",
            "--no-video",
            "--no-terminal",
            f"--input-ipc-server={self._ipc.server_address}",
        ]
        if normalize:
            # Even out loudness across sources. Uses mpv's built-in libavfilter
            # (no separate ffmpeg binary needed); dynaudnorm is single-pass and
            # real-time friendly, which suits streaming.
            args.append("--af=dynaudnorm")
        if volume is not None:
            # Start at the user's last-set level instead of mpv's default 100%.
            args.append(f"--volume={int(round(float(volume)))}")
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

        # Wait (up to 5s) for mpv to create its IPC endpoint, then connect.
        if self._ipc.connect(5.0):
            return True

        # [#10] Reap zombie on this attempt's failure
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        atexit.unregister(self.quit)
        return False

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
                self._ipc.send(msg.encode())

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

                    chunk = self._ipc.recv(4096)
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
            if self._ipc.connected:
                msg = json.dumps({"command": ["quit"]}) + "\n"
                self._ipc.send(msg.encode())
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
            self._ipc.close()
        except Exception:
            pass
        self._ipc.cleanup()
        try:
            atexit.unregister(self.quit)
        except Exception:
            pass
