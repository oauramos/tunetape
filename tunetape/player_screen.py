"""The player, btop style: now playing, live audio levels, stream stats, queue.

The daemon plays; this screen shows what it is doing and steers it. It polls
``player.status`` (docs/protocol.md §4.2) at the level meter's pace, follows
the daemon from track to track in place — whether the track ended, a key here
moved it, or another client did — and answers 'menu' (b, or the queue ran
out) or 'quit'. Leaving it never stops the music; ``__main__`` does that on
quit.
"""

import queue
import time

from textual import work
from textual.binding import Binding
from textual.color import Gradient
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import OptionList, ProgressBar, Static
from textual.worker import get_current_worker

from tunetape import debug
from tunetape.client import DaemonError, DaemonUnavailable
from tunetape.screens import NAV, FlowScreen, HelpModal
from tunetape.widgets import Cap, Graph, KeyCaps, LevelMeter, Marquee, TopBar

# One player.status per tick, at the level meter's pace; stream stats ride
# along every STATS_EVERY ticks (once a second).
POLL_SECONDS = 0.1
STATS_EVERY = 10
# Narrower than this, the player's caps show just their keys (space, ← →, m…).
KEYS_ONLY_BELOW = 96
# The level graph spans a tighter range than the meter's bars, so the swells
# of a song show as hills instead of a flat block.
GRAPH_FLOOR = -36.0
# The badge in the top bar, by the kind of the item playing (protocol §3.1).
SOURCE_NAMES = {
    "khinsider": "KHInsider", "spotify": "Spotify", "youtube_playlist": "YouTube",
    "youtube": "YouTube", "search": "YouTube", "file": "Local file",
    "direct": "Web", "generic": "Web",
}


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


def item_title(item) -> str:
    """What to call a queue item: its title once known, else its input (protocol §3.3)."""
    if not item:
        return ""
    return item.get("title") or item.get("input") or ""


def _loading_text(message) -> str:
    """'Loading track 5/64…' → 'loading track 5/64', for the loading line.

    Only the first letter is lowered: the daemon's message may carry a title.
    """
    text = (message or "loading").rstrip("…").strip() or "loading"
    return text[:1].lower() + text[1:]


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
    """Now playing with its controls, level and stream panels, and the queue.

    ``client`` is the daemon connection (``tunetape.client.Client``, or the
    demo's ``FakeClient``); ``status`` and ``items`` are a first
    ``player.status`` and ``queue.list`` so the screen opens fully drawn.
    """

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

    def __init__(self, client, status: dict = None, items: list = None):
        super().__init__()
        self.client = client
        self.status = dict(status or {})
        self.items = list(items or [])
        self.notice = None  # why the daemon stopped (a failure), for ui.play to show
        self.lost = None    # the reason, when the daemon went away mid-play
        # Controls go to the poll thread, which sends them in order between polls.
        self._commands = queue.Queue()
        self._shown = None  # the track the screen is drawn for (see _track_key)
        self._drawn = {}    # what each live widget shows, so a poll repaints only changes
        # Failures already in the queue when the screen opens were someone
        # else's news; only the ones that happen while it is up are toasted.
        self._reported = {it.get("id") for it in self.items if it.get("status") == "failed"}

    @property
    def current(self) -> dict:
        return self.status.get("current") or {}

    @property
    def current_index(self) -> int:
        return self.status.get("index", 0)

    @staticmethod
    def _track_key(status: dict) -> tuple:
        current = status.get("current") or {}
        return current.get("id"), status.get("index"), status.get("length")

    # --- layout ----------------------------------------------------------

    def compose(self):
        yield TopBar(right=self._source_badge())
        with Horizontal(id="player-body"):
            with Vertical(id="deck"):
                with Vertical(id="now", classes="box"):
                    with Horizontal(id="now-top"):
                        yield Marquee(id="title")
                        yield Static(loading_line(), id="status")
                    yield Static(id="subtitle")
                    with Horizontal(id="time-row"):
                        yield Static("00:00", id="elapsed")
                        yield ProgressBar(show_percentage=False, show_eta=False, id="progress")
                        yield Static("--:--", id="duration")
                    yield KeyCaps(PLAYER_CAPS, keys_only_below=KEYS_ONLY_BELOW, id="player-caps")
                with Horizontal(id="stats"):
                    with Vertical(id="level", classes="box"):
                        yield LevelMeter(id="meter")
                        yield Graph(maximum=1.0, tints=("$success", "$warning", "$error"),
                                    id="level-graph")
                    with Vertical(id="stream", classes="box"):
                        yield Static(stream_lines({}), id="stream-info")
                        yield Graph(tints=("$primary", "$accent"), id="net-graph")
            # Always there, hidden for a lone track: another client may add to
            # the queue while this screen is up.
            yield OptionList(*self._queue_rows(), id="queue", classes="box")

    def _source_badge(self) -> Content:
        source = SOURCE_NAMES.get(self.current.get("kind"))
        if not source:
            return Content("")
        return Content.from_markup("[$accent]●[/] [$foreground 60%]$source[/]", source=source)

    def _queue_rows(self) -> list:
        rows = []
        for i, item in enumerate(self.items):
            num = f"{i + 1:>3}"
            if i == self.current_index:
                markup = "[b $primary]▶ $num  $name[/]"
            elif i < self.current_index:
                markup = "[$foreground 30%]  $num  $name[/]"
            else:
                markup = "[$foreground 60%]  $num[/]  $name"
            rows.append(Content.from_markup(markup, num=num, name=item_title(item)))
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
        self._w_queue = self.query_one("#queue", OptionList)
        self._w_caps.set_tail(volume_gauge())
        self.query_one("#now").border_title = "now playing"
        self.query_one("#level").border_title = "level"
        self.query_one("#stream").border_title = "stream"
        self._w_queue.border_title = "queue"
        self._show_track()
        self.apply_gradient()
        self._apply(self.status)
        self._poll()

    def _show_track(self) -> None:
        """Fill in everything that belongs to the current track and the queue."""
        self._shown = self._track_key(self.status)
        self._drawn = {}
        current = self.current
        self.query_one("#title", Marquee).update(item_title(current))
        collection = current.get("collection") or {}
        details = [collection["title"]] if collection.get("title") else []
        length = self.status.get("length", 0)
        if length > 1:
            details.append(f"track {self.current_index + 1}/{length}")
        subtitle = self.query_one("#subtitle", Static)
        subtitle.update(Content(" · ".join(details)))
        subtitle.display = bool(details)
        self.query_one(TopBar).set_right(self._source_badge())
        self._w_elapsed.update("00:00")
        self._w_duration.update("--:--")
        self._w_progress.update(total=None)
        self._w_status.update(loading_line())
        self.query_one("#level").border_subtitle = ""
        queue_box = self._w_queue
        queue_box.set_class(length <= 1, "-single")
        queue_box.set_options(self._queue_rows())
        if self.items:
            queue_box.highlighted = min(max(self.current_index, 0), len(self.items) - 1)
            queue_box.scroll_to_highlight(top=False)
        queue_box.border_subtitle = f"{self.current_index + 1}/{length}" if length else ""
        self.refresh_bindings()
        self._w_caps.refresh(layout=True)

    def apply_gradient(self) -> None:
        """Paint the progress bar primary → accent in whatever theme is active."""
        theme = self.app.current_theme
        end = theme.accent or theme.secondary or theme.primary
        self._w_progress.gradient = Gradient.from_colors(theme.primary, end)

    # --- the flow's side -------------------------------------------------

    def set_loading(self, message: str) -> None:
        """Show the flow working on something (``ui.with_spinner``)."""
        self._drawn.pop("status", None)
        self._w_status.update(loading_line(_loading_text(message)))
        self._w_progress.update(total=None)
        self._w_meter.update_levels(None)

    def answer(self, result) -> None:
        # Stop polling and steering; the music itself plays on in the daemon.
        self.client = None
        super().answer(result)

    # --- talking to the daemon -------------------------------------------

    def _send(self, method: str, **params) -> None:
        if self.client is not None:
            self._commands.put((method, params))

    def _post(self, fn, *args) -> bool:
        try:
            self.app.call_from_thread(fn, *args)
        except RuntimeError:  # the app is shutting down
            return False
        return True

    @work(thread=True, exclusive=True)
    def _poll(self) -> None:
        """One thread for every call, so controls reach the daemon in the order pressed."""
        worker = get_current_worker()
        client = self.client
        seen = self._track_key(self.status)
        tick = 0
        due = time.monotonic()
        while not worker.is_cancelled and self.client is client:
            try:
                command = self._commands.get(timeout=max(due - time.monotonic(), 0.0))
            except queue.Empty:
                command = None
            items = None
            try:
                if command is None:
                    read = client.status(levels=True, stats=tick % STATS_EVERY == 0)
                    tick += 1
                    due = time.monotonic() + POLL_SECONDS
                else:
                    method, params = command
                    read = getattr(client, method)(**params)  # every control answers a Status
                key = self._track_key(read)
                if key != seen:
                    # A new track (here, on its own, or from another client):
                    # the queue around it may have changed too.
                    items = client.queue().get("items", [])
                    seen = key
            except DaemonUnavailable as e:
                self._post(self._lose, str(e))
                return
            except DaemonError as e:
                # Nothing to pause yet, no track after this one…: the next poll
                # shows how things really are.
                debug.log(f"Player: {e.message}", "WARN")
                continue
            if not self._post(self._apply, read, items):
                return
        # Left the screen: a key pressed on the way out (↑ then b) still counts.
        while True:
            try:
                method, params = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                getattr(client, method)(**params)
            except DaemonUnavailable:
                return
            except DaemonError:
                pass

    def _lose(self, message: str) -> None:
        if self.client is not None:
            self.lost = message
            self.answer("menu")

    def _report_failures(self, items: list) -> None:
        """The daemon skips a track that fails; say which one and why, without stopping."""
        for item in items:
            if item.get("status") == "failed" and item.get("id") not in self._reported:
                self._reported.add(item.get("id"))
                self.app.notify(item.get("error") or "Could not play this track.",
                                title=item_title(item), severity="error",
                                markup=False)  # "[Errno 2] …" is not markup

    def _repaint(self, name: str, value) -> bool:
        """True when ``value`` differs from what widget ``name`` last showed."""
        if name in self._drawn and self._drawn[name] == value:
            return False
        self._drawn[name] = value
        return True

    def _apply(self, read: dict, items: list = None) -> None:
        if self.client is None:
            return  # answered already: a poll that landed late
        state = read.get("state")
        if state == "idle":
            # The queue ran out, or playback was stopped; ``message`` says why
            # when it was a failure.
            self.notice = read.get("message")
            self.answer("menu")
            return
        if items is not None:
            self._report_failures(items)
            self.items = items
        self.status = read
        if self._track_key(read) != self._shown:
            self._show_track()

        loading, paused = state == "loading", state == "paused"
        position = read.get("position") or 0.0
        duration = read.get("duration") or 0.0
        if self._repaint("elapsed", format_time(position)):
            self._w_elapsed.update(self._drawn["elapsed"])
        if self._repaint("duration", format_time(duration) if duration > 0 else "--:--"):
            self._w_duration.update(self._drawn["duration"])
        if self._repaint("status", (state, read.get("message") if loading else None)):
            self._w_status.update(loading_line(_loading_text(read.get("message"))) if loading
                                  else status_line(paused))
        if self._repaint("volume", (read.get("volume"), read.get("muted"))):
            self._w_caps.set_tail(volume_gauge(read.get("volume"), bool(read.get("muted"))))
        # Loading, or a duration nobody knows (live streams) → Textual's
        # indeterminate pulse.
        if duration > 0 and not loading:
            self._w_progress.update(total=duration, progress=position)
            # The inner bar rounds its fill to its width, and one set before the
            # first layout rounds to nothing; while the position stands still
            # (paused) nothing would set it again, so hand it over each poll.
            self._w_bar.percentage = self._w_progress.percentage
        elif self._w_progress.total is not None:
            self._w_progress.update(total=None)
        if "levels" in read:
            levels = None if paused or loading else read["levels"]
            self._w_meter.update_levels(levels)
            loudest = max((rms for rms, _ in levels or ()), default=float("-inf"))
            self._w_level_graph.push(min(max(1 - loudest / GRAPH_FLOOR, 0.0), 1.0))
        if read.get("stats") is not None:
            self._w_stream.update(stream_lines(read["stats"]))
            self._w_net_graph.push(read["stats"].get("speed") or 0)

    # --- actions ---------------------------------------------------------

    def check_action(self, action: str, parameters: tuple):
        flag = {"next": "has_next", "prev": "has_prev"}.get(action)
        if flag is None:
            return True
        if self.status.get("length", 0) <= 1:
            return False  # a single track: no next/prev caps at all
        return True if self.status.get(flag) else None  # None = shown disabled

    def action_toggle_pause(self) -> None:
        self._send("toggle")

    def action_seek(self, seconds: float) -> None:
        self._send("seek", seconds=seconds)

    def action_volume(self, delta: int) -> None:
        self._send("volume", delta=delta)

    def action_mute(self) -> None:
        self._send("mute")

    def action_next(self) -> None:
        self._send("next")

    def action_prev(self) -> None:
        self._send("prev")

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
            self._send("jump", index=event.option_index)

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
        if 0 <= cell < VOLUME_BAR:
            self._send("volume", value=(cell + 1) * 100 / VOLUME_BAR)

    def on_click(self, event) -> None:
        """Click the progress bar to seek there."""
        widget = self._under_mouse(event)
        duration = self.status.get("duration") or 0
        if widget is None or duration <= 0:
            return
        if not any(n.id == "progress" for n in widget.ancestors_with_self):
            return
        bar = widget.region
        fraction = min(max((event.screen_x - bar.x) / max(bar.width - 1, 1), 0.0), 1.0)
        self._send("seek", seconds=fraction * duration, relative=False)
