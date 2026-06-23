import os

from tunetape.player import _parse_flat_playlist

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def test_parse_flat_playlist():
    tracks = _parse_flat_playlist(_read("yt_flat_playlist.txt"))

    # 4 content lines + an empty-title line; the blank line is skipped.
    assert len(tracks) == 5

    names = [t.name for t in tracks]
    assert "Beautiful Medieval Tavern Music - Medieval Inn Vol. 1" in names
    # A '|' inside a title is preserved (split on the first '|' only).
    assert "Song | with a pipe" in names
    # An empty title falls back to the video id.
    assert "ABC" in names

    # Every id becomes a single-video watch URL.
    assert tracks[0].resolve_hint == "https://www.youtube.com/watch?v=AawLM81gIHo"
    assert all(
        t.resolve_hint.startswith("https://www.youtube.com/watch?v=") for t in tracks
    )
