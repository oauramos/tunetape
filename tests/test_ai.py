import pytest

from tunetape import ai, config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point config at a temp dir and clear the env key so tests don't leak."""
    monkeypatch.setattr(config.paths, "data_dir", lambda: str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


def test_api_key_prefers_saved_then_env(monkeypatch):
    assert ai.api_key() == ""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert ai.api_key() == "env-key"
    config.set_setting("ai_api_key", "saved-key")
    # A saved key wins over the environment fallback.
    assert ai.api_key() == "saved-key"


def test_is_configured_tracks_key():
    assert ai.is_configured() is False
    config.set_setting("ai_api_key", "k")
    assert ai.is_configured() is True


def test_model_falls_back_to_default():
    assert ai.model() == ai._DEFAULT_MODEL
    config.set_setting("ai_model", "claude-custom")
    assert ai.model() == "claude-custom"


def test_extract_json_array_tolerates_fences_and_prose():
    text = 'Sure! Here you go:\n```json\n[{"artist": "A", "title": "B"}]\n```'
    assert ai._extract_json_array(text) == [{"artist": "A", "title": "B"}]


def test_extract_json_array_rejects_garbage():
    with pytest.raises(RuntimeError):
        ai._extract_json_array("no json here")
    with pytest.raises(RuntimeError):
        ai._extract_json_array('{"not": "an array"}')


def test_tracks_from_songs_builds_ytsearch_hints():
    songs = [
        {"artist": "Nujabes", "title": "Aruarian Dance"},
        {"artist": "", "title": "Solo Title"},   # missing artist is fine
        {"artist": "X", "title": ""},             # no title -> dropped
        "not a dict",                              # ignored
    ]
    tracks = ai._tracks_from_songs(songs)
    assert [t.name for t in tracks] == ["Nujabes — Aruarian Dance", "Solo Title"]
    assert tracks[0].resolve_hint == "ytsearch1:Nujabes Aruarian Dance"
    assert tracks[1].resolve_hint == "ytsearch1:Solo Title"


def test_suggest_tracks_requires_key():
    with pytest.raises(RuntimeError, match="No API key"):
        ai.suggest_tracks("some jazz")


def test_suggest_tracks_requires_request():
    config.set_setting("ai_api_key", "k")
    with pytest.raises(RuntimeError, match="Describe"):
        ai.suggest_tracks("   ")


def test_suggest_tracks_end_to_end(monkeypatch):
    config.set_setting("ai_api_key", "k")
    monkeypatch.setattr(
        ai, "_call_anthropic",
        lambda request, key: '[{"artist": "Miles Davis", "title": "So What"}]',
    )
    tracks = ai.suggest_tracks("cool jazz")
    assert len(tracks) == 1
    assert tracks[0].name == "Miles Davis — So What"
    assert tracks[0].resolve_hint == "ytsearch1:Miles Davis So What"


def test_suggest_tracks_empty_reply_raises(monkeypatch):
    config.set_setting("ai_api_key", "k")
    monkeypatch.setattr(ai, "_call_anthropic", lambda request, key: "[]")
    with pytest.raises(RuntimeError, match="didn't suggest"):
        ai.suggest_tracks("cool jazz")


def test_base_url_default_and_override():
    assert ai.base_url() == ai._DEFAULT_BASE_URL
    config.set_setting("ai_base_url", "https://proxy.example.com/")
    # Trailing slash is stripped so _messages_url doesn't double up.
    assert ai.base_url() == "https://proxy.example.com"
    assert ai._messages_url() == "https://proxy.example.com/v1/messages"


def test_auth_mode_normalizes():
    assert ai.auth_mode() == "x-api-key"          # default
    config.set_setting("ai_auth_mode", "BEARER")  # case-insensitive
    assert ai.auth_mode() == "bearer"
    config.set_setting("ai_auth_mode", "nonsense")
    assert ai.auth_mode() == "x-api-key"           # anything else -> default


def test_auth_headers_x_api_key():
    headers = ai._auth_headers("secret")
    assert headers["x-api-key"] == "secret"
    assert "authorization" not in headers
    assert headers["anthropic-version"] == ai._API_VERSION


def test_auth_headers_bearer():
    config.set_setting("ai_auth_mode", "bearer")
    headers = ai._auth_headers("secret")
    assert headers["authorization"] == "Bearer secret"
    assert "x-api-key" not in headers
    assert headers["anthropic-version"] == ai._API_VERSION


def test_check_connection_requires_key():
    with pytest.raises(RuntimeError, match="No API key"):
        ai.check_connection()


def test_check_connection_returns_model_on_success(monkeypatch):
    config.set_setting("ai_api_key", "k")
    config.set_setting("ai_model", "claude-test")
    calls = {}

    def fake_post(key, messages, *, system, max_tokens):
        calls["key"] = key
        calls["max_tokens"] = max_tokens
        return "OK"

    monkeypatch.setattr(ai, "_post_messages", fake_post)
    assert ai.check_connection() == "claude-test"
    # The ping must stay tiny.
    assert calls["key"] == "k"
    assert calls["max_tokens"] <= 16


def test_check_connection_propagates_errors(monkeypatch):
    config.set_setting("ai_api_key", "k")

    def boom(*a, **k):
        raise ConnectionError("Network error reaching the AI.")

    monkeypatch.setattr(ai, "_post_messages", boom)
    with pytest.raises(ConnectionError):
        ai.check_connection()
