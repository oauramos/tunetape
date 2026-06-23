import os
import select
import shutil
import sys
import termios
import threading
import time
import tty
from datetime import datetime, timezone

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tunetape import __version__, art, config, debug, paths
from tunetape.art import ACCENT, ACCENT2


def set_accent(color: str) -> None:
    """Recolor the UI accent live across the ui + art modules.

    ACCENT is read by name at render time throughout this module (and by
    art.render_welcome), so reassigning the module globals here recolors the
    whole interface on the next redraw — no per-call plumbing needed.
    """
    if color not in art.ACCENT_COLORS:
        return
    global ACCENT
    ACCENT = color
    art.ACCENT = color

console = Console()

# Module-level terminal state for atexit restoration
_saved_terminal_settings = None
_terminal_fd = None


def save_terminal_state():
    """Save terminal settings for emergency restoration."""
    global _saved_terminal_settings, _terminal_fd
    if sys.stdin.isatty():
        _terminal_fd = sys.stdin.fileno()
        _saved_terminal_settings = termios.tcgetattr(_terminal_fd)


def restore_terminal_state():
    """Restore terminal settings (safe to call from atexit/signal)."""
    global _saved_terminal_settings, _terminal_fd
    if _saved_terminal_settings is not None and _terminal_fd is not None:
        try:
            termios.tcsetattr(_terminal_fd, termios.TCSADRAIN, _saved_terminal_settings)
        except Exception:
            pass
        # Also leave alternate screen buffer if stuck in it
        try:
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.flush()
        except Exception:
            pass


def show_header():
    """Clear screen and print styled header."""
    console.clear()
    console.print()
    console.print(f"[bold {ACCENT}]  tunetape[/bold {ACCENT}] [dim]- Terminal Audio Player[/dim]")
    console.print()


def show_welcome(frame: int = 0):
    """Clear screen and display the cassette with the title baked in."""
    console.clear()
    console.print(art.render_welcome())
    console.print()


# --- btop-style boxed key-cap buttons --------------------------------------

def key_cap(key: str, label: str) -> Panel:
    """A small boxed key-cap: the key in the top border, label inside."""
    return Panel(
        label, title=str(key), box=box.SQUARE, border_style=ACCENT,
        padding=(0, 1), expand=False,
    )


def button_row(items) -> Columns:
    """Lay a list of (key, label) caps side by side, wrapping as needed."""
    return Columns([key_cap(k, l) for k, l in items], padding=(0, 1), expand=False)


# Two rows of menu buttons: actions on top, utility keys below. A single "Play"
# entry takes any supported URL (YouTube / Spotify / KHInsider) and auto-detects
# the source; full descriptions are on the Help screen (_commands_table).
_MENU_ROW1 = [
    ("1", "Play"),
    ("2", "History"),
    ("3", "Settings"),
]
_MENU_ROW2 = [
    ("h", "Help"),
    ("d", "Debug"),
    ("q", "Quit"),
]
_MENU_ITEMS = _MENU_ROW1 + _MENU_ROW2
_MENU_KEYS = {k for k, _ in _MENU_ITEMS}


def _menu_button(key: str, label: str) -> Panel:
    return Panel(
        f"[bold {ACCENT}]{key}[/]  {label}", box=box.ROUNDED,
        border_style=ACCENT, padding=(0, 1), expand=False,
    )


def _button_row(items) -> Table:
    """One horizontal row of boxed buttons (single line, no wrapping)."""
    grid = Table.grid(padding=(0, 1))
    for _ in items:
        grid.add_column()
    grid.add_row(*[_menu_button(k, l) for k, l in items])
    return grid


def _menu_buttons_grid() -> Padding:
    """Boxed menu buttons in two rows: [1 2 3] then [h d q]."""
    rows = Group(_button_row(_MENU_ROW1), _button_row(_MENU_ROW2))
    return Padding(rows, (0, 0, 0, 8))


def _draw_menu():
    """Render the welcome cassette + boxed menu buttons (scrolling, no clipping)."""
    console.clear()
    console.print(art.render_welcome())
    console.print()
    console.print(_menu_buttons_grid())
    console.print()


def _menu_fallback() -> str:
    """Non-interactive fallback (no TTY): print once and read a line."""
    _draw_menu()
    try:
        raw = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"
    return raw[:1] if raw and raw[0] in _MENU_KEYS else "q"


def _menu_renderable(frame: int):
    """The animated menu screen: shimmering tuna + two rows of boxed buttons."""
    return Group(
        art.render_welcome(frame),
        Text(""),
        _menu_buttons_grid(),
        Text.from_markup("  [dim]› press a key[/dim]"),
    )


def main_menu_loop() -> str:
    """Animated single-key main menu (the tuna shimmers). Returns 1/2/3/4/h/d/q."""
    if not sys.stdin.isatty():
        return _menu_fallback()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    frame = 0
    try:
        tty.setraw(fd)
        # Re-enable OPOST so Rich's \n still maps to \r\n (see PlayerUI.run).
        raw_attrs = termios.tcgetattr(fd)
        raw_attrs[1] = raw_attrs[1] | termios.OPOST
        termios.tcsetattr(fd, termios.TCSANOW, raw_attrs)
        console.clear()
        with Live(console=console, refresh_per_second=15, screen=True) as live:
            while True:
                live.update(_menu_renderable(frame))
                ready, _, _ = select.select([sys.stdin], [], [], 0.07)
                if ready:
                    ch = os.read(fd, 1).decode("utf-8", errors="ignore").lower()
                    if ch == "\x1b":
                        # Drain escape sequences (arrows) so they don't match keys.
                        while select.select([sys.stdin], [], [], 0.01)[0]:
                            os.read(fd, 1)
                    elif ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
                        return "q"
                    elif ch in _MENU_KEYS:
                        return ch
                frame += 1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def prompt_url() -> str:
    """Prompt user for a media URL (YouTube / Spotify / KHInsider, auto-detected)."""
    console.print("  Paste a YouTube, Spotify, or KHInsider URL:")
    console.print("  [dim]YouTube video or playlist · Spotify track or playlist · KHInsider album[/dim]")
    console.print("  [dim]enter to play  ·  b back  ·  q quit[/dim]")
    console.print()
    try:
        url = input("  > ")
    except (EOFError, KeyboardInterrupt):
        return ""
    return url.strip()


def with_spinner(message: str, func, *args, **kwargs):
    """Run a blocking ``func`` while showing an animated spinner.

    Rich's ``console.status`` refreshes the spinner on its own thread, so it
    keeps animating while ``func`` blocks (e.g. a yt-dlp subprocess call) on the
    main thread. Returns whatever ``func`` returns; exceptions propagate.
    """
    debug.log(message)
    with console.status(f"[{ACCENT}]{message}[/{ACCENT}]", spinner="dots"):
        return func(*args, **kwargs)


def show_error(message: str):
    """Render error panel and wait for keypress."""
    debug.log(message, "ERROR")
    console.print()
    console.print(Panel(
        f"[red]{escape(message)}[/red]",
        title="[bold red]Error[/bold red]",
        border_style="red",
        padding=(1, 2),
    ))
    console.print()
    console.print("  [dim]Press Enter to go back...[/dim]")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def _humanize_time(iso_str) -> str:
    """Render an ISO timestamp as a coarse 'time ago' string."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


# Short source tags shown in the Recently-played list.
_HISTORY_TAGS = {
    "youtube": "YT",
    "youtube_playlist": "PL",
    "spotify": "SP",
    "khinsider": "KH",
}


def show_history(entries: list) -> tuple:
    """Render the recently-played list and return an (action, payload) tuple.

    action is one of:
      'play'   -> payload is the selected entry dict
      'delete' -> payload is the url to remove
      'clear'  -> payload is None
      'back'   -> payload is None
    """
    console.print()
    console.print(f"  [bold {ACCENT}]Recently played[/bold {ACCENT}]")
    console.print()
    for i, e in enumerate(entries, 1):
        tag = _HISTORY_TAGS.get(e.get("type"), "??")
        title = escape(str(e.get("title", "Unknown")))
        plays = e.get("play_count", 1)
        when = _humanize_time(e.get("last_played"))
        extra = ""
        tc = e.get("track_count")
        if isinstance(tc, int) and tc > 1:
            li = int(e.get("last_index", 0) or 0)
            extra = f" [dim]· resume {li + 1}/{tc}[/dim]"
        console.print(
            f"  [bold]{i:>2}.[/bold] [dim]\\[{tag}][/dim] {title}{extra}"
            f"  [dim]· {plays}× · {when}[/dim]"
        )
    console.print()
    console.print(
        "  [dim]number to play  ·  d <n> delete  ·  c clear all  ·  b back  ·  q quit[/dim]"
    )
    console.print()
    while True:
        try:
            raw = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ("back", None)
        if raw == "q":
            return ("quit", None)
        if raw in ("b", ""):
            return ("back", None)
        if raw == "c":
            return ("clear", None)
        if raw.startswith("d"):
            num = raw[1:].strip()
            if num.isdigit():
                idx = int(num) - 1
                if 0 <= idx < len(entries):
                    return ("delete", entries[idx].get("url"))
        elif raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(entries):
                return ("play", entries[idx])
        console.print("  [dim]Invalid selection. Try again.[/dim]")


def show_settings(normalize_on: bool) -> str:
    """Render the settings screen and return a choice ('1'/'2'/'b'/'q')."""
    console.print()
    console.print(f"  [bold {ACCENT}]Settings[/bold {ACCENT}]")
    console.print()
    state = "[green]on[/green]" if normalize_on else "[dim]off[/dim]"
    console.print(f"  [bold]1.[/bold] Volume normalization: {state}")
    console.print("     [dim]Evens out loudness across tracks and sources.[/dim]")
    console.print(f"  [bold]2.[/bold] Accent color: [{ACCENT}]{ACCENT}[/{ACCENT}]")
    console.print("     [dim]Recolor the whole interface — pick your vibe.[/dim]")
    console.print("  [bold]b.[/bold] Back    [bold]q.[/bold] Quit")
    console.print()
    while True:
        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "b"
        if choice == "":
            return "b"
        if choice in ("1", "2", "b", "q"):
            return choice
        console.print("  [dim]Invalid selection. Try again.[/dim]")


def show_color_picker():
    """Live accent-color picker. Returns 'quit' to quit, else None (back)."""
    while True:
        current = config.get_setting("accent_color")
        show_welcome()  # the tuna redraws in the current accent — instant preview
        console.print(f"  [bold {ACCENT}]Accent color[/bold {ACCENT}]")
        console.print("  [dim]Pick a number to recolor the interface.[/dim]")
        console.print()
        for i, c in enumerate(art.ACCENT_COLORS, 1):
            mark = "  [dim]← current[/dim]" if c == current else ""
            console.print(f"  [bold]{i}.[/bold] [{c}]{c}[/{c}] [{c}]████████[/{c}]{mark}")
        console.print("  [bold]b.[/bold] Back    [bold]q.[/bold] Quit")
        console.print()
        try:
            raw = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "q":
            return "quit"
        if raw in ("b", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(art.ACCENT_COLORS):
            chosen = art.ACCENT_COLORS[int(raw) - 1]
            set_accent(chosen)
            config.set_setting("accent_color", chosen)
        else:
            console.print("  [dim]Invalid selection.[/dim]")


def _commands_table() -> Table:
    """A two-column table of every command, grouped by screen."""
    table = Table.grid(padding=(0, 3))
    table.add_column(justify="right", style=f"bold {ACCENT}", no_wrap=True)
    table.add_column()
    rows = [
        (f"[{ACCENT2}]Menu[/]", ""),
        ("1", "Play a URL — YouTube / Spotify / KHInsider"),
        ("2 / 3", "Recently played / Settings"),
        ("d", "Debug / Logs"),
        ("", ""),
        (f"[{ACCENT2}]Player[/]", ""),
        ("space", "Play / pause"),
        ("← / →", "Seek −/+ 10s"),
        (", / .", "Seek −/+ 30s"),
        ("↑ / ↓ , + / −", "Volume"),
        ("m", "Mute"),
        ("n / p", "Next / previous track"),
        ("h", "Toggle help"),
        ("", ""),
        (f"[{ACCENT2}]Everywhere[/]", ""),
        ("b", "Back (one screen)"),
        ("q", "Quit tunetape"),
        ("enter", "Select / confirm"),
    ]
    for left, right in rows:
        table.add_row(left, right)
    return table


def show_help():
    """Render the command reference. Returns 'quit' to quit, else None (back)."""
    console.clear()
    console.print(art.render_welcome())
    console.print(Panel(
        _commands_table(),
        title=f"[bold {ACCENT}]Help · Commands[/]",
        border_style=ACCENT,
        padding=(1, 3),
        expand=False,
    ))
    console.print()
    console.print("  [dim]enter or b to go back  ·  q to quit[/dim]")
    try:
        raw = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    return "quit" if raw == "q" else None


def show_debug():
    """Render the Debug/Logs viewer. Returns 'quit' to quit, else None (back)."""
    while True:
        console.clear()
        console.print()
        console.print(f"  [bold {ACCENT}]Debug / Logs[/]  [dim]· this session[/dim]")
        console.print()
        mpv = "[green]ok[/green]" if shutil.which("mpv") else "[red]missing[/red]"
        ytdlp = "[green]ok[/green]" if shutil.which("yt-dlp") else "[red]missing[/red]"
        console.print(
            f"  [dim]tunetape[/dim] {escape(__version__)}   "
            f"[dim]mpv[/dim] {mpv}   [dim]yt-dlp[/dim] {ytdlp}"
        )
        console.print(f"  [dim]data dir[/dim] {escape(paths.data_dir())}")
        console.print()
        records = debug.entries()
        if not records:
            console.print("  [dim]No events logged yet this session.[/dim]")
        else:
            for ts, level, msg in records[-200:]:
                color = {"ERROR": "red", "WARN": "yellow", "WARNING": "yellow"}.get(level, "dim")
                try:
                    stamp = ts.astimezone().strftime("%H:%M:%S")
                except (ValueError, OSError):
                    stamp = "--:--:--"
                console.print(
                    f"  [dim]{stamp}[/dim] [{color}]{level:<5}[/{color}] {escape(msg)}"
                )
        console.print()
        console.print("  [dim]c clear  ·  b back  ·  q quit[/dim]")
        try:
            raw = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw == "q":
            return "quit"
        if raw in ("b", ""):
            return None
        if raw == "c":
            debug.clear()


def _format_time(seconds: float) -> str:
    """Format seconds as mm:ss."""
    if seconds < 0:
        seconds = 0
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def _build_progress_bar(position: float, duration: float, width: int = 40) -> str:
    """Build a text progress bar."""
    if duration <= 0:
        fraction = 0.0
    else:
        fraction = min(max(position / duration, 0.0), 1.0)
    filled = int(width * fraction)
    return f"[{ACCENT}]" + "\u2501" * filled + f"[/{ACCENT}][dim]" + "\u2501" * (width - filled) + "[/dim]"


def _volume_bar(volume: float, muted: bool, width: int = 12) -> str:
    """Build a YouTube-style volume meter: 'Vol [\u2588\u2588\u2588\u2588\u2591\u2591\u2591\u2591] 75%' markup."""
    pct = int(round(volume))
    if muted:
        bar = "[dim]" + "\u2591" * width + "[/dim]"
        return f"[dim]Vol[/dim] {bar} [yellow]muted[/yellow]"
    frac = min(max(volume / 100.0, 0.0), 1.0)
    filled = int(round(width * frac))
    bar = f"[{ACCENT}]" + "\u2588" * filled + f"[/{ACCENT}][dim]" + "\u2591" * (width - filled) + "[/dim]"
    return f"[dim]Vol[/dim] {bar} {pct}%"


class PlayerUI:
    """TUI player with keyboard controls and live display."""

    def __init__(self, title: str, controller, playlist_info: dict = None):
        # [#12] Escape Rich markup in title to prevent injection
        self.title = escape(title)
        self.controller = controller
        self.playlist_info = playlist_info
        self._result = "menu"
        self._stop = threading.Event()
        self._result_lock = threading.Lock()
        self._display_ready = threading.Event()
        self._display_error = None
        self._show_help = False

    def _set_result(self, value: str):
        """Thread-safe first-writer-wins result setter."""
        with self._result_lock:
            if not self._stop.is_set():
                self._result = value
                self._stop.set()

    def run(self) -> str:
        """Run the player UI. Returns 'menu', 'quit', 'next_track', or 'prev_track'."""
        # [#13] Check for TTY before entering raw mode
        if not sys.stdin.isatty():
            raise RuntimeError("tunetape requires an interactive terminal.")

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        # [#7] Non-daemon thread so Live.__exit__ runs on shutdown
        display_thread = threading.Thread(target=self._display_loop)
        display_thread.start()

        # Wait for display thread to enter Live context before setting raw mode.
        # This avoids a race where tty.setraw() runs before Live has initialised.
        self._display_ready.wait(timeout=3)

        try:
            tty.setraw(fd)
            # tty.setraw() disables OPOST, which stops \n → \r\n translation.
            # Rich relies on the terminal to translate \n to \r\n; without it
            # the player text staircases off-screen and looks blank.
            # Re-enable OPOST so display output renders correctly.
            raw_attrs = termios.tcgetattr(fd)
            raw_attrs[1] = raw_attrs[1] | termios.OPOST
            termios.tcsetattr(fd, termios.TCSANOW, raw_attrs)
            while not self._stop.is_set():
                # [#8] Use select with timeout so we can check _stop flag
                ready, _, _ = select.select([sys.stdin], [], [], 0.5)
                if not ready:
                    continue

                ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                if not ch:
                    break

                if ch == " ":
                    self.controller.toggle_pause()
                elif ch == "q":
                    self._set_result("quit")
                elif ch == "b":
                    self._set_result("menu")
                elif ch == "n" and self.playlist_info and self.playlist_info.get("has_next"):
                    self._set_result("next_track")
                elif ch == "p" and self.playlist_info and self.playlist_info.get("has_prev"):
                    self._set_result("prev_track")
                elif ch == ".":
                    self.controller.seek(30)
                elif ch == ",":
                    self.controller.seek(-30)
                elif ch in ("+", "="):
                    self.controller.set_volume_relative(5)
                elif ch in ("-", "_"):
                    self.controller.set_volume_relative(-5)
                elif ch == "m":
                    self.controller.toggle_mute()
                elif ch == "h":
                    self._show_help = not self._show_help
                elif ch == "\x1b":
                    # [#6] Use select with timeout to avoid blocking on bare ESC
                    esc_ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if esc_ready:
                        seq1 = os.read(fd, 1).decode("utf-8", errors="ignore")
                        if seq1 == "[":
                            seq2_ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if seq2_ready:
                                seq2 = os.read(fd, 1).decode("utf-8", errors="ignore")
                                if seq2 == "C":  # Right
                                    self.controller.seek(10)
                                elif seq2 == "D":  # Left
                                    self.controller.seek(-10)
                                elif seq2 == "A":  # Up
                                    self.controller.set_volume_relative(5)
                                elif seq2 == "B":  # Down
                                    self.controller.set_volume_relative(-5)
                elif ch == "\x03":  # Ctrl+C
                    self._set_result("quit")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        self._stop.set()
        display_thread.join(timeout=3)
        return self._result

    def _player_controls(self):
        """Boxed control buttons, or the full help table when help is toggled."""
        if self._show_help:
            return Panel(
                _commands_table(), title=f"[bold {ACCENT}]Help \u00b7 Commands[/]",
                border_style=ACCENT, padding=(0, 2), expand=False,
            )
        items = [
            ("space", "play/pause"),
            ("\u25c4 \u25ba", "seek 10s"),
            (", .", "seek 30s"),
            ("\u2191 \u2193", "volume"),
            ("m", "mute"),
        ]
        if self.playlist_info:
            if self.playlist_info.get("has_next"):
                items.append(("n", "next"))
            if self.playlist_info.get("has_prev"):
                items.append(("p", "prev"))
        items += [("h", "help"), ("b", "back"), ("q", "quit")]
        return Padding(button_row(items), (0, 0, 0, 2))

    def _display_loop(self):
        """Poll controller and render the player display."""
        # [#7] Wrap in try/finally so _stop is always set if display thread crashes
        try:
            # Force a clean console for the Live display
            console.clear()
            with Live(console=console, refresh_per_second=2, screen=True) as live:
                self._display_ready.set()
                while not self._stop.is_set():
                    if not self.controller.is_alive():
                        if self.playlist_info and self.playlist_info.get("has_next"):
                            self._set_result("next_track")
                        else:
                            self._set_result("menu")
                        break

                    try:
                        position = self.controller.get_position()
                        duration = self.controller.get_duration()
                        paused = self.controller.is_paused()
                        volume = self.controller.get_volume()
                        muted = self.controller.is_muted()
                    except Exception:
                        self._set_result("menu")
                        break

                    pos_str = _format_time(position)
                    dur_str = _format_time(duration)
                    bar = _build_progress_bar(position, duration)
                    status = "[yellow]Paused[/yellow]" if paused else "[green]Playing[/green]"
                    vol_str = _volume_bar(volume, muted)

                    track_line = ""
                    if self.playlist_info:
                        track_line = f"  [dim]Track {self.playlist_info['track_label']}[/dim]\n"

                    info = Text.from_markup(
                        f"\n  [bold {ACCENT}]tunetape[/bold {ACCENT}] [dim]\u00b7 Terminal Audio Player[/dim]\n"
                        f"\n"
                        f"  [bold]Now Playing:[/bold] {self.title}\n"
                        f"{track_line}"
                        f"\n"
                        f"  {pos_str} {bar} {dur_str}\n"
                        f"\n"
                        f"  > {status}   {vol_str}\n"
                    )
                    display = Group(info, self._player_controls())

                    live.update(display)
                    time.sleep(0.5)
        except Exception as exc:
            self._display_error = exc
            debug.exception("Player display loop crashed", exc)
        finally:
            self._display_ready.set()
            self._stop.set()
