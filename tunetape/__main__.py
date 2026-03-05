import atexit
import signal
import sys

from tunetape.player import MPVController, check_dependencies, get_stream_info
from tunetape.ui import PlayerUI, show_error, show_header, show_loading, show_menu, prompt_url

_active_controller = None


def _cleanup():
    global _active_controller
    if _active_controller is not None:
        try:
            _active_controller.quit()
        except Exception:
            pass
        _active_controller = None


def _signal_handler(sig, frame):
    _cleanup()
    sys.exit(0)


def main():
    atexit.register(_cleanup)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        check_dependencies()
    except RuntimeError as e:
        show_error(str(e))
        return

    global _active_controller

    while True:
        show_header()
        choice = show_menu()

        if choice == "q":
            break
        elif choice == "1":
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

            try:
                controller = MPVController(info["stream_url"])
                _active_controller = controller
            except Exception as e:
                show_error(f"Could not start player: {e}")
                continue

            try:
                ui = PlayerUI(info["title"], controller)
                result = ui.run()
            finally:
                controller.quit()
                _active_controller = None

            if result == "quit":
                break


if __name__ == "__main__":
    main()
