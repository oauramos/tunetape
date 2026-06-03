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
