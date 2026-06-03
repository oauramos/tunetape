from datetime import datetime, timedelta, timezone

from tunetape.ui import _format_time, _build_progress_bar, _humanize_time

BAR_CHAR = "━"


def test_format_time():
    assert _format_time(0) == "00:00"
    assert _format_time(5) == "00:05"
    assert _format_time(65) == "01:05"
    assert _format_time(-3) == "00:00"
    assert _format_time(3599) == "59:59"
    assert _format_time(3600) == "60:00"


def test_progress_bar_zero_duration_does_not_crash():
    bar = _build_progress_bar(0, 0)
    assert isinstance(bar, str)
    assert bar.count(BAR_CHAR) == 40  # default width


def test_progress_bar_empty_and_full():
    empty = _build_progress_bar(0, 100)
    full = _build_progress_bar(100, 100)
    over = _build_progress_bar(500, 100)  # fraction clamps to 1.0
    assert empty.startswith("[cyan][/cyan]")  # nothing filled
    assert full.endswith("[dim][/dim]")  # nothing unfilled
    assert full.count(BAR_CHAR) == 40
    assert over.count(BAR_CHAR) == 40


def test_humanize_time():
    assert _humanize_time(None) == "—"
    assert _humanize_time("not-a-timestamp") == "—"
    now = datetime.now(timezone.utc)
    assert _humanize_time(now.isoformat()) == "just now"
    assert _humanize_time((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert _humanize_time((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert _humanize_time((now - timedelta(days=2)).isoformat()) == "2d ago"
