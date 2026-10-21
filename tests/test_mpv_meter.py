"""The player's level meter and stream panel read mpv properties over IPC."""

from tunetape.player import MPVController


def _controller(properties: dict):
    """An MPVController answering get_property from ``properties``, no mpv spawned."""
    c = MPVController.__new__(MPVController)

    def send(command):
        name = command[1]
        if name in properties:
            return {"error": "success", "data": properties[name]}
        return {"error": "property unavailable"}

    c._send = send
    return c


def test_levels_per_channel():
    c = _controller({"af-metadata/meter": {
        "lavfi.astats.1.RMS_level": "-12.5", "lavfi.astats.1.Peak_level": "-3.0",
        "lavfi.astats.2.RMS_level": "-inf", "lavfi.astats.2.Peak_level": "-inf",
        "lavfi.astats.Overall.RMS_level": "-12.5",
    }})
    assert c.get_levels() == [(-12.5, -3.0), (float("-inf"), float("-inf"))]


def test_no_meter_means_no_levels():
    # A build where the filter failed and playback retried without it.
    assert _controller({}).get_levels() is None


def test_stats_collects_what_mpv_knows():
    c = _controller({
        "audio-codec-name": "opus",
        "audio-bitrate": 124336,
        "audio-params": {"samplerate": 48000, "channels": "stereo", "hr-channels": "stereo"},
        "file-format": "mkv",
        "demuxer-cache-duration": 207.7,
        "demuxer-cache-state": {"total-bytes": 9232848},
        "cache-speed": 0,
        "af": [{"name": "dynaudnorm"}, {"name": "lavfi", "label": "meter"}],
    })
    assert c.get_stats() == {
        "codec": "opus", "bitrate": 124336, "samplerate": 48000, "channels": "stereo",
        "container": "mkv", "ahead": 207.7, "cached": 9232848, "speed": 0,
        "normalized": True,
    }


def test_stats_before_anything_loaded():
    stats = _controller({}).get_stats()
    assert stats["codec"] is None and stats["normalized"] is False
