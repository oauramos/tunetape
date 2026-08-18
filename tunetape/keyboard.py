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
    import ctypes
    import msvcrt

    _STD_INPUT_HANDLE = -10
    _ENABLE_PROCESSED_INPUT = 0x0001  # when set, the console turns Ctrl-C into SIGINT
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
    # plain ANSI sequence and works on modern Windows consoles (VT) too. Only
    # emit it to a real terminal so a redirected/piped stdout doesn't collect a
    # stray escape sequence.
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.flush()
    except Exception:
        pass


@contextmanager
def _windows_raw_mode():
    """Disable the console's Ctrl-C processing for the duration of the block.

    With ``ENABLE_PROCESSED_INPUT`` cleared, Ctrl-C is delivered as a ``\\x03``
    keypress (decoded to :data:`INTERRUPT`) instead of raising SIGINT — matching
    POSIX raw mode so callers handle it the same way on every platform. ``msvcrt``
    already reads keypresses without echo/line buffering, so nothing else changes.
    Falls back to a no-op when stdin isn't a real console (redirected/piped).
    """
    kernel32 = ctypes.windll.kernel32
    # Explicit signatures so the pointer-sized console HANDLE isn't truncated to
    # a 32-bit int by ctypes' default c_int return/arg types on 64-bit Windows.
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetStdHandle.argtypes = [ctypes.c_uint]
    kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    mode = ctypes.c_uint()
    have_mode = bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    if have_mode:
        kernel32.SetConsoleMode(handle, mode.value & ~_ENABLE_PROCESSED_INPUT)
    try:
        yield
    finally:
        if have_mode:
            kernel32.SetConsoleMode(handle, mode.value)


@contextmanager
def raw_mode():
    """Put the terminal in single-keypress mode for the duration of the block.

    POSIX: enable raw mode but keep OPOST so Rich's ``\\n`` still maps to
    ``\\r\\n`` (otherwise output staircases). Windows: keep ``msvcrt``'s direct
    key reads but disable console Ctrl-C processing so it arrives as INTERRUPT.
    """
    if _WINDOWS:
        with _windows_raw_mode():
            yield
        return
    if not sys.stdin.isatty():
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
        raw = os.read(fd, 1)
        if not raw:
            # Empty read = EOF: the controlling terminal/pty closed. Treat it
            # like Ctrl-C so callers quit instead of spinning on a fd that
            # select() reports readable forever.
            return INTERRUPT
        ch = raw.decode("utf-8", errors="ignore")
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
