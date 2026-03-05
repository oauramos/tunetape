import sys
import termios
import threading
import tty

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

console = Console()


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
    """Display spinner while fetching stream info."""
    # This is called before get_stream_info; the spinner is implicit
    # since get_stream_info blocks. We just print a status message.
    console.print(f"  [dim]Fetching stream...[/dim]")


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
        self.title = title
        self.controller = controller
        self._result = "menu"
        self._running = True

    def run(self) -> str:
        """Run the player UI. Returns 'menu' or 'quit'."""
        # Save terminal state
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        display_thread = threading.Thread(target=self._display_loop, daemon=True)
        display_thread.start()

        try:
            tty.setraw(fd)
            while self._running:
                ch = sys.stdin.read(1)
                if not ch:
                    break

                if ch == " ":
                    self.controller.toggle_pause()
                elif ch == "q":
                    self._result = "quit"
                    self._running = False
                elif ch == "b":
                    self._result = "menu"
                    self._running = False
                elif ch == ".":
                    self.controller.seek(30)
                elif ch == ",":
                    self.controller.seek(-30)
                elif ch == "\x1b":
                    # Escape sequence — read arrow keys
                    seq1 = sys.stdin.read(1)
                    if seq1 == "[":
                        seq2 = sys.stdin.read(1)
                        if seq2 == "C":  # Right
                            self.controller.seek(10)
                        elif seq2 == "D":  # Left
                            self.controller.seek(-10)
                elif ch == "\x03":  # Ctrl+C
                    self._result = "quit"
                    self._running = False
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        self._running = False
        display_thread.join(timeout=2)
        return self._result

    def _display_loop(self):
        """Poll controller and render the player display."""
        # Small delay to let raw mode settle
        import time
        time.sleep(0.1)

        with Live(console=console, refresh_per_second=2, screen=True) as live:
            while self._running:
                if not self.controller.is_alive():
                    self._result = "menu"
                    self._running = False
                    break

                position = self.controller.get_position()
                duration = self.controller.get_duration()
                paused = self.controller.is_paused()

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
