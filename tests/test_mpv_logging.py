"""mpv's output reaches the in-session debug log.

mpv reports a failed stream load (an HTTP 403 on the media URL, say) only in
its own output. That used to be silenced entirely, so such a failure surfaced
as a player frozen at 0:00 with nothing to diagnose it by.
"""

import os

from tunetape import debug
from tunetape.player import MPVController


def _controller(tmp_path):
    """An MPVController with its paths set up but no mpv process spawned."""
    c = MPVController.__new__(MPVController)
    c._log_dir = str(tmp_path)
    c._log_path = os.path.join(str(tmp_path), "mpv.stderr")
    c._log_file = None
    return c


def test_stderr_lines_land_in_debug_log(tmp_path):
    debug.clear()
    c = _controller(tmp_path)
    with open(c._log_path, "w", encoding="utf-8") as f:
        f.write("[ffmpeg] https: HTTP error 403 Forbidden\n"
                "Exiting... (Errors when loading file)\n")

    c._log_mpv_output("mpv")

    messages = [m for _, _, m in debug.entries()]
    assert any("403 Forbidden" in m for m in messages)
    assert all(m.startswith("mpv: ") for m in messages)
    assert all(level == "ERROR" for _, level, _ in debug.entries())


def test_only_the_tail_is_kept(tmp_path):
    # mpv repeats status lines; the load error is last, so the tail is what matters.
    debug.clear()
    c = _controller(tmp_path)
    with open(c._log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"line {i}" for i in range(50)) + "\n")

    c._log_mpv_output("mpv")

    entries = debug.entries()
    assert len(entries) == 10
    assert entries[-1][2] == "mpv: line 49"


def test_empty_and_missing_logs_are_silent(tmp_path):
    debug.clear()
    c = _controller(tmp_path)

    # No file at all.
    c._log_mpv_output("mpv")
    assert debug.entries() == []

    # Present but empty (mpv exited cleanly).
    open(c._log_path, "w").close()
    c._log_mpv_output("mpv")
    assert debug.entries() == []


def test_blank_lines_are_skipped(tmp_path):
    debug.clear()
    c = _controller(tmp_path)
    with open(c._log_path, "w", encoding="utf-8") as f:
        f.write("\n\nreal error\n\n")

    c._log_mpv_output("mpv")

    assert [m for _, _, m in debug.entries()] == ["mpv: real error"]


def test_long_lines_are_truncated(tmp_path):
    # mpv echoes the full stream URL (~1.5 KB of query string) in load errors.
    debug.clear()
    c = _controller(tmp_path)
    with open(c._log_path, "w", encoding="utf-8") as f:
        f.write("Failed to open https://example.com/videoplayback?" + "x" * 3000 + "\n")

    c._log_mpv_output("mpv")

    (msg,) = [m for _, _, m in debug.entries()]
    assert msg.endswith("…")
    assert len(msg) < 300
