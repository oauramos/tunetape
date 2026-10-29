"""Every tunetape screen except the player (that one lives in player_screen.py).

Screens are views only: the session flow in ``__main__`` shows one through
``ui`` and blocks until the screen ``answer()``s, so the flow reads like the
old line-by-line prompts. Each screen answers with the same values the Rich
functions it replaced used to return.
"""

import time
from typing import NamedTuple, Optional

from rich.cells import cell_len, set_cell_size
from textual import work
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, ProgressBar, Static

from tunetape import __version__
from tunetape.art import ASCII_HEIGHT, ASCII_WIDTH
from tunetape.widgets import AsciiTuna, Cap, KeyCaps, TopBar, Tuna

# The same navigation on every screen, always last and in this order:
# h help, b back, q quit. Screens with a text box can't take letters as
# keys, so there esc goes back and ctrl+c quits (both work everywhere).
HELP = Cap("h", "help", "help")
BACK = Cap("b", "back", "back")
QUIT = Cap("q", "quit", "app.quit_session")
NAV = [HELP, BACK, QUIT]
TYPING_NAV = [Cap("esc", "back", "back"), Cap("ctrl+c", "quit", "app.quit_session")]


NO_QUIT = object()  # QUIT_RESULT of screens with no "quit" answer: quitting closes the app
NO_BACK = object()  # BACK_RESULT of screens with nothing to go back to


class Answers:
    """For screens the session flow waits on (see ``ui.TunetapeApp.ask``)."""

    QUIT_RESULT = "quit"  # what the screen answers when the user quits from it

    def answer(self, result) -> None:
        self.app.answer(self, result)

    def action_quit_session(self) -> None:
        self.app.quit_session()


class FlowScreen(Answers, Screen):
    """A full screen of the flow, with the universal navigation keys."""

    BACK_RESULT = NO_BACK  # what b / esc answer
    BINDINGS = [
        Binding("b,escape", "back", show=False),
        Binding("q", "quit_session", show=False),
        Binding("h,question_mark", "help", show=False),
    ]

    def action_back(self) -> None:
        if self.BACK_RESULT is not NO_BACK:
            self.answer(self.BACK_RESULT)

    def action_help(self) -> None:
        if not isinstance(self.app.screen, HelpModal):
            self.app.push_screen(HelpModal())


class FlowModal(Answers, ModalScreen):
    pass


def card(title: str, *, subtitle: str = None, classes: str = "", **kwargs) -> Vertical:
    """The rounded panel every screen puts its content in."""
    panel = Vertical(classes=f"card {classes}".strip(), **kwargs)
    panel.border_title = title
    if subtitle:
        panel.border_subtitle = subtitle
    return panel


# --- main menu -------------------------------------------------------------

MENU_CAPS = [
    Cap("1", "Play", "pick('1')"),
    Cap("2", "History", "pick('2')"),
    Cap("3", "Discover", "pick('3')"),
    Cap("4", "Settings", "pick('4')"),
]
MENU_NAV = [Cap("m", "mcp", "pick('m')"), Cap("d", "debug", "pick('d')"),
            Cap("h", "help", "pick('h')"), QUIT]


def clip(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells, with an ellipsis when it had to be cut."""
    if cell_len(text) <= width:
        return text
    return set_cell_size(text, max(width - 1, 0)).rstrip() + "…"


class MenuScreen(FlowScreen):
    """The tuna, a welcome, and the menu buttons. Answers '1'-'4', 'm', 'h', 'd', 'q' or 'n'.

    With room for it (the art, the top bar and two rows of boxed caps) the
    welcome is the original ASCII tuna with the title inside; smaller
    terminals get the pixel tuna and the title underneath.

    While the daemon plays (from this window, or from the command line or an
    agent) a row above the buttons names the track; it, or n, answers 'n' to
    go back to the player. ``now_playing()`` is asked every NOW_EVERY seconds.
    """

    QUIT_RESULT = "q"
    NOW_EVERY = 1.0
    # ui.CSS shows the ASCII tuna only when the classes below say it fits:
    # -ascii-height with the menu alone, -ascii-tall with the now-playing row too.
    HORIZONTAL_BREAKPOINTS = [(0, "-pixel-width"), (ASCII_WIDTH + 4, "-ascii-width")]
    VERTICAL_BREAKPOINTS = [(0, "-pixel-height"), (1 + ASCII_HEIGHT + 6, "-ascii-height"),
                            (1 + ASCII_HEIGHT + 8, "-ascii-tall")]
    BINDINGS = [
        Binding("1", "pick('1')", show=False),
        Binding("2", "pick('2')", show=False),
        Binding("3", "pick('3')", show=False),
        Binding("4", "pick('4')", show=False),
        Binding("n", "pick('n')", show=False),
        Binding("h,question_mark", "pick('h')", show=False),
        Binding("d", "pick('d')", show=False),
        Binding("m", "pick('m')", show=False),
    ]

    def __init__(self, now_playing=None):
        super().__init__()
        self.now_playing = now_playing
        self.now = None  # the daemon's status while something is playing, else None

    def compose(self):
        version = Content.from_markup("[$foreground 50%]v$v[/]", v=__version__)
        yield TopBar(right=version)
        with Vertical(id="home"):
            yield AsciiTuna(id="ascii-tuna")
            yield Tuna(shine=True)
            yield Static(Content.from_markup(
                "[b $primary]Welcome to TuneTape[/]\n[$foreground 60%]Terminal Player by @oauramos[/]"
            ), id="welcome")
            yield KeyCaps([], navigable=True, align="center", id="menu-now")
            yield KeyCaps(MENU_CAPS, navigable=True, boxed=True, flat_below=22, align="center",
                          id="menu-caps")
            yield KeyCaps(MENU_NAV, navigable=True, align="center", id="menu-nav")

    def on_mount(self) -> None:
        self.query_one("#menu-now").display = False
        self.query_one("#menu-caps").focus()
        if self.now_playing is not None:
            self._check_now()
            self.set_interval(self.NOW_EVERY, self._check_now)

    @work(thread=True, exclusive=True, group="now-playing")
    def _check_now(self) -> None:
        # A socket round trip: off the app's thread, so a slow daemon never
        # stalls the menu.
        try:
            status = self.now_playing()
        except Exception:
            status = None
        try:
            self.app.call_from_thread(self._show_now, status)
        except RuntimeError:
            pass  # the app is shutting down

    def _show_now(self, status) -> None:
        active = (isinstance(status, dict) and bool(status.get("current"))
                  and status.get("state") in ("playing", "paused", "loading"))
        self.now = status if active else None
        self.set_class(active, "-now")
        self._draw_now()

    def _draw_now(self) -> None:
        try:
            row = self.query_one("#menu-now", KeyCaps)
            caps = self.query_one("#menu-caps")
        except NoMatches:
            return  # the menu was left while the daemon was asked: its widgets are gone
        if self.now is None:
            if row.has_focus:
                caps.focus()
            row.display = False
            return
        current = self.now["current"]
        glyph = {"paused": "❚❚", "loading": "…"}.get(self.now["state"], "▶")
        title = current.get("title") or current.get("input") or ""
        # The pill is " n " + label + " ", inside #home's two-column padding.
        room = self.size.width - 4 - 4 - cell_len(glyph) - 1
        label = f"{glyph} {clip(title, max(room, 8))}"
        if row.display and row.caps and row.caps[0].label == label:
            return
        row.caps = [Cap("n", label, "pick('n')")]
        row.cursor = 0
        row.display = True
        row.refresh(layout=True)

    def on_resize(self, event) -> None:
        if self.now is not None:
            self._draw_now()

    def check_action(self, action: str, parameters: tuple):
        if action == "pick" and parameters == ("n",) and self.now is None:
            return False  # nothing playing: n does nothing
        return True

    def action_pick(self, key: str) -> None:
        self.answer(key)


# --- text prompts (Play, Discover) -----------------------------------------

class PromptScreen(FlowScreen):
    """One line of input in a card. Answers the text, '' for back, 'q' to quit.

    ``detect`` (optional) turns the text typed so far into a hint shown under
    the input, e.g. which source a pasted URL belongs to.
    """

    QUIT_RESULT = "q"
    BACK_RESULT = ""

    def __init__(self, crumb: str, title: str, intro: Content, *, placeholder: str = "",
                 submit: str = "submit", note: Content = None, detect=None):
        super().__init__()
        self.crumb = crumb
        self.title_text = title
        self.intro = intro
        self.placeholder = placeholder
        self.submit_label = submit
        self.note = note or Content("")
        self.detect = detect

    def compose(self):
        yield TopBar(self.crumb)
        with Vertical(classes="body"):
            with card(self.title_text):
                yield Static(self.intro, classes="intro")
                yield Input(placeholder=self.placeholder, id="prompt-input")
                yield Static(self.note, id="prompt-note", classes="hint")
                yield KeyCaps([Cap("enter", self.submit_label, "submit"), *TYPING_NAV])

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.detect is not None:
            hint = self.detect(event.value.strip()) if event.value.strip() else None
            self.query_one("#prompt-note", Static).update(hint or self.note)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        value = self.query_one(Input).value.strip()
        if value:
            self.answer(value)


# --- lists (AI suggestions, history) ----------------------------------------

class ListScreen(FlowScreen):
    """A card holding a scrolling list. Subclasses answer from ``picked``."""

    QUIT_RESULT = ("quit", None)
    BACK_RESULT = ("back", None)
    CRUMB = ""
    CAPS: list = []

    def __init__(self, title: str, rows: list, *, subtitle: str = None, highlight: int = 0):
        super().__init__()
        self.title_text = title
        self.subtitle = subtitle
        self.rows = rows
        self.highlight = highlight

    def compose(self):
        yield TopBar(self.CRUMB)
        with Vertical(classes="body"):
            with card(self.title_text, subtitle=self.subtitle, classes="-fill"):
                yield OptionList(*self.rows, id="list")
                yield KeyCaps(self.CAPS)

    def on_mount(self) -> None:
        options = self.query_one("#list", OptionList)
        options.highlighted = min(self.highlight, len(self.rows) - 1) if self.rows else None
        options.focus()

    @property
    def current(self) -> Optional[int]:
        return self.query_one("#list", OptionList).highlighted

    def action_select(self) -> None:
        self.query_one("#list", OptionList).action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.picked(event.option_index)

    def picked(self, index: int) -> None:
        raise NotImplementedError


class SuggestionsScreen(ListScreen):
    """AI suggestions. Answers ('play', track), ('research', None), ('back', None) or quit."""

    CRUMB = "Discover"
    BINDINGS = [Binding("r", "research", show=False)]
    CAPS = [Cap("enter", "play", "select"), Cap("r", "new request", "research"), *NAV]

    def __init__(self, request: str, tracks: list):
        rows = [
            Content.from_markup("[$foreground 50%]$n[/]  $name", n=f"{i:>2}", name=t.name)
            for i, t in enumerate(tracks, 1)
        ]
        super().__init__("Suggestions", rows, subtitle=f"for “{request}”")
        self.tracks = tracks

    def picked(self, index: int) -> None:
        self.answer(("play", self.tracks[index]))

    def action_research(self) -> None:
        self.answer(("research", None))


class HistoryScreen(ListScreen):
    """Recently played. Answers ('play', entry), ('delete', url), ('clear', None) or back/quit."""

    CRUMB = "History"
    BINDINGS = [
        Binding("d,delete", "delete", show=False),
        Binding("c", "clear", show=False),
    ]
    CAPS = [Cap("enter", "play", "select"), Cap("d", "delete", "delete"),
            Cap("c", "clear all", "clear"), *NAV]

    def __init__(self, entries: list, rows: list, highlight: int = 0):
        super().__init__("Recently played", rows, subtitle=f"{len(entries)} entries",
                         highlight=highlight)
        self.entries = entries

    def picked(self, index: int) -> None:
        self.answer(("play", self.entries[index]))

    def action_delete(self) -> None:
        if self.current is not None:
            self.answer(("delete", self.entries[self.current].get("url")))

    def action_clear(self) -> None:
        self.answer(("clear", None))


# --- option lists (Settings, AI discovery) ----------------------------------

class Row(NamedTuple):
    key: str          # pressing it picks the row, like the old numbered menus
    label: str
    value: Content
    description: str = ""


class Edit(NamedTuple):
    """A row that asks for text before answering (see EditModal)."""

    title: str
    hint: Content
    placeholder: str = ""
    value: str = ""
    password: bool = False


class OptionsScreen(FlowScreen):
    """Settings-style rows: label, current value, a line of description.

    Answers (key, None) for a picked row, (key, text) for a row in ``edits``
    once its text is entered, ('b', None) for back and ('q', None) to quit.
    """

    QUIT_RESULT = ("q", None)
    BACK_RESULT = ("b", None)

    def __init__(self, crumb: str, title: str, rows: list, *, intro: Content = None,
                 edits: dict = None, highlight: int = 0):
        super().__init__()
        self.crumb = crumb
        self.title_text = title
        self.rows = rows
        self.intro = intro
        self.edits = edits or {}
        self.highlight = highlight

    def compose(self):
        width = max(len(r.label) for r in self.rows)
        options = [
            Content.from_markup("[b $accent]$key[/]  $label  ", key=r.key,
                                label=r.label.ljust(width)) + r.value
            for r in self.rows
        ]
        yield TopBar(self.crumb)
        with Vertical(classes="body"):
            with card(self.title_text):
                if self.intro:
                    yield Static(self.intro, classes="intro")
                yield OptionList(*options, id="options")
                with Vertical(classes="foot"):
                    # What the highlighted row does, kept off the rows so they
                    # stay one line each on narrow terminals.
                    yield Static(id="option-help")
                    yield KeyCaps([Cap("enter", "select", "select"), *NAV])

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        row = self.rows[event.option_index]
        self.query_one("#option-help", Static).update(Content(row.description))

    def on_mount(self) -> None:
        options = self.query_one("#options", OptionList)
        options.highlighted = min(self.highlight, len(self.rows) - 1)
        options.focus()

    @property
    def current(self) -> int:
        return self.query_one("#options", OptionList).highlighted or 0

    def on_key(self, event) -> None:
        for i, row in enumerate(self.rows):
            if event.character == row.key and row.key not in ("b", "q", "h", "?"):
                event.stop()
                self.query_one("#options", OptionList).highlighted = i
                self._pick(i)
                return

    def action_select(self) -> None:
        self.query_one("#options", OptionList).action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._pick(event.option_index)

    def _pick(self, index: int) -> None:
        key = self.rows[index].key
        edit = self.edits.get(key)
        if edit is None:
            self.answer((key, None))
            return

        def entered(text: Optional[str]) -> None:
            if text is not None:
                self.answer((key, text))

        self.app.push_screen(EditModal(edit), entered)


class EditModal(ModalScreen):
    """Asks for one line of text; dismisses with it, or None on escape."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, edit: Edit):
        super().__init__()
        self.edit = edit

    def compose(self):
        with card(self.edit.title, id="edit"):
            yield Static(self.edit.hint, classes="intro")
            yield Input(self.edit.value, placeholder=self.edit.placeholder,
                        password=self.edit.password)
            yield KeyCaps([Cap("enter", "save", "save"), Cap("esc", "cancel", "cancel")])

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_save(self) -> None:
        self.dismiss(self.query_one(Input).value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThemeScreen(ListScreen):
    """Theme picker with a live preview. Answers ('save', name), back or quit.

    Moving through the list recolours the whole app; ``ui.show_theme_picker``
    puts the original back unless one is picked.
    """

    CRUMB = "Settings"
    CAPS = [Cap("enter", "use theme", "select"), *NAV]

    def __init__(self, themes: list, current: str):
        names = [t.name for t in themes]
        rows = []
        for theme in themes:
            colors = (theme.primary, theme.secondary or theme.primary, theme.accent or theme.primary)
            swatch = [("  ", f"on {c}") for c in colors]
            mark = Content.from_markup("  [$foreground 50%]← current[/]") if theme.name == current else ""
            rows.append(Content.assemble(*swatch, "  ", theme.name, mark))
        super().__init__("Theme", rows, highlight=names.index(current) if current in names else 0)
        self.names = names

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.app.theme = self.names[event.option_index]

    def picked(self, index: int) -> None:
        self.answer(("save", self.names[index]))


# --- waiting (loading, AI check) --------------------------------------------

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class BusyScreen(FlowScreen):
    """A small box listing what the flow is doing, while it does something slow.

    Each message becomes a step: finished ones get a ✓ and how long they
    took, the current one a spinner and a running clock.
    """

    QUIT_RESULT = NO_QUIT

    def __init__(self, message: str, detail: str = ""):
        super().__init__()
        self.steps = [[message, time.monotonic(), None]]  # [message, started, ended]
        self.detail = detail
        self._tick = 0
        self._result = None  # (ok, text) once finish() is called

    def compose(self):
        yield TopBar()
        with Vertical(classes="body"):
            with Vertical(id="busy", classes="box"):
                yield Static(id="busy-steps")
                yield ProgressBar(total=None, show_percentage=False, show_eta=False,
                                  id="busy-bar")
                yield Static(id="busy-detail", classes="hint")
                yield KeyCaps([QUIT], id="busy-caps")

    def on_mount(self) -> None:
        box = self.query_one("#busy")
        box.border_title = "loading"
        self._draw()
        self.set_interval(1 / 12, self._spin)

    def _spin(self) -> None:
        self._tick += 1
        if self._result is None:
            self._draw()

    def _draw(self) -> None:
        now = time.monotonic()
        width = max(len(message.rstrip("…")) for message, _, _ in self.steps)
        lines = []
        for message, started, ended in self.steps:
            name = message.rstrip("…").ljust(width)
            seconds = f"{(ended or now) - started:.1f}s"
            if ended is None and self._result is None:
                glyph = SPINNER[self._tick % len(SPINNER)]
                lines.append(Content.from_markup(
                    "[$primary]$glyph[/] $name  [$foreground 50%]$seconds[/]",
                    glyph=glyph, name=name, seconds=seconds))
            else:
                lines.append(Content.from_markup(
                    "[$success]✓[/] [$foreground 60%]$name  $seconds[/]",
                    name=name, seconds=seconds))
        if self._result is not None:
            ok, text = self._result
            lines.append(Content.from_markup(
                "[b $success]✓ Connected[/] [$foreground 60%]· $text responded.[/]" if ok
                else "[b $warning]✗ $text[/]", text=text,
            ))
        self.query_one("#busy-steps", Static).update(Content("\n").join(lines))
        detail = self.query_one("#busy-detail", Static)
        detail.update(Content(self.detail))
        detail.display = bool(self.detail)

    def set_message(self, message: str, detail: str = "") -> None:
        """The flow moved on: close the current step and start ``message``."""
        now = time.monotonic()
        if self.steps[-1][2] is None:
            self.steps[-1][2] = now
        self.steps.append([message, now, None])
        self.detail, self._result = detail, None
        self.query_one("#busy-bar").display = True
        self._draw()

    def finish(self, ok: bool, text: str) -> None:
        """Hold a ✓ or ✗ under the steps instead of the spinner."""
        self.steps[-1][2] = time.monotonic()
        self._result = (ok, text)
        self.detail = ""
        self.query_one("#busy-bar").display = False
        self._draw()


# --- notices, errors, questions ---------------------------------------------

class NoticeScreen(FlowScreen):
    """Why AI discovery can't run, with a way into its settings. Answers 's', 'back' or 'quit'."""

    BACK_RESULT = "back"
    BINDINGS = [
        Binding("s", "settings", show=False),
        Binding("enter", "back", show=False),
    ]

    def __init__(self, title: str, message: str):
        super().__init__()
        self.title_text = title
        self.message = message

    def compose(self):
        yield TopBar("Discover")
        with Vertical(classes="body"):
            with card(self.title_text, classes="-warning"):
                yield Static(Content(self.message))
                yield KeyCaps([Cap("s", "AI settings", "settings"), *NAV])

    def action_settings(self) -> None:
        self.answer("s")


class ErrorModal(FlowModal):
    """An error floating over whatever screen raised it. Answers None."""

    QUIT_RESULT = NO_QUIT
    BINDINGS = [Binding("enter,escape,b,space,q", "close", show=False)]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self):
        with card("Error", id="error", classes="-error"):
            yield Static(Content(self.message))
            yield KeyCaps([Cap("enter", "ok", "close")])

    def action_close(self) -> None:
        self.answer(None)


class ConfirmModal(FlowModal):
    """A yes/no question. Answers True or False."""

    QUIT_RESULT = NO_QUIT
    BINDINGS = [
        Binding("y", "reply(True)", show=False),
        Binding("n,escape,b", "reply(False)", show=False),
    ]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self):
        with card("Confirm", id="confirm", classes="-warning"):
            yield Static(Content(self.question))
            yield KeyCaps([Cap("y", "yes", "reply(True)"), Cap("n", "no", "reply(False)")])

    def action_reply(self, yes: bool) -> None:
        self.answer(yes)


HELP_SECTIONS = {
    "Menu": [
        ("1", "Play a URL: YouTube, Spotify or KHInsider"),
        ("2", "Recently played"),
        ("3", "Discover: describe a vibe, an AI finds songs"),
        ("4", "Settings: normalization, theme, AI"),
        ("n", "Back to the player, while music plays"),
        ("m", "Connect an AI agent (MCP)"),
        ("d", "Debug / logs"),
    ],
    "Player": [
        ("space", "Play / pause"),
        ("← →", "Seek 10s (or click the bar)"),
        (", .", "Seek 30s"),
        ("↑ ↓", "Volume (or the wheel, or click the gauge)"),
        ("m", "Mute"),
        ("n p", "Next / previous track"),
        ("tab", "Browse the queue, enter plays"),
        ("b esc", "Menu; the music keeps playing"),
    ],
    "Everywhere": [
        ("h ?", "This help"),
        ("b esc", "Back (esc while typing)"),
        ("q", "Quit tunetape (stops the music)"),
        ("ctrl+c", "Quit, even while typing or loading"),
        ("ctrl+p", "Command palette: themes"),
    ],
}


class HelpModal(FlowModal):
    """The key reference. The player opens it with only its own sections."""

    QUIT_RESULT = "quit"
    BINDINGS = [
        Binding("escape,h,question_mark,b,enter", "close", show=False),
        Binding("q", "quit_session", show=False),
    ]

    def __init__(self, sections=tuple(HELP_SECTIONS)):
        super().__init__()
        self.sections = sections

    def compose(self):
        width = max(len(k) for name in self.sections for k, _ in HELP_SECTIONS[name])
        lines = []
        for name in self.sections:
            if lines:
                lines.append(Content(""))
            lines.append(Content.from_markup("[b $primary]$name[/]", name=name))
            lines.extend(
                Content.from_markup("[b $accent]$key[/]  $desc", key=k.rjust(width), desc=d)
                for k, d in HELP_SECTIONS[name]
            )
        with card("Keys", id="help"):
            with VerticalScroll():
                yield Static(Content("\n").join(lines))
            yield KeyCaps([Cap("b", "back", "close"), QUIT])

    def action_close(self) -> None:
        self.answer(None)


class DebugScreen(FlowScreen):
    """Versions, dependencies and this session's log. Answers 'c', None (back) or 'quit'."""

    BACK_RESULT = None
    BINDINGS = [Binding("c", "clear", show=False)]

    def __init__(self, about: Content, log: Content):
        super().__init__()
        self.about = about
        self.log_text = log

    def compose(self):
        yield TopBar("Debug")
        with Vertical(classes="body"):
            with card("Debug / Logs", subtitle="this session", classes="-fill"):
                yield Static(self.about, id="about", classes="intro")
                with VerticalScroll(id="log"):
                    yield Static(self.log_text)
                yield KeyCaps([Cap("c", "clear log", "clear"), *NAV])

    def on_mount(self) -> None:
        log = self.query_one("#log")
        log.focus()
        log.scroll_end(animate=False)

    def action_clear(self) -> None:
        self.answer("c")


class McpScreen(FlowScreen):
    """How to let an AI agent drive tunetape over MCP. Answers 'a' (add it to
    Claude Code), None (back) or 'quit'; c and j copy without answering."""

    BACK_RESULT = None
    BINDINGS = [
        Binding("a", "add", show=False),
        Binding("c", "copy('command')", show=False),
        Binding("j", "copy('json')", show=False),
    ]

    def __init__(self, setup):
        super().__init__()
        self.setup = setup  # mcp_setup.Setup

    def compose(self):
        from tunetape import mcp_setup

        setup = self.setup
        if setup.registered:
            status = Content.from_markup("[b $success]✓ added[/] [$foreground 50%]($scope scope)[/]",
                                         scope=setup.registered)
        elif setup.claude_found:
            status = Content.from_markup("[$foreground 60%]press[/] [b $accent]a[/] "
                                         "[$foreground 60%]to add it for you[/]")
        else:
            status = Content.from_markup("[$foreground 50%]claude not found on PATH[/]")
        heading = "[b $primary]$h[/]"
        code = "  [b $accent]$c[/]"
        lines = [
            Content.from_markup(
                "tunetape is also an MCP server: Claude Code, Claude Desktop or any MCP "
                "client can play music, queue links and skip tracks in this player."),
            Content(""),
            Content.from_markup(heading + "  ", h="Claude Code") + status,
            Content.from_markup(code, c=setup.claude_command),
            Content(""),
            Content.from_markup(heading, h="Claude Desktop, Cursor and other clients"),
            Content.from_markup("[$foreground 60%]Add this to the client's MCP config "
                                "(claude_desktop_config.json for Claude Desktop):[/]"),
            *(Content.from_markup(code, c=line) for line in setup.config_json.splitlines()),
            Content(""),
            Content.from_markup(heading, h="Then ask your agent"),
            *(Content.from_markup('  [$accent]"$e"[/]', e=e) for e in mcp_setup.EXAMPLES),
            Content(""),
            Content.from_markup("[$foreground 50%]Full guide: $d[/]", d=mcp_setup.DOCS),
        ]
        yield TopBar("MCP")
        with Vertical(classes="body"):
            with card("Connect an AI agent", classes="-fill"):
                with VerticalScroll(id="mcp"):
                    yield Static(Content("\n").join(lines))
                yield KeyCaps([Cap("a", "add to Claude Code", "add"),
                               Cap("c", "copy command", "copy('command')"),
                               Cap("j", "copy JSON", "copy('json')"), *NAV])

    def on_mount(self) -> None:
        self.query_one("#mcp").focus()

    def check_action(self, action: str, parameters: tuple):
        if action == "add":
            return self.setup.claude_found and not self.setup.registered
        return True

    def action_add(self) -> None:
        self.answer("a")

    def action_copy(self, what: str) -> None:
        from tunetape import mcp_setup

        text = self.setup.claude_command if what == "command" else self.setup.config_json
        self.app.copy_to_clipboard(text)
        mcp_setup.copy(text)
        self.app.notify("Copied the Claude Code command." if what == "command"
                        else "Copied the MCP config.")
