"""The YouTube client pinned for stream resolution.

yt-dlp's default client hands out stream URLs that mpv cannot open (see the
comment on _DEFAULT_YTDLP_CLIENTS in player.py), so tunetape pins the client
explicitly. These tests keep that flag wired up and overridable.
"""

from tunetape.player import _DEFAULT_YTDLP_CLIENTS, _ytdlp_client_args


def test_default_clients_prefer_web_embedded():
    # web_embedded must come first: it is the client whose URLs mpv can stream.
    assert _DEFAULT_YTDLP_CLIENTS.split(",")[0] == "web_embedded"


def test_client_args_shape(monkeypatch):
    monkeypatch.delenv("TUNETAPE_YTDLP_CLIENT", raising=False)
    args = _ytdlp_client_args()

    assert args == ["--extractor-args", f"youtube:player_client={_DEFAULT_YTDLP_CLIENTS}"]


def test_env_override(monkeypatch):
    monkeypatch.setenv("TUNETAPE_YTDLP_CLIENT", "tv_embedded")

    assert _ytdlp_client_args() == ["--extractor-args", "youtube:player_client=tv_embedded"]


def test_blank_env_falls_back_to_default(monkeypatch):
    # An empty or whitespace-only override must not produce an empty client list,
    # which yt-dlp would reject.
    monkeypatch.setenv("TUNETAPE_YTDLP_CLIENT", "   ")

    assert _ytdlp_client_args() == [
        "--extractor-args", f"youtube:player_client={_DEFAULT_YTDLP_CLIENTS}"
    ]
