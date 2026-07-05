import atexit
import signal
import sys

from tunetape import __version__, ai, config, debug, history, spotify
from tunetape.player import (
    MPVController, check_dependencies, get_stream_info, resolve_with_ytdlp,
    is_youtube_url, is_youtube_playlist_url, fetch_youtube_playlist,
)
from tunetape.khinsider import fetch_album, resolve_track_url, is_khinsider_url
from tunetape.playlist import Playlist
from tunetape.ui import (
    PlayerUI, main_menu_loop, set_accent, show_color_picker, show_debug,
    show_error, show_help, show_history, show_settings, show_welcome,
    show_ai_settings, show_ai_results, show_ai_unavailable, verify_ai_connection,
    with_spinner, prompt_url, prompt_discover, save_terminal_state,
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
        info = with_spinner("Fetching stream…", get_stream_info, url)
    except (ValueError, ConnectionError, RuntimeError) as e:
        show_error(str(e))
        return "menu"

    controller = None
    try:
        controller = MPVController(
            info["stream_url"], normalize=normalize, volume=config.get_volume()
        )
        _active_controller = controller
        debug.log(f"Now playing (YouTube): {info['title']}")
        history.record("youtube", url, info["title"])
        ui = PlayerUI(info["title"], controller)
        result = ui.run()
    except Exception as e:
        if controller is None:
            debug.exception("YouTube playback failed to start", e)
            show_error(f"Could not start player: {e}")
            return "menu"
        raise
    finally:
        if controller is not None:
            # Remember the volume the user left it at before tearing mpv down
            # (the socket is still live here; after quit() it's gone).
            config.set_volume(controller.get_volume())
            controller.quit()
        _active_controller = None

    return result


def _play_search(query: str, normalize: bool, title: str = None) -> str:
    """Play a single track from a ``ytsearch…`` query. Returns 'menu'/'quit'.

    Used by AI discovery (the model yields search queries, not URLs) and by
    replay of the resulting ``ai`` history entries. Mirrors _play_youtube but
    records under the ``ai`` type so the entry re-runs the same search on replay.
    """
    global _active_controller

    try:
        check_dependencies(require_ytdlp=True)
    except RuntimeError as e:
        show_error(str(e))
        return "menu"

    try:
        # AI hints are ``ytsearch1:<query>`` strings, not YouTube URLs, so they
        # go straight to the yt-dlp resolver (which accepts searches) rather than
        # get_stream_info's URL-only path.
        info = with_spinner("Finding audio…", resolve_with_ytdlp, query)
    except (ValueError, ConnectionError, RuntimeError) as e:
        show_error(str(e))
        return "menu"

    display = title or info["title"]
    controller = None
    try:
        controller = MPVController(
            info["stream_url"], normalize=normalize, volume=config.get_volume()
        )
        _active_controller = controller
        debug.log(f"Now playing (AI): {display}")
        history.record("ai", query, display)
        ui = PlayerUI(display, controller)
        result = ui.run()
    except Exception as e:
        if controller is None:
            debug.exception("AI playback failed to start", e)
            show_error(f"Could not start player: {e}")
            return "menu"
        raise
    finally:
        if controller is not None:
            config.set_volume(controller.get_volume())
            controller.quit()
        _active_controller = None

    return result


def _play_album(album, *, type: str, url: str, normalize: bool,
                start_index: int, resolve_fn) -> str:
    """Play an Album as a navigable playlist. Returns 'menu'/'quit'.

    Shared by every multi-track source (KHInsider, Spotify, YouTube playlists).
    ``resolve_fn(track)`` returns the streamable URL for a track (a per-source
    lazy resolver); everything else — fail-forward, volume carry-over, resume
    persistence, Playlist lifecycle — is source-agnostic.
    """
    global _active_controller

    playlist = Playlist(album, start_index=start_index)
    debug.log(f"Album loaded: {album.title} ({playlist.total_tracks} tracks)")
    # Record the album once when playback begins (bumps play_count); the resume
    # position is refreshed per-track via set_last_index without bumping it.
    history.record(
        type, url, album.title,
        track_count=playlist.total_tracks,
        last_index=playlist.current_index,
    )

    result = "menu"
    # Start at the user's last-set volume and carry it across tracks, so a
    # mid-album adjustment sticks for the rest of the album and the next launch.
    volume = config.get_volume()
    # A single dead/unreachable track shouldn't abandon the rest of the album:
    # fail forward to the next track, but stop after too many failures in a row.
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5
    try:
        while True:
            track = playlist.current_track
            failed = False
            stream_url = None
            try:
                stream_url = with_spinner(
                    f"Loading track {playlist.track_label}…", resolve_fn, track
                )
            except (RuntimeError, OSError) as e:
                show_error(f"Could not load track: {e}")
                failed = True

            if not failed:
                controller = None
                try:
                    controller = MPVController(
                        stream_url, normalize=normalize, volume=volume
                    )
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
                        debug.exception("Track playback failed to start", e)
                        show_error(f"Could not start player: {e}")
                        failed = True
                    else:
                        raise
                finally:
                    if controller is not None:
                        # Carry the latest volume to the next track and persist
                        # it (socket is still live before quit()).
                        volume = controller.get_volume()
                        config.set_volume(volume)
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


def _play_khinsider(url: str, normalize: bool, start_index: int = 0) -> str:
    """Fetch and play a KHInsider album from start_index. Returns 'menu'/'quit'."""
    try:
        album = with_spinner("Fetching album…", fetch_album, url)
    except (ValueError, RuntimeError, OSError) as e:
        show_error(str(e))
        return "menu"
    return _play_album(
        album, type="khinsider", url=url, normalize=normalize,
        start_index=start_index, resolve_fn=resolve_track_url,
    )


def _play_spotify(url: str, normalize: bool, start_index: int = 0) -> str:
    """Fetch and play a Spotify playlist/album/track. Returns 'menu'/'quit'.

    Audio for each track is found by searching YouTube, so yt-dlp is required.
    """
    try:
        check_dependencies(require_ytdlp=True)
    except RuntimeError as e:
        show_error(str(e))
        return "menu"
    try:
        album = with_spinner("Fetching Spotify…", spotify.fetch_spotify, url)
    except (ValueError, RuntimeError, OSError) as e:
        show_error(str(e))
        return "menu"
    return _play_album(
        album, type="spotify", url=url, normalize=normalize,
        start_index=start_index, resolve_fn=spotify.resolve_track_url,
    )


def _play_youtube_playlist(url: str, normalize: bool, start_index: int = 0) -> str:
    """Fetch and play a YouTube playlist. Returns 'menu'/'quit'."""
    try:
        check_dependencies(require_ytdlp=True)
    except RuntimeError as e:
        show_error(str(e))
        return "menu"
    try:
        album = with_spinner("Fetching playlist…", fetch_youtube_playlist, url)
    except (ValueError, RuntimeError, OSError) as e:
        show_error(str(e))
        return "menu"
    return _play_album(
        album, type="youtube_playlist", url=url, normalize=normalize,
        start_index=start_index,
        resolve_fn=lambda t: get_stream_info(t.resolve_hint)["stream_url"],
    )


def _play_url(url: str, normalize: bool, start_index: int = 0) -> str:
    """Detect the source from the URL and play it. Returns 'menu'/'quit'.

    Order matters: a YouTube playlist URL also matches a watch URL, so it must
    be checked before the single-video case.
    """
    url = url.strip()
    # AI-discovered tracks are stored as ytsearch queries, not real URLs;
    # replaying one just re-runs the search.
    if url.startswith("ytsearch"):
        return _play_search(url, normalize)
    if spotify.is_spotify_url(url):
        return _play_spotify(url, normalize, start_index)
    if is_youtube_playlist_url(url):
        return _play_youtube_playlist(url, normalize, start_index)
    if is_youtube_url(url):
        return _play_youtube(url, normalize)
    if is_khinsider_url(url):
        return _play_khinsider(url, normalize, start_index)
    show_error("Unrecognized URL. Paste a YouTube, Spotify, or KHInsider link.")
    return "menu"


def _resume(entry: dict) -> int:
    """Resume index for a history entry; a finished collection restarts at 0."""
    resume = int(entry.get("last_index", 0) or 0)
    tc = entry.get("track_count")
    if isinstance(tc, int) and tc > 0 and resume >= tc - 1:
        resume = 0  # finished -> start over from the top
    return resume


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
        elif action == "quit":
            return "quit"
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
            etype = entry.get("type")
            if etype == "ai_search":
                # A saved vibe: re-run discovery for a fresh suggestion list
                # instead of playing a single track.
                result = _discover(normalize, initial_request=entry["url"])
            elif etype == "ai":
                # A played suggestion: re-search but keep the original title so
                # the entry isn't renamed to yt-dlp's raw video title on replay.
                result = _play_search(entry["url"], normalize, entry.get("title"))
            else:
                # Re-detect the source from the stored URL (deterministic) and
                # resume where the user left off for multi-track collections.
                result = _play_url(entry["url"], normalize, start_index=_resume(entry))
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

        show_welcome()
        action = show_ai_unavailable(reason)
        if action == "quit":
            return "quit"
        if action == "s":
            if show_ai_settings() == "quit":
                return "quit"
            continue  # re-check with the updated settings
        return "menu"


def _discover_request(request: str, normalize: bool) -> str:
    """Fetch AI suggestions for ``request``, remember it, and run the results loop.

    Records the request itself as an ``ai_search`` history entry once it yields
    suggestions, so the vibe can be re-run later from Recently played. Returns
    'menu' (leave Discover), 'research' (user wants a fresh prompt), or 'quit'.
    """
    try:
        tracks = with_spinner("Asking the AI…", ai.suggest_tracks, request)
    except (RuntimeError, ConnectionError) as e:
        show_error(str(e))
        return "research"  # let the user rephrase
    # A vibe that produced suggestions is worth remembering; replay re-searches.
    history.record("ai_search", request, request)

    while True:
        show_welcome()
        action, payload = show_ai_results(request, tracks)
        if action == "quit":
            return "quit"
        if action == "back":
            return "menu"
        if action == "research":
            return "research"
        if action == "play":
            show_welcome()
            if _play_search(payload.resolve_hint, normalize, payload.name) == "quit":
                return "quit"
            # Return to the suggestion list so the user can keep exploring.


def _discover(normalize: bool, initial_request: str = None) -> str:
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
            show_welcome()
            request = prompt_discover()
            cmd = request.strip().lower()
            if cmd == "q":
                return "quit"
            if cmd in ("", "b"):
                return "menu"

        result = _discover_request(request, normalize)
        if result == "quit":
            return "quit"
        if result == "menu":
            return "menu"
        request = None  # 'research' -> prompt for a fresh vibe


def _settings_menu() -> str:
    """Show the settings screen and persist toggles. Returns 'menu'/'quit'."""
    while True:
        normalize = bool(config.get_setting("normalize_volume"))
        show_welcome()
        choice = show_settings(normalize)
        if choice == "q":
            return "quit"
        if choice in ("b", ""):
            return "menu"
        elif choice == "1":
            config.set_setting("normalize_volume", not normalize)
        elif choice == "2":
            if show_color_picker() == "quit":
                return "quit"
        elif choice == "3":
            if show_ai_settings() == "quit":
                return "quit"


def main():
    if "--version" in sys.argv[1:] or "-V" in sys.argv[1:]:
        print(f"tunetape {__version__}")
        return

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

    try:
        check_dependencies(require_ytdlp=False)
    except RuntimeError as e:
        show_error(str(e))
        return

    # Apply the user's saved accent color before anything is drawn.
    set_accent(config.get_setting("accent_color"))

    while True:
        normalize = bool(config.get_setting("normalize_volume"))
        choice = main_menu_loop()

        if choice == "q":
            break
        elif choice == "1":
            show_welcome()
            url = prompt_url()
            cmd = url.strip().lower()
            if cmd == "q":
                break
            if cmd in ("", "b"):
                continue  # empty / back -> return to menu
            if _play_url(url, normalize) == "quit":
                break
        elif choice == "2":
            if _recently_played(normalize) == "quit":
                break
        elif choice == "3":
            if _discover(normalize) == "quit":
                break
        elif choice == "4":
            if _settings_menu() == "quit":
                break
        elif choice == "h":
            if show_help() == "quit":
                break
        elif choice == "d":
            if show_debug() == "quit":
                break


if __name__ == "__main__":
    main()
