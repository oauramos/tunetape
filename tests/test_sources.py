"""Input classification (docs/protocol.md §3.1) and the per-kind expand/resolve."""

import os
import sys

import pytest

from tunetape import paths, player, sources
from tunetape.sources import SourceError, classify


@pytest.mark.parametrize("text,kind", [
    # 1. ytsearch wins over everything, even text that looks like a URL.
    ("ytsearch1:daft punk around the world", "search"),
    ("ytsearch5:https://example.com/x.mp3", "search"),
    # 2. Spotify
    ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "spotify"),
    ("https://open.spotify.com/intl-de/album/4aawyAB9vmqN3uQ7FjRGTy?si=x", "spotify"),
    ("spotify:track:6rqhFgbbKwnb9MLmUQDhG6", "spotify"),
    # 3. A YouTube playlist, also as a watch URL carrying list= (before #4).
    ("https://www.youtube.com/playlist?list=PL1234567890", "youtube_playlist"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890", "youtube_playlist"),
    # 4. YouTube video
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    # 5. KHInsider
    ("https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger", "khinsider"),
    # 7. Direct audio links, by path extension (query strings don't matter).
    ("https://example.com/audio/song.mp3", "direct"),
    ("http://example.com/a/B.FLAC?token=1", "direct"),
    ("https://cdn.example.com/x.opus", "direct"),
    ("https://example.com/stream.aiff", "direct"),
    # 8. Any other http(s) page goes to yt-dlp's generic extractors.
    ("https://soundcloud.com/artist/track", "generic"),
    ("https://example.com/page.html", "generic"),
    ("https://example.com/song.mp3.html", "generic"),
    # 9. Words. A colon only makes a scheme when the input has no whitespace.
    ("daft punk around the world", "search"),
    ("Re:Zero opening", "search"),
    ("artist: song title", "search"),
    ("lavfi:sine with spaces", "search"),  # only ever reaches yt-dlp as a search
    ("C:/not/a/path/on/posix" if sys.platform != "win32" else "plain words", "search"),
])
def test_classify(text, kind):
    assert classify(text) == kind


def test_khinsider_outranks_direct():
    # KHInsider (#5) is checked before the direct-extension rule (#7).
    url = "https://downloads.khinsider.com/game-soundtracks/album/x/01.mp3"
    assert classify(url) == "khinsider"


@pytest.mark.parametrize("text", [
    "ftp://example.com/song.mp3", "av://dshow:video=x", "lavfi:sine", "memory:abc",
    "rtsp://camera/stream", "smb://server/share/a.mp3", "data:audio/mp3;base64,AAAA",
    "whatever://thing", "spotify:artist:0OdUWJ0sBjDrqHygGUXeCF", "Re:Zero",
    "localhost:8080/x.mp3",
])
def test_other_schemes_are_rejected(text):
    with pytest.raises(SourceError) as e:
        classify(text)
    assert e.value.code == 1010


def test_empty_input_is_rejected():
    with pytest.raises(SourceError) as e:
        classify("   ")
    assert e.value.code == 1010


def test_local_files(tmp_path, monkeypatch):
    song = tmp_path / "My Song.mp3"
    song.write_bytes(b"")
    assert classify(str(song)) == "file"
    assert classify(song.as_uri()) == "file"
    assert sources.canonical_input(song.as_uri(), "file") == str(song)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert classify("~/My Song.mp3") == "file"
    assert sources.canonical_input("~/My Song.mp3", "file") == str(song)


@pytest.mark.parametrize("text", [
    "/no/such/file.mp3", "~/no-such-file.mp3", "./no-such-file.mp3", "../no-such.mp3",
    "file:///no/such/file.mp3",
])
def test_missing_file_is_an_error_not_a_search(text):
    with pytest.raises(SourceError) as e:
        classify(text)
    assert e.value.code == 1010
    assert e.value.message.startswith("No such file: ")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
@pytest.mark.parametrize("text", ["C:\\no\\such.mp3", "c:/no/such.mp3",
                                  "\\\\server\\share\\no.mp3"])
def test_windows_paths_are_paths(text):
    with pytest.raises(SourceError) as e:
        classify(text)
    assert e.value.message.startswith("No such file: ")


def test_remote_file_url_is_rejected():
    with pytest.raises(SourceError) as e:
        classify("file://otherhost/share/song.mp3")
    assert e.value.code == 1010


def test_canonical_input_for_words():
    assert sources.canonical_input("  daft punk ", "search") == "ytsearch1:daft punk"
    assert sources.canonical_input("ytsearch1:x", "search") == "ytsearch1:x"


def test_expand_single_kinds(tmp_path):
    title, tracks = sources.expand("ytsearch1:daft punk", "search")
    assert title is None and tracks[0].name == "daft punk"
    assert tracks[0].resolve_hint == "ytsearch1:daft punk"

    title, tracks = sources.expand("https://example.com/a/My%20Tune.mp3", "direct")
    assert title == "My Tune" and len(tracks) == 1

    song = tmp_path / "Track Name.flac"
    song.write_bytes(b"")
    title, tracks = sources.expand(str(song), "file")
    assert title == "Track Name"
    assert sources.resolve(tracks[0], "file") == str(song)

    song.unlink()
    with pytest.raises(RuntimeError):
        sources.resolve(tracks[0], "file")


def test_direct_resolves_to_itself():
    _, tracks = sources.expand("https://example.com/x.ogg", "direct")
    assert sources.resolve(tracks[0], "direct") == "https://example.com/x.ogg"


def test_yt_dlp_kinds_use_the_override(data_dir):
    # TUNETAPE_YTDLP points at tests/fakes/fake_ytdlp.py (see conftest).
    _, tracks = sources.expand("ytsearch1:daft punk", "search")
    assert sources.resolve(tracks[0], "search").startswith("https://media.invalid/")
    assert tracks[0].resolved_title == "Fake: daft punk"

    title, tracks = sources.expand("https://www.youtube.com/playlist?list=PLx", "youtube_playlist")
    assert [t.name for t in tracks] == ["Playlist video 1", "Playlist video 2", "Playlist video 3"]


# -- tool overrides (docs/architecture.md, "Test seams") ----------------------

def test_tool_command_override(monkeypatch):
    monkeypatch.setenv("TUNETAPE_MPV", "/usr/bin/python3 'tests/fakes/fake mpv.py' --x")
    if sys.platform == "win32":
        # A real interpreter: an override whose program is missing counts as
        # no mpv at all. The quotes keep a path with spaces in one piece.
        monkeypatch.setenv("TUNETAPE_MPV", f'"{sys.executable}" "fake mpv.py" --x')
        assert player.tool_command("mpv") == [sys.executable, "fake mpv.py", "--x"]
    else:
        assert player.tool_command("mpv") == ["/usr/bin/python3", "tests/fakes/fake mpv.py", "--x"]


def test_override_counts_as_installed(monkeypatch):
    monkeypatch.setattr(player, "find_executable", lambda name: None)
    monkeypatch.setenv("TUNETAPE_MPV", f'"{sys.executable}" fake_mpv.py')
    monkeypatch.setenv("TUNETAPE_YTDLP", f'"{sys.executable}" fake_ytdlp.py')
    player.check_dependencies(require_ytdlp=True)  # does not raise


def test_override_to_nothing_counts_as_missing(monkeypatch):
    # Even with the real tool installed, a dangling override means "missing":
    # that is how a test simulates an uninstalled yt-dlp.
    monkeypatch.setattr(player, "find_executable", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("TUNETAPE_MPV", f'"{sys.executable}" fake_mpv.py')
    monkeypatch.setenv("TUNETAPE_YTDLP", "/nonexistent/yt-dlp --flag")
    assert player.tool_command("yt-dlp") is None
    assert player._tool_argv("yt-dlp") == ["/nonexistent/yt-dlp", "--flag"]
    with pytest.raises(SourceError) as e:
        sources.check_dependencies("search")
    assert e.value.code == 1011
    sources.check_dependencies("file")


def test_missing_dependency_is_1011(monkeypatch):
    monkeypatch.setattr(player, "find_executable", lambda name: None)
    monkeypatch.delenv("TUNETAPE_MPV", raising=False)
    monkeypatch.delenv("TUNETAPE_YTDLP", raising=False)
    with pytest.raises(SourceError) as e:
        sources.check_dependencies("file")
    assert e.value.code == 1011 and "mpv" in e.value.message

    monkeypatch.setenv("TUNETAPE_MPV", f'"{sys.executable}"')
    sources.check_dependencies("file")  # mpv is enough for a local file
    sources.check_dependencies("direct")
    for kind in ("search", "youtube", "spotify", "youtube_playlist", "generic"):
        with pytest.raises(SourceError) as e:
            sources.check_dependencies(kind)
        assert e.value.code == 1011 and "yt-dlp" in e.value.message


def test_data_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNETAPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.data_dir() == str(tmp_path)
    # A relative override is ignored, like a relative XDG_DATA_HOME.
    monkeypatch.setenv("TUNETAPE_DATA_DIR", "relative/dir")
    assert paths.data_dir() != "relative/dir"
    assert os.path.isabs(paths.data_dir())
