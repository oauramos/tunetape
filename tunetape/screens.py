"""Every tunetape screen except the player (that one lives in player_screen.py).

Screens are views only: the session flow in ``__main__`` shows one through
``ui`` and blocks until the screen ``answer()``s, so the flow reads like the
old line-by-line prompts. Each screen answers with the same values the Rich
functions it replaced used to return.
"""

import time
from typing import NamedTuple, Optional

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, ProgressBar, Static

from tunetape import __version__
from tunetape.art import ASCII_HEIGHT, ASCII_WIDTH
from tunetape.widgets import AsciiTuna, Cap, KeyCaps, TopBar, Tuna

QUIT = Cap("q", "quit", "app.quit_session")
BACK = Cap("esc", "back", "back")


NO_QUIT = object()  # QUIT_RESULT of screens with no "quit" answer: quitting closes the app


class Answers:
    """For screens the session flow waits on (see ``ui.TunetapeApp.ask``)."""

    QUIT_RESULT = "quit"  # what the screen answers when the user quits from it

    def answer(self, result) -> None:
        self.app.answer(self, result)

    def action_quit_session(self) -> None:
        self.app.quit_session()


class FlowScreen(Answers, Screen):
    pass


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
    None,
    Cap("h", "Help", "pick('h')"),
    Cap("d", "Debug", "pick('d')"),
    Cap("q", "Quit", "app.quit_session"),
]


class MenuScreen(FlowScreen):
    """The tuna, a welcome, and the menu buttons. Answers '1'-'4', 'h', 'd' or 'q'.

    With room for it (the art, the top bar and two rows of boxed caps) the
    welcome is the original ASCII tuna with the title inside; smaller
    terminals get the pixel tuna and the title underneath.
    """

    QUIT_RESULT = "q"
    # ui.CSS shows the ASCII tuna only when both classes below say it fits.
    HORIZONTAL_BREAKPOINTS = [(0, "-pixel-width"), (ASCII_WIDTH + 4, "-ascii-width")]
    VERTICAL_BREAKPOINTS = [(0, "-pixel-height"), (1 + ASCII_HEIGHT + 6, "-ascii-height")]
    BINDINGS = [
        Binding("1", "pick('1')", show=False),
        Binding("2", "pick('2')", show=False),
        Binding("3", "pick('3')", show=False),
        Binding("4", "pick('4')", show=False),
        Binding("h,question_mark", "pick('h')", show=False),
        Binding("d", "pick('d')", show=False),
        Binding("q", "quit_session", show=False),
    ]

    def compose(self):
        version = Content.from_markup("[$foreground 50%]v$v[/]", v=__version__)
        yield TopBar(right=version)
        with Vertical(id="home"):
            yield AsciiTuna(id="ascii-tuna")
            yield Tuna(shine=True)
            yield Static(Content.from_markup(
                "[b $primary]Welcome to TuneTape[/]\n[$foreground 60%]Terminal Player by @oauramos[/]"
            ), id="welcome")
            yield KeyCaps(MENU_CAPS, navigable=True, flat_below=22, id="menu-caps")

    def on_mount(self) -> None:
        self.query_one("#menu-caps").focus()

    def action_pick(self, key: str) -> None:
        self.answer(key)


# --- text prompts (Play, Discover) -----------------------------------------

class PromptScreen(FlowScreen):
    """One line of input in a card. Answers the text, '' for back, 'q' to quit.

    ``detect`` (optional) turns the text typed so far into a hint shown under
    the input, e.g. which source a pasted URL belongs to.
    """

    QUIT_RESULT = "q"
    BINDINGS = [Binding("escape", "back", show=False)]

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
                yield KeyCaps([Cap("enter", self.submit_label, "submit"), BACK,
                               Cap("ctrl+c", "quit", "app.quit_session")])

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

    def action_back(self) -> None:
        self.answer("")


# --- lists (AI suggestions, history) ----------------------------------------

class ListScreen(FlowScreen):
    """A card holding a scrolling list. Subclasses answer from ``picked``."""

    QUIT_RESULT = ("quit", None)
    BINDINGS = [
        Binding("escape,b", "back", show=False),
        Binding("q", "quit_session", show=False),
    ]
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

    def action_back(self) -> None:
        self.answer(("back", None))


class SuggestionsScreen(ListScreen):
    """AI suggestions. Answers ('play', track), ('research', None), ('back', None) or quit."""

    CRUMB = "Discover"
    BINDINGS = [Binding("r", "research", show=False)]
    CAPS = [Cap("enter", "play", "select"), Cap("r", "new request", "research"), BACK, QUIT]

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
            Cap("c", "clear all", "clear"), BACK, QUIT]

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
    BINDINGS = [
        Binding("escape,b", "back", show=False),
        Binding("q", "quit_session", show=False),
    ]

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
                    yield KeyCaps([Cap("enter", "select", "select"), BACK, QUIT])

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
            if event.character == row.key and row.key not in ("b", "q"):
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

    def action_back(self) -> None:
        self.answer(("b", None))


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
    CAPS = [Cap("enter", "use theme", "select"), BACK, QUIT]

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

    def on_mount(self) -> None:
        box = self.query_one("#busy")
        box.border_title = "loading"
        box.border_subtitle = "ctrl+c quit"
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

    BINDINGS = [
        Binding("s", "settings", show=False),
        Binding("escape,b,enter", "back", show=False),
        Binding("q", "quit_session", show=False),
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
                yield KeyCaps([Cap("s", "AI settings", "settings"), BACK, QUIT])

    def action_settings(self) -> None:
        self.answer("s")

    def action_back(self) -> None:
        self.answer("back")


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
        ("d", "Debug / logs"),
    ],
    "Player": [
        ("space", "Play / pause"),
        ("← →", "Seek 10s (or click the bar)"),
        (", .", "Seek 30s"),
        ("↑ ↓", "Volume (or the mouse wheel)"),
        ("m", "Mute"),
        ("n p", "Next / previous track"),
        ("tab", "Browse the queue, enter plays"),
    ],
    "Everywhere": [
        ("b esc", "Back"),
        ("q", "Quit tunetape"),
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
        with card("Keys", subtitle="esc to close", id="help"):
            with VerticalScroll():
                yield Static(Content("\n").join(lines))

    def action_close(self) -> None:
        self.answer(None)


class DebugScreen(FlowScreen):
    """Versions, dependencies and this session's log. Answers 'c', None (back) or 'quit'."""

    BINDINGS = [
        Binding("c", "clear", show=False),
        Binding("escape,b", "back", show=False),
        Binding("q", "quit_session", show=False),
    ]

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
                yield KeyCaps([Cap("c", "clear log", "clear"), BACK, QUIT])

    def on_mount(self) -> None:
        log = self.query_one("#log")
        log.focus()
        log.scroll_end(animate=False)

    def action_clear(self) -> None:
        self.answer("c")

    def action_back(self) -> None:
        self.answer(None)
