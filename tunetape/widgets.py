"""Widgets shared by every tunetape screen: the mascot, the key caps, the top bar."""

from collections import deque
from typing import NamedTuple, Optional

from rich.cells import cell_len
from textual import actions
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from tunetape.art import TUNA_ART, ascii_tuna


class Tuna(Widget):
    """The tunetape mascot: a tuna carrying a cassette.

    It takes whatever height its parent leaves (up to the large tuna plus a
    blank row) and draws the biggest tuna that fits, down to a bare cassette,
    so screens never have to pick a size per breakpoint. ``playing`` spins the
    reels; ``shine`` sweeps a glint across the fish now and then.
    """

    DEFAULT_CSS = """
    Tuna {
        width: 1fr;
        height: 1fr;
        max-height: 14;
        padding-bottom: 1;
        content-align: center middle;
    }
    """

    playing = reactive(False)
    frame = reactive(0)

    def __init__(self, *, playing: bool = False, shine: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.set_reactive(Tuna.playing, playing)
        self.shine = shine
        self._reel = 0

    def on_mount(self) -> None:
        self.set_interval(0.15, self._advance)

    def _advance(self) -> None:
        if self.playing:
            self._reel += 1
        if self.playing or self.shine:
            self.frame += 1

    @property
    def variant(self) -> Optional[str]:
        """'large', 'small', 'cassette' or None: what fits the space it was given."""
        width, height = self.content_size
        for name, art in TUNA_ART.items():
            if width >= art.width and height >= art.height:
                return name
        return None

    def render(self) -> Content:
        name = self.variant
        if name is None:
            return Content("")
        art = TUNA_ART[name]
        shine = None
        if self.shine:
            # Sweep across the fish (the band leans two columns per row), then rest.
            period = art.width + 2 * art.height + 60
            shine = (self.frame * 2) % period - 2 * art.height
        return art.render(self._reel, shine)


class AsciiTuna(Widget):
    """The original hand-drawn tuna, with a shine sweeping across it."""

    DEFAULT_CSS = """
    AsciiTuna { width: 1fr; height: auto; content-align: center top; }
    """

    frame = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(1 / 15, self._advance)

    def _advance(self) -> None:
        if self.display:
            self.frame += 1

    def render(self) -> Content:
        return ascii_tuna(self.frame)


# Braille dots filled from the bottom up: left column, right column.
_LEFT_DOTS = (0x40, 0x04, 0x02, 0x01)
_RIGHT_DOTS = (0x80, 0x20, 0x10, 0x08)


def _braille(left: int, right: int) -> str:
    """A braille cell with ``left``/``right`` (0-4) dots lit from the bottom."""
    if not (left or right):
        return " "
    bits = sum(_LEFT_DOTS[:left]) + sum(_RIGHT_DOTS[:right])
    return chr(0x2800 + bits)


class Graph(Widget):
    """A btop-style scrolling area graph in braille, newest sample on the right.

    Each cell holds two samples side by side and four levels of height, and
    the rows are tinted along ``tints`` from the bottom up. Values are
    plotted against ``maximum``, or against the largest one on screen.
    """

    DEFAULT_CSS = """
    Graph { width: 1fr; height: 1fr; }
    """

    def __init__(self, *, maximum: float = None, tints=("$primary",), **kwargs):
        super().__init__(**kwargs)
        self.maximum = maximum
        self.tints = tints
        self._samples = deque(maxlen=1024)

    def push(self, value: float) -> None:
        self._samples.append(max(float(value), 0.0))
        self.refresh()

    def render(self) -> Content:
        width, height = self.content_size
        rows = graph_rows(list(self._samples), width, height, self.maximum)
        lines = []
        for row, text in enumerate(rows):
            tint = self.tints[min((height - 1 - row) * len(self.tints) // height,
                                  len(self.tints) - 1)]
            lines.append(Content.styled(text, tint))
        return Content("\n").join(lines)


def graph_rows(samples: list, width: int, height: int, maximum: float = None) -> list:
    """Rows of braille (top first) plotting the last ``2 * width`` samples."""
    if width <= 0 or height <= 0:
        return []
    shown = samples[-2 * width:]
    top = maximum or max(shown, default=0.0) or 1.0
    dots = [0] * (2 * width - len(shown)) + [
        round(min(value / top, 1.0) * height * 4) for value in shown
    ]
    rows = []
    for row in range(height):
        floor = (height - 1 - row) * 4  # dots below this row
        rows.append("".join(
            _braille(min(max(dots[2 * c] - floor, 0), 4), min(max(dots[2 * c + 1] - floor, 0), 4))
            for c in range(width)
        ))
    return rows


METER_FLOOR = -50.0  # dBFS at the left end of a level bar
_EIGHTHS = " ▏▎▍▌▋▊▉"


def _level_fraction(db: float) -> float:
    return min(max((db - METER_FLOOR) / -METER_FLOOR, 0.0), 1.0)


class LevelMeter(Widget):
    """Per-channel level bars, btop-meter style: RMS as a green → red bar,
    the recent peak as a tick that falls back slowly, and the dB figure."""

    DEFAULT_CSS = """
    LevelMeter { width: 1fr; height: auto; }
    """

    NAMES = {0: "L", 1: "R"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._levels = []  # (rms, peak) per channel
        self._held = []    # peak tick per channel

    def update_levels(self, levels) -> None:
        """New (rms, peak) dBFS per channel; None or [] means silence."""
        silence = (float("-inf"), float("-inf"))
        levels = list(levels or [silence] * (len(self._levels) or 2))
        held = []
        for i, (_, peak) in enumerate(levels):
            previous = self._held[i] if i < len(self._held) else METER_FLOOR
            held.append(max(peak, previous - 1.5))  # hold, then fall 1.5 dB a frame
        relayout = len(levels) != len(self._levels)
        self._levels, self._held = levels, held
        self.refresh(layout=relayout)

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self._levels) or 2

    def render(self) -> Content:
        width = self.content_size.width
        bar = max(width - 8, 1)  # "L " + bar + " -12.3"
        lines = []
        levels = self._levels or [(float("-inf"), float("-inf"))] * 2
        names = {0: "M"} if len(levels) == 1 else self.NAMES
        for i, (rms, _) in enumerate(levels):
            cells = _level_fraction(rms) * bar
            full, part = int(cells), int((cells - int(cells)) * 8)
            tick = min(int(_level_fraction(self._held[i]) * bar), bar - 1) if i < len(self._held) else -1
            parts = [(f"{names.get(i, str(i + 1))} ", "$foreground 60%")]
            for x in range(bar):
                if x < full:
                    char = "█"
                elif x == full and part:
                    char = _EIGHTHS[part]
                elif x == tick and tick > full:
                    char = "▕"
                else:
                    char = "·"
                at = x / bar
                color = "$success" if at < 0.6 else "$warning" if at < 0.85 else "$error"
                parts.append((char, color if char != "·" else "$foreground 15%"))
            figure = f"{rms:>5.1f}" if rms > METER_FLOOR else "  -∞ "
            parts.append((f" {figure}", "$foreground 60%"))
            lines.append(Content.assemble(*parts))
        return Content("\n").join(lines)


class Cap(NamedTuple):
    """One key cap: the key it shows, its label, and the action a click runs.

    ``action`` is run on the screen (or ``app.<name>``); a cap without one is a
    hint, like "← → seek", whose keys have no single thing to click.
    """

    key: str
    label: str
    action: Optional[str] = None


GAP = 1  # columns between caps


def balanced_rows(widths: list, available: int, gap: int = GAP) -> list:
    """Split caps into as few rows as fit ``available``, with the rows as even as possible.

    Returns lists of indices into ``widths``. A cap wider than the space gets a
    row to itself.
    """

    def greedy(limit: int) -> list:
        rows, row, used = [], [], 0
        for i, width in enumerate(widths):
            need = width if not row else used + gap + width
            if row and need > limit:
                rows.append(row)
                row, used = [i], width
            else:
                row.append(i)
                used = need
        if row:
            rows.append(row)
        return rows

    if not widths:
        return []
    count = len(greedy(available))
    # The narrowest limit that still needs no more rows than the space allows.
    low, high = max(widths), max(available, max(widths))
    while low < high:
        mid = (low + high) // 2
        if len(greedy(mid)) <= count:
            high = mid
        else:
            low = mid + 1
    return greedy(low)


# (border, key, label, fill) per look; fill is the background inside a cap.
_BOXED_LOOKS = {
    "normal": ("$primary 45%", "b $accent", "$foreground", ""),
    "hot": ("$primary", "b $accent", "b $primary", " on $primary 12%"),
    "disabled": ("$foreground 15%", "$foreground 30%", "$foreground 30%", ""),
}
_FLAT_LOOKS = {
    "normal": ("b $accent", "$foreground 80%", " on $panel"),
    "hot": ("b $accent", "b $primary", " on $primary 25%"),
    "disabled": ("$foreground 30%", "$foreground 30%", " on $panel"),
}


TAIL = -1  # the index KeyCaps gives its tail in layouts and hit boxes


class KeyCaps(Widget):
    """A block of key caps laid out in rows that wrap evenly.

    By default each cap is a one-line pill (`` q quit ``), rows start at the
    left, and a ``tail`` (say, a volume gauge) sits at the right end of the
    last row. ``boxed`` caps are little boxes with the key in the top border
    instead, falling back to pills when the terminal is shorter than
    ``flat_below`` rows. Clicking a cap runs its action; with ``navigable``
    the arrows move a cursor between caps and enter presses the one under it.
    ``None`` in ``caps`` starts a new row. Caps whose action the screen's
    ``check_action`` hides or disables are dropped or dimmed, so call
    ``refresh(layout=True)`` when that changes.
    """

    DEFAULT_CSS = """
    KeyCaps { width: 1fr; height: auto; }
    """

    BINDINGS = [
        Binding("left", "move(-1)", show=False),
        Binding("right", "move(1)", show=False),
        Binding("up", "move_row(-1)", show=False),
        Binding("down", "move_row(1)", show=False),
        Binding("enter", "press", show=False),
    ]

    cursor = reactive(-1)
    hover = reactive(-1)

    class TailClicked(Message):
        """The tail was clicked ``x`` cells from its left edge."""

        def __init__(self, x: int):
            super().__init__()
            self.x = x

    def __init__(self, caps: list, *, navigable: bool = False, boxed: bool = False,
                 flat_below: int = 0, align: str = "left", **kwargs):
        super().__init__(**kwargs)
        self.caps = list(caps)
        self.can_focus = navigable
        self.boxed = boxed
        self.flat_below = flat_below
        self.align = align
        self.tail = None  # Content drawn after the last cap
        self._hits = []  # (x0, x1, y0, y1, index) of each cap drawn last

    def on_mount(self) -> None:
        if self.can_focus:
            self.cursor = next(iter(self._pressable()), -1)

    def set_tail(self, content: Content) -> None:
        relayout = self.tail is None or self.tail.cell_length != content.cell_length
        self.tail = content
        self.refresh(layout=relayout)

    # --- state and layout --------------------------------------------------

    @property
    def flat(self) -> bool:
        # Read at layout time: a resize lays the screen out again, and the
        # height always changes with the style, so the caps repaint too.
        return not self.boxed or self.screen.size.height < self.flat_below

    def _state(self, cap: Cap) -> Optional[bool]:
        """True to draw, None to draw disabled, False to leave out."""
        if cap.action is None:
            return True
        namespace, name, params = actions.parse(cap.action)
        if namespace:
            return True
        return self.screen.check_action(name, params)

    def _pressable(self) -> list:
        return [
            i for i, cap in enumerate(self.caps)
            if cap is not None and cap.action and self._state(cap) is True
        ]

    def _cap_width(self, cap: Cap) -> int:
        key, label = cell_len(cap.key), cell_len(cap.label)
        if self.flat:
            return key + label + 3
        return max(label + 4, key + 6)

    def _layout(self, width: int) -> list:
        """Rows of (index, cap, enabled, width) that fit ``width`` columns."""
        segments, segment = [], []
        for i, cap in enumerate(self.caps):
            if cap is None:
                segments.append(segment)
                segment = []
                continue
            state = self._state(cap)
            if state is not False:
                segment.append((i, cap, state is True, self._cap_width(cap)))
        if self.tail is not None:
            # A wider gap before the tail sets it apart from the caps.
            segment.append((TAIL, None, True, self.tail.cell_length + 2))
        segments.append(segment)
        rows = []
        for segment in filter(None, segments):
            for row in balanced_rows([c[3] for c in segment], width):
                rows.append([segment[j] for j in row])
        return rows

    def get_content_width(self, container, viewport) -> int:
        # As wide as one row of everything, for auto-width parents (dialogs).
        widths = [c[3] for row in self._layout(10_000) for c in row]
        return min(sum(widths) + GAP * max(len(widths) - 1, 0), container.width)

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self._layout(width)) * (1 if self.flat else 3)

    # --- drawing -----------------------------------------------------------

    def _look(self, index: int, cap: Cap, enabled: bool) -> str:
        if not enabled:
            return "disabled"
        if cap.action and (index == self.hover or (self.has_focus and index == self.cursor)):
            return "hot"
        return "normal"

    def _draw(self, cap: Cap, width: int, look: str) -> list:
        if self.flat:
            key_style, label_style, fill = _FLAT_LOOKS[look]
            return [Content.assemble(
                (f" {cap.key} ", key_style + fill), (f"{cap.label} ", label_style + fill),
            )]
        border, key_style, label_style, fill = _BOXED_LOOKS[look]
        inner = width - 2
        title = f" {cap.key} "
        left = (inner - cell_len(title)) // 2
        right = inner - left - cell_len(title)
        pad = (inner - cell_len(cap.label)) // 2
        label = " " * pad + cap.label + " " * (inner - pad - cell_len(cap.label))
        return [
            Content.assemble(("╭" + "─" * left, border), (title, key_style),
                             ("─" * right + "╮", border)),
            Content.assemble(("│", border), (label, label_style + fill), ("│", border)),
            Content.assemble(("╰" + "─" * inner + "╯", border)),
        ]

    def render(self) -> Content:
        width = self.size.width
        height = 1 if self.flat else 3
        lines, hits = [], []
        for r, row in enumerate(self._layout(width)):
            used = sum(c[3] for c in row) + GAP * (len(row) - 1)
            x = max(0, (width - used) // 2) if self.align == "center" else 0
            parts = [[" " * x] for _ in range(height)]
            for n, (index, cap, enabled, cap_width) in enumerate(row):
                if n:
                    for part in parts:
                        part.append(" " * GAP)
                if index == TAIL:
                    # Pushed to the right edge of its row (the layout kept
                    # at least two columns free in front of it).
                    tail_width = self.tail.cell_length
                    shift = max(width - x - tail_width, 0)
                    x += shift
                    hits.append((x, x + tail_width, r * height, (r + 1) * height, TAIL))
                    for i, part in enumerate(parts):
                        part.append(" " * shift)
                        part.append(self.tail if i == height // 2 else " " * tail_width)
                    x += tail_width
                    continue
                hits.append((x, x + cap_width, r * height, (r + 1) * height, index))
                for part, piece in zip(parts, self._draw(cap, cap_width, self._look(index, cap, enabled))):
                    part.append(piece)
                x += cap_width + GAP
            lines.extend(Content.assemble(*part) for part in parts)
        self._hits = hits
        return Content("\n").join(lines)

    # --- mouse and keys ----------------------------------------------------

    def _cap_at(self, x: int, y: int):
        for x0, x1, y0, y1, index in self._hits:
            if x0 <= x < x1 and y0 <= y < y1:
                return index
        return None

    def on_mouse_move(self, event) -> None:
        index = self._cap_at(event.x, event.y)
        self.hover = index if index is not None and index in self._pressable() else -1

    def on_leave(self, event) -> None:
        self.hover = -1

    async def on_click(self, event) -> None:
        index = self._cap_at(event.x, event.y)
        if index == TAIL:
            event.stop()
            x0 = next(h[0] for h in self._hits if h[4] == TAIL)
            self.post_message(self.TailClicked(event.x - x0))
        elif index is not None:
            event.stop()
            await self.press(index)

    async def press(self, index: int) -> None:
        cap = self.caps[index]
        if cap is not None and cap.action and self._state(cap) is True:
            await self.screen.run_action(cap.action)

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def action_move(self, step: int) -> None:
        order = [h[4] for h in self._hits if h[4] in self._pressable()]
        if order:
            at = order.index(self.cursor) if self.cursor in order else 0
            self.cursor = order[(at + step) % len(order)]

    def action_move_row(self, step: int) -> None:
        """Up/down: the nearest cap in the row above or below."""
        boxes = {h[4]: h for h in self._hits if h[4] in self._pressable()}
        here = boxes.get(self.cursor)
        if here is None:
            return
        rows = sorted({box[2] for box in boxes.values()})
        target = rows.index(here[2]) + step
        if not 0 <= target < len(rows):
            # Off the top or bottom row: on to the next block of caps, if any.
            if step > 0:
                self.screen.focus_next(KeyCaps)
            else:
                self.screen.focus_previous(KeyCaps)
            return
        centre = (here[0] + here[1]) / 2
        candidates = [box for box in boxes.values() if box[2] == rows[target]]
        self.cursor = min(candidates, key=lambda box: abs((box[0] + box[1]) / 2 - centre))[4]

    async def action_press(self) -> None:
        if self.cursor >= 0:
            await self.press(self.cursor)


class TopBar(Horizontal):
    """One-line bar across the top: the brand, where you are, and a note on the right."""

    DEFAULT_CSS = """
    TopBar { height: 1; padding: 0 2; background: $panel; }
    TopBar > #brand { width: auto; }
    TopBar > #crumb {
        width: 1fr;
        padding-left: 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    TopBar > #right { width: auto; padding-left: 1; }
    """

    def __init__(self, crumb: str = "Terminal Audio Player", right=None, **kwargs):
        super().__init__(**kwargs)
        self._crumb = crumb
        self._right = right

    def compose(self):
        yield Static(Content.from_markup("[b $primary]◉ tunetape[/]"), id="brand")
        yield Static(Content(self._crumb), id="crumb")
        yield Static(self._right or "", id="right")

    def set_right(self, content) -> None:
        self.query_one("#right", Static).update(content or "")
