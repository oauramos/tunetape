import json
import os

import pytest

from tunetape import spotify
from tunetape.spotify import _NextDataParser, _clean, fetch_spotify

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def test_next_data_parser_extracts_json():
    p = _NextDataParser()
    p.feed(_read("spotify_playlist.html"))
    # The <script> CDATA body is captured intact and is valid JSON; the
    # unrelated trailing <script> must not clobber it.
    data = json.loads(p.json_text)
    assert data["props"]["pageProps"]["state"]["data"]["entity"]["type"] == "playlist"


def test_clean_normalizes_nbsp():
    # Spotify joins multiple artists with a non-breaking space.
    assert _clean("Artist Two,\xa0Artist Three") == "Artist Two, Artist Three"
    assert _clean(None) == ""
    assert _clean("  hi  ") == "hi"


def test_fetch_spotify_playlist(monkeypatch):
    monkeypatch.setattr(spotify, "_fetch_page", lambda url: _read("spotify_playlist.html"))
    album = fetch_spotify("https://open.spotify.com/playlist/ABC123?si=x")

    assert album.title == "Test Mix"
    assert [t.name for t in album.tracks] == ["First Song", "Second Song", "Blocked Song"]
    # The resolve hint is the YouTube search: "<artist> <title>".
    assert album.tracks[0].resolve_hint == "ytsearch1:Artist One First Song"
    # Multiple artists are kept; an unplayable-on-Spotify track is still listed
    # (it may exist on YouTube — fail-forward handles genuine misses).
    assert album.tracks[1].resolve_hint == "ytsearch1:Artist Two, Artist Three Second Song"
    assert album.tracks[2].name == "Blocked Song"


def test_fetch_spotify_single_track(monkeypatch):
    monkeypatch.setattr(spotify, "_fetch_page", lambda url: _read("spotify_track.html"))
    album = fetch_spotify("https://open.spotify.com/track/ABC123")

    assert len(album.tracks) == 1
    t = album.tracks[0]
    assert t.name == "Never Gonna Give You Up"
    # On a single-track embed the artist is in artists[], not subtitle (null).
    assert t.resolve_hint == "ytsearch1:Rick Astley Never Gonna Give You Up"


def test_fetch_spotify_bad_format_raises_valueerror(monkeypatch):
    monkeypatch.setattr(spotify, "_fetch_page", lambda url: "<html>no next data here</html>")
    with pytest.raises(ValueError):
        fetch_spotify("https://open.spotify.com/playlist/ABC123")


def test_parse_ref_rejects_bad_url():
    with pytest.raises(ValueError):
        spotify._parse_ref("https://example.com/foo")
