"""The player: tuna + tape, tape counter, progress, volume, key caps and the queue.

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
from textual.widgets import Digits, OptionList, ProgressBar, Static
from textual.worker import get_current_worker

from tunetape.screens import FlowScreen, HelpModal
from tunetape.widgets import Cap, KeyCaps, TopBar, Tuna

VOLUME_STEPS = "▁▂▃▄▅▆▇█"
POLL_SECONDS = 0.25
# Below this many rows the key caps shrink to one line each, which leaves the
# small tuna room on a 24-row terminal.
FLAT_CAPS_BELOW = 28


def format_time(seconds: float) -> str:
    """Format seconds as mm:ss."""
    seconds = max(seconds, 0)
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def volume_meter(volume: float, muted: bool) -> Content:
    if muted:
        return Content.from_markup(
            "[$foreground 60%]vol[/] [$foreground 30%]$steps[/] [b $error]muted[/]",
            steps=VOLUME_STEPS,
        )
    lit = round(len(VOLUME_STEPS) * min(max(volume / 100.0, 0.0), 1.0))
    return Content.from_markup(
        "[$foreground 60%]vol[/] [$primary]$lit[/][$foreground 30%]$unlit[/] $pct%",
        lit=VOLUME_STEPS[:lit], unlit=VOLUME_STEPS[lit:], pct=str(int(round(volume))),
    )


def status_line(paused: bool) -> Content:
    if paused:
        return Content.from_markup("[b $warning]❚❚  PAUSED[/]")
    return Content.from_markup("[b $success]▶  PLAYING[/]")


def loading_line(message: str = "LOADING") -> Content:
    return Content.from_markup("[$foreground 60%]…  $message[/]", message=message)


PLAYER_CAPS = [
    Cap("space", "play/pause", "toggle_pause"),
    Cap("← →", "seek 10s"),
    Cap("↑ ↓", "volume"),
    Cap("m", "mute", "mute"),
    Cap("p", "prev", "prev"),
    Cap("n", "next", "next"),
    Cap("h", "help", "help"),
    Cap("b", "back", "back"),
    Cap("q", "quit", "app.quit_session"),
]


class PlayerScreen(FlowScreen):
    """Now playing, with the album's queue alongside on wide terminals."""

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
        self._state = None  # the last poll, for click-to-seek

    @property
    def current_index(self) -> int:
        return self.playlist_info.get("index", 0)

    # --- layout ----------------------------------------------------------

    def compose(self):
        yield TopBar(right=self._source_badge())
        with Horizontal(id="player-body", classes="" if self.tracks else "-solo"):
            with Vertical(id="deck"):
                yield Tuna()
                with Horizontal(id="info"):
                    with Vertical(id="titles"):
                        yield Static(id="title")
                        yield Static(id="subtitle")
                        yield Static(id="track-no")
                    with Vertical(id="counter-box"):
                        yield Digits("00:00", id="counter")
                        yield Static(id="duration")
                yield ProgressBar(show_percentage=False, show_eta=False, id="progress")
                with Horizontal(id="status-row"):
                    # mpv takes a moment to open the stream before the first poll
                    yield Static(loading_line(), id="status")
                    yield Static(id="volume")
                yield KeyCaps(PLAYER_CAPS, flat_below=FLAT_CAPS_BELOW, id="player-caps")
            if self.tracks:
                yield OptionList(*self._queue_rows(), id="queue")

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
        self._w_counter = self.query_one("#counter", Digits)
        self._w_duration = self.query_one("#duration", Static)
        self._w_progress = self.query_one("#progress", ProgressBar)
        self._w_status = self.query_one("#status", Static)
        self._w_volume = self.query_one("#volume", Static)
        self._w_tuna = self.query_one(Tuna)
        self._w_caps = self.query_one("#player-caps", KeyCaps)
        self.query_one("#deck").border_title = "Now playing"
        if self.tracks:
            queue = self.query_one("#queue", OptionList)
            queue.border_title = "Queue"
            queue.border_subtitle = f"{len(self.tracks)} tracks"
        self._show_track()
        self.apply_gradient()
        self._poll()

    def _show_track(self) -> None:
        """Fill in everything that belongs to the current track."""
        self.query_one("#title", Static).update(Content(self.track_title))
        subtitle = self.query_one("#subtitle", Static)
        subtitle.update(Content(self.subtitle or ""))
        subtitle.display = bool(self.subtitle)
        label = self.playlist_info.get("track_label")
        track_no = self.query_one("#track-no", Static)
        track_no.update(f"Track {label}" if label else "")
        track_no.display = bool(label)
        self._w_counter.update("00:00")
        self._w_duration.update("")
        self._w_progress.update(total=None)
        self._w_status.update(loading_line())
        self._w_tuna.playing = False
        if self.tracks:
            queue = self.query_one("#queue", OptionList)
            queue.set_options(self._queue_rows())
            queue.highlighted = self.current_index
            queue.scroll_to_highlight(top=False)
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
        self._state = None
        self._show_track()

    def set_loading(self, message: str) -> None:
        """Show the flow fetching the next track (``ui.with_spinner``)."""
        self._w_status.update(loading_line(message.rstrip("…")))
        self._w_progress.update(total=None)
        self._w_tuna.playing = False

    def answer(self, result) -> None:
        # The flow quits this controller next; stop polling and steering it.
        self.controller = None
        super().answer(result)

    # --- controller polling ---------------------------------------------

    @work(thread=True, exclusive=True)
    def _poll(self) -> None:
        worker = get_current_worker()
        while not worker.is_cancelled:
            controller = self.controller
            if controller is not None:
                state = self._read(controller)
                try:
                    self.app.call_from_thread(self._apply, controller, state)
                except RuntimeError:  # the app is shutting down
                    return
            time.sleep(POLL_SECONDS)

    @staticmethod
    def _read(controller):
        """The controller's state, 'ended' once mpv exits, or 'error'."""
        try:
            if not controller.is_alive():
                return "ended"
            return {
                "position": controller.get_position(),
                "duration": controller.get_duration(),
                "paused": controller.is_paused(),
                "volume": controller.get_volume(),
                "muted": controller.is_muted(),
            }
        except Exception:
            return "error"

    def _apply(self, controller, state) -> None:
        if controller is not self.controller:
            return  # a read of the previous track that landed late
        if state == "ended":
            self.answer("next_track" if self.playlist_info.get("has_next") else "menu")
        elif state == "error":
            self.answer("menu")
        else:
            self._state = state
            self._render_state(state)

    def _render_state(self, s: dict) -> None:
        self._w_counter.update(format_time(s["position"]))
        self._w_duration.update(Content.from_markup("of $dur", dur=format_time(s["duration"])))
        # Unknown duration (live streams) → Textual's indeterminate pulse.
        if s["duration"] > 0:
            self._w_progress.update(total=s["duration"], progress=s["position"])
        else:
            self._w_progress.update(total=None)
        self._w_status.update(status_line(s["paused"]))
        self._w_volume.update(volume_meter(s["volume"], s["muted"]))
        self._w_tuna.playing = not s["paused"]

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

    def on_click(self, event) -> None:
        """Click the progress bar to seek there."""
        widget = self._under_mouse(event)
        if widget is None or self._state is None or self._state["duration"] <= 0:
            return
        if not any(n.id == "progress" for n in widget.ancestors_with_self):
            return
        bar = widget.region
        fraction = min(max((event.screen_x - bar.x) / max(bar.width - 1, 1), 0.0), 1.0)
        target = fraction * self._state["duration"]
        self.action_seek(target - self._state["position"])
