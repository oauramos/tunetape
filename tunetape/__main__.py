import atexit
import signal
import sys

from tunetape import __version__, config, history
from tunetape.player import MPVController, check_dependencies, get_stream_info
from tunetape.khinsider import fetch_album, resolve_track_url
from tunetape.playlist import Playlist
from tunetape.ui import (
    PlayerUI, show_error, show_history, show_loading, show_menu, show_settings,
    show_welcome, prompt_url, prompt_khinsider_url, save_terminal_state,
    restore_terminal_state, console,
)

_active_controller = None


def _cleanup():
    """Clean up controller and restore terminal. Safe to call multiple times."""
    global _active_controller
    if _active_controller is not None:
        try:
            _active_controller.quit()
        except Exception:
            pass
        _active_controller = None
    restore_terminal_state()


def _signal_handler(sig, frame):
    """Handle SIGINT/SIGTERM: restore the terminal and exit; atexit runs _cleanup()."""
    # [#2] Don't call _cleanup() here — it would acquire the RLock which may deadlock.
    # Restore terminal state (no locks involved) and exit; the atexit handler
    # calls _cleanup(), and each MPVController also reaps itself via atexit.
    restore_terminal_state()
    sys.exit(0)


def _play_youtube(url: str, normalize: bool) -> str:
    """Resolve and play a YouTube URL. Returns the PlayerUI result ('menu'/'quit')."""
    global _active_controller

    try:
        check_dependencies(require_ytdlp=True)
    except RuntimeError as e:
        show_error(str(e))
        return "menu"

    try:
        show_loading(url)
        info = get_stream_info(url)
    except (ValueError, ConnectionError, RuntimeError) as e:
        show_error(str(e))
        return "menu"

    controller = None
    try:
        controller = MPVController(info["stream_url"], normalize=normalize)
        _active_controller = controller
        history.record("youtube", url, info["title"])
        ui = PlayerUI(info["title"], controller)
        result = ui.run()
    except Exception as e:
        if controller is None:
            show_error(f"Could not start player: {e}")
            return "menu"
        raise
    finally:
        if controller is not None:
            controller.quit()
        _active_controller = None

    return result


def _play_khinsider(url: str, normalize: bool, start_index: int = 0) -> str:
    """Fetch and play a KHInsider album from start_index. Returns 'menu'/'quit'."""
    global _active_controller

    try:
        console.print("  [dim]Fetching album...[/dim]")
        album = fetch_album(url)
    except (ValueError, RuntimeError, OSError) as e:
        show_error(str(e))
        return "menu"

    playlist = Playlist(album, start_index=start_index)
    # Record the album once when playback begins (bumps play_count); the resume
    # position is refreshed per-track via set_last_index without bumping it.
    history.record(
        "khinsider", url, album.title,
        track_count=playlist.total_tracks,
        last_index=playlist.current_index,
    )

    result = "menu"
    # A single dead/unreachable track shouldn't abandon the rest of the album:
    # fail forward to the next track, but stop after too many failures in a row.
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5
    try:
        while True:
            track = playlist.current_track
            failed = False
            direct_url = None
            try:
                console.print(f"  [dim]Loading track {playlist.track_label}...[/dim]")
                direct_url = resolve_track_url(track)
            except (RuntimeError, OSError) as e:
                show_error(f"Could not load track: {e}")
                failed = True

            if not failed:
                controller = None
                try:
                    controller = MPVController(direct_url, normalize=normalize)
                    _active_controller = controller
                    history.set_last_index(url, playlist.current_index)
                    playlist_info = {
                        "track_label": playlist.track_label,
                        "has_next": playlist.has_next(),
                        "has_prev": playlist.has_prev(),
                    }
                    ui = PlayerUI(track.name, controller, playlist_info)
                    result = ui.run()
                    consecutive_failures = 0
                except Exception as e:
                    if controller is None:
                        show_error(f"Could not start player: {e}")
                        failed = True
                    else:
                        raise
                finally:
                    if controller is not None:
                        controller.quit()
                    _active_controller = None

            if failed:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES or not playlist.has_next():
                    result = "menu"
                    break
                playlist.next()
                continue

            if result == "next_track" and playlist.has_next():
                playlist.next()
            elif result == "prev_track" and playlist.has_prev():
                playlist.prev()
            elif result == "quit":
                break
            else:
                break
    finally:
        playlist.close()

    return result


def _recently_played(normalize: bool) -> str:
    """Show the history menu and replay a selection. Returns 'menu'/'quit'."""
    while True:
        entries = history.load()
        if not entries:
            show_error("No listening history yet.")
            return "menu"

        show_welcome()
        action, payload = show_history(entries)

        if action == "back":
            return "menu"
        elif action == "delete":
            if payload:
                history.remove(payload)
            continue
        elif action == "clear":
            console.print()
            try:
                confirm = input("  Clear all history? (y/N) ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
            if confirm == "y":
                history.clear()
            continue
        elif action == "play":
            entry = payload
            show_welcome()
            if entry.get("type") == "youtube":
                result = _play_youtube(entry["url"], normalize)
            else:
                resume = int(entry.get("last_index", 0) or 0)
                tc = entry.get("track_count")
                if isinstance(tc, int) and tc > 0 and resume >= tc - 1:
                    resume = 0  # finished album -> start over from the top
                result = _play_khinsider(entry["url"], normalize, start_index=resume)
            return "quit" if result == "quit" else "menu"


def _settings_menu():
    """Show the settings screen and persist toggles."""
    while True:
        normalize = bool(config.get_setting("normalize_volume"))
        show_welcome()
        choice = show_settings(normalize)
        if choice in ("q", ""):
            return
        elif choice == "1":
            config.set_setting("normalize_volume", not normalize)


def main():
    if "--version" in sys.argv[1:] or "-V" in sys.argv[1:]:
        print(f"tunetape {__version__}")
        return

    save_terminal_state()
    atexit.register(_cleanup)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        check_dependencies(require_ytdlp=False)
    except RuntimeError as e:
        show_error(str(e))
        return

    while True:
        normalize = bool(config.get_setting("normalize_volume"))
        show_welcome()
        choice = show_menu()

        if choice == "q":
            break
        elif choice == "1":
            show_welcome()
            url = prompt_url()
            if not url.strip():
                show_error("No URL entered. Please paste a YouTube URL.")
                continue
            if _play_youtube(url, normalize) == "quit":
                break
        elif choice == "2":
            show_welcome()
            url = prompt_khinsider_url()
            if not url.strip():
                show_error("No URL entered.")
                continue
            if _play_khinsider(url, normalize) == "quit":
                break
        elif choice == "3":
            if _recently_played(normalize) == "quit":
                break
        elif choice == "4":
            _settings_menu()


if __name__ == "__main__":
    main()
