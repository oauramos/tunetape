"""Spotify source adapter.

Spotify's own audio is DRM-protected and can't be streamed directly, so Spotify
is used purely as a *metadata* source: we read the public embed page (no login,
no API key) to get each track's title + artist, then resolve the actual audio by
searching YouTube via yt-dlp (``ytsearch1:<artist> <title>``) at play time.

Mirrors the KHInsider adapter's shape — a URL matcher, a fetcher that returns an
``Album`` of lazy ``Track`` objects, and a lazy per-track resolver — and uses
only the standard library plus the existing yt-dlp binary.
"""

import json
import re
import urllib.request
from html.parser import HTMLParser

from tunetape.models import Album, Track
from tunetape.player import resolve_with_ytdlp

# open.spotify.com links, optionally with an `intl-xx/` locale segment and a
# trailing `?si=…` (ignored — we capture only kind + id). Plus `spotify:` URIs.
_SPOTIFY_URL_RE = re.compile(
    r"^(?:https?://)?open\.spotify\.com/(?:intl-[a-z]{2}/)?"
    r"(playlist|track|album)/([A-Za-z0-9]+)"
)
_SPOTIFY_URI_RE = re.compile(r"^spotify:(playlist|track|album):([A-Za-z0-9]+)$")

_EMBED_URL = "https://open.spotify.com/embed/{kind}/{sid}"

# A browser-ish UA — Spotify's embed is a web page and is less reliable for
# unfamiliar user agents.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"


def is_spotify_url(url: str) -> bool:
    url = url.strip()
    return bool(_SPOTIFY_URL_RE.match(url) or _SPOTIFY_URI_RE.match(url))


def _parse_ref(url: str):
    """Return (kind, id) for a Spotify URL or URI. Raises ValueError."""
    url = url.strip()
    m = _SPOTIFY_URL_RE.match(url) or _SPOTIFY_URI_RE.match(url)
    if not m:
        raise ValueError("Invalid Spotify URL.")
    return m.group(1), m.group(2)


class _NextDataParser(HTMLParser):
    """Capture the JSON body of <script id="__NEXT_DATA__">…</script>.

    HTMLParser treats <script> as CDATA, so the JSON arrives intact via
    handle_data without being mangled as markup.
    """

    def __init__(self):
        super().__init__()
        self._capture = False
        self._chunks = []
        self.json_text = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self._capture = True

    def handle_endtag(self, tag):
        if tag == "script" and self._capture:
            self._capture = False
            self.json_text = "".join(self._chunks)

    def handle_data(self, data):
        if self._capture:
            self._chunks.append(data)


def _fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(s) -> str:
    # Spotify joins multiple artists with a non-breaking space.
    return (s or "").replace("\xa0", " ").strip()


def _query(artist: str, name: str) -> str:
    """Build the yt-dlp search that finds this track's audio on YouTube."""
    parts = " ".join(p for p in (artist, name) if p)
    return f"ytsearch1:{parts}"


def _track_from_item(item: dict):
    """A playlist/album track: title=name, subtitle=artist(s)."""
    name = _clean(item.get("title"))
    if not name:
        return None
    return Track(name=name, resolve_hint=_query(_clean(item.get("subtitle")), name))


def _track_from_entity(entity: dict):
    """A single track: title=name, artist(s) are in artists[] (subtitle is null)."""
    name = _clean(entity.get("title") or entity.get("name"))
    if not name:
        return None
    artists = entity.get("artists") or []
    artist = _clean(", ".join(
        a.get("name", "") for a in artists if isinstance(a, dict)
    ))
    return Track(name=name, resolve_hint=_query(artist, name))


def _format_err(url: str) -> str:
    return (
        "Could not read Spotify data. The link may be private, or "
        "Spotify's page format may have changed.\n\n"
        f"URL: {url}"
    )


def fetch_spotify(url: str) -> Album:
    """Fetch a Spotify playlist/album/track and return an Album of Tracks.

    Each track's audio is resolved later via resolve_track_url (a YouTube
    search). One HTTP request to the public embed page; no auth.
    """
    kind, sid = _parse_ref(url)
    html = _fetch_page(_EMBED_URL.format(kind=kind, sid=sid))

    parser = _NextDataParser()
    parser.feed(html)
    if not parser.json_text:
        raise ValueError(_format_err(url))

    try:
        entity = json.loads(parser.json_text)["props"]["pageProps"]["state"]["data"]["entity"]
        title = _clean(entity.get("title") or entity.get("name")) or "Spotify"
        track_list = entity.get("trackList") or []
        if track_list:
            tracks = [_track_from_item(it) for it in track_list]
        else:
            tracks = [_track_from_entity(entity)]
    except (KeyError, TypeError, IndexError, ValueError):
        raise ValueError(_format_err(url))

    tracks = [t for t in tracks if t]
    if not tracks:
        raise ValueError(_format_err(url))
    return Album(title=title, tracks=tracks)


def resolve_track_url(track: Track) -> str:
    """Resolve a track's YouTube audio stream URL (cached on the track)."""
    if track.direct_url:
        return track.direct_url
    info = resolve_with_ytdlp(track.resolve_hint)
    track.direct_url = info["stream_url"]
    return track.direct_url
