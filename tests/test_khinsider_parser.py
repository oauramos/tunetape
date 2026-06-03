import os

from tunetape.khinsider import _AlbumPageParser, _TrackPageParser

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def test_album_parser_title_and_tracks():
    base = "https://downloads.khinsider.com/game-soundtracks/album/wii-console-background-music"
    parser = _AlbumPageParser(base)
    parser.feed(_read("album.html"))

    # First <h2> inside #pageContent wins; the earlier one is ignored.
    assert parser.title.strip() == "Wii Console Background Music"

    names = [name for _, name in parser.tracks]
    urls = [url for url, _ in parser.tracks]

    # Only same-album links are captured (the other-album decoy is excluded).
    assert len(parser.tracks) == 4
    assert "Title Theme" in names
    assert "Mii Channel" in names
    # Extension broadening: a .flac track must be picked up too.
    assert "Flac Track" in names
    assert any(u.endswith(".flac") for u in urls)
    # Empty anchor text -> name derived from URL filename (numbers/ext stripped).
    assert "instrumental" in names
    # All resolved to absolute khinsider URLs, deduped.
    assert all(u.startswith("https://downloads.khinsider.com") for u in urls)
    assert len(set(urls)) == len(urls)


def test_track_parser_skips_khinsider_and_finds_cdn_link():
    parser = _TrackPageParser()
    parser.feed(_read("track.html"))
    assert parser.direct_url == (
        "https://vgmsite.com/soundtracks/wii-console-background-music/title-theme.mp3"
    )
