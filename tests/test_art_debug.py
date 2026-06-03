from tunetape import art, debug, ui


def test_cassette_is_static_multiline():
    base = art.render_cassette()
    assert "\n" in base and base.strip()  # non-empty, multi-line art
    # The frame/spinning args are accepted for compatibility but ignored.
    assert art.render_cassette(0) == base
    assert art.render_cassette(5, spinning=True) == base
    assert art.render_cassette(spinning=False) == base


def test_debug_ring_buffer():
    debug.clear()
    assert debug.entries() == []
    debug.log("first")
    debug.log("bad thing", "error")
    records = debug.entries()
    assert len(records) == 2
    assert records[0][1] == "INFO"
    assert records[1][1] == "ERROR" and records[1][2] == "bad thing"
    debug.clear()
    assert debug.entries() == []


def test_debug_exception_records_error():
    debug.clear()
    try:
        raise ValueError("nope")
    except ValueError as exc:
        debug.exception("while testing", exc)
    rec = debug.entries()[-1]
    assert rec[1] == "ERROR"
    assert "ValueError" in rec[2] and "nope" in rec[2]
    debug.clear()


def test_with_spinner_returns_value_and_logs():
    debug.clear()
    result = ui.with_spinner("doing thing", lambda x, y: x + y, 2, y=40)
    assert result == 42
    assert any("doing thing" in msg for _, _, msg in debug.entries())
    debug.clear()


def test_with_spinner_propagates_exceptions():
    import pytest

    def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        ui.with_spinner("risky", boom)
