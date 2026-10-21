"""Headless tests for the Textual interface: the flow thread, every screen, the player."""

import asyncio
from datetime import datetime, timezone

import pytest
from textual import events

import tunetape.__main__ as main
from tunetape import config, ui
from tunetape.demo import DEMO_ALBUM, DEMO_TRACKS, FakeController, playlist_info_for
from tunetape.models import Track
from tunetape.widgets import KeyCaps, LevelMeter, Tuna, balanced_rows, graph_rows

NOTHING = object()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep config.json (theme, volume) out of the real data dir, and start
    every test with fresh remembered highlights."""
    monkeypatch.setattr(config.paths, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ui, "_settings_at", {})
    monkeypatch.setattr(ui, "_history_at", 0)
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


def keys(*names, wait_for=None):
    """A script that waits for a screen, then presses ``names``."""

    async def script(pilot, app):
        if wait_for:
            await until(pilot, on(app, wait_for))
        await pilot.pause(0.1)
        for name in names:
            await pilot.press(name)
            await pilot.pause(0.05)

    return script


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


def test_level_meter_holds_peaks():
    meter = LevelMeter()
    meter.update_levels([(-10.0, -3.0), (-12.0, -6.0)])
    meter.update_levels([(-30.0, -28.0), (-30.0, -28.0)])
    assert meter._held == [-4.5, -7.5]  # falling back from the peak, not dropped
    meter.update_levels(None)  # paused / loading: silence on both channels
    assert [rms for rms, _ in meter._levels] == [float("-inf")] * 2


# --- the player --------------------------------------------------------------

def album_flow(index=11, controller=None, results=None):
    controller = controller or FakeController(187, 41, frozen=True)

    def flow():
        return ui.play(DEMO_TRACKS[index], controller, playlist_info_for(index, len(DEMO_TRACKS)),
                       source="KHInsider", subtitle=DEMO_ALBUM, tracks=DEMO_TRACKS)

    return flow, controller


def play_keys(names, index=11, size=(108, 30)):
    flow, controller = album_flow(index)
    result, app = drive(flow, keys(*names, wait_for="PlayerScreen"), size)
    return result, controller, app


def test_counter_shows_position():
    def counter(app):
        try:
            return str(app.screen.query_one("#elapsed").render())
        except Exception:
            return None

    async def script(pilot, app):
        await until(pilot, lambda: counter(app) == "00:41")

    flow, _ = album_flow()
    drive(flow, script)


def test_space_toggles_pause():
    assert play_keys(["space"])[1].is_paused()


def test_seek_keys():
    _, controller, _ = play_keys(["right", "right", "left", "full_stop", "comma", "comma"])
    assert controller.get_position() == 41 + 10 + 10 - 10 + 30 - 30 - 30


@pytest.mark.parametrize("names, volume", [(["up", "plus"], 90), (["down", "minus"], 70)])
def test_volume_keys(names, volume):
    assert play_keys(names)[1].get_volume() == volume


def test_mute():
    assert play_keys(["m"])[1].is_muted()


@pytest.mark.parametrize("key, result", [
    ("n", "next_track"),
    ("p", "prev_track"),
    ("b", "menu"),
    ("escape", "menu"),
    ("q", "quit"),
    ("ctrl+c", "quit"),
])
def test_player_exit_keys(key, result):
    assert play_keys([key])[0] == result


def test_prev_disabled_on_first_track():
    assert play_keys(["p"], index=0)[0] is NOTHING


def test_next_disabled_on_last_track():
    assert play_keys(["n"], index=len(DEMO_TRACKS) - 1)[0] is NOTHING


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

    flow, _ = album_flow()
    assert drive(flow, script)[0] is NOTHING  # the player keeps playing under help
    assert seen == ["HelpModal", "PlayerScreen"]


def test_q_on_player_help_quits():
    assert play_keys(["h", "q"])[0] == "quit"


def test_queue_takes_arrows_once_focused():
    result, controller, _ = play_keys(["tab", "down"])
    assert controller.get_volume() == 80 and result is NOTHING


def test_queue_enter_jumps():
    assert play_keys(["tab", "down", "down", "enter"])[0] == "jump_track:13"


def test_escape_leaves_queue_before_menu():
    assert play_keys(["tab", "escape"])[0] is NOTHING
    assert play_keys(["tab", "escape", "escape"])[0] == "menu"


@pytest.mark.parametrize("names, check", [
    (["m"], lambda r, c: c.is_muted()),
    (["p"], lambda r, c: r == "prev_track"),
    (["b"], lambda r, c: r == "menu"),
])
def test_keys_work_at_60_columns(names, check):
    result, controller, _ = play_keys(names, size=(60, 24))
    assert check(result, controller)


@pytest.mark.parametrize("size, queue", [
    ((108, 30), True),
    ((80, 24), False),
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

    flow, _ = album_flow()
    drive(flow, script, size)
    assert seen.pop("queue") is queue
    assert all(seen.values()), seen


def test_level_and_stream_panels_follow_the_controller():
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(1.3)  # a stats read lands once a second
        screen = app.screen
        seen["meter"] = str(screen.query_one("#meter").render())
        seen["stream"] = str(screen.query_one("#stream-info").render())
        seen["samples"] = len(screen.query_one("#level-graph")._samples)

    flow, _ = album_flow(controller=FakeController(187, 41))
    drive(flow, script)
    assert seen["meter"].startswith("L ") and "\nR " in seen["meter"]
    assert "opus · 131 kb/s" in seen["stream"] and "48 kHz · stereo" in seen["stream"]
    assert seen["samples"] >= 5


def test_mouse_wheel_changes_volume_but_scrolls_the_queue():
    flow, controller = album_flow()

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot._post_mouse_events([events.MouseScrollUp], "#level-graph")
        await pilot._post_mouse_events([events.MouseScrollUp], "#elapsed")
        await pilot._post_mouse_events([events.MouseScrollDown], "#title")
        await pilot._post_mouse_events([events.MouseScrollDown], "#queue")
        await pilot.pause(0.1)

    drive(flow, script)
    assert controller.get_volume() == 80 + 5 + 5 - 5


def test_volume_gauge_sits_after_quit_and_sets_volume_on_click():
    flow, controller = album_flow()
    seen = {}

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.5)  # a state poll, so the gauge knows the volume
        caps = app.screen.query_one(KeyCaps)
        quit_box = next(h for h in caps._hits if h[4] >= 0 and caps.caps[h[4]].label == "quit")
        tail = next(h for h in caps._hits if h[4] == -1)
        seen["after_quit"] = tail[2] == quit_box[2] and tail[0] > quit_box[1]
        seen["at_right_edge"] = tail[1] == caps.size.width
        await pilot.click("#player-caps", offset=(tail[0] + 10 + 2, tail[2]))  # 3rd of 10 cells
        await pilot.pause(0.1)

    drive(flow, script)
    assert seen == {"after_quit": True, "at_right_edge": True}
    assert controller.get_volume() == 30


def test_clicking_caps_and_the_bar():
    flow, controller = album_flow()

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.pause(0.4)  # a poll, so the bar knows the duration
        caps = app.screen.query_one(KeyCaps)
        for x0, x1, y0, y1, index in caps._hits:
            if caps.caps[index].label == "mute":
                await pilot.click("#player-caps", offset=(x0 + 1, y0))
        await pilot.click("#progress Bar", offset=(0, 0))  # the very start
        await pilot.pause(0.1)

    drive(flow, script)
    assert controller.is_muted()
    assert controller.get_position() == 0


@pytest.mark.parametrize("info, result", [
    (None, "menu"),
    ({"index": 0, "track_label": "1/2", "has_next": True, "has_prev": False}, "next_track"),
])
def test_track_end_moves_on(info, result):
    async def script(pilot, app):
        await pilot.pause(1.0)

    flow_result, _ = drive(lambda: ui.play("x", FakeController(1.0, 0.9), info), script)
    assert flow_result == result


def test_next_track_reuses_the_player_screen():
    screens = []

    def flow():
        for index in (3, 4):
            controller = FakeController(187, 0, frozen=True)
            screens.append(ui.play(DEMO_TRACKS[index], controller,
                                   playlist_info_for(index, len(DEMO_TRACKS)),
                                   tracks=DEMO_TRACKS))
            screens.append(ui._app.screen)
        return screens

    async def script(pilot, app):
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("n")
        await until(pilot, lambda: len(screens) == 2)
        await pilot.pause(0.1)
        assert str(app.screen.query_one("#title").render()) == DEMO_TRACKS[4]
        await pilot.press("b")

    result, _ = drive(flow, script)
    assert result[0] == "next_track" and result[2] == "menu"
    assert result[1] is result[3]


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
    (["q"], "q"),
    (["right", "right", "enter"], "3"),
    (["down", "enter"], "d"),            # down from the buttons into the nav row
    (["down", "right", "enter"], "h"),
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
    (lambda: ui.show_history(HISTORY), "HistoryScreen", ("back", None), ("quit", None)),
    (lambda: ui.show_settings(True), "OptionsScreen", "b", "q"),
    (ui.show_ai_settings, "OptionsScreen", "back", "quit"),
    (lambda: ui.show_ai_results("x", [Track("A", "a")]), "SuggestionsScreen",
     ("back", None), ("quit", None)),
    (lambda: ui.show_ai_unavailable("nope"), "NoticeScreen", "back", "quit"),
    (ui.show_debug, "DebugScreen", None, "quit"),
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
        caps = app.screen.query_one(KeyCaps)
        x0, _, y0, _, _ = next(h for h in caps._hits if caps.caps[h[4]].label == "Settings")
        await pilot.click(KeyCaps, offset=(x0 + 2, y0 + 1))

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


HISTORY = [
    {"type": "youtube", "url": "u1", "title": "One", "play_count": 2,
     "last_played": datetime.now(timezone.utc).isoformat()},
    {"type": "khinsider", "url": "u2", "title": "Two", "track_count": 10, "last_index": 3},
]


@pytest.mark.parametrize("names, result", [
    (["enter"], ("play", HISTORY[0])),
    (["down", "d"], ("delete", "u2")),
    (["c"], ("clear", None)),
    (["escape"], ("back", None)),
    (["q"], ("quit", None)),
])
def test_history(names, result):
    assert drive(lambda: ui.show_history(HISTORY), keys(*names, wait_for="HistoryScreen"))[0] == result


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


# --- __main__ end to end ------------------------------------------------------

def test_session_plays_a_youtube_url(monkeypatch):
    controller = FakeController(200, 10, frozen=True)
    monkeypatch.setattr(main, "check_dependencies", lambda **kw: None)
    monkeypatch.setattr(main, "get_stream_info",
                        lambda url: {"stream_url": "s", "title": "A Song"})
    monkeypatch.setattr(main, "MPVController", lambda *a, **kw: controller)
    monkeypatch.setattr(main.history, "record", lambda *a, **kw: None)

    async def script(pilot, app):
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("1")
        await until(pilot, on(app, "PromptScreen"))
        await pilot.press(*"https://youtu.be/abc", "enter")
        await until(pilot, on(app, "PlayerScreen"))
        await pilot.press("up", "b")
        await until(pilot, on(app, "MenuScreen"))
        await pilot.press("q")

    result, _ = drive(main._session, script)
    assert result is None  # the session ended on its own
    assert config.get_volume() == 85  # saved when the track was left
