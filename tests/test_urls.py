#!/usr/bin/env python3
"""
tunetape URL Test Agent
Tests 30 YouTube music URLs against the player backend.
Verifies: URL validation, stream extraction, and mpv playback (3s each).
"""

import subprocess
import sys
import time
import json
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tunetape.player import get_stream_info, check_dependencies, MPVController

# 30 popular YouTube music URLs (mixed formats)
TEST_URLS = [
    # Standard watch URLs
    ("Rick Astley - Never Gonna Give You Up", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("Toto - Africa", "https://www.youtube.com/watch?v=FTQbiNvZqaY"),
    ("a-ha - Take On Me", "https://www.youtube.com/watch?v=djV11Xbc914"),
    ("Queen - Bohemian Rhapsody", "https://www.youtube.com/watch?v=fJ9rUzIMcZQ"),
    ("Eagles - Hotel California", "https://www.youtube.com/watch?v=BciS5krYL80"),
    ("Bee Gees - Stayin Alive", "https://www.youtube.com/watch?v=fNFzfwLM72c"),
    ("Nirvana - Smells Like Teen Spirit", "https://www.youtube.com/watch?v=hTWKbfoikeg"),
    ("Michael Jackson - Billie Jean", "https://www.youtube.com/watch?v=Zi_XLOBDo_Y"),
    ("The Beatles - Here Comes The Sun", "https://www.youtube.com/watch?v=KQetemT1sWc"),
    ("Led Zeppelin - Stairway To Heaven", "https://www.youtube.com/watch?v=QkF3oxziUI4"),
    ("Pink Floyd - Comfortably Numb", "https://www.youtube.com/watch?v=_FrOQC-zEog"),
    ("Guns N Roses - Sweet Child O Mine", "https://www.youtube.com/watch?v=1w7OgIMMRc4"),
    ("AC/DC - Back In Black", "https://www.youtube.com/watch?v=pAgnJDJN4VA"),
    ("Bob Marley - Three Little Birds", "https://www.youtube.com/watch?v=zaGUr6wzyT8"),
    ("Adele - Someone Like You", "https://www.youtube.com/watch?v=hLQl3WQQoQ0"),
    ("Ed Sheeran - Shape of You", "https://www.youtube.com/watch?v=JGwWNGJdvx8"),
    ("Imagine Dragons - Radioactive", "https://www.youtube.com/watch?v=ktvTqknDobU"),
    ("Coldplay - Yellow", "https://www.youtube.com/watch?v=yKNxeF4KMsY"),
    ("The Weeknd - Blinding Lights", "https://www.youtube.com/watch?v=4NRXx6U8ABQ"),
    ("Dua Lipa - Levitating", "https://www.youtube.com/watch?v=TUVcZfQe-Kw"),
    # youtu.be short URLs
    ("Daft Punk - Get Lucky", "https://youtu.be/5NV6Rdv1a3I"),
    ("Pharrell - Happy", "https://youtu.be/ZbZSe6N_BXs"),
    ("Mark Ronson - Uptown Funk", "https://youtu.be/OPf0YbXqDm0"),
    ("Luis Fonsi - Despacito", "https://youtu.be/kJQP7kiw5Fk"),
    ("PSY - Gangnam Style", "https://youtu.be/9bZkp7q19f0"),
    # More standard URLs
    ("Eminem - Lose Yourself", "https://www.youtube.com/watch?v=_Yhyp-_hX2s"),
    ("Linkin Park - In The End", "https://www.youtube.com/watch?v=eVTXPUF4Oz4"),
    ("Green Day - Boulevard of Broken Dreams", "https://www.youtube.com/watch?v=Soa3gO7tL-c"),
    ("Oasis - Wonderwall", "https://www.youtube.com/watch?v=bx1Bh8ZvH84"),
    ("Radiohead - Creep", "https://www.youtube.com/watch?v=XFkzRNyygfk"),
]


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def test_stream_extraction(name, url):
    """Test if yt-dlp can extract the audio stream URL."""
    try:
        info = get_stream_info(url)
        title = info["title"]
        has_stream = info["stream_url"].startswith("http")
        return True, title, info["stream_url"]
    except ValueError as e:
        return False, f"Invalid URL: {e}", None
    except ConnectionError as e:
        return False, f"Network error: {e}", None
    except RuntimeError as e:
        return False, f"Extraction failed: {e}", None


def test_playback(stream_url, seconds=3):
    """Test if mpv can play the stream for N seconds."""
    sock_path = f"/tmp/tunetape_test_{os.getpid()}.sock"
    try:
        proc = subprocess.Popen(
            ["mpv", "--no-video", "--no-terminal",
             f"--input-ipc-server={sock_path}", stream_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(seconds)
        alive = proc.poll() is None
        proc.terminate()
        proc.wait(timeout=3)
        return alive
    except Exception as e:
        return False
    finally:
        if os.path.exists(sock_path):
            os.unlink(sock_path)


def main():
    print()
    print(f"  {Colors.BOLD}{Colors.CYAN}tunetape Test Agent{Colors.RESET}")
    print(f"  {Colors.DIM}Testing {len(TEST_URLS)} YouTube URLs{Colors.RESET}")
    print(f"  {Colors.DIM}{'=' * 60}{Colors.RESET}")
    print()

    # Check dependencies first
    try:
        check_dependencies()
        print(f"  {Colors.GREEN}[ok]{Colors.RESET} mpv and yt-dlp found")
    except RuntimeError as e:
        print(f"  {Colors.RED}[!!]{Colors.RESET} {e}")
        sys.exit(1)

    print()

    results = {"pass": 0, "fail_extract": 0, "fail_play": 0}

    for i, (name, url) in enumerate(TEST_URLS, 1):
        prefix = f"  [{i:02d}/{len(TEST_URLS)}]"
        print(f"{prefix} {Colors.DIM}Testing:{Colors.RESET} {name}")

        # Phase 1: Stream extraction
        ok, title, stream_url = test_stream_extraction(name, url)
        if not ok:
            print(f"{prefix} {Colors.RED}[FAIL]{Colors.RESET} Extract: {title}")
            results["fail_extract"] += 1
            print()
            continue

        print(f"{prefix} {Colors.GREEN}[ok]{Colors.RESET}   Extract: {title[:50]}")

        # Phase 2: Playback test (3 seconds)
        played = test_playback(stream_url, seconds=3)
        if played:
            print(f"{prefix} {Colors.GREEN}[ok]{Colors.RESET}   Playback: 3s OK")
            results["pass"] += 1
        else:
            print(f"{prefix} {Colors.RED}[FAIL]{Colors.RESET} Playback: mpv failed")
            results["fail_play"] += 1

        print()

    # Summary
    total = len(TEST_URLS)
    passed = results["pass"]
    failed_e = results["fail_extract"]
    failed_p = results["fail_play"]
    failed = failed_e + failed_p

    print(f"  {Colors.DIM}{'=' * 60}{Colors.RESET}")
    print()
    print(f"  {Colors.BOLD}Results:{Colors.RESET}")
    print(f"    {Colors.GREEN}Passed:          {passed}/{total}{Colors.RESET}")
    if failed_e:
        print(f"    {Colors.RED}Failed extract:  {failed_e}/{total}{Colors.RESET}")
    if failed_p:
        print(f"    {Colors.RED}Failed playback: {failed_p}/{total}{Colors.RESET}")

    pct = (passed / total) * 100
    color = Colors.GREEN if pct >= 80 else Colors.YELLOW if pct >= 50 else Colors.RED
    print()
    print(f"  {color}{Colors.BOLD}Success rate: {pct:.0f}%{Colors.RESET}")
    print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
