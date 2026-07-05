"""Cross-platform single-key terminal input.

The rest of tunetape talks to this module instead of touching ``termios`` /
``msvcrt`` directly, so the platform-specific raw-mode and key-decoding logic
lives in exactly one place.

Readers return one of:
  * a single printable character (``str`` of length 1), returned verbatim
    (callers lowercase where they need to), or
  * one of the named tokens below for keys that have no single-char form, or
  * ``None`` from :func:`poll_key` when the timeout elapses with no input.

The named tokens are multi-character strings, so ``key == " "`` /
``key == "q"`` style checks in callers never collide with them.
"""

import sys
import time
from contextlib import contextmanager

# --- normalized key tokens -------------------------------------------------
UP = "<up>"
DOWN = "<down>"
LEFT = "<left>"
RIGHT = "<right>"
ENTER = "<enter>"
ESC = "<esc>"
INTERRUPT = "<interrupt>"  # Ctrl-C / Ctrl-D

_ARROWS = (UP, DOWN, LEFT, RIGHT)

_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    import msvcrt
else:
    import os
    import select
    import termios
    import tty


# --- terminal state save / restore (used by signal + atexit handlers) ------
_saved_terminal_settings = None
_terminal_fd = None


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
    # Also leave the alternate screen buffer if we're stuck in it. This is a
    # plain ANSI sequence and works on modern Windows consoles (VT) too.
    try:
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()
    except Exception:
        pass


@contextmanager
def raw_mode():
    """Put the terminal in single-keypress mode for the duration of the block.

    POSIX: enable raw mode but keep OPOST so Rich's ``\\n`` still maps to
    ``\\r\\n`` (otherwise output staircases). Windows: a no-op — ``msvcrt``
    reads keypresses directly without changing console mode.
    """
    if _WINDOWS or not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[1] = attrs[1] | termios.OPOST
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --- key reading -----------------------------------------------------------
if _WINDOWS:

    # Second byte of a Windows special-key sequence -> normalized token.
    _WIN_SPECIAL = {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT}

    def _decode_win_char(ch):
        if ch in ("\x00", "\xe0"):  # arrow / function-key prefix
            code = msvcrt.getwch()
            return _WIN_SPECIAL.get(code, ESC)
        if ch == "\x1b":
            return ESC
        if ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            return INTERRUPT
        if ch in ("\r", "\n"):
            return ENTER
        return ch

    def poll_key(timeout):
        """Wait up to ``timeout`` seconds for a key; return a token or ``None``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                return _decode_win_char(msvcrt.getwch())
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def read_key_blocking():
        """Block until a key is pressed; return its normalized token."""
        return poll_key(None)

else:

    _ESC_SEQ = {"C": RIGHT, "D": LEFT, "A": UP, "B": DOWN}

    def _decode_posix(fd):
        ch = os.read(fd, 1).decode("utf-8", errors="ignore")
        if ch == "\x1b":
            # Try to read a CSI arrow sequence (ESC [ <letter>); drain anything
            # else so stray bytes aren't read as separate keypresses.
            token = ESC
            if select.select([sys.stdin], [], [], 0.05)[0]:
                s1 = os.read(fd, 1).decode("utf-8", errors="ignore")
                if s1 == "[" and select.select([sys.stdin], [], [], 0.05)[0]:
                    s2 = os.read(fd, 1).decode("utf-8", errors="ignore")
                    token = _ESC_SEQ.get(s2, ESC)
            while select.select([sys.stdin], [], [], 0.001)[0]:
                os.read(fd, 1)
            return token
        if ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            return INTERRUPT
        if ch in ("\r", "\n"):
            return ENTER
        return ch

    def poll_key(timeout):
        """Wait up to ``timeout`` seconds for a key; return a token or ``None``.

        Must be called inside a :func:`raw_mode` block.
        """
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        return _decode_posix(sys.stdin.fileno())

    def read_key_blocking():
        """Block until a key is pressed; return its normalized token.

        Must be called inside a :func:`raw_mode` block.
        """
        return _decode_posix(sys.stdin.fileno())
