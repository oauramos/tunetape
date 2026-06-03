import atexit
import json
import os
import tempfile

from tunetape.khinsider import Album, Track


class Playlist:
    def __init__(self, album: Album, start_index: int = 0):
        self._album = album
        n = len(album.tracks)
        # Clamp the resume index so a shrunk album can't IndexError.
        self._index = max(0, min(start_index, n - 1)) if n else 0
        # Create temp cache file
        fd, self._cache_path = tempfile.mkstemp(
            prefix="tunetape_playlist_", suffix=".json"
        )
        os.close(fd)
        # Safety net if the process is killed; close() handles the normal path
        # and unregisters this so handlers don't accumulate across albums.
        atexit.register(self._cleanup)
        self._save_cache()

    @property
    def current_track(self) -> Track:
        return self._album.tracks[self._index]

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def track_label(self) -> str:
        return f"{self._index + 1}/{self.total_tracks}"

    @property
    def total_tracks(self) -> int:
        return len(self._album.tracks)

    @property
    def album_title(self) -> str:
        return self._album.title

    def has_next(self) -> bool:
        return self._index < len(self._album.tracks) - 1

    def has_prev(self) -> bool:
        return self._index > 0

    def next(self) -> Track:
        if not self.has_next():
            raise IndexError("No next track")
        self._index += 1
        self._save_cache()
        return self.current_track

    def prev(self) -> Track:
        if not self.has_prev():
            raise IndexError("No previous track")
        self._index -= 1
        self._save_cache()
        return self.current_track

    def _save_cache(self):
        data = {
            "album_title": self._album.title,
            "current_index": self._index,
            "tracks": [
                {
                    "name": t.name,
                    "track_page_url": t.track_page_url,
                    "direct_url": t.direct_url,
                }
                for t in self._album.tracks
            ],
        }
        try:
            with open(self._cache_path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def close(self):
        """Remove the temp cache and drop the atexit handler. Idempotent."""
        self._cleanup()
        try:
            atexit.unregister(self._cleanup)
        except Exception:
            pass

    def _cleanup(self):
        try:
            if os.path.exists(self._cache_path):
                os.unlink(self._cache_path)
        except OSError:
            pass
