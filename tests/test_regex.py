import pytest

from tunetape.player import _YOUTUBE_RE, is_youtube_url, is_youtube_playlist_url
from tunetape.khinsider import _KHINSIDER_RE, _CDN_AUDIO_RE, is_khinsider_url
from tunetape.spotify import is_spotify_url

VALID_YOUTUBE = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ",
    "https://youtu.be/5NV6Rdv1a3I",
    "http://youtu.be/5NV6Rdv1a3I",
    "https://www.youtube.com/shorts/abc123XYZ",
    "https://www.youtube.com/live/abc123XYZ",
    "https://www.youtube.com/embed/abc123XYZ",
    "youtube.com/watch?v=abc123",
]

INVALID_YOUTUBE = [
    "https://example.com/watch?v=abc",
    "https://vimeo.com/12345",
    "https://youtube.com/",
    "not a url",
    "",
]


@pytest.mark.parametrize("url", VALID_YOUTUBE)
def test_youtube_regex_accepts(url):
    assert _YOUTUBE_RE.match(url)


@pytest.mark.parametrize("url", INVALID_YOUTUBE)
def test_youtube_regex_rejects(url):
    assert not _YOUTUBE_RE.match(url)


VALID_KHINSIDER = [
    "https://downloads.khinsider.com/game-soundtracks/album/wii-console-background-music",
    "http://downloads.khinsider.com/game-soundtracks/album/some-album",
    "  https://downloads.khinsider.com/game-soundtracks/album/x  ",  # is_khinsider_url strips
]

INVALID_KHINSIDER = [
    "https://downloads.khinsider.com/game-soundtracks/",
    "https://khinsider.com/game-soundtracks/album/x",
    "https://example.com/album/x",
    "",
]


@pytest.mark.parametrize("url", VALID_KHINSIDER)
def test_is_khinsider_url_accepts(url):
    assert is_khinsider_url(url)


@pytest.mark.parametrize("url", INVALID_KHINSIDER)
def test_is_khinsider_url_rejects(url):
    assert not is_khinsider_url(url)


CDN_MATCH = [
    "https://vgmsite.com/soundtracks/wii/title.mp3",
    "https://eta.vgmtreasurechest.com/x/track.flac",
    "http://example.com/a.ogg",
    "https://cdn.example.com/song.m4a?token=abc123",
    # .wav must match too — the album filter accepts it, so the CDN matcher must
    # as well, or .wav albums would list tracks that never resolve.
    "https://vgmsite.com/soundtracks/x/track.wav",
]

CDN_NO_MATCH = [
    "/relative/path.mp3",
    "https://example.com/file.txt",
    "ftp://example.com/file.mp3",
]


@pytest.mark.parametrize("url", CDN_MATCH)
def test_cdn_audio_regex_matches(url):
    assert _CDN_AUDIO_RE.match(url)


@pytest.mark.parametrize("url", CDN_NO_MATCH)
def test_cdn_audio_regex_rejects(url):
    assert not _CDN_AUDIO_RE.match(url)


VALID_YT_PLAYLIST = [
    "https://www.youtube.com/playlist?list=PLXdS14D-qaXI8c3fY3p4OnvP60pVKKsxj",
    "https://www.youtube.com/watch?v=abc123&list=PL123",
    "https://www.youtube.com/watch?list=PL123&v=abc123",
    "https://music.youtube.com/playlist?list=PL123",
    "https://m.youtube.com/watch?v=abc&list=PL123",
]

INVALID_YT_PLAYLIST = [
    "https://www.youtube.com/watch?v=abc123",  # single video, no list
    "https://youtu.be/abc123",
    "https://open.spotify.com/playlist/abc",
    "",
]


@pytest.mark.parametrize("url", VALID_YT_PLAYLIST)
def test_is_youtube_playlist_accepts(url):
    assert is_youtube_playlist_url(url)


@pytest.mark.parametrize("url", INVALID_YT_PLAYLIST)
def test_is_youtube_playlist_rejects(url):
    assert not is_youtube_playlist_url(url)


def test_watch_with_list_routes_to_playlist():
    # A watch+list URL matches BOTH the single-video and the playlist matcher;
    # _play_url checks playlist first, which is why ordering there matters.
    url = "https://www.youtube.com/watch?v=abc123&list=PL123"
    assert is_youtube_playlist_url(url)
    assert is_youtube_url(url)


VALID_SPOTIFY = [
    "https://open.spotify.com/playlist/6P5N9xiyHaQ7DoSVhQoQco?si=5f9c0147bd9e425e",
    "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
    "https://open.spotify.com/album/1A2GTWGtFfWp7KSQTwWOyo",
    "https://open.spotify.com/intl-pt/track/4cOdK2wGLETKBW3PvgPWqT",
    "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
    "open.spotify.com/playlist/abc123",  # scheme optional
]

INVALID_SPOTIFY = [
    "https://open.spotify.com/",
    "https://spotify.com/track/abc",  # must be the open. host
    "https://youtube.com/watch?v=abc",
    "spotify:foo:bar",
    "",
]


@pytest.mark.parametrize("url", VALID_SPOTIFY)
def test_is_spotify_url_accepts(url):
    assert is_spotify_url(url)


@pytest.mark.parametrize("url", INVALID_SPOTIFY)
def test_is_spotify_url_rejects(url):
    assert not is_spotify_url(url)
