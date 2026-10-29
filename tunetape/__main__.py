"""``tunetape``: the commands, and the interactive session.

The session flow below (menu → post → player → back to the menu) is plain
blocking code on the UI's flow thread (see tunetape/ui.py). It plays nothing
itself: the daemon (docs/protocol.md) owns the queue, mpv, the resolvers and
the listening history, and this is one of its clients. So leaving the player
leaves the music playing; quitting stops it.
"""

import atexit
import signal
import sys
import threading
import time

from tunetape import __version__, ai, config, history, sources
from tunetape.client import Client, DaemonError, DaemonUnavailable, log_path
from tunetape.models import Track
from tunetape.ui import (
    confirm, main_menu, play, prompt_discover, prompt_url, restore_terminal_state,
    run_session, save_terminal_state, show_ai_results, show_ai_settings,
    show_ai_unavailable, show_debug, show_error, show_help, show_history, show_mcp,
    show_settings, show_theme_picker, verify_ai_connection, with_spinner,
)

ERR_AI_NOT_CONFIGURED = 1020

# What the busy screen says while the daemon fetches a post, by its kind.
_FETCHING = {
    "khinsider": "Fetching album…", "spotify": "Fetching Spotify…",
    "youtube_playlist": "Fetching playlist…", "search": "Finding audio…",
}
_COLLECTION_TYPES = ("khinsider", "spotify", "youtube_playlist")


class _DaemonLink:
    """The session's connection to the daemon: made on first use, again after a drop.

    The flow thread and the menu's now-playing check both use it, hence the lock.
    """

    def __init__(self, connect=None):
        self._connect = connect or (lambda spawn: Client.connect(
            spawn=spawn, name=f"tunetape-tui/{__version__}"))
        self._lock = threading.Lock()
        self._client = None

    def get(self, spawn: bool = True):
        """The live client, connecting (and starting the daemon) if there is none."""
        with self._lock:
            if self._client is None:
                self._client = self._connect(spawn)
            return self._client

    def drop(self, client=None) -> None:
        """Forget a connection that failed, so the next ``get`` makes a new one."""
        with self._lock:
            if client is not None and client is not self._client:
                return  # already replaced
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def peek(self):
        """The daemon's status for the menu, or None. Never starts a daemon."""
        with self._lock:
            client = self._client
        if client is None:
            return None
        try:
            return client.status()
        except DaemonUnavailable:
            self.drop(client)
        except DaemonError:
            pass
        return None

    def stop_playback(self, settle: float = 10.0) -> None:
        """Quitting: stop the music, then hang up. Best effort, never raises.

        The daemon stays up and exits on its own once idle. A post still being
        fetched would start playing after the stop, so it gets ``settle``
        seconds to land first.
        """
        with self._lock:
            client, self._client = self._client, None
        if client is None:
            return
        try:
            deadline = time.monotonic() + settle
            while time.monotonic() < deadline and any(
                    r.get("status") in ("pending", "expanding")
                    for r in client.requests(timeout=2).get("requests", [])):
                time.sleep(0.1)
            client.stop(timeout=5)
        except DaemonError:
            pass
        finally:
            client.close()


_link = _DaemonLink()


def _cleanup():
    """Stop playback and restore the terminal. Safe to call multiple times."""
    _link.stop_playback(settle=2.0)
    restore_terminal_state()


def _signal_handler(sig, frame):
    """Handle SIGINT/SIGTERM: restore the terminal and exit; atexit runs _cleanup()."""
    # Don't stop playback here: that is socket I/O behind the link's lock, and
    # a handler can interrupt whatever holds it. Restore the terminal (no locks
    # involved) and exit; the atexit handler calls _cleanup(), which stops it.
    restore_terminal_state()
    sys.exit(0)


def _unreachable(error: DaemonError) -> str:
    """The error modal's text when the daemon can't be reached."""
    message = str(error)
    if log_path() not in message:
        message += f"\n\nCheck {log_path()} for what went wrong."
    return message


def _listen(text: str, *, title: str = None, start_index: int = 0) -> str:
    """Post ``text`` to play now and open the player on it. Returns 'menu'/'quit'.

    The busy screen stays up while the daemon fetches (an album page, a
    playlist); a link it can't use comes back as an error (1010 unsupported,
    1011 mpv or yt-dlp missing, 1014 the fetch failed).
    """
    try:
        kind = sources.classify(text)
    except sources.SourceError:
        kind = None  # the daemon says why
    client = _link.get()
    try:
        with_spinner(_FETCHING.get(kind, "Fetching stream…"), client.play, text,
                     title=title, start_index=start_index, wait=True)
    except DaemonUnavailable:
        raise
    except DaemonError as e:
        show_error(e.message)
        return "menu"
    return play(client)


def _resume(entry: dict) -> int:
    """Resume index for a history entry; a finished collection restarts at 0."""
    resume = int(entry.get("last_index", 0) or 0)
    tc = entry.get("track_count")
    if isinstance(tc, int) and tc > 0 and resume >= tc - 1:
        resume = 0  # finished -> start over from the top
    return resume


def _recently_played() -> str:
    """Show the history menu and replay a selection. Returns 'menu'/'quit'."""
    while True:
        entries = _link.get().history().get("entries", [])
        if not entries:
            show_error("No listening history yet.")
            return "menu"

        action, payload = show_history(entries)

        if action == "back":
            return "menu"
        elif action == "quit":
            return "quit"
        elif action == "delete":
            # The daemon only ever appends to history; removing is ours to do.
            if payload:
                history.remove(payload)
            continue
        elif action == "clear":
            if confirm("Clear all listening history?"):
                history.clear()
            continue
        elif action == "play":
            entry = payload
            if entry.get("type") == "ai_search":
                # A saved vibe: re-run discovery for a fresh suggestion list
                # instead of playing a single track.
                result = _discover(initial_request=entry["url"])
            elif entry.get("type") in _COLLECTION_TYPES:
                # Resume where the user left off; the album keeps the title its
                # page has now.
                result = _listen(entry["url"], start_index=_resume(entry))
            else:
                # Keep the stored title, so an AI pick isn't renamed to
                # yt-dlp's raw video title on replay.
                result = _listen(entry["url"], title=entry.get("title"))
            return "quit" if result == "quit" else "menu"


def _ai_preflight() -> str:
    """Verify AI discovery is set up and reachable before the user types anything.

    Runs a tiny connection check up front so a bad key / model / endpoint is
    caught here instead of after the user has written out a request. On failure
    it offers a shortcut straight into AI settings and re-checks after; returns
    'ok' to proceed, or 'menu'/'quit' to leave Discover.
    """
    while True:
        if not ai.is_configured():
            reason = (
                "AI discovery isn't set up yet. Add an Anthropic API key in "
                "Settings → AI discovery (or set ANTHROPIC_API_KEY)."
            )
        elif ai.is_verified():
            # Already confirmed reachable this session — skip the round-trip.
            return "ok"
        else:
            ok, detail = verify_ai_connection()
            if ok:
                return "ok"
            reason = detail

        action = show_ai_unavailable(reason)
        if action == "quit":
            return "quit"
        if action == "s":
            if show_ai_settings() == "quit":
                return "quit"
            continue  # re-check with the updated settings
        return "menu"


def _discover_request(request: str) -> str:
    """Ask the daemon for suggestions for ``request`` and run the results loop.

    The daemon records the request as an ``ai_search`` history entry, so the
    vibe can be re-run later from Recently played. Returns 'menu' (leave
    Discover), 'research' (user wants a fresh prompt), or 'quit'.
    """
    while True:
        try:
            found = with_spinner("Asking the AI…", _link.get().discover, request)
        except DaemonUnavailable:
            raise
        except DaemonError as e:
            if e.code != ERR_AI_NOT_CONFIGURED:
                show_error(e.message)
                return "research"  # let the user rephrase
            # Set up here but not for the daemon (say, a key only in this
            # shell's environment): same way out as the pre-flight.
            action = show_ai_unavailable(e.message)
            if action == "quit":
                return "quit"
            if action != "s":
                return "menu"
            if show_ai_settings() == "quit":
                return "quit"
            continue  # ask again with the saved settings
        break
    tracks = [Track(name=t.get("title") or t.get("input", ""), resolve_hint=t.get("input", ""))
              for t in found.get("tracks", [])]

    while True:
        action, payload = show_ai_results(request, tracks)
        if action == "quit":
            return "quit"
        if action == "back":
            return "menu"
        if action == "research":
            return "research"
        if action == "play":
            if _listen(payload.resolve_hint, title=payload.name) == "quit":
                return "quit"
            # Return to the suggestion list so the user can keep exploring.


def _discover(initial_request: str = None) -> str:
    """AI discovery: describe a vibe, pick from suggestions, play. Returns 'menu'/'quit'.

    ``initial_request`` seeds the first round from a replayed ``ai_search`` entry,
    skipping the prompt; pressing 'r' (new search) then falls back to the prompt.
    """
    gate = _ai_preflight()
    if gate != "ok":
        return gate

    request = initial_request
    while True:
        if request is None:
            request = prompt_discover()
            cmd = request.strip().lower()
            if cmd == "q":
                return "quit"
            if cmd in ("", "b"):
                return "menu"

        result = _discover_request(request)
        if result == "quit":
            return "quit"
        if result == "menu":
            return "menu"
        request = None  # 'research' -> prompt for a fresh vibe


def _settings_menu() -> str:
    """Show the settings screen and persist toggles. Returns 'menu'/'quit'.

    Settings stay in config.json on this side; the daemon reads them each
    time a track starts.
    """
    while True:
        normalize = bool(config.get_setting("normalize_volume"))
        choice = show_settings(normalize)
        if choice == "q":
            return "quit"
        if choice in ("b", ""):
            return "menu"
        elif choice == "1":
            config.set_setting("normalize_volume", not normalize)
        elif choice == "2":
            if show_theme_picker() == "quit":
                return "quit"
        elif choice == "3":
            if show_ai_settings() == "quit":
                return "quit"


def _daemon_log():
    """(heading, entries) for the Debug screen's daemon section."""
    client = _link.get()
    hello = client.hello or {}
    heading = " · ".join(str(part) for part in (
        hello.get("server"), f"pid {hello['pid']}" if hello.get("pid") else None) if part)
    return heading, client.log().get("entries", [])


def _play_url() -> str:
    url = prompt_url()
    cmd = url.strip().lower()
    if cmd == "q":
        return "quit"
    if cmd in ("", "b"):
        return "menu"  # empty / back -> return to menu
    return _listen(url.strip())


_MENU = {
    "1": _play_url,
    "2": _recently_played,
    "3": _discover,
    "4": _settings_menu,
    "n": lambda: play(_link.get()),  # back to the player on what's playing
    "m": show_mcp,
    "h": show_help,
    "d": lambda: show_debug(_daemon_log),
}


def _connect() -> bool:
    """Reach the daemon, starting it if it isn't running. False after explaining why not."""
    try:
        try:
            _link.get(spawn=False)
        except DaemonUnavailable:
            with_spinner("Starting the tunetape daemon…", _link.get)
    except DaemonUnavailable as e:
        show_error(_unreachable(e))
        return False
    except DaemonError as e:  # 1002: a daemon that speaks another protocol
        show_error(e.message)
        return False
    return True


def _session():
    """The whole interactive session; runs on the UI's flow thread."""
    try:
        if not _connect():
            return
        while True:
            choice = main_menu(_link.peek)
            if choice == "q":
                break
            action = _MENU.get(choice)
            if action is None:
                continue
            try:
                result = action()
            except DaemonUnavailable as e:
                # The daemon went away (crashed, or `tunetape shutdown`). Say
                # so; the next thing that needs it starts a new one.
                _link.drop()
                show_error(_unreachable(e))
                continue
            if result == "quit":
                break
    finally:
        # q (anywhere), ctrl+c, or the app closing: the music stops with us.
        _link.stop_playback()


def main():
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(f"tunetape {__version__}")
        return

    if argv:
        # Any argument means a command (`tunetape play …`, `tunetape daemon`);
        # only a bare `tunetape` opens the interactive player below.
        from tunetape import cli
        sys.exit(cli.main(argv))

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("tunetape requires an interactive terminal.", file=sys.stderr)
        sys.exit(1)

    save_terminal_state()
    atexit.register(_cleanup)
    signal.signal(signal.SIGINT, _signal_handler)
    # SIGTERM exists but isn't delivered the same way on Windows; register it
    # only where it's meaningful so this stays a no-crash on all platforms.
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, OSError):
            pass

    run_session(_session)


if __name__ == "__main__":
    main()
