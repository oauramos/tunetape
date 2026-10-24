"""The player on fake data, for trying the interface without a daemon, mpv or a network.

    python -m tunetape.demo             # a single fake track
    python -m tunetape.demo --album     # a fake 24-track album with its queue

``FakeClient`` stands in for :class:`tunetape.client.Client`: a little daemon
of its own, with a queue, a clock that runs while not paused and advances to
the next track at the end of one, and answers shaped like the protocol's
(docs/protocol.md §3.4). The tests and the README screenshots use the same fakes.
"""

import argparse
import math
import random
import threading
import time

from tunetape import ui
from tunetape.client import DaemonError, DaemonUnavailable

ERR_NOTHING_PLAYING = 1012
ERR_NOT_FOUND = 1013

COLLECTION_KINDS = ("khinsider", "spotify", "youtube_playlist")


class FakeClient:
    """A Client whose daemon is a clock: what the TUI sees of a real one, minus the audio.

    ``tracks`` are titles; with ``album`` set they are one collection of that
    name, else each is a single track. ``frozen`` holds the clock still for
    screenshots and tests; ``load_seconds`` shows the loading state that long
    whenever a track starts, like the daemon resolving it. ``catalog`` maps an
    input to what posting it gives: a dict of ``FakeClient`` arguments, or a
    DaemonError to raise. Every call is recorded in ``calls``.
    """

    def __init__(self, tracks=(), *, album: str = None, kind: str = None, index: int = 0,
                 position: float = 0.0, durations=None, volume: float = 80.0,
                 paused: bool = False, muted: bool = False, frozen: bool = False,
                 load_seconds: float = 0.0, state: str = None, catalog: dict = None):
        self._lock = threading.RLock()
        self.calls = []
        self.catalog = dict(catalog or {})
        self.suggestions = []        # discover(): [{"title", "input"}], or a DaemonError
        self.history_entries = []
        self.log_entries = []
        self.down = False            # True: the daemon is gone; every call fails
        self.closed = False
        self.hello = {"protocol": 1, "server": "tunetape/demo", "pid": 0}
        self.volume_level = float(volume)
        self.muted = muted
        self.frozen = frozen
        self.load_seconds = load_seconds
        self._ids = 0
        self._load(tracks, album=album, kind=kind, index=index, position=position,
                   durations=durations, paused=paused, state=state)

    # --- the fake daemon --------------------------------------------------

    def _load(self, tracks, *, album=None, kind=None, index=0, position=0.0, durations=None,
              paused=False, state=None):
        tracks = list(tracks)
        kind = kind or ("khinsider" if album else "youtube")
        if durations is None:
            durations = [245.0] if len(tracks) == 1 else [
                150.0 + (i * 37) % 120 for i in range(len(tracks))]
        elif isinstance(durations, (int, float)):
            durations = [float(durations)] * len(tracks)
        self.items = []
        for i, title in enumerate(tracks):
            self._ids += 1
            self.items.append({
                "id": f"i-{self._ids:04x}", "request_id": "r-demo", "title": title,
                "kind": kind, "input": f"demo:{self._ids}",
                "collection": {"title": album, "index": i, "count": len(tracks)} if album else None,
                "status": "queued", "error": None, "duration": float(durations[i]),
            })
        self.index = -1
        self.state = "idle"
        self.message = None
        self.paused = False
        self.position = 0.0
        if self.items and state != "idle":
            self._begin(min(max(index, 0), len(self.items) - 1), position)
            self.paused = paused
        self._last = time.monotonic()

    def _begin(self, index, position=0.0):
        if 0 <= self.index < len(self.items) and self.items[self.index]["status"] == "playing":
            self.items[self.index]["status"] = "played"
        self.index = index
        self.position = position
        self.paused = False
        self.items[index]["status"] = "playing"
        self.state = "loading" if self.load_seconds > 0 else "playing"
        if self.state == "loading":
            item = self.items[index]
            coll = item["collection"]
            self.message = (f"Loading track {coll['index'] + 1}/{coll['count']}…" if coll
                            else f"Loading {item['title']}…")
        self._started = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        elapsed, self._last = now - self._last, now
        if self.frozen:
            return
        if self.state == "loading" and now - self._started >= self.load_seconds:
            self.state, self.message = "playing", None
        if self.state != "playing" or self.paused:
            return
        self.position += elapsed
        if self.position >= self.items[self.index]["duration"]:
            if self.index + 1 < len(self.items):
                self._begin(self.index + 1)  # auto-advance, as the daemon does
            else:
                self.items[self.index]["status"] = "played"
                self.state, self.position = "idle", 0.0

    def _call(self, name, *args):
        self.calls.append((name,) + args)
        if self.down:
            raise DaemonUnavailable("The connection to the tunetape daemon was lost.")
        self._tick()

    def _need_playing(self):
        if self.state not in ("playing", "loading"):
            raise DaemonError(ERR_NOTHING_PLAYING, "Nothing is playing.")

    def _status(self, levels=False, stats=False) -> dict:
        item = self.items[self.index] if 0 <= self.index < len(self.items) else None
        state = "paused" if self.state == "playing" and self.paused else self.state
        result = {
            "state": state, "message": self.message,
            "position": self.position if item else 0.0,
            "duration": item["duration"] if item else 0.0,
            "volume": self.volume_level, "muted": self.muted,
            "current": dict(item) if item else None,
            "index": self.index if item else -1, "length": len(self.items),
            "has_next": item is not None and self.index < len(self.items) - 1,
            "has_prev": item is not None and self.index > 0,
        }
        live = self.state == "playing" and not self.paused
        if levels:
            result["levels"] = self._levels() if live else None
        if stats:
            result["stats"] = self._stats() if self.state != "idle" else None
        return result

    @staticmethod
    def _levels():
        """Something music-shaped for the level meter: a swell plus jitter."""
        t = time.monotonic()
        levels = []
        for channel in range(2):
            rms = -16 + 5 * math.sin(t * 1.7 + channel) + random.uniform(-3, 3)
            levels.append([rms, rms + random.uniform(3, 8)])
        return levels

    @staticmethod
    def _stats() -> dict:
        t = time.monotonic()
        return {
            "codec": "opus", "bitrate": 131_000, "samplerate": 48000, "channels": "stereo",
            "container": "webm", "ahead": 96.4, "cached": 3_480_000,
            # A download that comes in bursts, like a stream topping up its cache.
            "speed": max(0.0, 900_000 * math.sin(t / 2.0)) + random.uniform(0, 60_000),
            "normalized": True,
        }

    # --- the Client API the TUI uses ----------------------------------------

    def play(self, input, *, title=None, start_index=0, wait=False, timeout=None) -> dict:
        with self._lock:
            self._call("play", input, title, start_index)
            spec = self.catalog.get(input, {"tracks": [title or input]})
            if isinstance(spec, DaemonError):
                raise spec
            spec = dict(spec)
            spec.setdefault("index", start_index)
            self._load(**spec)
            album = spec.get("album")
            return {"request": {"id": "r-demo", "input": input, "kind": self.items[0]["kind"],
                                "title": album or title or self.items[0]["title"],
                                "status": "done", "error": None,
                                "item_ids": [it["id"] for it in self.items]}}

    def status(self, *, levels=False, stats=False, timeout=None) -> dict:
        with self._lock:
            self._call("status")
            return self._status(levels, stats)

    def queue(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("queue")
            return {"items": [dict(it) for it in self.items], "index": self.index}

    def requests(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("requests")
            return {"requests": []}

    def toggle(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("toggle")
            self._need_playing()
            self.paused = not self.paused
            return self._status()

    def pause(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("pause")
            self._need_playing()
            self.paused = True
            return self._status()

    def resume(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("resume")
            self._need_playing()
            self.paused = False
            return self._status()

    def next(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("next")
            if not 0 <= self.index < len(self.items) - 1:
                raise DaemonError(ERR_NOT_FOUND, "There is no next track.")
            self._begin(self.index + 1)
            return self._status()

    def prev(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("prev")
            if not 0 < self.index < len(self.items):
                raise DaemonError(ERR_NOT_FOUND, "There is no previous track.")
            self._begin(self.index - 1)
            return self._status()

    def jump(self, index, *, timeout=None) -> dict:
        with self._lock:
            self._call("jump", index)
            if not 0 <= index < len(self.items):
                raise DaemonError(ERR_NOT_FOUND, f"There is no track {index + 1} in the queue.")
            self._begin(index)
            return self._status()

    def stop(self, *, timeout=None) -> dict:
        with self._lock:
            self._call("stop")
            if 0 <= self.index < len(self.items) and self.items[self.index]["status"] == "playing":
                self.items[self.index]["status"] = "played"
            self.state, self.message, self.paused = "idle", None, False
            return self._status()

    def seek(self, seconds, *, relative=True, timeout=None) -> dict:
        with self._lock:
            self._call("seek", seconds, relative)
            self._need_playing()
            target = self.position + seconds if relative else seconds
            self.position = min(max(target, 0.0), self.items[self.index]["duration"])
            return self._status()

    def volume(self, *, value=None, delta=None, timeout=None) -> dict:
        with self._lock:
            self._call("volume", value, delta)
            target = value if value is not None else self.volume_level + delta
            self.volume_level = min(max(float(target), 0.0), 130.0)
            return self._status()

    def mute(self, muted=None, *, timeout=None) -> dict:
        with self._lock:
            self._call("mute", muted)
            self._need_playing()
            self.muted = (not self.muted) if muted is None else muted
            return self._status()

    def discover(self, request, *, timeout=None) -> dict:
        with self._lock:
            self._call("discover", request)
            if isinstance(self.suggestions, DaemonError):
                raise self.suggestions
            return {"tracks": list(self.suggestions)}

    def history(self, limit=50, *, timeout=None) -> dict:
        with self._lock:
            self._call("history", limit)
            return {"entries": list(self.history_entries)[:limit]}

    def log(self, limit=200, *, timeout=None) -> dict:
        with self._lock:
            self._call("log", limit)
            return {"entries": list(self.log_entries)[-limit:]}

    def close(self) -> None:
        self.closed = True

    def called(self, name) -> list:
        """The recorded calls to ``name``, each as its tuple of arguments."""
        return [c[1:] for c in self.calls if c[0] == name]


DEMO_ALBUM = "Chrono Trigger Original Soundtrack"
DEMO_TRACKS = [
    "A Premonition", "Chrono Trigger", "Morning Sunlight", "Peaceful Days",
    "Secret of the Forest", "Guardia Millennial Fair", "Yearnings of the Wind",
    "Wind Scene", "Frog's Theme", "Robo's Theme", "Ruined World",
    "Corridors of Time", "Zeal Palace", "Schala's Theme", "Battle with Magus",
    "Silent Light", "Johnny of the Robo Gang", "Lavos's Theme",
    "Manoria Cathedral", "Courage and Pride", "Gato's Song", "Battle",
    "Good Night", "To Far Away Times",
]
DEMO_SINGLE = "Daft Punk - Around the World (Official Video)"


def single_client(**kwargs) -> FakeClient:
    """One YouTube video, part-way through."""
    kwargs.setdefault("position", 83)
    return FakeClient([DEMO_SINGLE], kind="youtube", **kwargs)


def album_client(index: int = 11, **kwargs) -> FakeClient:
    """The 24-track album, on track 12, with the queue alongside on a wide terminal."""
    kwargs.setdefault("position", 41)
    return FakeClient(DEMO_TRACKS, album=DEMO_ALBUM, kind="khinsider", index=index, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="tunetape's player on fake data")
    parser.add_argument("--album", action="store_true", help="a fake album with a queue")
    args = parser.parse_args()
    client = album_client(load_seconds=0.4) if args.album else single_client()
    ui.run_session(lambda: ui.play(client))


if __name__ == "__main__":
    main()
