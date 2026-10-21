from datetime import datetime, timedelta, timezone

from tunetape.player_screen import VOLUME_BAR, format_time, volume_gauge
from tunetape.ui import _humanize_time


def test_format_time():
    assert format_time(0) == "00:00"
    assert format_time(5) == "00:05"
    assert format_time(65) == "01:05"
    assert format_time(-3) == "00:00"
    assert format_time(3599) == "59:59"
    assert format_time(3600) == "60:00"


def test_volume_gauge():
    assert volume_gauge(80).plain.rstrip().endswith("80%")
    assert volume_gauge(130).plain.rstrip().endswith("130%")  # mpv goes past 100
    assert "muted" in volume_gauge(80, True).plain
    assert "--" in volume_gauge().plain  # before the first reading
    # Same width whatever it shows, so the caps don't reflow as it changes.
    widths = {volume_gauge(v, m).cell_length for v in (None, 0, 5, 100, 130) for m in (0, 1)}
    assert len(widths) == 1
    assert volume_gauge(50).plain.count("━") == VOLUME_BAR


def test_humanize_time():
    assert _humanize_time(None) == "—"
    assert _humanize_time("not-a-timestamp") == "—"
    now = datetime.now(timezone.utc)
    assert _humanize_time(now.isoformat()) == "just now"
    assert _humanize_time((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert _humanize_time((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert _humanize_time((now - timedelta(days=2)).isoformat()) == "2d ago"


def test_stream_lines():
    from tunetape.player_screen import stream_lines

    text = stream_lines({
        "codec": "opus", "bitrate": 124336, "samplerate": 48000, "channels": "stereo",
        "container": "mkv", "ahead": 207.7, "cached": 9232848, "speed": 1536,
        "normalized": True,
    }).plain
    assert "codec opus · 124 kb/s" in text
    assert "audio 48 kHz · stereo" in text
    assert "cache 03:27 ahead · 8.8 MB" in text
    assert "net ↓ 1.5 KB/s" in text and "norm  on" in text
    assert stream_lines({}).plain.count("—") == 5  # nothing known yet
