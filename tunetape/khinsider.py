import re
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional


@dataclass
class Track:
    name: str
    track_page_url: str
    direct_url: Optional[str] = None


@dataclass
class Album:
    title: str
    tracks: list = field(default_factory=list)


_KHINSIDER_RE = re.compile(
    r"^https?://downloads\.khinsider\.com/game-soundtracks/album/.+"
)


def is_khinsider_url(url: str) -> bool:
    return bool(_KHINSIDER_RE.match(url.strip()))


class _AlbumPageParser(HTMLParser):
    """Parse an album page to extract title and track links."""

    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self.title = ""
        self.tracks = []  # list of (url, name)
        self._in_page_content = False
        self._in_first_h2 = False
        self._h2_count = 0
        self._seen_hrefs = set()
        self._current_track_href = None
        self._current_track_text = ""
        parts = urllib.parse.urlparse(base_url)
        self._album_path = parts.path.rstrip("/")

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "div" and attrs_dict.get("id") == "pageContent":
            self._in_page_content = True
        if tag == "h2" and self._in_page_content:
            self._h2_count += 1
            if self._h2_count == 1:
                self._in_first_h2 = True

        if tag == "a":
            href = attrs_dict.get("href", "")
            if self._album_path in href and href.endswith(".mp3"):
                full_url = urllib.parse.urljoin(
                    "https://downloads.khinsider.com", href
                )
                if full_url not in self._seen_hrefs:
                    self._seen_hrefs.add(full_url)
                    self._current_track_href = full_url
                    self._current_track_text = ""

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_first_h2 = False
        if tag == "a" and self._current_track_href:
            name = self._current_track_text.strip()
            if not name:
                # Derive name from URL filename as fallback
                path = urllib.parse.urlparse(self._current_track_href).path
                name = urllib.parse.unquote(urllib.parse.unquote(
                    path.rsplit("/", 1)[-1]
                ))
                name = re.sub(r"\.\w+$", "", name)  # strip extension
                name = re.sub(r"^\d+[\s\-_.]+", "", name)  # strip leading numbers
                name = name or "Untitled"
            self.tracks.append((self._current_track_href, name))
            self._current_track_href = None
            self._current_track_text = ""

    def handle_data(self, data):
        if self._in_first_h2:
            self.title += data
        if self._current_track_href:
            self._current_track_text += data


_CDN_AUDIO_RE = re.compile(
    r"https?://.*\.(mp3|flac|ogg|m4a)(\?.*)?$", re.IGNORECASE
)


class _TrackPageParser(HTMLParser):
    """Parse a track page to find the direct CDN download link."""

    def __init__(self):
        super().__init__()
        self.direct_url = None

    def handle_starttag(self, tag, attrs):
        if self.direct_url:
            return
        if tag == "a":
            href = dict(attrs).get("href", "")
            # Match any external audio file URL (not a khinsider page link)
            if _CDN_AUDIO_RE.match(href) and "khinsider.com" not in href:
                self.direct_url = href


def _fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "tunetape/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_album(url: str) -> Album:
    """Fetch album page and return Album with tracks (1 HTTP request)."""
    url = url.strip()
    if not is_khinsider_url(url):
        raise ValueError("Invalid KHInsider album URL.")

    html = _fetch_page(url)
    parser = _AlbumPageParser(url)
    parser.feed(html)

    if not parser.tracks:
        raise ValueError(f"No tracks found on album page: {url}")

    title = parser.title.strip() or "Unknown Album"
    tracks = [
        Track(name=name, track_page_url=track_url)
        for track_url, name in parser.tracks
    ]
    return Album(title=title, tracks=tracks)


def resolve_track_url(track: Track) -> str:
    """Resolve direct MP3 URL for a track (1 HTTP request, cached)."""
    if track.direct_url:
        return track.direct_url

    html = _fetch_page(track.track_page_url)
    parser = _TrackPageParser()
    parser.feed(html)

    if not parser.direct_url:
        raise RuntimeError(f"Could not find download link for: {track.name}")

    track.direct_url = parser.direct_url
    return track.direct_url
