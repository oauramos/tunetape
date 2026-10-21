"""The player on fake data, for trying the interface without mpv or a network.

    python -m tunetape.demo             # a single fake track
    python -m tunetape.demo --album     # a fake 24-track album with its queue

The tests and the README screenshots use the same fakes.
"""

import argparse
import math
import random
import time

from tunetape import ui


class FakeController:
    """Stands in for MPVController: a clock that runs while not paused."""

    def __init__(self, duration: float, position: float = 0.0, volume: float = 80.0,
                 paused: bool = False, muted: bool = False, frozen: bool = False):
        self._duration = duration
        self._position = position
        self._volume = volume
        self._paused = paused
        self._muted = muted
        self._frozen = frozen  # screenshots and tests: hold the clock still
        self._last = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        if not (self._paused or self._frozen):
            self._position = min(self._position + now - self._last, self._duration)
        self._last = now

    def toggle_pause(self):
        self._tick()
        self._paused = not self._paused

    def seek(self, seconds: float):
        self._tick()
        self._position = min(max(self._position + seconds, 0.0), self._duration)

    def set_volume_relative(self, delta: float):
        self._volume = min(max(self._volume + delta, 0.0), 130.0)

    def toggle_mute(self):
        self._muted = not self._muted

    def get_volume(self) -> float:
        return self._volume

    def is_muted(self) -> bool:
        return self._muted

    def get_position(self) -> float:
        self._tick()
        return self._position

    def get_duration(self) -> float:
        return self._duration

    def is_paused(self) -> bool:
        return self._paused

    def is_alive(self) -> bool:
        self._tick()
        return self._position < self._duration

    def get_levels(self):
        """Something music-shaped for the level meter: a swell plus jitter."""
        if self._paused:
            return None
        t = time.monotonic()
        levels = []
        for channel in range(2):
            rms = -16 + 5 * math.sin(t * 1.7 + channel) + random.uniform(-3, 3)
            levels.append((rms, rms + random.uniform(3, 8)))
        return levels

    def get_stats(self) -> dict:
        t = time.monotonic()
        return {
            "codec": "opus", "bitrate": 131_000, "samplerate": 48000, "channels": "stereo",
            "container": "webm", "ahead": 96.4, "cached": 3_480_000,
            # A download that comes in bursts, like a stream topping up its cache.
            "speed": max(0.0, 900_000 * math.sin(t / 2.0)) + random.uniform(0, 60_000),
            "normalized": True,
        }

    def quit(self):
        pass


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


def playlist_info_for(index: int, total: int) -> dict:
    return {
        "index": index,
        "track_label": f"{index + 1}/{total}",
        "has_next": index + 1 < total,
        "has_prev": index > 0,
    }


def play_single() -> str:
    controller = FakeController(duration=245, position=83)
    return ui.play("Daft Punk - Around the World (Official Video)", controller,
                   source="YouTube")


def play_album(index: int = 11) -> str:
    """Mirrors __main__._play_album: the screen answers, the flow moves the cursor."""
    position = 41.0
    while True:
        ui.with_spinner(f"Loading track {index + 1}/{len(DEMO_TRACKS)}…", time.sleep, 0.4)
        controller = FakeController(duration=150 + (index * 37) % 120, position=position)
        result = ui.play(
            DEMO_TRACKS[index], controller, playlist_info_for(index, len(DEMO_TRACKS)),
            source="KHInsider", subtitle=DEMO_ALBUM, tracks=DEMO_TRACKS,
        )
        position = 0.0
        if result == "next_track":
            index += 1
        elif result == "prev_track":
            index -= 1
        elif result.startswith("jump_track:"):
            index = int(result.split(":", 1)[1])
        else:
            return result


def main() -> None:
    parser = argparse.ArgumentParser(description="tunetape's player on fake data")
    parser.add_argument("--album", action="store_true", help="a fake album with a queue")
    args = parser.parse_args()
    ui.run_session(play_album if args.album else play_single)


if __name__ == "__main__":
    main()
