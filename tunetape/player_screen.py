"""The player, btop style: now playing, live audio levels, stream stats, queue.

``PlayerScreen`` answers the strings the old Rich player returned ('menu',
'quit', 'next_track', 'prev_track'), plus 'jump_track:<n>' when a track is
picked from the queue. Between tracks of an album the flow keeps the same
screen and calls ``load()`` with the next track, so nothing flickers.
"""

import time

from textual import work
from textual.binding import Binding
from textual.color import Gradient
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import OptionList, ProgressBar, Static
from textual.worker import get_current_worker

from tunetape.screens import NAV, FlowScreen, HelpModal
from tunetape.widgets import Cap, Graph, KeyCaps, LevelMeter, TopBar

# The poll runs at the level meter's pace; the rest is read less often.
POLL_SECONDS = 0.1
STATE_EVERY = 3   # position aside, playback state every 0.3 s
STATS_EVERY = 10  # stream stats every second
# The level graph spans a tighter range than the meter's bars, so the swells
# of a song show as hills instead of a flat block.
GRAPH_FLOOR = -36.0


def format_time(seconds: float) -> str:
    """Format seconds as mm:ss."""
    seconds = max(seconds, 0)
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def format_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024


VOLUME_BAR = 10   # cells in the volume gauge's bar
GAUGE_LEAD = 10   # cells before the bar: the " ↑ ↓ vol " pill and a space


def volume_gauge(volume: float = None, muted: bool = False) -> Content:
    """`` ↑ ↓ vol  ━━━━━━━━━━  80%``: a pill like the caps, then a flat bar
    drawn like the progress bar. ``None`` until the first reading."""
    level = 0.0 if volume is None or muted else min(max(volume / 100.0, 0.0), 1.0)
    lit = round(VOLUME_BAR * level)
    if muted:
        figure, figure_style = " muted", "b $error"
    elif volume is None:
        figure, figure_style = "   --", "$foreground 50%"
    else:
        figure, figure_style = f" {round(volume):>3}%", "$foreground 80%"
    return Content.assemble(
        (" ↑ ↓ ", "b $accent on $panel"), ("vol ", "$foreground 80% on $panel"), " ",
        ("━" * lit, "$primary"), ("━" * (VOLUME_BAR - lit), "$panel"),
        (figure.ljust(6), figure_style),
    )


def status_line(paused: bool) -> Content:
    if paused:
        return Content.from_markup("[b $warning]❚❚ paused[/]")
    return Content.from_markup("[b $success]▶ playing[/]")


def loading_line(message: str = "loading") -> Content:
    return Content.from_markup("[$foreground 60%]… $message[/]", message=message)


def stream_lines(stats: dict) -> Content:
    """The stream panel's figures, one per line, unknowns as a dash."""

    def known(*values) -> bool:
        return all(v not in (None, "", 0) for v in values)

    codec = stats.get("codec") or "—"
    if known(stats.get("bitrate")):
        codec += f" · {stats['bitrate'] / 1000:.0f} kb/s"
    audio = "—"
    if known(stats.get("samplerate")):
        audio = f"{stats['samplerate'] / 1000:g} kHz"
        if stats.get("channels"):
            audio += f" · {stats['channels']}"
    cache = "—"
    if stats.get("ahead") is not None:
        cache = f"{format_time(stats['ahead'])} ahead"
        if known(stats.get("cached")):
            cache += f" · {format_bytes(stats['cached'])}"
    speed = stats.get("speed")
    rows = [
        ("codec", codec),
        ("audio", audio),
        ("file", stats.get("container") or "—"),
        ("cache", cache),
        ("net ↓", f"{format_bytes(speed)}/s" if speed is not None else "—"),
        ("norm", "on" if stats.get("normalized") else "off"),
    ]
    return Content("\n").join(
        Content.from_markup("[$foreground 60%]$label[/] [$primary]$value[/]",
                            label=label.ljust(5), value=value)
        for label, value in rows
    )


# The volume gauge follows these as the caps' tail (see volume_gauge).
PLAYER_CAPS = [
    Cap("space", "play/pause", "toggle_pause"),
    Cap("← →", "seek"),
    Cap("m", "mute", "mute"),
    Cap("p", "prev", "prev"),
    Cap("n", "next", "next"),
    *NAV,
]


class PlayerScreen(FlowScreen):
    """Now playing with its controls, level and stream panels, and the queue."""

    AUTO_FOCUS = ""  # nothing: ↑/↓ stay on volume until tab focuses the queue

    BINDINGS = [
        Binding("space", "toggle_pause", show=False),
        Binding("left", "seek(-10)", show=False),
        Binding("right", "seek(10)", show=False),
        Binding("comma", "seek(-30)", show=False),
        Binding("full_stop", "seek(30)", show=False),
        Binding("up,plus,equals_sign", "volume(5)", show=False),
        Binding("down,minus,underscore", "volume(-5)", show=False),
        Binding("m", "mute", show=False),
        Binding("p", "prev", show=False),
        Binding("n", "next", show=False),
        Binding("h,question_mark", "help", show=False),
        Binding("b,escape", "back", show=False),
        Binding("q", "quit_session", show=False),
    ]

    def __init__(self, title: str, controller, playlist_info: dict = None, *,
                 source: str = None, subtitle: str = None, tracks: list = None):
        super().__init__()
        self.track_title = title
        self.controller = controller
        self.playlist_info = playlist_info or {}
        self.source = source
        self.subtitle = subtitle
        self.tracks = tracks or []
        self._state = {}  # the latest poll, merged

    @property
    def current_index(self) -> int:
        return self.playlist_info.get("index", 0)

    # --- layout ----------------------------------------------------------

    def compose(self):
        yield TopBar(right=self._source_badge())
        with Horizontal(id="player-body"):
            with Vertical(id="deck"):
                with Vertical(id="now", classes="box"):
                    with Horizontal(id="now-top"):
                        yield Static(id="title")
                        yield Static(loading_line(), id="status")
                    yield Static(id="subtitle")
                    with Horizontal(id="time-row"):
                        yield Static("00:00", id="elapsed")
                        yield ProgressBar(show_percentage=False, show_eta=False, id="progress")
                        yield Static("--:--", id="duration")
                    yield KeyCaps(PLAYER_CAPS, id="player-caps")
                with Horizontal(id="stats"):
                    with Vertical(id="level", classes="box"):
                        yield LevelMeter(id="meter")
                        yield Graph(maximum=1.0, tints=("$success", "$warning", "$error"),
                                    id="level-graph")
                    with Vertical(id="stream", classes="box"):
                        yield Static(stream_lines({}), id="stream-info")
                        yield Graph(tints=("$primary", "$accent"), id="net-graph")
            if self.tracks:
                yield OptionList(*self._queue_rows(), id="queue", classes="box")

    def _source_badge(self) -> Content:
        if not self.source:
            return Content("")
        return Content.from_markup("[$accent]●[/] [$foreground 60%]$source[/]", source=self.source)

    def _queue_rows(self) -> list:
        rows = []
        for i, name in enumerate(self.tracks):
            num = f"{i + 1:>3}"
            if i == self.current_index:
                markup = "[b $primary]▶ $num  $name[/]"
            elif i < self.current_index:
                markup = "[$foreground 30%]  $num  $name[/]"
            else:
                markup = "[$foreground 60%]  $num[/]  $name"
            rows.append(Content.from_markup(markup, num=num, name=name))
        return rows

    def on_mount(self) -> None:
        self._w_elapsed = self.query_one("#elapsed", Static)
        self._w_duration = self.query_one("#duration", Static)
        self._w_progress = self.query_one("#progress", ProgressBar)
        self._w_bar = self._w_progress.query_one("#bar")
        self._w_status = self.query_one("#status", Static)
        self._w_meter = self.query_one("#meter", LevelMeter)
        self._w_level_graph = self.query_one("#level-graph", Graph)
        self._w_net_graph = self.query_one("#net-graph", Graph)
        self._w_stream = self.query_one("#stream-info", Static)
        self._w_caps = self.query_one("#player-caps", KeyCaps)
        self._w_caps.set_tail(volume_gauge())
        self.query_one("#now").border_title = "now playing"
        self.query_one("#level").border_title = "level"
        self.query_one("#stream").border_title = "stream"
        if self.tracks:
            queue = self.query_one("#queue", OptionList)
            queue.border_title = "queue"
            queue.border_subtitle = f"{len(self.tracks)} tracks"
        self._show_track()
        self.apply_gradient()
        self._poll()

    def _show_track(self) -> None:
        """Fill in everything that belongs to the current track."""
        self.query_one("#title", Static).update(Content(self.track_title))
        details = [self.subtitle] if self.subtitle else []
        label = self.playlist_info.get("track_label")
        if label:
            details.append(f"track {label}")
        subtitle = self.query_one("#subtitle", Static)
        subtitle.update(Content(" · ".join(details)))
        subtitle.display = bool(details)
        self._w_elapsed.update("00:00")
        self._w_duration.update("--:--")
        self._w_progress.update(total=None)
        self._w_status.update(loading_line())
        self.query_one("#level").border_subtitle = ""
        if self.tracks:
            queue = self.query_one("#queue", OptionList)
            queue.set_options(self._queue_rows())
            queue.highlighted = self.current_index
            queue.scroll_to_highlight(top=False)
            queue.border_subtitle = f"{self.current_index + 1}/{len(self.tracks)}"
        self.refresh_bindings()
        self._w_caps.refresh(layout=True)

    def apply_gradient(self) -> None:
        """Paint the progress bar primary → accent in whatever theme is active."""
        theme = self.app.current_theme
        end = theme.accent or theme.secondary or theme.primary
        self._w_progress.gradient = Gradient.from_colors(theme.primary, end)

    # --- the flow's side -------------------------------------------------

    def load(self, title: str, controller, playlist_info: dict = None) -> None:
        """Play the next track of the same album on this screen."""
        self.track_title = title
        self.playlist_info = playlist_info or {}
        self.controller = controller
        self._state = {}
        self._show_track()

    def set_loading(self, message: str) -> None:
        """Show the flow fetching the next track (``ui.with_spinner``)."""
        self._w_status.update(loading_line(message.rstrip("…").lower()))
        self._w_progress.update(total=None)
        self._w_meter.update_levels(None)

    def answer(self, result) -> None:
        # The flow quits this controller next; stop polling and steering it.
        self.controller = None
        super().answer(result)

    # --- controller polling ---------------------------------------------

    @work(thread=True, exclusive=True)
    def _poll(self) -> None:
        worker = get_current_worker()
        tick = 0
        while not worker.is_cancelled:
            controller = self.controller
            if controller is not None:
                state = self._read(controller, tick % STATE_EVERY == 0, tick % STATS_EVERY == 0)
                try:
                    self.app.call_from_thread(self._apply, controller, state)
                except RuntimeError:  # the app is shutting down
                    return
            tick += 1
            time.sleep(POLL_SECONDS)

    @staticmethod
    def _read(controller, state: bool, stats: bool):
        """A poll of the controller: 'ended' once mpv exits, 'error' if it fails."""
        try:
            if not controller.is_alive():
                return "ended"
            read = {"position": controller.get_position()}
            # Controllers without a level meter (or stream stats) just skip them.
            if hasattr(controller, "get_levels"):
                read["levels"] = controller.get_levels()
            if state:
                read.update(
                    duration=controller.get_duration(),
                    paused=controller.is_paused(),
                    volume=controller.get_volume(),
                    muted=controller.is_muted(),
                )
            if stats and hasattr(controller, "get_stats"):
                read["stats"] = controller.get_stats()
            return read
        except Exception:
            return "error"

    def _apply(self, controller, read) -> None:
        if controller is not self.controller:
            return  # a read of the previous track that landed late
        if read == "ended":
            self.answer("next_track" if self.playlist_info.get("has_next") else "menu")
            return
        if read == "error":
            self.answer("menu")
            return
        self._state.update(read)
        s = self._state
        paused = s.get("paused", False)
        self._w_elapsed.update(format_time(s["position"]))
        if "duration" in read:
            self._w_duration.update(format_time(s["duration"]) if s["duration"] > 0 else "--:--")
            self._w_status.update(status_line(paused))
            self._w_caps.set_tail(volume_gauge(s["volume"], s["muted"]))
        # Unknown duration (live streams) → Textual's indeterminate pulse.
        if s.get("duration", 0) > 0:
            self._w_progress.update(total=s["duration"], progress=s["position"])
            # The inner bar rounds its fill to its width, and one set before the
            # first layout rounds to nothing; while the position stands still
            # (paused) nothing would set it again, so hand it over each poll.
            self._w_bar.percentage = self._w_progress.percentage
        if "levels" in read:
            levels = None if paused else read["levels"]
            self._w_meter.update_levels(levels)
            loudest = max((rms for rms, _ in levels or ()), default=float("-inf"))
            self._w_level_graph.push(min(max(1 - loudest / GRAPH_FLOOR, 0.0), 1.0))
        if "stats" in read:
            self._w_stream.update(stream_lines(read["stats"]))
            self._w_net_graph.push(read["stats"].get("speed") or 0)

    # --- actions ---------------------------------------------------------

    def check_action(self, action: str, parameters: tuple):
        flag = {"next": "has_next", "prev": "has_prev"}.get(action)
        if flag is None:
            return True
        if not self.playlist_info:
            return False  # a single track: no next/prev caps at all
        return True if self.playlist_info.get(flag) else None  # None = shown disabled

    def action_toggle_pause(self) -> None:
        if self.controller:
            self.controller.toggle_pause()

    def action_seek(self, seconds: float) -> None:
        if self.controller:
            self.controller.seek(seconds)

    def action_volume(self, delta: int) -> None:
        if self.controller:
            self.controller.set_volume_relative(delta)

    def action_mute(self) -> None:
        if self.controller:
            self.controller.toggle_mute()

    def action_next(self) -> None:
        self.answer("next_track")

    def action_prev(self) -> None:
        self.answer("prev_track")

    def action_help(self) -> None:
        self.app.push_screen(HelpModal(("Player", "Everywhere")))

    def action_back(self) -> None:
        # Escape first leaves the queue; only then does it mean "back to menu".
        if isinstance(self.focused, OptionList):
            self.set_focus(None)
            return
        self.answer("menu")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index != self.current_index:
            self.answer(f"jump_track:{event.option_index}")

    # --- mouse -----------------------------------------------------------

    def _under_mouse(self, event):
        try:
            widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except Exception:
            return None
        return widget

    def _scroll_volume(self, event, delta: int) -> None:
        widget = self._under_mouse(event)
        if widget is not None and any(n.id == "queue" for n in widget.ancestors_with_self):
            return  # the wheel scrolls the queue, even past its ends
        event.stop()
        self.action_volume(delta)

    def on_mouse_scroll_up(self, event) -> None:
        self._scroll_volume(event, 5)

    def on_mouse_scroll_down(self, event) -> None:
        self._scroll_volume(event, -5)

    def on_key_caps_tail_clicked(self, event: KeyCaps.TailClicked) -> None:
        """A click on the volume gauge's bar sets the volume there."""
        cell = event.x - GAUGE_LEAD
        if 0 <= cell < VOLUME_BAR and "volume" in self._state:
            target = (cell + 1) * 100 / VOLUME_BAR
            self.action_volume(target - self._state["volume"])

    def on_click(self, event) -> None:
        """Click the progress bar to seek there."""
        widget = self._under_mouse(event)
        if widget is None or self._state.get("duration", 0) <= 0:
            return
        if not any(n.id == "progress" for n in widget.ancestors_with_self):
            return
        bar = widget.region
        fraction = min(max((event.screen_x - bar.x) / max(bar.width - 1, 1), 0.0), 1.0)
        target = fraction * self._state["duration"]
        self.action_seek(target - self._state["position"])
