"""What an input string is, what it expands to, and how a track becomes audio.

Every way to queue something takes one input string (docs/protocol.md §3.1).
This module owns the three steps the daemon's engine runs on it:

- ``classify(input)`` names its kind, synchronously and without I/O, so a bad
  input is rejected the moment it is posted;
- ``expand(input, kind)`` turns it into ``(title, [Track])`` — a network fetch
  for the collection kinds (KHInsider, Spotify, YouTube playlists), plain
  string work for everything else; the engine runs it on a worker thread;
- ``resolve(track, kind)`` turns one track into the URL or path mpv opens. It
  runs only when the track becomes current, since the stream URLs YouTube and
  KHInsider's CDN hand out are signed and expire.
"""

import os
import re
import urllib.parse
import urllib.request

from tunetape import khinsider, player, spotify
from tunetape.models import Track

# The order is the classification order (protocol §3.1).
KINDS = ("search", "spotify", "youtube_playlist", "youtube", "khinsider",
         "file", "direct", "generic")
COLLECTION_KINDS = ("spotify", "youtube_playlist", "khinsider")
# Kinds whose expansion or resolution runs yt-dlp. youtube_playlist enumerates
# the playlist with yt-dlp, so it needs the binary too.
YTDLP_KINDS = ("search", "youtube", "spotify", "youtube_playlist", "generic")

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
                    ".wav", ".webm", ".mka", ".wma", ".aiff")

ERR_UNSUPPORTED = 1010
ERR_MISSING_DEPENDENCY = 1011

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# Schemes that mpv, ffmpeg or yt-dlp would act on. An input that starts with
# one of these, or with any ``scheme://``, is a URL and must be http(s) to be
# accepted: handing ``lavfi:`` or ``av://`` to mpv would let a client open
# devices and filter graphs. Anything else with a colon ("Re:Zero opening") is
# just words, which only ever reach yt-dlp as a search.
_KNOWN_SCHEMES = {
    "about", "appending", "av", "bd", "bluray", "cdda", "concat", "crypto",
    "data", "dav", "davs", "dvb", "dvd", "dvdnav", "edl", "fd", "fdclose",
    "file", "ftp", "ftps", "gopher", "gophers", "hls", "http", "https", "ipfs",
    "ipns", "javascript", "lavf", "lavfi", "mailto", "memory", "mf", "mms",
    "mmsh", "mmst", "mpv", "null", "pipe", "rtmp", "rtmpe", "rtmps", "rtmpt",
    "rtmpte", "rtmpts", "rtp", "rtsp", "rtsps", "sftp", "slice", "smb", "spotify",
    "srt", "srtp", "subfile", "tcp", "tee", "tls", "udp", "unix", "v4l2", "ytdl",
    "ytsearch",
}


class SourceError(Exception):
    """An input that can't be queued; ``code`` is the protocol error code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_url(text: str) -> bool:
    m = _SCHEME_RE.match(text)
    if not m:
        return False
    return text[m.end():].startswith("//") or m.group(1).lower() in _KNOWN_SCHEMES


def _is_http(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _looks_like_path(text: str) -> bool:
    return (text == "~" or text.startswith(("~/", "~" + os.sep))
            or text.lower().startswith("file:") or os.path.isabs(text))


def local_path(text: str) -> str:
    """The absolute filesystem path an absolute / ``~`` / ``file://`` input names."""
    text = text.strip()
    if text.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(text)
        if parsed.netloc not in ("", "localhost"):
            raise SourceError(ERR_UNSUPPORTED, "Only local file:// URLs can be played.")
        path = urllib.request.url2pathname(parsed.path)
    else:
        path = os.path.expanduser(text)
    return os.path.abspath(path)


def classify(text: str) -> str:
    """The kind of an input (protocol §3.1). Raises SourceError (1010).

    First match wins. A path-shaped input that doesn't name an existing file is
    rejected rather than searched for: "/music/song.mp3" on YouTube is never
    what was meant.
    """
    text = (text or "").strip()
    if not text:
        raise SourceError(ERR_UNSUPPORTED, "Nothing to play: the input is empty.")
    if text.startswith("ytsearch"):
        return "search"
    if spotify.is_spotify_url(text):
        return "spotify"
    if player.is_youtube_playlist_url(text):
        return "youtube_playlist"
    if player.is_youtube_url(text):
        return "youtube"
    if khinsider.is_khinsider_url(text):
        return "khinsider"
    if _looks_like_path(text):
        path = local_path(text)
        if os.path.isfile(path):
            return "file"
        raise SourceError(ERR_UNSUPPORTED, f"No such file: {path}")
    if _is_http(text):
        ext = os.path.splitext(urllib.parse.urlparse(text).path)[1].lower()
        return "direct" if ext in AUDIO_EXTENSIONS else "generic"
    if _is_url(text):
        scheme = _SCHEME_RE.match(text).group(1).lower()
        if scheme == "spotify":
            raise SourceError(ERR_UNSUPPORTED,
                              "Only Spotify tracks, albums and playlists can be played.")
        raise SourceError(ERR_UNSUPPORTED,
                          f"Unsupported link type ({scheme}:). Use an http(s) URL or a local file.")
    return "search"


def canonical_input(text: str, kind: str) -> str:
    """The form an input is queued, recorded in history and replayed under.

    Plain words become the ``ytsearch1:`` query they run; local paths become
    absolute paths, so replay doesn't depend on the client's working directory.
    """
    text = text.strip()
    if kind == "search" and not text.startswith("ytsearch"):
        return f"ytsearch1:{text}"
    if kind == "file":
        return local_path(text)
    return text


def check_dependencies(kind: str) -> None:
    """Raise SourceError (1011) when mpv, or yt-dlp for this kind, is missing."""
    try:
        player.check_dependencies(require_ytdlp=kind in YTDLP_KINDS)
    except RuntimeError as e:
        raise SourceError(ERR_MISSING_DEPENDENCY, str(e))


def _search_words(query: str) -> str:
    return query.split(":", 1)[1].strip() if ":" in query else query


def _url_basename(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return os.path.splitext(name)[0] or url


def placeholder_title(text: str, kind: str):
    """A display title for a single-track input before it is resolved, or None.

    None means "only the resolver knows" (yt-dlp prints the real title).
    """
    if kind == "file":
        return os.path.splitext(os.path.basename(text))[0] or text
    if kind == "direct":
        return _url_basename(text)
    return None


def expand(text: str, kind: str):
    """``(title, [Track])`` for a canonical input. May do network I/O.

    ``title`` is None for a single track whose title only resolution reveals.
    Raises ValueError / RuntimeError / OSError / ConnectionError with a
    user-facing message.
    """
    if kind == "khinsider":
        album = khinsider.fetch_album(text)
        return album.title, album.tracks
    if kind == "spotify":
        album = spotify.fetch_spotify(text)
        return album.title, album.tracks
    if kind == "youtube_playlist":
        album = player.fetch_youtube_playlist(text)
        return album.title, album.tracks
    title = placeholder_title(text, kind)
    if kind == "search":
        name = title or _search_words(text)
    else:
        name = title or text
    return title, [Track(name=name, resolve_hint=text)]


def resolve(track: Track, kind: str) -> str:
    """The URL or path mpv should open for ``track``. Blocking; may hit the network.

    A title learned on the way (yt-dlp prints one) lands on
    ``track.resolved_title``. Raises RuntimeError / OSError / ValueError /
    ConnectionError with a user-facing message.
    """
    if kind == "khinsider":
        return khinsider.resolve_track_url(track)
    if kind == "spotify":
        return spotify.resolve_track_url(track)
    if kind in ("file", "direct"):
        if kind == "file" and not os.path.isfile(track.resolve_hint):
            raise RuntimeError(f"The file is gone: {track.resolve_hint}")
        return track.resolve_hint
    # search, youtube, youtube_playlist entries and generic pages: yt-dlp.
    # Stream URLs expire, so these are re-resolved every time they play.
    info = player.resolve_with_ytdlp(track.resolve_hint)
    if info.get("title") and info["title"] != "Unknown title":
        track.resolved_title = info["title"]
    return info["stream_url"]
