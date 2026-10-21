"""KHInsider page fetching: browser-like fetchers first, urllib last."""

import pytest

from tunetape import khinsider
from tunetape.khinsider import _HTTPStatus

URL = "https://downloads.khinsider.com/game-soundtracks/album/x"


def fetchers(monkeypatch, *behaviours):
    """Install fake fetchers; each behaviour is HTML, None (unavailable) or an exception."""
    calls = []

    def make(i, behaviour):
        def fetch(url):
            calls.append(i)
            if isinstance(behaviour, Exception):
                raise behaviour
            return behaviour
        fetch.__name__ = f"fetch{i}"
        return fetch

    monkeypatch.setattr(khinsider, "_FETCHERS", tuple(make(i, b) for i, b in enumerate(behaviours)))
    return calls


def test_first_fetcher_that_works_wins(monkeypatch):
    calls = fetchers(monkeypatch, None, "<html>ok</html>", "<html>never</html>")
    assert khinsider._fetch_page(URL) == "<html>ok</html>"
    assert calls == [0, 1]  # the unavailable one is skipped, the third never runs


def test_a_blocked_fetcher_falls_through(monkeypatch):
    calls = fetchers(monkeypatch, _HTTPStatus(403, "curl_cffi"), OSError("boom"), "<html>ok</html>")
    assert khinsider._fetch_page(URL) == "<html>ok</html>"
    assert calls == [0, 1, 2]


def test_blocked_everywhere_says_so(monkeypatch):
    fetchers(monkeypatch, None, _HTTPStatus(403, "curl"), _HTTPStatus(403, "urllib"))
    with pytest.raises(RuntimeError, match="Cloudflare"):
        khinsider._fetch_page(URL)


def test_missing_page_says_so(monkeypatch):
    fetchers(monkeypatch, _HTTPStatus(404, "curl_cffi"), _HTTPStatus(404, "curl"), _HTTPStatus(404, "urllib"))
    with pytest.raises(RuntimeError, match="404"):
        khinsider._fetch_page(URL)


def test_curl_reports_the_status(monkeypatch):
    class Done:
        returncode = 0
        stdout = ("<html>page</html>" + khinsider._STATUS_MARK + "403").encode()
        stderr = b""

    monkeypatch.setattr(khinsider.shutil, "which", lambda name: "/usr/bin/curl")
    seen = {}

    def run(args, **kwargs):
        seen["args"] = args
        return Done()

    monkeypatch.setattr(khinsider.subprocess, "run", run)
    with pytest.raises(_HTTPStatus) as caught:
        khinsider._fetch_with_curl(URL)
    assert caught.value.status == 403
    assert "--http1.1" in seen["args"] and seen["args"][-1] == URL

    Done.stdout = ("<html>page</html>" + khinsider._STATUS_MARK + "200").encode()
    assert khinsider._fetch_with_curl(URL) == "<html>page</html>"


def test_curl_is_skipped_when_missing_or_for_odd_urls(monkeypatch):
    monkeypatch.setattr(khinsider.shutil, "which", lambda name: None)
    assert khinsider._fetch_with_curl(URL) is None
    monkeypatch.setattr(khinsider.shutil, "which", lambda name: "/usr/bin/curl")
    assert khinsider._fetch_with_curl("-o/etc/passwd") is None  # never passed to curl as a flag


def test_impersonation_tries_other_browsers_and_remembers(monkeypatch):
    curl_cffi = pytest.importorskip("curl_cffi")
    tried = []

    class Response:
        def __init__(self, status):
            self.status_code, self.text = status, "<html>ok</html>"

    def get(url, impersonate, timeout):
        tried.append(impersonate)
        return Response(200 if impersonate == "safari" else 403)

    monkeypatch.setattr(curl_cffi.requests, "get", get)
    monkeypatch.setattr(khinsider, "_IMPERSONATE", ["chrome", "safari", "firefox"])
    assert khinsider._fetch_impersonating(URL) == "<html>ok</html>"
    assert tried == ["chrome", "safari"]
    tried.clear()
    khinsider._fetch_impersonating(URL)
    assert tried == ["safari"]  # the one that worked goes first next time


def test_impersonation_stops_at_a_missing_page(monkeypatch):
    curl_cffi = pytest.importorskip("curl_cffi")
    tried = []

    def get(url, impersonate, timeout):
        tried.append(impersonate)
        return type("R", (), {"status_code": 404, "text": ""})()

    monkeypatch.setattr(curl_cffi.requests, "get", get)
    with pytest.raises(_HTTPStatus) as caught:
        khinsider._fetch_impersonating(URL)
    assert caught.value.status == 404 and len(tried) == 1

