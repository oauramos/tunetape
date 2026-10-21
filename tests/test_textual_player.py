"""Headless tests for the Textual player screen (needs the [tui] extra)."""

import asyncio

import pytest

pytest.importorskip("textual")

import tunetape.__main__ as main  # noqa: E402
from tunetape.textual_player import (  # noqa: E402
    DEMO_ALBUM,
    DEMO_TRACKS,
    FakeController,
    TextualPlayer,
    playlist_info_for,
)


def _drive(keys=(), index=11, size=(108, 30)):
    """Press ``keys`` on a frozen fake album. Returns (result, controller, state)."""
    controller = FakeController(187, 41, frozen=True)
    app = TextualPlayer(
        DEMO_TRACKS[index], controller, playlist_info_for(index, len(DEMO_TRACKS)),
        source="KHInsider", subtitle=DEMO_ALBUM, tracks=DEMO_TRACKS,
    )

    async def run():
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.3)
            for key in keys:
                await pilot.press(key)
            await pilot.pause()
            return {
                "screen": type(app.screen).__name__,
                "focused": type(app.focused).__name__,
                "counter": str(app._w_counter.value),
                "tuna": [t.id for t in app.query("Tuna") if t.display and t.parent.display],
            }

    state = asyncio.run(run())
    return app.return_value, controller, state


def test_counter_shows_position():
    _, _, state = _drive()
    assert state["counter"] == "00:41"


def test_space_toggles_pause():
    _, controller, _ = _drive(["space"])
    assert controller.is_paused()


def test_seek_keys():
    _, controller, _ = _drive(["right", "right", "left", "full_stop", "comma", "comma"])
    assert controller.get_position() == 41 + 10 + 10 - 10 + 30 - 30 - 30


@pytest.mark.parametrize("keys, volume", [
    (["up", "plus"], 90),
    (["down", "minus"], 70),
])
def test_volume_keys(keys, volume):
    _, controller, _ = _drive(keys)
    assert controller.get_volume() == volume


def test_mute():
    _, controller, _ = _drive(["m"])
    assert controller.is_muted()


@pytest.mark.parametrize("key, result", [
    ("n", "next_track"),
    ("p", "prev_track"),
    ("b", "menu"),
    ("escape", "menu"),
    ("q", "quit"),
    ("ctrl+c", "quit"),
])
def test_exit_keys(key, result):
    assert _drive([key])[0] == result


def test_prev_disabled_on_first_track():
    assert _drive(["p"], index=0)[0] is None


def test_next_disabled_on_last_track():
    assert _drive(["n"], index=len(DEMO_TRACKS) - 1)[0] is None


def test_help_opens_and_closes():
    assert _drive(["h"])[2]["screen"] == "HelpScreen"
    result, _, state = _drive(["h", "escape"])
    assert state["screen"] == "Screen" and result is None


def test_queue_takes_arrows_once_focused():
    result, controller, state = _drive(["tab", "down"])
    assert state["focused"] == "OptionList"
    assert controller.get_volume() == 80 and result is None


def test_queue_enter_jumps():
    assert _drive(["tab", "down", "down", "enter"])[0] == "jump_track:13"


def test_escape_leaves_queue_before_menu():
    result, _, state = _drive(["tab", "escape"])
    assert state["focused"] == "NoneType" and result is None


@pytest.mark.parametrize("keys, check", [
    (["m"], lambda r, c: c.is_muted()),
    (["p"], lambda r, c: r == "prev_track"),
    (["b"], lambda r, c: r == "menu"),
])
def test_compact_width_keeps_footerless_keys(keys, check):
    # Below 70 columns Mute / Prev / Back drop out of the footer but still work.
    result, controller, _ = _drive(keys, size=(60, 30))
    assert check(result, controller)


@pytest.mark.parametrize("size, tuna", [
    ((108, 30), ["tuna-large"]),
    ((80, 30), ["tuna-large"]),
    ((60, 30), ["tuna-small"]),
    ((108, 24), ["tuna-small"]),
    ((60, 20), []),
])
def test_tuna_size_follows_terminal(size, tuna):
    assert _drive(size=size)[2]["tuna"] == tuna


@pytest.mark.parametrize("info, result", [
    (None, "menu"),
    ({"index": 0, "track_label": "1/2", "has_next": True, "has_prev": False}, "next_track"),
])
def test_track_end_moves_on(info, result):
    app = TextualPlayer("x", FakeController(1.0, 0.9), info)

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause(1.0)

    asyncio.run(run())
    assert app.return_value == result


def test_main_picks_textual_player_when_installed(monkeypatch):
    assert isinstance(main._player("t", FakeController(10)), TextualPlayer)
    monkeypatch.setattr(main, "TextualPlayer", None)
    assert isinstance(main._player("t", FakeController(10)), main.PlayerUI)
