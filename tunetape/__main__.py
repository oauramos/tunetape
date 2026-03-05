import atexit
import signal
import sys

from tunetape.player import MPVController, check_dependencies, get_stream_info
from tunetape.khinsider import fetch_album, is_khinsider_url, resolve_track_url
from tunetape.playlist import Playlist
from tunetape.ui import (
    PlayerUI, show_error, show_header, show_loading, show_menu, show_welcome,
    prompt_url, prompt_khinsider_url, save_terminal_state, restore_terminal_state,
    console,
)

_active_controller = None
_should_quit = False


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
    """Handle SIGINT/SIGTERM by setting quit flag. Avoids deadlock by not calling quit() directly."""
    global _should_quit
    _should_quit = True
    # [#2] Don't call _cleanup() here — it would acquire the RLock which may deadlock.
    # Instead, restore terminal state (no locks involved) and exit.
    # atexit handler will call _cleanup() which handles controller.quit().
    restore_terminal_state()
    sys.exit(0)


def main():
    save_terminal_state()
    atexit.register(_cleanup)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        check_dependencies(require_ytdlp=False)
    except RuntimeError as e:
        show_error(str(e))
        return

    global _active_controller

    while True:
        show_welcome()
        choice = show_menu()

        if choice == "q":
            break
        elif choice == "1":
            try:
                check_dependencies(require_ytdlp=True)
            except RuntimeError as e:
                show_error(str(e))
                continue

            show_header()
            url = prompt_url()

            if not url.strip():
                show_error("No URL entered. Please paste a YouTube URL.")
                continue

            try:
                show_loading(url)
                info = get_stream_info(url)
            except ValueError as e:
                show_error(str(e))
                continue
            except ConnectionError as e:
                show_error(str(e))
                continue
            except RuntimeError as e:
                show_error(str(e))
                continue

            controller = None
            try:
                controller = MPVController(info["stream_url"])
                _active_controller = controller
                ui = PlayerUI(info["title"], controller)
                result = ui.run()
            except Exception as e:
                if controller is None:
                    show_error(f"Could not start player: {e}")
                    continue
                raise
            finally:
                if controller is not None:
                    controller.quit()
                _active_controller = None

            if result == "quit":
                break

        elif choice == "2":
            show_header()
            url = prompt_khinsider_url()

            if not url.strip():
                show_error("No URL entered.")
                continue

            try:
                console.print("  [dim]Fetching album...[/dim]")
                album = fetch_album(url)
            except (ValueError, RuntimeError, OSError) as e:
                show_error(str(e))
                continue

            playlist = Playlist(album)
            quit_app = False

            while True:
                track = playlist.current_track
                try:
                    console.print(f"  [dim]Loading track {playlist.track_label}...[/dim]")
                    direct_url = resolve_track_url(track)
                except (RuntimeError, OSError) as e:
                    show_error(f"Could not load track: {e}")
                    break

                controller = None
                try:
                    controller = MPVController(direct_url)
                    _active_controller = controller
                    playlist_info = {
                        "track_label": playlist.track_label,
                        "has_next": playlist.has_next(),
                        "has_prev": playlist.has_prev(),
                    }
                    ui = PlayerUI(track.name, controller, playlist_info)
                    result = ui.run()
                except Exception as e:
                    if controller is None:
                        show_error(f"Could not start player: {e}")
                        break
                    raise
                finally:
                    if controller is not None:
                        controller.quit()
                    _active_controller = None

                if result == "next_track" and playlist.has_next():
                    playlist.next()
                elif result == "prev_track" and playlist.has_prev():
                    playlist.prev()
                elif result == "quit":
                    quit_app = True
                    break
                else:
                    break

            if quit_app:
                break


if __name__ == "__main__":
    main()
