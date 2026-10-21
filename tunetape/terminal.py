"""Emergency terminal restore for the signal and atexit handlers.

Textual puts the terminal back itself when the app exits normally. These cover
the abnormal exits (SIGTERM, a crash): put the saved tty settings back and
switch off what the app switched on, so the shell isn't left in raw mode,
in the alternate screen, or echoing mouse reports.
"""

import sys

_WINDOWS = sys.platform == "win32"

if not _WINDOWS:
    import termios

_saved_terminal_settings = None
_terminal_fd = None

# Leave the alternate screen, stop mouse reporting (all modes Textual uses),
# stop bracketed paste, and show the cursor. All no-ops if already off, and
# plain VT sequences that modern Windows consoles understand too.
_RESET = (
    "\x1b[?1049l"
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l"
    "\x1b[?2004l"
    "\x1b[?25h"
)


def save_terminal_state():
    """Save terminal settings for emergency restoration (POSIX only)."""
    global _saved_terminal_settings, _terminal_fd
    if _WINDOWS:
        return
    if sys.stdin.isatty():
        _terminal_fd = sys.stdin.fileno()
        _saved_terminal_settings = termios.tcgetattr(_terminal_fd)


def restore_terminal_state():
    """Restore terminal settings (safe to call from atexit/signal)."""
    if not _WINDOWS and _saved_terminal_settings is not None and _terminal_fd is not None:
        try:
            termios.tcsetattr(_terminal_fd, termios.TCSADRAIN, _saved_terminal_settings)
        except Exception:
            pass
    # Only write to a real terminal so a redirected stdout doesn't collect
    # stray escape sequences.
    try:
        if sys.stdout.isatty():
            sys.stdout.write(_RESET)
            sys.stdout.flush()
    except Exception:
        pass
