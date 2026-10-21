"""Live network/playback tests. Skipped unless TUNETAPE_NETWORK_TESTS is set.

    TUNETAPE_NETWORK_TESTS=1 pytest tests/test_network.py

For an exhaustive sweep of many URLs, run the standalone script instead:

    python tests/test_urls.py
"""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TUNETAPE_NETWORK_TESTS"),
    reason="set TUNETAPE_NETWORK_TESTS=1 to run live network tests",
)

from tunetape import khinsider
from tunetape.player import MPVController, check_dependencies, get_stream_info

URLS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/5NV6Rdv1a3I",
]


@pytest.mark.parametrize("url", URLS)
def test_live_extraction(url):
    info = get_stream_info(url)
    assert info["stream_url"].startswith("http")
    assert info["title"]


def test_live_playback():
    check_dependencies(require_ytdlp=True)
    info = get_stream_info(URLS[0])
    controller = MPVController(info["stream_url"])
    try:
        time.sleep(3)
        assert controller.is_alive()
    finally:
        controller.quit()


KHINSIDER_ALBUM = "https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger-2015"


@pytest.mark.parametrize("fetch", [khinsider._fetch_impersonating, khinsider._fetch_with_curl])
def test_live_khinsider_gets_past_cloudflare(fetch, monkeypatch):
    # Each browser-like fetcher on its own; urllib alone is turned away.
    if fetch is khinsider._fetch_impersonating:
        pytest.importorskip("curl_cffi")
    monkeypatch.setattr(khinsider, "_FETCHERS", (fetch,))
    album = khinsider.fetch_album(KHINSIDER_ALBUM)
    assert album.tracks
    assert khinsider.resolve_track_url(album.tracks[0]).startswith("http")

