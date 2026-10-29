"""Headless tests for the Textual interface: the flow thread, every screen, the player.

The player and the session talk to the daemon through ``tunetape.demo.FakeClient``;
the last test runs the real daemon (with the fake mpv) under the real session.
"""

import asyncio
import time
from datetime import datetime, timezone

import pytest
from textual import events
from textual.widgets import Input

import tunetape.__main__ as main
from conftest import needs_fake_mpv, pid_alive
from tunetape import client as client_mod, config, debug, mcp_setup, ui
from tunetape.client import DaemonError, DaemonUnavailable
from tunetape.demo import DEMO_ALBUM, DEMO_TRACKS, FakeClient, album_client
from tunetape.models import Track
from tunetape.widgets import (
    KeyCaps, LevelMeter, Tuna, balanced_rows, graph_rows, marquee_window,
)

NOTHING = object()


def _no_daemon(spawn):
    raise AssertionError("this test must not reach a real daemon")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep config.json (theme, volume) and any daemon files out of the real data
    dir, start every test with fresh remembered highlights, and make sure no
    test but the end-to-end one can reach (or start) a real daemon."""
    monkeypatch.setenv("TUNETAPE_DATA_DIR", str(tmp_path))
    # The MCP screen reads Claude Code's config, may run claude and copies to
    # the clipboard: none of it real here.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(mcp_setup, "claude_cli", lambda: None)
    monkeypatch.setattr(mcp_setup, "copy", lambda text: True)
    monkeypatch.setattr(ui, "_settings_at", {})
    monkeypatch.setattr(ui, "_history_at", 0)
    monkeypatch.setattr(main, "_link", main._DaemonLink(_no_daemon))
    return tmp_path


async def until(pilot, check, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting for the app")


def on(app, name):
    """True once screen ``name`` is up and composed."""
    return lambda: (type(app.screen).__name__ == name and app.screen.is_mounted
                    and bool(app.screen.children))


def drive(flow, script, size=(108, 30)):
    """Run ``flow`` as the session while ``script(pilot, app)`` plays the user.

    Returns (what the flow returned, the app). The flow gets a moment to finish
    after the script; NOTHING means it was still waiting on a screen.
    """
    out = {}

    def session():
        out["result"] = flow()

    app = ui.TunetapeApp(session)
    ui._app = app

    async def go():
        async with app.run_test(size=size) as pilot:
            await script(pilot, app)
            for _ in range(40):
                if "result" in out:
                    break
                await pilot.pause(0.05)

    try:
        asyncio.run(go())
    finally:
        ui._app = None
    return out.get("result", NOTHING), app


def keys(*names, wait_for=None, settle=0.0):
    """A script that waits for a screen, then presses ``names``.

    ``settle`` waits that long after the last key, so controls the player
    hands to its poll thread reach the (fake) daemon before the app closes.
    """

    async def script(pilot, app):
        if wait_for:
            await until(pilot, on(app, wait_for))
        await pilot.pause(0.1)
        for name in names:
            await pilot.press(name)
            await pilot.pause(0.05)
        if settle:
            await pilot.pause(settle)

    return script


def text(app, selector):
    return str(app.screen.query_one(selector).render())


# --- key caps ----------------------------------------------------------------

def test_balanced_rows_fit_and_even_out():
    widths = [14, 12, 10, 8, 8, 8, 8, 8, 8]
    rows = balanced_rows(widths, 54)
    assert len(rows) == 2
    assert [i for row in rows for i in row] == list(range(9))
    used = [sum(widths[i] for i in row) + len(row) - 1 for row in rows]
    assert max(used) <= 54 and abs(used[0] - used[1]) <= 10


def test_balanced_rows_one_row_when_it_fits():
    assert balanced_rows([10, 10], 80) == [[0, 1]]
    assert balanced_rows([], 80) == []
    assert balanced_rows([30], 10) == [[0]]  # too wide: alone, not lost


def test_graph_rows_fill_braille_from_the_bottom():
    # Four samples, two per cell; full scale is two rows of four dots.
    assert graph_rows([0.0, 0.25, 0.5, 1.0], 2, 2, maximum=1.0) == [" ⢸", "⢠⣿"]
    assert graph_rows([1.0], 3, 1, maximum=1.0) == ["  ⢸"]  # newest on the right
    assert graph_rows([5.0, 10.0], 1, 1) == ["⣼"]  # scaled to the largest shown
    assert graph_rows([], 0, 3) == []


def test_marquee_window_loops_long_text():
    text = "Rick Astley - Never Gonna Give You Up"
    assert marquee_window("fits", 10, 7) == "fits"  # short text never moves
    assert marquee_window(text, 10, 0) == "Rick Astle"
    assert marquee_window(text, 10, 5) == "Astley - N"
    assert marquee_window(text, 10, len(text)) == "   ·   Ric"  # the gap, then around again
    assert marquee_window(text, 10, len(text) + 7) == marquee_window(text, 10, 0)
    # Two-cell characters never get cut in half at the edge.
    assert marquee_window("日本語のタイトル", 5, 0) == "日本 "


def test_level_meter_holds_peaks():
    meter = LevelMeter()
    meter.update_levels([(-10.0, -3.0), (-12.0, -6.0)])
    meter.update_levels([(-30.0, -28.0), (-30.0, -28.0)])
    assert meter._held == [-4.5, -7.5]  # falling back from the peak, not dropped
    meter.update_levels(None)  # paused / loading: silence on both channels
    assert [rms for rms, _ in meter._levels] == [float("-inf")] * 2




# --- the player --------------------------------------------------------------

def player(client):
    """The flow that shows ``client``'s queue on the player."""
    return lambda: ui.play(client)


def play_keys(names, index=11, size=(108, 30), client=None):
    client = client or album_client(index=index, frozen=True)
    result, app = drive(player(client), keys(*names, wait_for="PlayerScreen", settle=0.3), size)
    return result, client, app


def test_counter_shows_position():
    def counter(app):
        try:
            return text(app, "#elapsed")
        except Exception:
            return None

    async def script(pilot, app):
        await until(pilot, lambda: counter(app) == "00:41")

    drive(player(album_client(frozen=True)), script)


def test_space_toggles_pause():
    client = play_keys(["space"])[1]
    assert client.paused and client.called("toggle") == [()]


def test_seek_keys():
    _, client, _ = play_keys(["right", "right", "left", "full_stop", "comma", "comma"])
    assert client.position == 41 + 10 + 10 - 10 + 30 - 30 - 30
    assert [args for args in client.called("seek")] == [
        (10, True), (10, True), (-10, True), (30, True), (-30, True), (-30, True)]


@pytest.mark.parametrize("names, volume", [(["up", "plus"], 90), (["down", "minus"], 70)])
def test_volume_keys(names, volume):
    client = play_keys(names)[1]
    assert client.volume_level == volume
    assert all(value is None and abs(delta) == 5 for value, delta in client.called("volume"))


def test_mute():
    assert play_keys(["m"])[1].muted


@pytest.mark.parametrize("key, result", [
    ("b", "menu"),
    ("escape", "menu"),
    ("q", "quit"),
    ("ctrl+c", "quit"),
])
def test_player_exit_keys(key, result):
    flow_result, client, _ = play_keys([key])
    assert flow_result == result
    # Leaving the player never stops the music; quitting is __main__'s to do.
    assert client.state == "playing" and client.called("stop") == []


@pytest.mark.parametrize("key, method, index", [("n", "next", 12), ("p", "prev", 10)])
def test_next_and_prev_move_the_daemon_and_stay_on_the_player(key, method, index):
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        screen = app.screen
        await pilot.press(key)
        await until(pilot, lambda: text(app, "#title") == DEMO_TRACKS[index])
        seen["same screen"] = app.screen is screen
        seen["subtitle"] = text(app, "#subtitle")

    client = album_client(frozen=True)
    result, _ = drive(player(client), script)
    assert result is NOTHING and client.called(method) == [()] and client.index == index
    assert seen == {"same screen": True,
                    "subtitle": f"{DEMO_ALBUM} · track {index + 1}/{len(DEMO_TRACKS)}"}


def test_prev_disabled_on_first_track():
    result, client, _ = play_keys(["p"], index=0)
    assert result is NOTHING and client.called("prev") == []


def test_next_disabled_on_last_track():
    result, client, _ = play_keys(["n"], index=len(DEMO_TRACKS) - 1)
    assert result is NOTHING and client.called("next") == []


def test_a_single_track_has_no_next_prev_or_queue():
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.2)
        seen["caps"] = text(app, "#player-caps")
        seen["queue"] = app.screen.query_one("#queue").display
        await pilot.press("n", "p")
        await pilot.pause(0.3)

    client = FakeClient(["Just One"], frozen=True)
    drive(player(client), script)
    assert " next " not in seen["caps"] and " prev " not in seen["caps"]
    assert seen["queue"] is False
    assert client.called("next") == client.called("prev") == []


def test_player_help_opens_and_closes():
    seen = []

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("h")
        await until(pilot, on(app, "HelpModal"))
        seen.append(type(app.screen).__name__)
        await pilot.press("escape")
        await until(pilot, on(app, "PlayerScreen"))
        seen.append(type(app.screen).__name__)

    assert drive(player(album_client(frozen=True)), script)[0] is NOTHING
    assert seen == ["HelpModal", "PlayerScreen"]


def test_q_on_player_help_quits():
    assert play_keys(["h", "q"])[0] == "quit"


def test_queue_takes_arrows_once_focused():
    result, client, _ = play_keys(["tab", "down"])
    assert client.volume_level == 80 and result is NOTHING


def test_queue_enter_jumps():
    result, client, _ = play_keys(["tab", "down", "down", "enter"])
    assert result is NOTHING
    assert client.called("jump") == [(13,)] and client.index == 13


def test_escape_leaves_queue_before_menu():
    assert play_keys(["tab", "escape"])[0] is NOTHING
    assert play_keys(["tab", "escape", "escape"])[0] == "menu"


def test_a_key_pressed_on_the_way_out_still_counts():
    result, client, _ = play_keys(["up", "b"])
    assert result == "menu" and client.volume_level == 85


@pytest.mark.parametrize("names, check", [
    (["m"], lambda r, c: c.muted),
    (["p"], lambda r, c: c.index == 10),
    (["b"], lambda r, c: r == "menu"),
])
@pytest.mark.parametrize("size", [(60, 30), (60, 24), (60, 20)])
def test_keys_work_at_60_columns(names, check, size):
    result, client, _ = play_keys(names, size=size)
    assert check(result, client)


@pytest.mark.parametrize("size, queue", [
    ((108, 30), True),
    ((80, 24), False),
    ((60, 30), False),
    ((60, 24), False),
    ((60, 20), False),
])
def test_panels_fit_the_terminal(size, queue):
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.3)
        screen = app.screen
        for name in ("now", "level", "stream", "player-caps", "meter", "stream-info"):
            region = screen.query_one(f"#{name}").region
            seen[name] = region.height > 0 and region.right <= size[0] and region.bottom <= size[1]
        seen["queue"] = screen.query_one("#queue").display

    drive(player(album_client(frozen=True)), script, size)
    assert seen.pop("queue") is queue
    assert all(seen.values()), seen


def test_level_and_stream_panels_follow_the_daemon():
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(1.3)  # stats ride along once a second
        screen = app.screen
        seen["meter"] = text(app, "#meter")
        seen["stream"] = text(app, "#stream-info")
        seen["samples"] = len(screen.query_one("#level-graph")._samples)

    client = album_client()
    drive(player(client), script)
    assert seen["meter"].startswith("L ") and "\nR " in seen["meter"]
    assert "opus · 131 kb/s" in seen["stream"] and "48 kHz · stereo" in seen["stream"]
    assert seen["samples"] >= 5
    # One player.status per poll, and the queue only when the track changes.
    assert len(client.called("status")) >= 10 and len(client.called("queue")) == 1


def test_loading_shows_on_the_status_line():
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        # Wait for the line rather than a fixed pause: a slow CI runner may
        # take longer than the pause just to paint the screen.
        await until(pilot, lambda: "loading" in text(app, "#status"))
        seen["loading"] = text(app, "#status")
        await until(pilot, lambda: "playing" in text(app, "#status"))

    drive(player(album_client(load_seconds=3.0)), script)
    assert seen["loading"] == "… loading track 12/24"


@pytest.mark.parametrize("size, labels", [((96, 30), True), ((95, 30), False), ((60, 24), False)])
def test_player_caps_drop_labels_below_96_columns(size, labels):
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.2)
        seen["text"] = text(app, "#player-caps")

    drive(player(album_client(frozen=True)), script, size)
    assert " space " in seen["text"] and " q " in seen["text"]
    assert ("play/pause" in seen["text"]) is labels
    assert "vol" in seen["text"]  # the volume gauge stays as it is


def test_long_titles_scroll_like_a_car_radio():
    long_title = "A Very Long Song Title That Will Never Fit In Sixty Columns (Remastered)"
    seen = []

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.5)
        seen.append(text(app, "#title"))
        await pilot.pause(3.0)  # past the two-second hold
        seen.append(text(app, "#title"))

    drive(player(FakeClient([long_title], position=10, frozen=True)), script, (60, 24))
    start, later = seen
    assert long_title.startswith(start) and len(start) < len(long_title)
    assert later != start and later.strip() in long_title and len(later) == len(start)


def test_mouse_wheel_changes_volume_but_scrolls_the_queue():
    client = album_client(frozen=True)

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot._post_mouse_events([events.MouseScrollUp], "#level-graph")
        await pilot._post_mouse_events([events.MouseScrollUp], "#elapsed")
        await pilot._post_mouse_events([events.MouseScrollDown], "#title")
        await pilot._post_mouse_events([events.MouseScrollDown], "#queue")
        await pilot.pause(0.3)

    drive(player(client), script)
    assert client.volume_level == 80 + 5 + 5 - 5


def test_volume_gauge_sits_after_quit_and_sets_volume_on_click():
    client = album_client(frozen=True)
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.5)
        caps = app.screen.query_one(KeyCaps)
        quit_box = next(h for h in caps._hits if h[4] >= 0 and caps.caps[h[4]].label == "quit")
        tail = next(h for h in caps._hits if h[4] == -1)
        seen["after_quit"] = tail[2] == quit_box[2] and tail[0] > quit_box[1]
        seen["at_right_edge"] = tail[1] == caps.size.width
        await pilot.click("#player-caps", offset=(tail[0] + 10 + 2, tail[2]))  # 3rd of 10 cells
        await pilot.pause(0.3)

    drive(player(client), script)
    assert seen == {"after_quit": True, "at_right_edge": True}
    assert client.volume_level == 30 and client.called("volume") == [(30.0, None)]


def test_clicking_caps_and_the_bar():
    client = album_client(frozen=True)

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.4)  # a poll, so the bar knows the duration
        caps = app.screen.query_one(KeyCaps)
        for x0, x1, y0, y1, index in caps._hits:
            if caps.caps[index].label == "mute":
                await pilot.click("#player-caps", offset=(x0 + 1, y0))
        await pilot.click("#progress Bar", offset=(0, 0))  # the very start
        await pilot.pause(0.3)

    drive(player(client), script)
    assert client.muted
    assert client.called("seek") == [(0.0, False)] and client.position == 0


def test_the_daemon_moving_on_updates_the_screen_in_place():
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        screen = app.screen
        seen["first"] = text(app, "#title")
        await until(pilot, lambda: text(app, "#title") == "Two")
        seen["same screen"] = app.screen is screen
        seen["subtitle"] = text(app, "#subtitle")
        seen["highlighted"] = screen.query_one("#queue").highlighted

    # Long enough that a slow CI runner still catches the first track on
    # screen before the fake daemon moves on.
    client = FakeClient(["One", "Two", "Three"], album="Tape", durations=[2.5, 30, 30])
    result, _ = drive(player(client), script)
    assert result is NOTHING
    assert seen == {"first": "One", "same screen": True, "subtitle": "Tape · track 2/3",
                    "highlighted": 1}


def test_queue_end_returns_to_the_menu():
    async def script(pilot, app):
        await pilot.pause(1.2)

    result, _ = drive(player(FakeClient(["x"], durations=[0.4])), script)
    assert result == "menu"


def test_a_failure_that_stopped_playback_is_shown():
    seen = {}
    client = album_client(frozen=True)

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        client.state, client.message = "idle", "Could not load track: gone"
        await until(pilot, on(app, "ErrorModal"))
        seen["error"] = app.screen.message
        await pilot.press("enter")

    assert drive(player(client), script)[0] == "menu"
    assert seen["error"] == "Could not load track: gone"


def test_a_skipped_track_is_toasted(monkeypatch):
    toasts = []
    monkeypatch.setattr(ui.TunetapeApp, "notify",
                        lambda self, message, **kw: toasts.append((kw.get("title"), message)))
    client = album_client(frozen=True)

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        with client._lock:  # what fail-forward looks like: marked failed, on to the next
            client.items[11].update(status="failed", error="Could not load track: gone")
            client._begin(12)
        await until(pilot, lambda: text(app, "#title") == DEMO_TRACKS[12])
        await pilot.pause(0.1)

    drive(player(client), script)
    assert toasts == [(DEMO_TRACKS[11], "Could not load track: gone")]


def test_the_daemon_going_away_mid_play_raises():
    client = album_client(frozen=True)

    def flow():
        try:
            return ui.play(client)
        except DaemonUnavailable as e:
            return f"lost: {e}"

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        client.down = True

    result, _ = drive(flow, script)
    assert result == "lost: The connection to the tunetape daemon was lost."


def test_play_on_an_idle_daemon_returns_at_once():
    assert drive(player(FakeClient(["x"], state="idle")), keys())[0] == "menu"


# --- the session flow --------------------------------------------------------

def test_with_spinner_shows_progress_and_returns():
    def flow():
        return ui.with_spinner("Fetching…", lambda: type(ui._app.screen).__name__)

    result, _ = drive(flow, keys())
    assert result == "BusyScreen"


def test_busy_screen_lists_each_step():
    def flow():
        ui.with_spinner("Fetching playlist…", lambda: None)
        ui.with_spinner("Loading track 1/7…", lambda: None)
        return [step[0] for step in ui._app.screen.steps], ui._app.screen.steps[0][2] is not None

    result, _ = drive(flow, keys())
    assert result == (["Fetching playlist…", "Loading track 1/7…"], True)


def test_quit_while_busy_closes_the_session():
    import threading

    release = threading.Event()

    def flow():
        try:
            ui.with_spinner("Fetching…", release.wait, 5)
        except ui.SessionClosed:
            return "closed"

    async def script(pilot, app):
        await until(pilot, on(app, "BusyScreen"))
        await pilot.press("ctrl+c")
        release.set()

    assert drive(flow, script)[0] == "closed"


def test_error_and_confirm():
    def flow():
        ui.show_error("boom")
        return ui.confirm("Sure?"), ui.confirm("Really?")

    result, _ = drive(flow, keys("enter", "y", "n", wait_for="ErrorModal"))
    assert result == (True, False)


@pytest.mark.parametrize("names, result", [
    (["2"], "2"),
    (["d"], "d"),
    (["m"], "m"),
    (["q"], "q"),
    (["right", "right", "enter"], "3"),
    (["down", "enter"], "m"),            # down from the buttons into the nav row
    (["down", "right", "enter"], "d"),
    (["down", "right", "right", "enter"], "h"),
])
def test_menu(names, result):
    assert drive(ui.main_menu, keys(*names, wait_for="MenuScreen"))[0] == result


@pytest.mark.parametrize("size, ascii", [((108, 30), True), ((85, 30), True),
                                         ((108, 29), False), ((60, 24), False)])
def test_menu_shows_the_ascii_tuna_when_it_fits(size, ascii):
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.pause(0.1)
        seen["ascii"] = app.screen.query_one("#ascii-tuna").display
        seen["pixel"] = app.screen.query_one(Tuna).display
        seen["welcome"] = app.screen.query_one("#welcome").display

    drive(ui.main_menu, script, size)
    assert seen == {"ascii": ascii, "pixel": not ascii, "welcome": not ascii}


@pytest.mark.parametrize("flow, wait_for, back, quit_", [
    (lambda: ui.show_history(SCREEN_HISTORY), "HistoryScreen", ("back", None), ("quit", None)),
    (lambda: ui.show_settings(True), "OptionsScreen", "b", "q"),
    (ui.show_ai_settings, "OptionsScreen", "back", "quit"),
    (lambda: ui.show_ai_results("x", [Track("A", "a")]), "SuggestionsScreen",
     ("back", None), ("quit", None)),
    (lambda: ui.show_ai_unavailable("nope"), "NoticeScreen", "back", "quit"),
    (ui.show_debug, "DebugScreen", None, "quit"),
    (ui.show_mcp, "McpScreen", None, "quit"),
    (ui.show_theme_picker, "ThemeScreen", None, "quit"),
])
def test_navigation_keys_are_the_same_everywhere(flow, wait_for, back, quit_):
    for key in ("b", "escape"):
        assert drive(flow, keys(key, wait_for=wait_for))[0] == back, key
    assert drive(flow, keys("q", wait_for=wait_for))[0] == quit_

    seen = []

    async def help_then_back(pilot, app):
        await until(pilot, on(app, wait_for))
        await pilot.press("h")
        await until(pilot, on(app, "HelpModal"))
        seen.append("help")
        await pilot.press("b")  # closes help, not the screen behind it
        await until(pilot, on(app, wait_for))
        seen.append("back")

    assert drive(flow, help_then_back)[0] is NOTHING
    assert seen == ["help", "back"]


def test_typing_b_q_h_in_a_text_box_types_them():
    async def script(pilot, app):
        await until(pilot, on(app, "PromptScreen"))
        await pilot.press(*"bqh?", "enter")

    assert drive(ui.prompt_discover, script)[0] == "bqh?"
    assert drive(ui.prompt_discover, keys("escape", wait_for="PromptScreen"))[0] == ""
    assert drive(ui.prompt_discover, keys("ctrl+c", wait_for="PromptScreen"))[0] == "q"


def test_every_screen_ends_its_caps_with_the_same_navigation():
    from tunetape import screens
    from tunetape.player_screen import PLAYER_CAPS

    nav = [(c.key, c.label) for c in screens.NAV]
    for caps in (PLAYER_CAPS, screens.HistoryScreen.CAPS, screens.SuggestionsScreen.CAPS,
                 screens.ThemeScreen.CAPS):
        assert [(c.key, c.label) for c in caps[-3:]] == nav


def test_menu_click():
    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.pause(0.1)
        caps = app.screen.query_one("#menu-caps", KeyCaps)
        x0, _, y0, _, _ = next(h for h in caps._hits if caps.caps[h[4]].label == "Settings")
        await pilot.click("#menu-caps", offset=(x0 + 2, y0 + 1))

    assert drive(ui.main_menu, script)[0] == "4"


def test_prompt_url_detects_and_returns():
    async def script(pilot, app):
        await until(pilot, on(app, "PromptScreen"))
        await pilot.press(*"https://youtu.be/abc")
        await pilot.pause(0.05)
        note = str(app.screen.query_one("#prompt-note").render())
        assert "YouTube video" in note
        await pilot.press("enter")

    assert drive(ui.prompt_url, script)[0] == "https://youtu.be/abc"
    assert drive(ui.prompt_url, keys("escape", wait_for="PromptScreen"))[0] == ""


SCREEN_HISTORY = [
    {"type": "youtube", "url": "u1", "title": "One", "play_count": 2,
     "last_played": datetime.now(timezone.utc).isoformat()},
    {"type": "khinsider", "url": "u2", "title": "Two", "track_count": 10, "last_index": 3},
]


@pytest.mark.parametrize("names, result", [
    (["enter"], ("play", SCREEN_HISTORY[0])),
    (["down", "d"], ("delete", "u2")),
    (["c"], ("clear", None)),
    (["escape"], ("back", None)),
    (["q"], ("quit", None)),
])
def test_history(names, result):
    assert drive(lambda: ui.show_history(SCREEN_HISTORY), keys(*names, wait_for="HistoryScreen"))[0] == result


def test_suggestions():
    tracks = [Track("A", "ytsearch1:A"), Track("B", "ytsearch1:B")]
    flow = lambda: ui.show_ai_results("vibes", tracks)  # noqa: E731
    assert drive(flow, keys("down", "enter", wait_for="SuggestionsScreen"))[0] == ("play", tracks[1])
    assert drive(flow, keys("r", wait_for="SuggestionsScreen"))[0] == ("research", None)


@pytest.mark.parametrize("names, result", [(["2"], "2"), (["down", "enter"], "2"), (["escape"], "b")])
def test_settings(names, result):
    assert drive(lambda: ui.show_settings(True), keys(*names, wait_for="OptionsScreen"))[0] == result


def test_ai_settings_edits_the_model():
    async def script(pilot, app):
        await until(pilot, on(app, "OptionsScreen"))
        await pilot.press("3")
        await until(pilot, on(app, "EditModal"))
        await pilot.press(*"my-model", "enter")
        await until(pilot, lambda: on(app, "OptionsScreen")() and app.screen.is_current)
        await pilot.pause(0.1)
        await pilot.press("escape")

    assert drive(ui.show_ai_settings, script)[0] == "back"
    assert config.get_setting("ai_model") == "my-model"


def test_theme_picker_saves_or_reverts():
    async def pick(pilot, app):
        await until(pilot, on(app, "ThemeScreen"))
        await pilot.press("down", "enter")

    _, app = drive(ui.show_theme_picker, pick)
    assert config.get_setting("theme") == app.theme != "tunetape"

    config.set_setting("theme", "tunetape")
    _, app = drive(ui.show_theme_picker, keys("down", "down", "escape", wait_for="ThemeScreen"))
    assert app.theme == "tunetape" and config.get_setting("theme") == "tunetape"


def mcp_screen(script, size=(108, 30)):
    seen = {}

    async def run(pilot, app):
        await until(pilot, on(app, "McpScreen"))
        await pilot.pause(0.1)
        seen["text"] = str(app.screen.query_one("#mcp Static").render())
        seen["caps"] = text(app, "KeyCaps")
        await script(pilot, app, seen)

    result, app = drive(ui.show_mcp, run, size)
    return result, seen


async def _nothing(pilot, app, seen):
    pass


def test_mcp_screen_shows_both_setups():
    _, seen = mcp_screen(_nothing)
    assert mcp_setup.claude_command() in seen["text"]
    assert '"mcpServers"' in seen["text"] and "claude not found" in seen["text"]
    assert "add to Claude Code" not in seen["caps"]  # no claude to run
    assert "copy command" in seen["caps"]


def test_mcp_screen_fits_a_small_terminal():
    _, seen = mcp_screen(_nothing, size=(60, 24))
    assert "Claude Code" in seen["text"] and " q " in seen["caps"]


def test_mcp_screen_copies(monkeypatch):
    copied = []
    monkeypatch.setattr(mcp_setup, "copy", copied.append)

    async def script(pilot, app, seen):
        await pilot.press("c", "j")
        await pilot.pause(0.1)

    assert mcp_screen(script)[0] is NOTHING
    assert copied == [mcp_setup.claude_command(), mcp_setup.config_json()]


def test_mcp_screen_adds_tunetape_to_claude_code(monkeypatch):
    added = []

    def add():
        added.append(True)
        return True, "Added tunetape to Claude Code."

    monkeypatch.setattr(mcp_setup, "claude_cli", lambda: ["claude"])
    monkeypatch.setattr(mcp_setup, "add_to_claude_code", add)

    async def script(pilot, app, seen):
        assert "add to Claude Code" in seen["caps"]
        monkeypatch.setattr(mcp_setup, "registered", lambda: "user")
        await pilot.press("a")
        for _ in range(100):  # the screen comes back, now with tunetape added
            await pilot.pause(0.05)
            if on(app, "McpScreen")() and "added" in str(
                    app.screen.query_one("#mcp Static").render()):
                break
        seen["after"] = str(app.screen.query_one("#mcp Static").render())
        seen["after_caps"] = text(app, "KeyCaps")
        await pilot.press("b")

    result, seen = mcp_screen(script)
    assert result is None and added == [True]
    assert "✓ added" in seen["after"] and "user scope" in seen["after"]
    assert "add to Claude Code" not in seen["after_caps"]


def test_a_notice_from_the_flow_shows_up():
    async def script(pilot, app):
        await until(pilot, lambda: bool(app._notifications))

    def flow():
        ui._notify("Connected · claude responded.", "error")
        return "shown"

    result, app = drive(flow, script)
    assert result == "shown"
    assert [n.severity for n in app._notifications] == ["error"]


def test_debug_clears_the_log():
    from tunetape import debug

    debug.log("something happened")
    assert drive(ui.show_debug, keys("c", "escape", wait_for="DebugScreen"))[0] is None
    assert debug.entries() == []


def test_verify_ai_connection_reports_failures():
    def check():
        raise ConnectionError("no route")

    result, _ = drive(lambda: ui.verify_ai_connection(check), keys())
    assert result == (False, "no route")


# --- __main__: the session against a fake daemon -----------------------------

@pytest.fixture
def daemon(monkeypatch):
    """A FakeClient as the session's daemon, idle until something is posted."""
    fake = FakeClient(frozen=True)
    monkeypatch.setattr(main, "_link", main._DaemonLink(lambda spawn: fake))
    return fake


def session(script, size=(108, 30)):
    return drive(main._session, script, size)


async def post_url(pilot, app, url):
    await until(pilot, on(app, "MenuScreen"))
    await pilot.press("1")
    await until(pilot, on(app, "PromptScreen"))
    app.screen.query_one(Input).value = url
    await pilot.press("enter")


def now_row(app):
    """The menu's now-playing row: its text, or None while it is hidden."""
    row = app.screen.query_one("#menu-now")
    return str(row.render()).strip() if row.display else None


def test_play_back_keeps_playing_and_the_menu_reopens_it(daemon):
    seen = {}

    async def script(pilot, app):
        await post_url(pilot, app, "https://youtu.be/abc")
        await until(pilot, on(app, "PlayerScreen"))
        seen["title"] = text(app, "#title")
        await pilot.press("b")
        await until(pilot, on(app, "MenuScreen"))
        await until(pilot, lambda: now_row(app) is not None)
        seen["row"] = now_row(app)
        seen["state after b"] = daemon.state
        await pilot.press("n")
        await until(pilot, on(app, "PlayerScreen"))
        seen["reopened"] = text(app, "#title")
        await pilot.press("q")

    result, _ = session(script)
    assert result is None  # the session ended on its own
    assert daemon.called("play") == [("https://youtu.be/abc", None, 0)]
    assert seen == {"title": "https://youtu.be/abc", "row": "n ▶ https://youtu.be/abc",
                    "state after b": "playing", "reopened": "https://youtu.be/abc"}
    # q stopped the music and hung up.
    assert daemon.state == "idle" and daemon.called("stop") == [()] and daemon.closed


def test_q_on_the_menu_stops_playback_too(daemon):
    async def script(pilot, app):
        await post_url(pilot, app, "https://youtu.be/abc")
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("b")
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("q")

    assert session(script)[0] is None
    assert daemon.state == "idle" and daemon.called("stop") == [()]


def test_ctrl_c_stops_playback(daemon):
    async def script(pilot, app):
        await post_url(pilot, app, "https://youtu.be/abc")
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("ctrl+c")

    session(script)
    assert daemon.called("stop") == [()]


@pytest.mark.parametrize("size, ascii", [((108, 32), True), ((108, 30), False),
                                         ((60, 30), False), ((60, 24), False), ((60, 20), False)])
def test_menu_shows_what_the_daemon_already_plays(monkeypatch, size, ascii):
    long_title = "A Very Long Song Title That Will Never Fit In Sixty Columns (Remastered)"
    fake = FakeClient([long_title], frozen=True)  # posted from the CLI, say
    monkeypatch.setattr(main, "_link", main._DaemonLink(lambda spawn: fake))
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await until(pilot, lambda: now_row(app) is not None)
        await pilot.pause(0.1)
        seen["row"] = now_row(app)
        for name in ("menu-now", "menu-caps", "menu-nav"):
            region = app.screen.query_one(f"#{name}").region
            seen[name] = (region.height > 0 and region.x >= 0 and region.right <= size[0]
                          and region.bottom <= size[1])
        seen["screen"] = type(app.screen).__name__  # no jump into the player
        seen["ascii"] = app.screen.query_one("#ascii-tuna").display
        await pilot.press("n")
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("q")

    session(script, size)
    assert seen.pop("screen") == "MenuScreen"
    assert seen.pop("ascii") is ascii  # the big tuna steps aside when the row needs its rows
    row = seen.pop("row")
    assert row.startswith("n ▶ A Very Long")
    if size[0] == 60:
        assert row.endswith("…") and len(row) <= 60 - 4
    assert all(seen.values()), seen


def test_the_now_playing_row_goes_when_playback_ends(daemon):
    seen = {}

    async def script(pilot, app):
        await post_url(pilot, app, "https://youtu.be/abc")
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("b")
        await until(pilot, on(app, "MenuScreen"))
        await until(pilot, lambda: now_row(app) is not None)
        daemon.stop()  # say, `tunetape stop` from another terminal
        await until(pilot, lambda: now_row(app) is None)
        await pilot.press("n")  # nothing to go back to
        await pilot.pause(0.2)
        seen["screen"] = type(app.screen).__name__
        await pilot.press("q")

    session(script)
    assert seen == {"screen": "MenuScreen"}


def test_menu_up_from_the_buttons_reaches_the_now_playing_row(monkeypatch):
    fake = FakeClient(["Song"], frozen=True)
    monkeypatch.setattr(main, "_link", main._DaemonLink(lambda spawn: fake))

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await until(pilot, lambda: now_row(app) is not None)
        await pilot.press("up", "enter")
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("q")

    assert session(script)[0] is None


@pytest.mark.parametrize("error", [
    DaemonError(1010, "Unsupported link type (ftp:). Use an http(s) URL or a local file."),
    DaemonError(1011, "mpv is not installed. Install it with: brew install mpv"),
    DaemonError(1014, "Could not fetch the album page."),
])
def test_a_failed_post_shows_the_error_and_goes_back_to_the_menu(daemon, error):
    daemon.catalog["bad"] = error
    seen = {}

    async def script(pilot, app):
        await post_url(pilot, app, "bad")
        await until(pilot, on(app, "ErrorModal"))
        seen["error"] = app.screen.message
        await pilot.press("enter")
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("q")

    assert session(script)[0] is None
    assert seen["error"] == error.message


def test_an_unreachable_daemon_at_start_explains_and_exits(monkeypatch):
    def connect(spawn):
        raise DaemonUnavailable("The tunetape daemon failed to start (exit status 1).")

    monkeypatch.setattr(main, "_link", main._DaemonLink(connect))
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "ErrorModal"))
        seen["error"] = app.screen.message
        await pilot.press("enter")

    assert session(script)[0] is None
    assert seen["error"].startswith("The tunetape daemon failed to start (exit status 1).")
    assert client_mod.log_path() in seen["error"]
    assert client_mod.log_path().endswith("daemon.log")


def test_a_late_now_playing_answer_after_leaving_the_menu_is_ignored():
    # The status worker can answer after the menu was dismissed and its widgets
    # removed (seen on Windows CI): that must not take the app down.
    from tunetape.screens import MenuScreen

    screen = MenuScreen(lambda: None)
    screen._show_now({"state": "playing", "current": {"title": "Late"}})
    screen._show_now(None)


def test_the_daemon_going_away_mid_session_explains_and_reconnects(monkeypatch):
    clients = [album_client(frozen=True), FakeClient(frozen=True)]
    monkeypatch.setattr(main, "_link", main._DaemonLink(lambda spawn: clients.pop(0)))
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await until(pilot, lambda: now_row(app) is not None)
        await pilot.press("n")
        await until(pilot, on(app, "PlayerScreen"))
        main._link.get().down = True  # the daemon crashed
        await until(pilot, on(app, "ErrorModal"))
        seen["error"] = app.screen.message
        await pilot.press("enter")
        await until(pilot, on(app, "MenuScreen"))
        await post_url(pilot, app, "https://youtu.be/abc")  # a new daemon
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("q")

    assert session(script)[0] is None
    assert "lost" in seen["error"] and client_mod.log_path() in seen["error"]
    assert clients == []


HISTORY = [
    {"type": "youtube", "url": "u1", "title": "One", "play_count": 2,
     "last_played": datetime.now(timezone.utc).isoformat()},
    {"type": "khinsider", "url": "u2", "title": "Two", "track_count": 10, "last_index": 3},
    {"type": "ai", "url": "ytsearch1:three", "title": "Three"},
]


@pytest.mark.parametrize("names, posted", [
    (["enter"], ("u1", "One", 0)),
    (["down", "enter"], ("u2", None, 3)),          # resumes the album
    (["down", "down", "enter"], ("ytsearch1:three", "Three", 0)),  # keeps the AI title
])
def test_history_replays_through_the_daemon(daemon, names, posted):
    daemon.history_entries = HISTORY

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("2")
        await until(pilot, on(app, "HistoryScreen"))
        for name in names:
            await pilot.press(name)
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("q")

    session(script)
    assert daemon.called("play") == [posted]


def test_history_delete_stays_local(daemon, monkeypatch):
    removed = []
    monkeypatch.setattr(main.history, "remove", removed.append)
    daemon.history_entries = HISTORY

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("2")
        await until(pilot, on(app, "HistoryScreen"))
        await pilot.press("down", "d")
        await pilot.pause(0.2)
        await pilot.press("q")

    session(script)
    assert removed == ["u2"]


def _ai_ready(monkeypatch):
    monkeypatch.setattr(main.ai, "is_configured", lambda: True)
    monkeypatch.setattr(main.ai, "is_verified", lambda: True)


def test_discover_asks_the_daemon_and_plays_a_pick(daemon, monkeypatch):
    _ai_ready(monkeypatch)
    daemon.suggestions = [{"title": "Song A", "input": "ytsearch1:a"},
                          {"title": "Song B", "input": "ytsearch1:b"}]
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("3")
        await until(pilot, on(app, "PromptScreen"))
        await pilot.press(*"vibes", "enter")
        await until(pilot, on(app, "SuggestionsScreen"))
        await pilot.press("down", "enter")
        await until(pilot, on(app, "PlayerScreen"))
        seen["title"] = text(app, "#title")
        await pilot.press("b")  # back to the picks; the song plays on
        await until(pilot, on(app, "SuggestionsScreen"))
        seen["state"] = daemon.state
        await pilot.press("q")

    session(script)
    assert daemon.called("discover") == [("vibes",)]
    assert daemon.called("play") == [("ytsearch1:b", "Song B", 0)]
    assert seen == {"title": "Song B", "state": "playing"}


@pytest.mark.parametrize("error, screen", [
    (DaemonError(1020, "AI discovery isn't set up yet."), "NoticeScreen"),
    (DaemonError(1021, "The AI request failed: timeout"), "ErrorModal"),
])
def test_discover_errors(daemon, monkeypatch, error, screen):
    _ai_ready(monkeypatch)
    daemon.suggestions = error
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("3")
        await until(pilot, on(app, "PromptScreen"))
        await pilot.press(*"vibes", "enter")
        await until(pilot, on(app, screen))
        seen["screen"] = screen
        await pilot.press("ctrl+c")

    session(script)
    assert seen == {"screen": screen}


def test_debug_shows_the_daemon_log(daemon):
    debug.clear()
    debug.log("from the window")
    daemon.log_entries = [{"time": "2026-10-23T14:02:11Z", "level": "INFO",
                           "message": "Now playing: from the daemon"}]
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("d")
        await until(pilot, on(app, "DebugScreen"))
        seen["log"] = str(app.screen.log_text)
        await pilot.press("q")

    session(script, (60, 24))
    log = seen["log"]
    assert log.index("this window") < log.index("from the window") < log.index("\ndaemon ")
    assert "tunetape/demo" in log and "Now playing: from the daemon" in log


# --- __main__ against the real daemon ------------------------------------------

@needs_fake_mpv
def test_end_to_end_with_the_real_daemon(data_dir, audio, monkeypatch):
    """The real session, the real daemon (spawned on demand), the fake mpv."""
    monkeypatch.setattr(main, "_link", main._DaemonLink())
    song = audio("e2e-fake-dur-30.mp3")
    seen = {}

    async def script(pilot, app):
        await post_url(pilot, app, song)
        await until(pilot, on(app, "PlayerScreen"), timeout=20)
        await until(pilot, lambda: app.screen.status.get("state") == "playing", timeout=20)
        await until(pilot, lambda: text(app, "#elapsed") != "00:00", timeout=10)
        seen["title"] = text(app, "#title")
        seen["status"] = text(app, "#status")
        info = client_mod.read_discovery()
        seen["pid"] = info and info.get("pid")
        await pilot.press("q")

    try:
        result, _ = session(script)
        assert result is None
        assert seen["title"] == "e2e-fake-dur-30" and seen["status"] == "▶ playing"
        with client_mod.Client.connect(spawn=False) as c:
            assert c.status()["state"] == "idle"  # q stopped it
            entries = c.history()["entries"]
        assert entries and entries[0]["type"] == "file" and entries[0]["url"] == song
    finally:
        try:
            with client_mod.Client.connect(spawn=False, timeout=2) as c:
                c.shutdown()
        except DaemonError:
            pass
        pid = seen.get("pid")
        deadline = time.monotonic() + 10
        while pid and pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
    assert not (pid and pid_alive(pid)), "the daemon was left running"
