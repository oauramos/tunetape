"""The tunetape look: the Textual theme and the tuna + tape mascot.

tunetape = tuna + tape, so the mascot is a pixel-art tuna carrying a cassette.
The fish is drawn two pixels per cell with half blocks; in the grids below "."
is empty, "o" outline, "b" body and "f" fin. The grids are the source of truth
and are edited by hand. The small tuna carries a shorter tape and stands in
when the large one does not fit.
"""

from textual.content import Content
from textual.theme import Theme

# Cyan + pink on a deep navy; every screen styles itself from theme variables,
# so any Textual theme picked in Settings recolours the whole interface.
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
DEFAULT_THEME = TUNETAPE_THEME.name

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
# The same fish caught in the light, for the shine that sweeps across it.
SHINE_STYLES = {"o": "$primary-lighten-3", "b": "$primary-background", "f": "$primary"}

HUB_FRAMES = "┼╳"  # reel hub spokes, an eighth of a turn per frame


def fish_cells(pixels: list, eye: tuple, styles: dict = PIXEL_STYLES) -> list:
    """The fish as rows of cells (a space, or a (char, style) pair), no cassette yet."""
    rows = []
    for r in range(0, len(pixels), 2):
        row = []
        for top, bottom in zip(pixels[r], pixels[r + 1]):
            if top == "." and bottom == ".":
                row.append(" ")
            elif top == bottom:
                row.append(("█", styles[top]))
            elif top == ".":
                row.append(("▄", styles[bottom]))
            elif bottom == ".":
                row.append(("▀", styles[top]))
            else:
                row.append(("▀", f"{styles[top]} on {styles[bottom]}"))
        rows.append(row)
    eye_row, eye_col = eye
    rows[eye_row][eye_col] = ("●", f"$background on {styles['b']}")
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


def cassette_large(frame: int) -> list:
    hub = _hub(frame)
    return [
        "╭────────────────────────╮",
        "│ ╭───╮ ┌────────┐ ╭───╮ │",
        f"│ │ {hub} │ │{_tape(frame, 8)}│ │ {hub} │ │",
        "│ ╰───╯ └────────┘ ╰───╯ │",
        "╰────╱──────────────╲────╯",
    ]


def cassette_small(frame: int) -> list:
    hub = _hub(frame)
    return [
        "╭────────────────╮",
        f"│ ({hub}) {_tape(frame, 6)} ({hub}) │",
        "╰───╱────────╲───╯",
    ]


class TunaArt:
    """One size of the mascot, ready to draw at any animation frame."""

    def __init__(self, pixels: list, eye: tuple, tape_at: tuple, cassette):
        self.cells = fish_cells(pixels, eye)
        self.shine_cells = fish_cells(pixels, eye, SHINE_STYLES)
        self.width = len(pixels[0])
        self.height = len(self.cells)
        self.tape_at = tape_at  # top-left cell of the cassette window
        self.cassette = cassette

    def render(self, frame: int = 0, shine: int = None) -> Content:
        """The tuna with its cassette; ``shine`` is the column of a diagonal glint."""
        tape = [
            Content.from_markup(f"[$primary on $background]{row}[/]")
            for row in self.cassette(frame)
        ]
        tape_row, tape_col = self.tape_at
        tape_end = tape_col + tape[0].cell_length
        lines = []
        for r, (row, lit) in enumerate(zip(self.cells, self.shine_cells)):
            if shine is not None:
                # A band a few cells wide, leaning like light through water.
                row = [
                    lit[c] if 0 <= c + 2 * r - shine < 6 else cell
                    for c, cell in enumerate(row)
                ]
            k = r - tape_row
            if 0 <= k < len(tape):
                lines.append(Content.assemble(*row[:tape_col], tape[k], *row[tape_end:]))
            else:
                lines.append(Content.assemble(*row))
        return Content("\n").join(lines)


class CassetteArt:
    """Just the tape, for when not even the small tuna fits."""

    width, height = 18, 3

    def render(self, frame: int = 0, shine: int = None) -> Content:
        return Content("\n").join(
            Content.from_markup(f"[$primary]{row}[/]") for row in cassette_small(frame)
        )


# Biggest first: the mascot widget draws the first one that fits.
TUNA_ART = {
    "large": TunaArt(TUNA_LARGE, (5, 3), (4, 9), cassette_large),
    "small": TunaArt(TUNA_SMALL, (3, 3), (3, 7), cassette_small),
    "cassette": CassetteArt(),
}
