"""tunetape's interface: one Textual app for the whole session.

The session flow in ``__main__`` (menu → post to the daemon → player → back
to the menu) is plain blocking code. ``run_session`` starts the Textual app on
the main thread and runs that flow on a worker thread; each function below
shows a screen and blocks the flow until the screen answers, returning what
the old Rich prompt of the same name returned. Slow calls (a post the daemon
is still fetching, the AI) run on the flow thread while the app keeps
animating.
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from textual.app import App
from textual.binding import Binding
from textual.content import Content
from textual.screen import ModalScreen

from tunetape import __version__, ai, config, debug, paths
from tunetape.art import DEFAULT_THEME, TUNETAPE_THEME
from tunetape.player_screen import PlayerScreen, format_time
from tunetape.screens import (
    BusyScreen, ConfirmModal, DebugScreen, Edit, ErrorModal, HelpModal, HistoryScreen,
    McpScreen, MenuScreen, NO_QUIT, NoticeScreen, OptionsScreen, PromptScreen, Row, SuggestionsScreen,
    ThemeScreen,
)
# Re-exported for __main__'s signal and atexit handlers.
from tunetape.terminal import restore_terminal_state, save_terminal_state  # noqa: F401

CSS = """
/* --- shared layout ------------------------------------------------------ */
.body { height: 1fr; padding: 1 2; align: center middle; }
/* A card sizes to its content up to the screen; its intro is docked on top and
   its key caps at the bottom, so a long list in between scrolls instead of
   pushing the caps off screen. */
.card {
    width: 1fr;
    max-width: 84;
    height: auto;
    max-height: 100%;
    padding: 1 2;
    border: round $primary 50%;
    border-title-color: $primary;
    border-title-style: bold;
    border-subtitle-color: $text-muted;
}
.card.-fill { height: 1fr; }
.card.-warning { border: round $warning 70%; border-title-color: $warning; }
.card.-error { border: round $error 70%; border-title-color: $error; }
.card > .intro { dock: top; height: auto; margin-bottom: 1; }
.card > KeyCaps { dock: bottom; margin-top: 1; }
.card > .foot { dock: bottom; height: auto; }
.card > .foot > KeyCaps { margin-top: 1; }
.card > OptionList, .card > VerticalScroll { height: auto; max-height: 100%; }
.card.-fill > OptionList, .card.-fill > VerticalScroll { height: 1fr; }
.card Input { border: round $primary 40%; background: transparent; }
.card Input:focus { border: round $primary; }
.card OptionList {
    padding: 0;
    border: none;
    background: transparent;
    scrollbar-size-vertical: 1;
    text-wrap: nowrap;
    text-overflow: ellipsis;
}
.card OptionList:focus { border: none; }
/* A tint rather than Textual's solid block cursor, which hides coloured text. */
OptionList > .option-list--option-highlighted {
    color: $foreground;
    background: $primary 10%;
    text-style: none;
}
OptionList:focus > .option-list--option-highlighted {
    background: $primary 22%;
    text-style: bold;
}
.hint { color: $text-muted; }
#option-help { height: 2; margin-top: 1; color: $text-muted; }
Screen.-compact .body { padding: 1 1; }
Screen.-compact .card { padding: 0 1; }

/* --- menu and waiting -------------------------------------------------- */
#home { height: 1fr; align: center middle; padding: 0 2; }
#welcome { width: 1fr; text-align: center; margin-bottom: 1; }
#menu-now { margin-bottom: 1; }
#ascii-tuna { display: none; }
MenuScreen.-ascii-width.-ascii-height #ascii-tuna,
MenuScreen.-ascii-width.-ascii-tall #ascii-tuna { display: block; }
MenuScreen.-ascii-width.-ascii-height Tuna,
MenuScreen.-ascii-width.-ascii-height #welcome,
MenuScreen.-ascii-width.-ascii-tall Tuna,
MenuScreen.-ascii-width.-ascii-tall #welcome { display: none; }
/* The now-playing row takes two rows the ASCII tuna only has when -ascii-tall. */
MenuScreen.-now.-ascii-width.-ascii-height #ascii-tuna { display: none; }
MenuScreen.-now.-ascii-width.-ascii-height Tuna,
MenuScreen.-now.-ascii-width.-ascii-height #welcome { display: block; }
#busy { width: 64; max-width: 100%; height: auto; padding: 1 2; }
#busy-bar { width: 100%; margin-top: 1; }
#busy-bar Bar { width: 1fr; }
#busy-bar Bar > .bar--indeterminate { color: $primary; background: $panel; }
#busy-detail { margin-top: 1; }
#busy-caps { margin-top: 1; }

/* --- modals ------------------------------------------------------------ */
ErrorModal, ConfirmModal, HelpModal, EditModal {
    align: center middle;
    background: $background 60%;
}
#error, #confirm, #edit { width: 64; max-width: 100%; background: $surface; }
#help { width: auto; max-width: 100%; padding: 1 3; background: $surface; }
#help > VerticalScroll, #help Static, #help > KeyCaps { width: auto; }
#log { scrollbar-size-vertical: 1; }

/* --- player ------------------------------------------------------------ */
/* btop-like: square-ish boxes edge to edge, titles in the borders. */
.box {
    border: round $primary 40%;
    border-title-color: $primary;
    border-title-style: bold;
    border-subtitle-color: $text-muted;
    padding: 0 1;
    background: $background;
}
#player-body { height: 1fr; padding: 0 1; }
#deck { width: 1fr; height: 100%; }
#now { height: auto; }
#now-top, #time-row { height: 1; }
#title { width: 1fr; text-style: bold; }
#status { width: auto; padding-left: 2; }
#subtitle { color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
#time-row { margin-top: 1; }
#elapsed { width: auto; color: $primary; }
#duration { width: auto; color: $text-muted; }
#progress { width: 1fr; padding: 0 1; }
#progress Bar { width: 1fr; }
#progress Bar > .bar--bar { color: $primary; background: $panel; }
#progress Bar > .bar--complete { color: $primary; background: $panel; }
#progress Bar > .bar--indeterminate { color: $primary; background: $panel; }
#player-caps { margin-top: 1; }
#menu-nav { margin-top: 1; }
#stats { height: 1fr; }
#level, #stream { width: 1fr; height: 100%; }
#level-graph, #net-graph { margin-top: 1; }
#queue {
    width: 40;
    height: 100%;
    margin-left: 1;
    padding: 0;
    scrollbar-size-vertical: 1;
    scrollbar-color: $primary 30%;
    scrollbar-color-hover: $primary 60%;
    scrollbar-background: $background;
    /* one line per track: YouTube titles run long */
    text-wrap: nowrap;
    text-overflow: ellipsis;
}
#queue:focus { border: round $primary; }
#queue.-single, PlayerScreen.-narrow #queue, PlayerScreen.-compact #queue { display: none; }
PlayerScreen.-compact #player-body { padding: 0; }
"""


class SessionClosed(BaseException):
    """The app closed while the flow was using it (ctrl+c, or q while loading).

    A BaseException, like KeyboardInterrupt, so the flow's ``except Exception``
    error handling unwinds past it (running its ``finally`` cleanup) instead
    of reporting it.
    """


class TunetapeApp(App):
    """The whole session: screens come and go as the flow thread asks for them."""

    TITLE = "tunetape"
    CSS = CSS
    # -compact pads less; the player's queue needs deck (62) + queue (42) + gutters.
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (70, "-narrow"), (108, "-wide")]
    BINDINGS = [Binding("ctrl+c,ctrl+q", "quit_session", "Quit", show=False, priority=True)]

    def __init__(self, flow):
        super().__init__()
        self._session_flow = flow
        self._waiter = None  # (screen, event, box) of the screen the flow waits on
        self._session_done = threading.Event()
        self._session_thread = None
        self.flow_error = None

    # --- lifecycle ---------------------------------------------------------

    def on_mount(self) -> None:
        self.register_theme(TUNETAPE_THEME)
        saved = config.get_setting("theme")
        self.theme = saved if saved in self.available_themes else DEFAULT_THEME

    def on_ready(self) -> None:
        # Not on mount: call_from_thread needs the app fully running.
        self._session_thread = threading.Thread(target=self._run_flow, name="tunetape-session",
                                                daemon=True)
        self._session_thread.start()

    def _run_flow(self) -> None:
        try:
            self._session_flow()
        except SessionClosed:
            pass
        except BaseException as exc:  # re-raised by run_session once the terminal is back
            self.flow_error = exc
            debug.exception("Session crashed", exc)
        finally:
            if not self._session_done.is_set():  # else the app is already on its way out
                self._session_done.set()
                try:
                    self.call_from_thread(self.exit)
                except RuntimeError:
                    pass

    def exit(self, *args, **kwargs) -> None:
        self._session_done.set()
        waiter, self._waiter = self._waiter, None
        if waiter is not None:
            waiter[1].set()  # no result: ask() raises SessionClosed in the flow
        super().exit(*args, **kwargs)

    @property
    def closed(self) -> bool:
        return self._session_done.is_set()

    def watch_theme(self, _theme: str) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, PlayerScreen) and screen.is_mounted:
                screen.apply_gradient()

    # --- called from the flow thread ---------------------------------------

    def call(self, fn, *args):
        """Run ``fn`` on the app's thread and return its result."""
        if self.closed or not self.is_running:
            raise SessionClosed
        return self.call_from_thread(fn, *args)

    def ask(self, build):
        """Show the screen ``build()`` returns and block until it answers."""
        done = threading.Event()
        box = {}

        def present():
            screen = build()
            self._waiter = (screen, done, box)
            self._present(screen)

        self.call(present)
        while not done.wait(0.25):
            if self.closed:
                break
        if "result" not in box:
            raise SessionClosed
        return box["result"]

    def show_busy(self, message: str, detail: str = "") -> None:
        """Show that the flow is working: on the player if one is up, else a busy screen."""

        def busy():
            top = self.flow_screen
            if isinstance(top, PlayerScreen):
                top.set_loading(message)
            elif isinstance(top, BusyScreen):
                top.set_message(message, detail)
            else:
                self._present(BusyScreen(message, detail))

        self.call(busy)

    # --- on the app's thread -----------------------------------------------

    @property
    def flow_screen(self):
        """The topmost full screen (modals sit on top of it), if any."""
        for screen in reversed(self.screen_stack[1:]):
            if not isinstance(screen, ModalScreen):
                return screen
        return None

    def _present(self, screen) -> None:
        if screen in self.screen_stack:
            return  # already up
        if isinstance(screen, ModalScreen):
            self.push_screen(screen)
            return
        # Drop anything stacked over the current screen (the player's help).
        while len(self.screen_stack) > 2:
            self.pop_screen()
        if len(self.screen_stack) > 1:
            self.switch_screen(screen)
        else:
            self.push_screen(screen)

    def answer(self, screen, result) -> None:
        """A screen's answer: wake the flow if it is waiting on that screen."""
        if isinstance(screen, ModalScreen) and screen.is_current:
            self.pop_screen()
        waiter = self._waiter
        if waiter is None or waiter[0] is not screen:
            return  # e.g. the player's help, which nobody waits on
        self._waiter = None
        waiter[2]["result"] = result
        waiter[1].set()

    def action_quit_session(self) -> None:
        self.quit_session()

    def quit_session(self) -> None:
        """q / ctrl+c: let the waiting screen answer 'quit' so the flow winds down."""
        waiter = self._waiter
        if waiter is None or waiter[0].QUIT_RESULT is NO_QUIT:
            self.exit()  # mid-load or on a modal: the flow stops at its next ask
            return
        screen = waiter[0]
        while self.screen is not screen and len(self.screen_stack) > 2:
            self.pop_screen()
        screen.answer(screen.QUIT_RESULT)


_app: Optional[TunetapeApp] = None


def run_session(flow) -> None:
    """Run ``flow`` (the whole session) with the Textual app on screen."""
    global _app
    app = _app = TunetapeApp(flow)
    try:
        app.run()
    finally:
        app._session_done.set()
        if app._session_thread is not None:
            # Give the flow a moment to stop playback in the daemon.
            app._session_thread.join(timeout=3)
        _app = None
    saved = config.get_setting("theme")
    if app.theme and app.theme != saved:
        config.set_setting("theme", app.theme)  # e.g. picked from the ctrl+p palette
    if app.flow_error is not None:
        raise app.flow_error


def _ask(build):
    if _app is None:
        raise RuntimeError("tunetape's interface is not running.")
    return _app.ask(build)


def _notify(message: str, severity: str = "information") -> None:
    if _app is not None:
        _app.call(lambda: _app.notify(message, severity=severity))


# --- menu and prompts ------------------------------------------------------

def main_menu(now_playing=None) -> str:
    """The main menu. Returns '1'-'4', 'm', 'h', 'd', 'q', or 'n' (back to the player).

    ``now_playing()`` returns the daemon's status, or None when there is no
    daemon to ask; while it says something is playing the menu offers 'n'.
    """
    return _ask(lambda: MenuScreen(now_playing))


def _detect_source(text: str) -> Content:
    """A hint under the URL box naming the source a pasted link belongs to."""
    from tunetape import khinsider, player, spotify

    names = [
        (spotify.is_spotify_url, "Spotify"),
        (player.is_youtube_playlist_url, "YouTube playlist"),
        (player.is_youtube_url, "YouTube video"),
        (khinsider.is_khinsider_url, "KHInsider album"),
    ]
    for matches, name in names:
        if matches(text):
            return Content.from_markup("[$success]●[/] $name", name=name)
    return Content.from_markup("[$warning]●[/] not a YouTube, Spotify or KHInsider link")


def prompt_url() -> str:
    """Ask for a media URL. Returns it, '' for back or 'q' to quit."""
    return _ask(lambda: PromptScreen(
        "Play", "Play a URL",
        Content("Paste a YouTube, Spotify or KHInsider link."),
        placeholder="https://…", submit="play",
        note=Content("YouTube video or playlist · Spotify track or playlist · KHInsider album"),
        detect=_detect_source,
    ))


def prompt_discover() -> str:
    """Ask for a free-text music request. Returns it, '' for back or 'q' to quit."""
    return _ask(lambda: PromptScreen(
        "Discover", "Discover",
        Content("Describe what you want to hear, and an AI picks the songs."),
        placeholder="upbeat 80s synthwave", submit="ask",
        note=Content('e.g. "calm piano for focus" · "90s hip hop classics"'),
    ))


def show_ai_results(request: str, tracks: list) -> tuple:
    """Pick from AI suggestions. Returns ('play', track), ('research', None), ('back', None) or ('quit', None)."""
    return _ask(lambda: SuggestionsScreen(request, tracks))


def show_ai_unavailable(message: str) -> str:
    """Explain why AI discovery can't run. Returns 's' (open AI settings), 'back' or 'quit'."""
    debug.log(message, "ERROR")
    return _ask(lambda: NoticeScreen("AI discovery unavailable", message))


# --- waiting and errors ----------------------------------------------------

def with_spinner(message: str, func, *args, **kwargs):
    """Run a blocking ``func`` while the app shows ``message``; returns its result.

    The app animates on its own thread, so this just runs ``func`` here on the
    flow thread. Without a running app (tests) it simply calls ``func``.
    """
    debug.log(message)
    app = _app
    if app is None:
        return func(*args, **kwargs)
    app.show_busy(message)
    result = func(*args, **kwargs)
    if app.closed:
        raise SessionClosed  # quit while loading: don't go on to play it
    return result


def show_error(message: str) -> None:
    """Show an error over the current screen and wait for the user to close it."""
    debug.log(message, "ERROR")
    if _app is None:
        print(f"tunetape: {message}", file=sys.stderr)
        return
    _ask(lambda: ErrorModal(message))


def confirm(question: str) -> bool:
    """A yes/no question."""
    return _ask(lambda: ConfirmModal(question))


def verify_ai_connection(check=None) -> tuple:
    """Run the AI connection check behind the busy screen. Returns (ok, detail).

    ``detail`` is the model id on success or a user-facing error message on
    failure. Shared by the Discover pre-flight and Settings → Test connection.
    """
    check = check or ai.check_connection
    app = _app
    if app is not None:
        app.show_busy("Verifying AI connection…", "reaching the model endpoint…")
    try:
        result = (True, check())
    except (RuntimeError, ConnectionError) as e:
        result = (False, str(e))
    except Exception as e:  # a surprise must not leave the spinner up
        result = (False, str(e) or "Unexpected error during verification.")
    if app is not None:
        def hold():
            top = app.flow_screen
            if isinstance(top, BusyScreen):
                top.finish(*result)

        app.call(hold)
        time.sleep(0.9)  # long enough to read the ✓/✗ before moving on
    return result


# --- history ---------------------------------------------------------------

def _humanize_time(iso_str) -> str:
    """Render an ISO timestamp as a coarse 'time ago' string."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(int((datetime.now(timezone.utc) - dt).total_seconds()), 0)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


# Source tags shown in the Recently-played list, with the colour of each.
_HISTORY_TAGS = {
    "youtube": ("YT", "$error"),
    "youtube_playlist": ("PL", "$error"),
    "spotify": ("SP", "$success"),
    "khinsider": ("KH", "$warning"),
    "ai": ("AI", "$accent"),
    "ai_search": ("AI", "$accent"),
}


def _history_row(entry: dict) -> Content:
    tag, color = _HISTORY_TAGS.get(entry.get("type"), ("??", "$foreground"))
    details = []
    tc = entry.get("track_count")
    if entry.get("type") == "ai_search":
        details.append("search")  # a re-runnable Discover request, not one track
    elif isinstance(tc, int) and tc > 1:
        details.append(f"resume {int(entry.get('last_index', 0) or 0) + 1}/{tc}")
    plays = entry.get("play_count", 1)
    details.append(f"played {plays}×")
    details.append(_humanize_time(entry.get("last_played")))
    return Content.from_markup(
        f"[b {color}]$tag[/]  $title\n    [$foreground 50%]$details[/]",
        tag=tag, title=str(entry.get("title", "Unknown")), details=" · ".join(details),
    )


_history_at = 0  # keeps the highlight in place across deletes


def show_history(entries: list) -> tuple:
    """The recently-played list. Returns (action, payload): ('play', entry),
    ('delete', url), ('clear', None), ('back', None) or ('quit', None)."""
    global _history_at
    rows = [_history_row(e) for e in entries]
    action, payload = _ask(lambda: HistoryScreen(entries, rows, _history_at))
    urls = [e.get("url") for e in entries]
    if action == "delete" and payload in urls:
        _history_at = urls.index(payload)
    elif action == "play":
        _history_at = 0  # it moves to the top
    return action, payload


# --- settings --------------------------------------------------------------

def _state(on: bool, on_text: str = "on", off_text: str = "off") -> Content:
    if on:
        return Content.from_markup("[b $success]$t[/]", t=on_text)
    return Content.from_markup("[$foreground 50%]$t[/]", t=off_text)


def _value(text: str) -> Content:
    return Content.from_markup("[$primary]$t[/]", t=text)


_settings_at = {}  # remembers the highlighted row per settings screen


def _options(name: str, build_screen, rows: list) -> tuple:
    key, text = _ask(build_screen)
    keys = [r.key for r in rows]
    if key in keys:
        _settings_at[name] = keys.index(key)
    return key, text


def show_settings(normalize_on: bool) -> str:
    """The settings screen. Returns '1' / '2' / '3', 'b' (back) or 'q'."""
    rows = [
        Row("1", "Volume normalization", _state(normalize_on),
            "Evens out loudness across tracks and sources."),
        Row("2", "Theme", _value(_app.theme if _app else DEFAULT_THEME),
            "Recolour the whole interface (or ctrl+p anywhere)."),
        Row("3", "AI discovery", _state(ai.is_configured(), "configured", "not set"),
            "Describe a vibe and let an AI find songs for you."),
    ]
    key, _ = _options("settings", lambda: OptionsScreen(
        "Settings", "Settings", rows, highlight=_settings_at.get("settings", 0)), rows)
    return key


def show_theme_picker() -> Optional[str]:
    """Pick a theme with a live preview. Returns 'quit' to quit, else None (back)."""
    app = _app
    original = app.theme
    themes = sorted(app.available_themes.values(), key=lambda t: t.name != DEFAULT_THEME)
    action, name = _ask(lambda: ThemeScreen(themes, original))
    if action == "save":
        config.set_setting("theme", name)
        return None
    app.call(setattr, app, "theme", original)  # browsed but didn't pick: undo the preview
    return "quit" if action == "quit" else None


def _mask_key(key: str) -> Content:
    """Show only the tail of an API key so the screen never leaks the whole thing."""
    if not key:
        return _state(False, off_text="not set")
    tail = key[-4:] if len(key) >= 4 else key
    return Content.from_markup("[b $success]set[/] [$foreground 50%]···$tail[/]", tail=tail)


def show_ai_settings() -> str:
    """Configure AI discovery (provider, key, model, endpoint, auth). Returns 'quit'/'back'."""
    while True:
        prov = ai.provider()  # 'anthropic' | 'openai'
        is_openai = prov == "openai"
        env_var = "OPENAI_API_KEY" if is_openai else "ANTHROPIC_API_KEY"
        saved_key = config.get_setting("ai_api_key") or ""
        saved_model = (config.get_setting("ai_model") or "").strip()
        saved_base = (config.get_setting("ai_base_url") or "").strip()
        model = saved_model or ai.default_model()
        base = saved_base or ai.default_base_url()
        path = "/v1/chat/completions" if is_openai else "/v1/messages"
        is_bearer = ai.auth_mode() == "bearer"

        if not saved_key and os.environ.get(env_var):
            key_note = f"Using {env_var} from your environment."
        else:
            key_note = f"Or set {env_var}. Stored locally in config."
        rows = [
            Row("1", "Provider", _value(prov),
                "anthropic (Messages API) or openai (/v1/chat/completions: OpenAI, Ollama, …)."),
            Row("2", "API key", _mask_key(saved_key), key_note),
            Row("3", "Model", _value(model),
                "Any model id for the provider; blank resets to the default."),
            Row("4", "Endpoint", _value(base),
                f"Base URL of a gateway, proxy or local server; blank resets. POSTs to <base>{path}."),
        ]
        if not is_openai:
            rows.append(Row("5", "Auth", _value("Bearer token" if is_bearer else "x-api-key"),
                            "How the key is sent: x-api-key (Anthropic) or Bearer (proxies, OAuth)."))
        if saved_key:
            rows.append(Row("c", "Clear saved key", Content("")))
        if ai.is_configured():
            rows.append(Row("t", "Test connection", Content(""),
                            "Send a tiny request to confirm the key, model and endpoint work."))
        edits = {
            "2": Edit("API key", Content("Paste your API key (blank keeps the current one)."),
                      password=True),
            "3": Edit("Model", Content.from_markup(
                "Model id. Blank for the default, [$primary]$m[/].", m=ai.default_model()),
                placeholder=ai.default_model(), value=saved_model),
            "4": Edit("Endpoint", Content.from_markup(
                "Base URL. Blank for the default; tunetape POSTs to <base>$p.", p=path),
                placeholder=ai.default_base_url(), value=saved_base),
        }
        intro = Content.from_markup(
            "[$foreground 60%]Suggests songs from a text request. Works with Anthropic or any "
            "OpenAI-compatible endpoint, local models included.[/]"
        )
        choice, text = _options("ai", lambda: OptionsScreen(
            "Settings › AI discovery", "AI discovery", rows, intro=intro, edits=edits,
            highlight=_settings_at.get("ai", 0)), rows)

        if choice == "q":
            return "quit"
        if choice == "b":
            return "back"
        if choice == "1":
            # Two providers — flip between them.
            config.set_setting("ai_provider", "anthropic" if is_openai else "openai")
        elif choice == "2":
            if text:
                config.set_setting("ai_api_key", text)
        elif choice == "3":
            config.set_setting("ai_model", text or "")
        elif choice == "4":
            config.set_setting("ai_base_url", text or "")
        elif choice == "5" and not is_openai:
            # Two modes only — flip between them.
            config.set_setting("ai_auth_mode", "x-api-key" if is_bearer else "bearer")
        elif choice == "c" and saved_key:
            config.set_setting("ai_api_key", "")
        elif choice == "t" and ai.is_configured():
            ok, detail = verify_ai_connection()
            if ok:
                _notify(f"Connected · {detail} responded.")
            else:
                _notify(detail, "error")


# --- help, MCP and debug ---------------------------------------------------

def show_help() -> Optional[str]:
    """The key reference. Returns 'quit' to quit, else None (back)."""
    return _ask(HelpModal)


def show_mcp() -> Optional[str]:
    """How to connect an AI agent over MCP, and adding tunetape to Claude Code
    on request. Returns 'quit' to quit, else None (back)."""
    from tunetape import mcp_setup

    while True:
        setup = mcp_setup.setup()
        result = _ask(lambda: McpScreen(setup))
        if result == "quit":
            return "quit"
        if result != "a":
            return None
        ok, detail = with_spinner("Adding tunetape to Claude Code…", mcp_setup.add_to_claude_code)
        debug.log(detail, "INFO" if ok else "ERROR")
        _notify(detail, "information" if ok else "error")


def _log_line(ts: datetime, level: str, msg: str) -> Content:
    color = {"ERROR": "$error", "WARN": "$warning", "WARNING": "$warning"}.get(
        level, "$foreground 50%")
    try:
        stamp = ts.astimezone().strftime("%H:%M:%S")
    except (ValueError, OSError, AttributeError):
        stamp = "--:--:--"
    return Content.from_markup(
        f"[$foreground 50%]$stamp[/] [{color}]$level[/] $msg",
        stamp=stamp, level=f"{level:<5}", msg=msg,
    )


def _daemon_records(entries: list) -> list:
    """``daemon.log`` entries (protocol §4.5) as (time, level, message) records."""
    records = []
    for entry in entries:
        try:
            ts = datetime.strptime(str(entry.get("time")), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            ts = None
        records.append((ts, str(entry.get("level") or "INFO").upper(),
                        str(entry.get("message", ""))))
    return records


def _hint(text: str) -> Content:
    return Content.from_markup("[$foreground 50%]$t[/]", t=text)


def show_debug(daemon_log=None) -> Optional[str]:
    """The Debug/Logs viewer. Returns 'quit' to quit, else None (back).

    Two logs, one after the other: this window's, then the daemon's, which
    does the playing and so has most of the story. ``daemon_log()`` returns
    (a heading such as 'tunetape/0.10.0 · pid 4123', the ``daemon.log``
    entries), or raises DaemonError when the daemon can't be asked.
    """
    from tunetape.client import DaemonError, log_path
    from tunetape.player import tool_command

    while True:
        def found(name: str) -> str:
            return "[b $success]ok[/]" if tool_command(name) else "[b $error]missing[/]"

        about = Content.from_markup(
            f"[$foreground 60%]tunetape[/] $v   [$foreground 60%]mpv[/] {found('mpv')}"
            f"   [$foreground 60%]yt-dlp[/] {found('yt-dlp')}\n"
            "[$foreground 60%]data dir[/] $dir",
            v=__version__, dir=paths.data_dir(),
        )
        lines = [Content.from_markup("[b $primary]this window[/]")]
        own = debug.entries()[-200:]
        lines.extend(_log_line(*record) for record in own)
        if not own:
            lines.append(_hint("No events logged yet this session."))
        if daemon_log is not None:
            lines.append(Content(""))
            try:
                heading, entries = daemon_log()
            except DaemonError as e:
                heading, entries, failure = "", [], str(e)
            else:
                failure = None
            lines.append(Content.from_markup(
                "[b $primary]daemon[/] [$foreground 50%]$h[/]", h=heading))
            lines.append(_hint(log_path()))
            records = _daemon_records(entries)
            lines.extend(_log_line(*record) for record in records)
            if failure:
                lines.append(_hint(f"Could not read its log: {failure}"))
            elif not records:
                lines.append(_hint("Nothing logged yet."))
        log = Content("\n").join(lines)
        result = _ask(lambda: DebugScreen(about, log))
        if result == "quit":
            return "quit"
        if result != "c":
            return None
        debug.clear()


# --- the player ------------------------------------------------------------

def play(client) -> str:
    """Show the daemon's queue on the player until the user leaves or it runs out.

    Returns 'menu' (b, or nothing left to play: the music, if any, keeps
    going) or 'quit'. A failure that stopped playback is shown before it
    returns; a daemon that went away raises DaemonUnavailable.
    """
    from tunetape.client import DaemonUnavailable

    status = client.status()
    if status.get("state") == "idle":
        if status.get("message"):
            show_error(status["message"])  # it failed before the player was up
        return "menu"
    items = client.queue().get("items", [])
    shown = []

    def build():
        screen = PlayerScreen(client, status, items)
        shown.append(screen)
        return screen

    result = _ask(build)
    screen = shown[0]
    if screen.lost is not None:
        raise DaemonUnavailable(screen.lost)
    if screen.notice:
        show_error(screen.notice)
    return "quit" if result == "quit" else "menu"


__all__ = [
    "SessionClosed", "TunetapeApp", "run_session", "main_menu", "prompt_url",
    "prompt_discover", "show_ai_results", "show_ai_unavailable", "with_spinner",
    "show_error", "confirm", "verify_ai_connection", "show_history", "show_settings",
    "show_theme_picker", "show_ai_settings", "show_help", "show_mcp", "show_debug", "play",
    "format_time", "save_terminal_state", "restore_terminal_state",
]
