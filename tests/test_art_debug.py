from tunetape import art, debug, ui


def test_tuna_grids_are_rectangular():
    for pixels in (art.TUNA_LARGE, art.TUNA_SMALL):
        assert len(pixels) % 2 == 0  # two pixel rows per cell row
        assert len({len(row) for row in pixels}) == 1
        assert set("".join(pixels)) <= {".", "o", "b", "f"}


def test_tuna_art_sizes_match_their_render():
    for name, drawing in art.TUNA_ART.items():
        lines = drawing.render(frame=3, shine=10).plain.split("\n")
        assert len(lines) == drawing.height, name
        assert {len(line) for line in lines} == {drawing.width}, name


def test_reels_turn_between_frames():
    tuna = art.TUNA_ART["small"]
    assert tuna.render(0).plain != tuna.render(1).plain


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
