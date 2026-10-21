"""The player screen rebuilt on Textual.

A drop-in for ``ui.PlayerUI``: it takes the same title, controller and
playlist_info, and ``run()`` returns the same result strings ('menu', 'quit',
'next_track', 'prev_track'), plus 'jump_track:<n>' when a track is picked from
the queue. ``__main__`` uses it instead of PlayerUI whenever ``textual`` is
installed, which comes with the optional extra: ``pip install 'tunetape[tui]'``.

Standalone demos (no menus):

    python -m tunetape.textual_player                 # fake single track
    python -m tunetape.textual_player --playlist      # fake album with a queue
    python -m tunetape.textual_player --url <yt-url>  # real mpv playback
"""

import argparse
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Gradient
from textual.containers import Center, Horizontal, Vertical
from textual.content import Content
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Digits, Footer, OptionList, ProgressBar, Static
from textual.widgets._footer import FooterKey
from textual.worker import get_current_worker

from tunetape.ui import _format_time

# Same cyan + magenta pair as art.ACCENT / ACCENT2, as a full Textual theme.
TUNETAPE_THEME = Theme(
    name="tunetape",
    primary="#8BE9FD",
    secondary="#BD93F9",
    accent="#FF79C6",
    foreground="#E4E6EB",
    background="#15161C",
    surface="#1C1D25",
    panel="#262836",
    success="#50FA7B",
    warning="#F1FA8C",
    error="#FF5555",
    dark=True,
)

# Tuna + tape: the cassette rides inside a tuna. Pixel art drawn two pixels per
# cell with half blocks; "." is empty, "o" outline, "b" body, "f" fin. The small
# tuna carries a shorter tape and stands in on narrow or short terminals.
TUNA_LARGE = [  # 56 x 13 cells
    ".......................o................................",
    "....................oooo..............................o.",
    "..................oofffo............................ooo.",
    ".................offffffo.........................oobo..",
    "...........oooooobbbbbbbboooo.....................obbo..",
    "........ooobbbbbbbbbbbbbbbbbboooo................obbbo..",
    "......oobbbbbbbbbbbbbbbbbbbbbbbbbooo............obbbo...",
    ".....obbbbbbbbbbbbbbbbbbbbbbbbbbbbbboo.........obbbbo...",
    "....obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbooo.....obbbbo....",
    "..oobbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo..oobbbbo.....",
    ".obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbboobbbbbbo.....",
    ".obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo.....",
    "obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo.....",
    "obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo.....",
    ".obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo.....",
    ".obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbboobbbbbbo.....",
    "..oobbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo..oobbbbo.....",
    "....obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbooo.....obbbbo....",
    ".....obbbbbbbbbbbbbbbbbbbbbbbbbbbbbboo.........obbbbo...",
    "......oobbbbbbbbbbbbbbbbbbbbbbbbbooo............obbbo...",
    "........ooobbbbbbbbbbbbbbbbbboooo................obbbo..",
    "...........oooooooooooooooooo.....................obbo..",
    "..................................................oobo..",
    "....................................................ooo.",
    "......................................................o.",
    "........................................................",
]
TUNA_SMALL = [  # 42 x 9 cells
    "...............o.......................oo.",
    ".............ooo.....................oobo.",
    ".........oooobbboooo................obbo..",
    "......ooobbbbbbbbbbbooooo..........obbbo..",
    "....oobbbbbbbbbbbbbbbbbbbooo......obbbo...",
    "...obbbbbbbbbbbbbbbbbbbbbbbboo...obbbbo...",
    "..obbbbbbbbbbbbbbbbbbbbbbbbbbbooobbbbo....",
    ".obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo....",
    "obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo.....",
    "obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo.....",
    ".obbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbo....",
    "..obbbbbbbbbbbbbbbbbbbbbbbbbbbooobbbbo....",
    "...obbbbbbbbbbbbbbbbbbbbbbbboo...obbbbo...",
    "....oobbbbbbbbbbbbbbbbbbbooo......obbbo...",
    "......ooobbbbbbbbbbbooooo..........obbbo..",
    ".........ooooooooooo................obbo..",
    ".....................................oobo.",
    ".......................................oo.",
]
# Solid theme colours: an alpha fill drawn as the foreground of a half block
# would blend with the outline behind it instead of with the background.
PIXEL_STYLES = {"o": "$primary", "b": "$primary-background-darken-2", "f": "$primary-muted"}

HUB_FRAMES = "┼╳"  # reel hub spokes, an eighth of a turn per frame
VOLUME_STEPS = "▁▂▃▄▅▆▇█"


def _fish_cells(pixels: list, eye: tuple) -> list:
    """The fish as rows of (char, style) cells, before the cassette is laid in."""
    rows = []
    for r in range(0, len(pixels), 2):
        row = []
        for top, bottom in zip(pixels[r], pixels[r + 1]):
            if top == "." and bottom == ".":
                row.append(" ")
            elif top == bottom:
                row.append(("█", PIXEL_STYLES[top]))
            elif top == ".":
                row.append(("▄", PIXEL_STYLES[bottom]))
            elif bottom == ".":
                row.append(("▀", PIXEL_STYLES[top]))
            else:
                row.append(("▀", f"{PIXEL_STYLES[top]} on {PIXEL_STYLES[bottom]}"))
        rows.append(row)
    eye_row, eye_col = eye
    rows[eye_row][eye_col] = ("●", f"$background on {PIXEL_STYLES['b']}")
    return rows


def _tape(frame: int, length: int) -> str:
    # A glint travels left (supply reel) to right (take-up reel) along the tape.
    glint = frame % (length + 2)
    return "".join(
        "[$primary]═[/]" if 0 <= i - glint + 2 < 2 else "[$foreground 35%]═[/]"
        for i in range(length)
    )


def _hub(frame: int) -> str:
    return f"[b $accent]{HUB_FRAMES[frame % len(HUB_FRAMES)]}[/]"


def _cassette_large(frame: int) -> list:
    hub = _hub(frame)
    return [
        "╭────────────────────────╮",
        "│ ╭───╮ ┌────────┐ ╭───╮ │",
        f"│ │ {hub} │ │{_tape(frame, 8)}│ │ {hub} │ │",
        "│ ╰───╯ └────────┘ ╰───╯ │",
        "╰────╱──────────────╲────╯",
    ]


def _cassette_small(frame: int) -> list:
    hub = _hub(frame)
    return [
        "╭────────────────╮",
        f"│ ({hub}) {_tape(frame, 6)} ({hub}) │",
        "╰───╱────────╲───╯",
    ]


# (pixels, eye cell, top-left cell of the cassette window, cassette drawer)
TUNA_SIZES = {
    "large": (TUNA_LARGE, (5, 3), (4, 9), _cassette_large),
    "small": (TUNA_SMALL, (3, 3), (3, 7), _cassette_small),
}


class Tuna(Static):
    """The tunetape mascot: a tuna carrying a cassette whose reels spin while playing."""

    playing = reactive(False)
    frame = reactive(0)

    def __init__(self, size: str = "large", **kwargs):
        super().__init__(**kwargs)
        pixels, eye, self._tape_at, self._cassette = TUNA_SIZES[size]
        self._cells = _fish_cells(pixels, eye)

    def on_mount(self) -> None:
        self.set_interval(0.15, self._advance)

    def _advance(self) -> None:
        if self.playing:
            self.frame += 1

    def render(self) -> Content:
        tape = [
            Content.from_markup(f"[$primary on $background]{row}[/]")
            for row in self._cassette(self.frame)
        ]
        tape_row, tape_col = self._tape_at
        tape_end = tape_col + tape[0].cell_length
        lines = []
        for r, row in enumerate(self._cells):
            k = r - tape_row
            if 0 <= k < len(tape):
                lines.append(Content.assemble(*row[:tape_col], tape[k], *row[tape_end:]))
            else:
                lines.append(Content.assemble(*row))
        return Content("\n").join(lines)


class PlayerFooter(Footer):
    """Footer that tags the keys narrow terminals can live without (help lists them)."""

    OPTIONAL_ACTIONS = {"mute", "prev", "back"}

    def compose(self) -> ComposeResult:
        for widget in super().compose():
            if isinstance(widget, FooterKey) and widget.action in self.OPTIONAL_ACTIONS:
                widget.add_class("-optional")
            yield widget


def _volume_meter(volume: float, muted: bool) -> Content:
    if muted:
        return Content.from_markup(
            "[$foreground 60%]vol[/] [$foreground 30%]$steps[/] [b $error]muted[/]",
            steps=VOLUME_STEPS,
        )
    frac = min(max(volume / 100.0, 0.0), 1.0)
    lit = round(len(VOLUME_STEPS) * frac)
    return Content.from_markup(
        "[$foreground 60%]vol[/] [$primary]$lit[/][$foreground 30%]$unlit[/] $pct%",
        lit=VOLUME_STEPS[:lit],
        unlit=VOLUME_STEPS[lit:],
        pct=str(int(round(volume))),
    )


def _status(paused: bool) -> Content:
    if paused:
        return Content.from_markup("[b $warning]❚❚  PAUSED[/]")
    return Content.from_markup("[b $success]▶  PLAYING[/]")


HELP_KEYS = [
    ("space", "Play / pause"),
    ("← →", "Seek 10s"),
    (", .", "Seek 30s"),
    ("↑ ↓  + -", "Volume"),
    ("m", "Mute"),
    ("n p", "Next / previous track"),
    ("tab", "Browse the queue (enter plays)"),
    ("ctrl+p", "Command palette: change theme"),
    ("b esc", "Back to menu"),
    ("q", "Quit"),
]


class HelpScreen(ModalScreen):
    """Key reference floating over the player (which keeps running behind it)."""

    BINDINGS = [Binding("escape,h,question_mark,q", "dismiss", "Close")]

    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 45%; }
    #help {
        width: auto;
        height: auto;
        padding: 1 3;
        background: $surface;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        width = max(len(k) for k, _ in HELP_KEYS)
        rows = [
            Content.from_markup("[b $accent]$key[/]   $desc", key=k.rjust(width), desc=d)
            for k, d in HELP_KEYS
        ]
        yield Static(Content("\n").join(rows), id="help")

    def on_mount(self) -> None:
        panel = self.query_one("#help")
        panel.border_title = "Keys"
        panel.border_subtitle = "esc to close"


class TextualPlayer(App):
    """Player screen: tuna + tape, tape counter, progress, volume and the queue."""

    TITLE = "tunetape"
    AUTO_FOCUS = None  # keep ↑/↓ on volume until the queue is focused (tab)
    # -compact swaps in the small tuna and trims the footer; the queue needs
    # deck (62) + queue (42) + gutters. Heights: the large tuna needs 27 rows in
    # all, the small one 23, and below that the player goes without a fish.
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (70, "-narrow"), (108, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (23, "-medium"), (27, "-tall")]

    CSS = """
    #topbar { height: 1; padding: 0 2; background: $panel; }
    #brand { width: auto; padding-right: 2; }
    #tagline { width: 1fr; color: $text-muted; }
    #source { width: auto; }

    #body { height: 1fr; padding: 1 2 0 2; }
    #body.-solo { align-horizontal: center; }

    #deck {
        width: 1fr;
        max-width: 84;
        height: 100%;
        padding: 0 2;
        align: center middle;
        border: round $primary 50%;
        border-title-color: $primary;
        border-title-style: bold;
    }
    #tuna-box { height: auto; margin-bottom: 1; }
    #tuna-large { width: 56; height: 13; }
    #tuna-small { width: 42; height: 9; display: none; }
    #info { height: auto; }
    #titles { width: 1fr; height: 4; align-vertical: middle; }
    #counter-box { width: 17; height: auto; }
    #counter { width: 100%; text-align: right; color: $primary; }
    #duration { width: 100%; text-align: right; color: $text-muted; }

    #title { text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
    #subtitle { color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    #track-no { color: $text-muted; }
    #subtitle.-empty, #track-no.-empty { display: none; }

    #progress { width: 100%; margin-top: 1; }
    #progress Bar { width: 1fr; }
    #progress Bar > .bar--bar { color: $primary; background: $panel; }
    #progress Bar > .bar--complete { color: $primary; background: $panel; }
    #progress Bar > .bar--indeterminate { color: $primary; background: $panel; }

    #status-row { height: 1; margin-top: 1; }
    #status { width: 1fr; }
    #volume { width: auto; }

    #queue {
        width: 40;
        height: 100%;
        margin-left: 2;
        background: $background;
        border: round $primary 25%;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
        scrollbar-size-vertical: 1;
        scrollbar-color: $primary 30%;
        scrollbar-color-hover: $primary 60%;
        scrollbar-background: $background;
        /* one line per track: YouTube titles run long */
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #queue:focus { border: round $primary; border-title-color: $primary; }

    Screen.-narrow #queue, Screen.-compact #queue { display: none; }
    Screen.-narrow FooterKey.-command-palette,
    Screen.-compact FooterKey.-command-palette { display: none; }

    Screen.-compact #body { padding: 1 1 0 1; }
    Screen.-compact #brand { width: 1fr; }
    Screen.-compact #deck { padding: 0 1; }
    Screen.-compact #tagline { display: none; }
    Screen.-compact FooterKey.-optional { display: none; }
    /* compact footer groups ("← → Seek") otherwise butt into the next key */
    #footer FooterLabel { margin-right: 1; }

    Screen.-compact #tuna-large, Screen.-medium #tuna-large { display: none; }
    Screen.-compact #tuna-small, Screen.-medium #tuna-small { display: block; }
    Screen.-short #tuna-box { display: none; }
    """

    SEEK = Binding.Group("Seek", compact=True)
    VOLUME = Binding.Group("Vol", compact=True)
    BINDINGS = [
        Binding("space", "toggle_pause", "Play/Pause"),
        Binding("left", "seek(-10)", "Back 10s", key_display="←", group=SEEK),
        Binding("right", "seek(10)", "Forward 10s", key_display="→", group=SEEK),
        Binding("comma", "seek(-30)", "Back 30s", show=False),
        Binding("full_stop", "seek(30)", "Forward 30s", show=False),
        Binding("up,plus,equals_sign", "volume(5)", "Volume up", key_display="↑", group=VOLUME),
        Binding("down,minus,underscore", "volume(-5)", "Volume down", key_display="↓",
                group=VOLUME),
        Binding("m", "mute", "Mute"),
        Binding("p", "prev", "Prev"),
        Binding("n", "next", "Next"),
        Binding("h,question_mark", "toggle_help", "Help"),
        Binding("b,escape", "back", "Back"),
        Binding("q", "finish('quit')", "Quit"),
        Binding("ctrl+c", "finish('quit')", "Quit", show=False, priority=True),
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
        self.current_index = self.playlist_info.get("index", 0)

    def run(self, *args, **kwargs) -> str:
        # An app that dies without calling exit() returns None; PlayerUI's
        # callers expect a result string, so fall back to the menu.
        return super().run(*args, **kwargs) or "menu"

    # --- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(Content.from_markup("[b $primary]◉ tunetape[/]"), id="brand")
            yield Static("Terminal Audio Player", id="tagline")
            yield Static(self._source_badge(), id="source")
        with Horizontal(id="body", classes="" if self.tracks else "-solo"):
            with Vertical(id="deck"):
                with Center(id="tuna-box"):
                    yield Tuna("large", id="tuna-large")
                    yield Tuna("small", id="tuna-small")
                with Horizontal(id="info"):
                    with Vertical(id="titles"):
                        yield Static(Text(self.track_title), id="title")
                        yield Static(Text(self.subtitle or ""), id="subtitle",
                                     classes="" if self.subtitle else "-empty")
                        label = self.playlist_info.get("track_label")
                        yield Static(f"Track {label}" if label else "", id="track-no",
                                     classes="" if label else "-empty")
                    with Vertical(id="counter-box"):
                        yield Digits("00:00", id="counter")
                        yield Static("", id="duration")
                yield ProgressBar(show_percentage=False, show_eta=False, id="progress")
                with Horizontal(id="status-row"):
                    # mpv takes a moment to open the stream before the first poll
                    yield Static(Content.from_markup("[$foreground 60%]…  LOADING[/]"), id="status")
                    yield Static("", id="volume")
            if self.tracks:
                yield OptionList(*self._queue_rows(), id="queue")
        yield PlayerFooter(compact=True, id="footer")

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
        # Poll updates must reach these even while the help modal is on top,
        # and App.query_one only searches the active screen.
        self._w_counter = self.query_one("#counter", Digits)
        self._w_duration = self.query_one("#duration", Static)
        self._w_progress = self.query_one("#progress", ProgressBar)
        self._w_status = self.query_one("#status", Static)
        self._w_volume = self.query_one("#volume", Static)
        self._w_tunas = list(self.query(Tuna))
        self.register_theme(TUNETAPE_THEME)
        self.theme = "tunetape"  # watch_theme paints the progress gradient
        self.query_one("#deck").border_title = "Now playing"
        if self.tracks:
            queue = self.query_one("#queue", OptionList)
            queue.border_title = "Queue"
            queue.border_subtitle = f"{len(self.tracks)} tracks"
            queue.highlighted = self.current_index
            queue.scroll_to_highlight(top=False)
        self._poll_controller()

    def watch_theme(self, _theme: str) -> None:
        if hasattr(self, "_w_progress"):
            self._apply_gradient()

    def _apply_gradient(self) -> None:
        """Paint the progress bar primary → accent in whatever theme is active."""
        theme = self.current_theme
        end = theme.accent or theme.secondary or theme.primary
        self._w_progress.gradient = Gradient.from_colors(theme.primary, end)

    # --- controller polling ---------------------------------------------

    @work(thread=True, exclusive=True)
    def _poll_controller(self) -> None:
        worker = get_current_worker()
        while not worker.is_cancelled:
            if not self.controller.is_alive():
                self.call_from_thread(self._track_ended)
                return
            try:
                state = {
                    "position": self.controller.get_position(),
                    "duration": self.controller.get_duration(),
                    "paused": self.controller.is_paused(),
                    "volume": self.controller.get_volume(),
                    "muted": self.controller.is_muted(),
                }
            except Exception:
                self.call_from_thread(self.exit, "menu")
                return
            self.call_from_thread(self._render_state, state)
            time.sleep(0.25)

    def _render_state(self, s: dict) -> None:
        self._w_counter.update(_format_time(s["position"]))
        self._w_duration.update(Content.from_markup("of $dur", dur=_format_time(s["duration"])))
        # Unknown duration (live streams) → Textual's indeterminate pulse.
        if s["duration"] > 0:
            self._w_progress.update(total=s["duration"], progress=s["position"])
        else:
            self._w_progress.update(total=None)
        self._w_status.update(_status(s["paused"]))
        self._w_volume.update(_volume_meter(s["volume"], s["muted"]))
        for tuna in self._w_tunas:
            tuna.playing = not s["paused"]

    def _track_ended(self) -> None:
        self.exit("next_track" if self.playlist_info.get("has_next") else "menu")

    # --- actions ---------------------------------------------------------

    def check_action(self, action: str, parameters: tuple):
        flag = {"next": "has_next", "prev": "has_prev"}.get(action)
        if flag is None:
            return True
        if not self.playlist_info:
            return False  # single track: hide next/prev from the footer
        return True if self.playlist_info.get(flag) else None  # None = shown disabled

    def action_toggle_pause(self) -> None:
        self.controller.toggle_pause()

    def action_seek(self, seconds: int) -> None:
        self.controller.seek(seconds)

    def action_volume(self, delta: int) -> None:
        self.controller.set_volume_relative(delta)

    def action_mute(self) -> None:
        self.controller.toggle_mute()

    def action_next(self) -> None:
        self.exit("next_track")

    def action_prev(self) -> None:
        self.exit("prev_track")

    def action_toggle_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_back(self) -> None:
        # Escape first leaves the queue; only then does it mean "back to menu".
        if isinstance(self.focused, OptionList):
            self.set_focus(None)
            return
        self.exit("menu")

    def action_finish(self, result: str) -> None:
        self.exit(result)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index != self.current_index:
            self.exit(f"jump_track:{event.option_index}")


# --- standalone demo -------------------------------------------------------

class FakeController:
    """Stands in for MPVController so the screen runs without mpv."""

    def __init__(self, duration: float, position: float = 0.0, volume: float = 80.0,
                 paused: bool = False, muted: bool = False, frozen: bool = False):
        self._duration = duration
        self._position = position
        self._volume = volume
        self._paused = paused
        self._muted = muted
        self._frozen = frozen  # screenshots: hold the clock still
        self._last = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        if not (self._paused or self._frozen):
            self._position = min(self._position + now - self._last, self._duration)
        self._last = now

    def toggle_pause(self):
        self._tick()
        self._paused = not self._paused

    def seek(self, seconds: float):
        self._tick()
        self._position = min(max(self._position + seconds, 0.0), self._duration)

    def set_volume_relative(self, delta: float):
        self._volume = min(max(self._volume + delta, 0.0), 100.0)

    def toggle_mute(self):
        self._muted = not self._muted

    def get_volume(self) -> float:
        return self._volume

    def is_muted(self) -> bool:
        return self._muted

    def get_position(self) -> float:
        self._tick()
        return self._position

    def get_duration(self) -> float:
        return self._duration

    def is_paused(self) -> bool:
        return self._paused

    def is_alive(self) -> bool:
        self._tick()
        return self._position < self._duration

    def quit(self):
        pass


DEMO_ALBUM = "Chrono Trigger Original Soundtrack"
DEMO_TRACKS = [
    "A Premonition", "Chrono Trigger", "Morning Sunlight", "Peaceful Days",
    "Secret of the Forest", "Guardia Millennial Fair", "Yearnings of the Wind",
    "Wind Scene", "Frog's Theme", "Robo's Theme", "Ruined World",
    "Corridors of Time", "Zeal Palace", "Schala's Theme", "Battle with Magus",
    "Silent Light", "Johnny of the Robo Gang", "Lavos's Theme",
    "Manoria Cathedral", "Courage and Pride", "Gato's Song", "Battle",
    "Good Night", "To Far Away Times",
]


def playlist_info_for(index: int, total: int) -> dict:
    return {
        "index": index,
        "track_label": f"{index + 1}/{total}",
        "has_next": index + 1 < total,
        "has_prev": index > 0,
    }


def _demo_single() -> None:
    controller = FakeController(duration=245, position=83)
    TextualPlayer("Daft Punk - Around the World (Official Video)", controller,
                  source="YouTube").run()


def _demo_playlist() -> None:
    # Mirrors __main__._play_album: the screen returns, the caller moves the
    # cursor and launches the next track.
    index, position = 11, 41.0
    while True:
        controller = FakeController(duration=150 + (index * 37) % 120, position=position)
        result = TextualPlayer(
            DEMO_TRACKS[index], controller, playlist_info_for(index, len(DEMO_TRACKS)),
            source="KHInsider", subtitle=DEMO_ALBUM, tracks=DEMO_TRACKS,
        ).run()
        position = 0.0
        if result == "next_track":
            index += 1
        elif result == "prev_track":
            index -= 1
        elif result and result.startswith("jump_track:"):
            index = int(result.split(":", 1)[1])
        else:
            return


def _play_url(url: str) -> None:
    from tunetape import config, player

    player.check_dependencies(require_ytdlp=True)
    print("Fetching stream…")
    info = player.get_stream_info(url)
    controller = player.MPVController(info["stream_url"], volume=config.get_volume())
    try:
        TextualPlayer(info["title"], controller, source="YouTube").run()
    finally:
        controller.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="tunetape Textual player prototype")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--playlist", action="store_true", help="fake album with a queue")
    group.add_argument("--url", help="play a real YouTube URL through mpv")
    args = parser.parse_args()
    if args.url:
        _play_url(args.url)
    elif args.playlist:
        _demo_playlist()
    else:
        _demo_single()


if __name__ == "__main__":
    main()
