"""Cassette ("tuna") ASCII art and the shared UI palette.

The original hand-drawn cassette. ``render_cassette`` returns the raw art;
``render_welcome`` returns a styled Rich Text with the title/signature overlaid
inside the cassette's dark label window. ``render_cassette`` keeps the
``frame``/``spinning`` args for call-site compatibility (the art is static).
"""

from rich.text import Text

# Tape-deck palette, reused across the UI for a consistent look.
ACCENT = "cyan"
ACCENT2 = "magenta"

CASSETTE = (
    '                                 ,\n'
    '                               _▄▓▌\n'
    '                             ╓▓╬▄╠█▄\n'
    '                            ▄▌║▓Ñ▒╬╬▓▄_           ,▄▄▄\n'
    '                           ▓Ñ║█╬▓█╣▓▌╬▓▓▌▄     ▄▓▓Ñ▓▀"\n'
    '                   __▄▄▄▓▀▀▀▀╬╬╬╬╬╬╬╬▀▀▀▀▓██▄▄███▓▓█▓▓▄▓▓▄▓▓▓▓▓▄\n'
    '              _╓▄æ▀▀╠▄φ╠╠╠╫ÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑÑ╫▒╠╫▌          _▄▄▓æ⌐\n'
    '           ,▄▓▀╠╦╠╠╠╠╠╠╠Ä█,                                  ╫╠╫▌      ,▄▓▓╫██└\n'
    '         ,▓▓╠╠╢▓╝▓Å▌╠╩╩╩╠╠▓▌                                 ╞╠╫▌    ╓▓▓╬╣╫█`\n'
    '       ╔▓╬ª╙"└╢▓██Ñ▓╜  ▄  \'▓▌                                ╞╠╫▌   ▓▓▓▓╬▓▌\n'
    '        ╙▀▓▄╥ `╨Å▀▀^   ╙▌  ▐▌   æ▓▓▀█╬╬╣████▀▀▓█▓╬╬▓▀▓▓W     ╞╠╫█▄▓███▓╬█▀\n'
    '       ╫█▀▀▀▀▓▄"▓      ▐▌  ╢▌  ▓Ñ▓─ ┌▓▌╠████M ██▒╫█┐ ─╫▓█    ╞╣▓▌╠╬╬█▓╬█▌\n'
    '        └╙▓▓µ╔_╙"    ,▄▀ _▄█   ╙▓▓█▄█▓╠▓████▄,██▌╠▓▓▄█▓▓Ñ    ╞╬▓█╬▓▓█╬╬█µ\n'
    '            ╙▀▀▓▄▄B▀▀Ü╔░▄█╙      └╙╙╙╙╙╙╙╙╙╙╙╙╙╙╙╙╙╙╙╙"      ╞╬▓▌▀▀██▓▓╬█_\n'
    '                `"╙▀▀▀█▓▓                                    ╞╬▓▌  ╙██▓╬╬█\n'
    '                     ⁿ█╬▓▄___________________________________▓╬▓█   \'▓▓▓╬╬█_\n'
    '                     j█╬╬╬╬╬╬╣▓Φ@@@@@@@@@@@@@@@@@@@@@@@▒█╬╬╬╬╬╬▓█     └▀▓▓╬█▄\n'
    '                     ²█╬╬╬╬╬╬█▒╬╬▓▓╬╫██╬╬╬╬╬╬╬╬╬██╬╬▓▓╬▓Ñ█╬╬╬╬╬▓█        └▀▓█▓,\n'
    '                      █▓▓▓▓▓█▓▓▓▓██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓█▓▓▓█▓▓▓▓▓█▀            ```\n'
    '                              ⁿ█▓██╬▓█▄           └▀███▓█─\n'
    '                                ╙▓▓Ñ▒╫▌              ▀█▓╬█▄\n'
    '                                  ╙▓▌▓▌                └╙▀██▄\n'
    '                                     ╙▀\n'
)

# User-selectable accent colors (Settings → Accent color), for fun.
ACCENT_COLORS = [
    "cyan", "bright_cyan", "green", "yellow",
    "magenta", "bright_magenta", "blue", "red",
]

# The cassette's dark label window: columns [28, 61) are blank on rows 7-8,
# right below the top edge. The title/signature are overlaid here. The window
# is 33 wide — exactly enough for the longest line.
_WIN_START = 28
_WIN_END = 61
# row index -> (text, weight). Weight pairs with the live accent at render time.
_LABELS = {
    7: ("Welcome to TuneTape", "bold"),
    8: ("Terminal Player by @oauramos", "dim"),
}


# A soft "shine" that sweeps diagonally across the tuna when animated.
_SHIMMER_PERIOD = 120  # columns between sweeps (large -> one band at a time)
_SHIMMER_BAND = 9      # width of the bright band
_SHIMMER_SPEED = 3     # columns advanced per frame


def _char_style(col: int, row: int, frame) -> str:
    """Style one art glyph; a bright band sweeps diagonally when animated."""
    if frame is None:
        return ACCENT
    pos = (col + row - frame * _SHIMMER_SPEED) % _SHIMMER_PERIOD
    return f"bold {ACCENT}" if pos < _SHIMMER_BAND else ACCENT


def _append_art(out: Text, text: str, start_col: int, row: int, frame) -> None:
    """Append art text, grouping consecutive same-styled columns into runs."""
    if not text:
        return
    run, run_style = text[0], _char_style(start_col, row, frame)
    for j in range(1, len(text)):
        st = _char_style(start_col + j, row, frame)
        if st == run_style:
            run += text[j]
        else:
            out.append(run, style=run_style)
            run, run_style = text[j], st
    out.append(run, style=run_style)


def render_cassette(frame: int = 0, spinning: bool = False) -> str:
    """Return the raw cassette art (static; args kept for compatibility)."""
    return CASSETTE


def render_welcome(frame=None) -> Text:
    """Return the cassette as styled Rich Text, with the title/signature
    overlaid inside its dark window (alignment preserved). When ``frame`` is an
    int, a bright band sweeps diagonally across the tuna — call repeatedly with
    an increasing frame to animate. Uses the current accent color."""
    width = _WIN_END - _WIN_START
    out = Text()
    for i, line in enumerate(CASSETTE.rstrip("\n").split("\n")):
        if i in _LABELS:
            text, weight = _LABELS[i]
            _append_art(out, line[:_WIN_START], 0, i, frame)
            out.append(text.center(width), style=f"{weight} {ACCENT}")
            _append_art(out, line[_WIN_END:], _WIN_END, i, frame)
        else:
            _append_art(out, line, 0, i, frame)
        out.append("\n")
    return out
