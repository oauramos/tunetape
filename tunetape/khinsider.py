import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from tunetape import __version__, debug

# Track/Album live in a neutral module so other source adapters (spotify, the
# YouTube-playlist code) don't have to import from khinsider. Re-exported here
# so existing `from tunetape.khinsider import Track, Album` callers keep working.
from tunetape.models import Album, Track

_KHINSIDER_RE = re.compile(
    r"^https?://downloads\.khinsider\.com/game-soundtracks/album/.+"
)

# Audio extensions tunetape can stream — single source of truth so the
# album-page link filter (_TRACK_LINK_EXTS) and the CDN link matcher
# (_CDN_AUDIO_RE, built from the same tuple below) can never drift apart.
_AUDIO_EXTS = ("mp3", "flac", "ogg", "m4a", "wav")
_TRACK_LINK_EXTS = tuple("." + ext for ext in _AUDIO_EXTS)


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
            if self._album_path in href and href.lower().endswith(_TRACK_LINK_EXTS):
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
    r"https?://.*\.(" + "|".join(_AUDIO_EXTS) + r")(\?.*)?$", re.IGNORECASE
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


# KHInsider's pages sit behind Cloudflare, which (since October 2026) turns
# away Python's own TLS handshake: urllib gets "Sorry, you have been blocked"
# whatever headers it sends. Fetchers that look like a browser get through:
# curl_cffi, which presents Chrome's TLS and HTTP/2 fingerprint, and the
# system curl over HTTP/1.1 (macOS's /usr/bin/curl is what the Homebrew build
# uses, as it doesn't bundle curl_cffi). urllib stays last for when neither
# is around. Only the HTML pages are guarded; the audio CDN serves mpv as is.
_USER_AGENT = f"tunetape/{__version__} (+https://github.com/oauramos/tunetape)"
_TIMEOUT = 30
_STATUS_MARK = "\n--tunetape-status:"


class _HTTPStatus(Exception):
    """A fetcher reached the server and got a non-200 answer."""

    def __init__(self, status: int, via: str):
        super().__init__(f"HTTP {status} via {via}")
        self.status = status


# Which browser fingerprint gets through varies with the curl_cffi version
# (0.13's "chrome" is turned away where its "safari" isn't), so try a few and
# start with whichever worked last time.
_IMPERSONATE = ["chrome", "safari", "firefox"]


def _fetch_impersonating(url: str):
    """curl_cffi posing as a browser, or None when it isn't installed."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    refused = None
    for target in list(_IMPERSONATE):
        try:
            response = cffi_requests.get(url, impersonate=target, timeout=_TIMEOUT)
        except ValueError:  # a target this curl_cffi doesn't know
            continue
        if response.status_code == 200:
            _IMPERSONATE.remove(target)
            _IMPERSONATE.insert(0, target)
            return response.text
        refused = _HTTPStatus(response.status_code, f"curl_cffi ({target})")
        if response.status_code != 403:
            break  # a 404 is the same whatever we look like
    raise refused or _HTTPStatus(0, "curl_cffi")


def _fetch_with_curl(url: str):
    """The system curl over HTTP/1.1, or None when there is no curl."""
    curl = shutil.which("curl")
    if curl is None or not url.startswith(("https://", "http://")):
        return None
    proc = subprocess.run(
        [curl, "--http1.1", "--silent", "--show-error", "--location",
         "--max-time", str(_TIMEOUT), "--user-agent", _USER_AGENT,
         "--write-out", _STATUS_MARK + "%{http_code}", url],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise OSError(proc.stderr.decode("utf-8", "replace").strip() or "curl failed")
    body, _, status = proc.stdout.decode("utf-8", "replace").rpartition(_STATUS_MARK)
    if status.strip() != "200":
        raise _HTTPStatus(int(status.strip() or 0), "curl")
    return body


def _fetch_with_urllib(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise _HTTPStatus(e.code, "urllib") from e


_FETCHERS = (_fetch_impersonating, _fetch_with_curl, _fetch_with_urllib)


def _fetch_page(url: str) -> str:
    """The page's HTML, from the first fetcher KHInsider lets through."""
    failures = []
    for fetch in _FETCHERS:
        try:
            html = fetch(url)
        except Exception as e:  # each fetcher has its own errors; try the next
            debug.log(f"KHInsider fetch via {fetch.__name__}: {e}", "WARN")
            failures.append(e)
            continue
        if html is not None:
            return html
    statuses = {e.status for e in failures if isinstance(e, _HTTPStatus)}
    if statuses == {404}:
        raise RuntimeError("KHInsider has no page at this URL (HTTP 404). Double-check the link.")
    if 403 in statuses:
        raise RuntimeError(
            "KHInsider refused the connection (HTTP 403 from its Cloudflare protection). "
            "Try again later; if it keeps happening, check that curl is installed."
        )
    raise RuntimeError(f"Could not reach KHInsider: {failures[-1] if failures else 'no fetcher'}")


def fetch_album(url: str) -> Album:
    """Fetch album page and return Album with tracks (1 HTTP request)."""
    url = url.strip()
    if not is_khinsider_url(url):
        raise ValueError("Invalid KHInsider album URL.")

    html = _fetch_page(url)
    parser = _AlbumPageParser(url)
    parser.feed(html)

    if not parser.tracks:
        raise ValueError(
            "No tracks found on the album page. Double-check the URL, "
            "or KHInsider's page format may have changed.\n\n"
            f"URL: {url}"
        )

    title = parser.title.strip() or "Unknown Album"
    tracks = [
        Track(name=name, resolve_hint=track_url)
        for track_url, name in parser.tracks
    ]
    return Album(title=title, tracks=tracks)


def resolve_track_url(track: Track) -> str:
    """Resolve direct MP3 URL for a track (1 HTTP request, cached)."""
    if track.direct_url:
        return track.direct_url

    html = _fetch_page(track.resolve_hint)
    parser = _TrackPageParser()
    parser.feed(html)

    if not parser.direct_url:
        raise RuntimeError(
            f"Could not find a download link for '{track.name}'. "
            "KHInsider's page format may have changed."
        )

    track.direct_url = parser.direct_url
    return track.direct_url
