import pytest

from tunetape.khinsider import Album, Track
from tunetape.playlist import Playlist


def _album(n=3):
    return Album(title="A", tracks=[Track(f"t{i}", f"p{i}") for i in range(n)])


def test_start_index_clamps_high():
    pl = Playlist(_album(3), start_index=99)
    assert pl.current_index == 2
    pl.close()


def test_start_index_clamps_low():
    pl = Playlist(_album(3), start_index=-5)
    assert pl.current_index == 0
    pl.close()


def test_start_index_in_range():
    pl = Playlist(_album(3), start_index=1)
    assert pl.current_index == 1
    assert pl.track_label == "2/3"
    pl.close()


def test_next_prev_bounds():
    pl = Playlist(_album(2))
    assert pl.has_prev() is False
    assert pl.has_next() is True
    pl.next()
    assert pl.current_index == 1
    assert pl.has_next() is False
    with pytest.raises(IndexError):
        pl.next()
    pl.prev()
    assert pl.current_index == 0
    with pytest.raises(IndexError):
        pl.prev()
    pl.close()


def test_total_tracks():
    pl = Playlist(_album(5))
    assert pl.total_tracks == 5
    pl.close()
