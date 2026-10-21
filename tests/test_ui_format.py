from datetime import datetime, timedelta, timezone

from tunetape.player_screen import format_time, volume_meter
from tunetape.ui import _humanize_time


def test_format_time():
    assert format_time(0) == "00:00"
    assert format_time(5) == "00:05"
    assert format_time(65) == "01:05"
    assert format_time(-3) == "00:00"
    assert format_time(3599) == "59:59"
    assert format_time(3600) == "60:00"


def test_volume_meter():
    assert volume_meter(0, False).plain.endswith(" 0%")
    assert volume_meter(130, False).plain.endswith("130%")  # mpv goes past 100
    assert "muted" in volume_meter(80, True).plain


def test_humanize_time():
    assert _humanize_time(None) == "—"
    assert _humanize_time("not-a-timestamp") == "—"
    now = datetime.now(timezone.utc)
    assert _humanize_time(now.isoformat()) == "just now"
    assert _humanize_time((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert _humanize_time((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert _humanize_time((now - timedelta(days=2)).isoformat()) == "2d ago"
