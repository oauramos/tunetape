import pytest

from tunetape.player import _YOUTUBE_RE
from tunetape.khinsider import _KHINSIDER_RE, _CDN_AUDIO_RE, is_khinsider_url

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
