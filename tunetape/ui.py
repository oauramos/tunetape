import os
import select
import sys
import termios
import threading
import time
import tty

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

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
    console.print("[bold cyan]  tunetape[/bold cyan] [dim]- Terminal YouTube Player[/dim]")
    console.print()


def show_menu() -> str:
    """Render main menu and return user choice."""
    console.print("  [bold]1.[/bold] Play URL")
    console.print("  [bold]q.[/bold] Quit")
    console.print()
    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "q"
    return choice


def prompt_url() -> str:
    """Prompt user for a YouTube URL."""
    console.print("  Paste YouTube URL:")
    console.print()
    try:
        url = input("  > ")
    except (EOFError, KeyboardInterrupt):
        return ""
    return url.strip()


def show_loading(url: str):
    """Display status message while fetching stream info."""
    console.print("  [dim]Fetching stream...[/dim]")


def show_error(message: str):
    """Render error panel and wait for keypress."""
    console.print()
    console.print(Panel(
        f"[red]{message}[/red]",
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
    return "[cyan]" + "\u2501" * filled + "[/cyan][dim]" + "\u2501" * (width - filled) + "[/dim]"


class PlayerUI:
    """TUI player with keyboard controls and live display."""

    def __init__(self, title: str, controller):
        # [#12] Escape Rich markup in title to prevent injection
        self.title = escape(title)
        self.controller = controller
        self._result = "menu"
        self._stop = threading.Event()

    def run(self) -> str:
        """Run the player UI. Returns 'menu' or 'quit'."""
        # [#13] Check for TTY before entering raw mode
        if not sys.stdin.isatty():
            raise RuntimeError("tunetape requires an interactive terminal.")

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        # [#7] Non-daemon thread so Live.__exit__ runs on shutdown
        display_thread = threading.Thread(target=self._display_loop)
        display_thread.start()

        try:
            tty.setraw(fd)
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
                    self._result = "quit"
                    self._stop.set()
                elif ch == "b":
                    self._result = "menu"
                    self._stop.set()
                elif ch == ".":
                    self.controller.seek(30)
                elif ch == ",":
                    self.controller.seek(-30)
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
                elif ch == "\x03":  # Ctrl+C
                    self._result = "quit"
                    self._stop.set()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        self._stop.set()
        display_thread.join(timeout=3)
        return self._result

    def _display_loop(self):
        """Poll controller and render the player display."""
        time.sleep(0.1)

        # [#7] Wrap in try/finally so _stop is always set if display thread crashes
        try:
            with Live(console=console, refresh_per_second=2, screen=True) as live:
                while not self._stop.is_set():
                    if not self.controller.is_alive():
                        self._result = "menu"
                        self._stop.set()
                        break

                    try:
                        position = self.controller.get_position()
                        duration = self.controller.get_duration()
                        paused = self.controller.is_paused()
                    except Exception:
                        self._result = "menu"
                        self._stop.set()
                        break

                    pos_str = _format_time(position)
                    dur_str = _format_time(duration)
                    bar = _build_progress_bar(position, duration)
                    status = "[yellow]Paused[/yellow]" if paused else "[green]Playing[/green]"

                    display = Text.from_markup(
                        f"\n"
                        f"  [bold cyan]tunetape[/bold cyan] [dim]- Terminal YouTube Player[/dim]\n"
                        f"\n"
                        f"  [bold]Now Playing:[/bold] {self.title}\n"
                        f"\n"
                        f"  {pos_str} {bar} {dur_str}\n"
                        f"\n"
                        f"  > {status}\n"
                        f"\n"
                        f"  [dim][space] play/pause  [\u2190/\u2192] -/+10s  [,/.] -/+30s  [b] back  [q] quit[/dim]\n"
                    )

                    live.update(display)
                    time.sleep(0.5)
        except Exception:
            pass
        finally:
            self._stop.set()
