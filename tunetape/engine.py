"""The daemon's player: requests, the queue, and the playback state machine.

This is the session flow that used to live in ``tunetape/__main__.py``
(``_play_url`` / ``_play_album``: one mpv per track, fail-forward, history,
volume carried across tracks), rebuilt so that nothing blocks the caller:

- posting an input classifies it at once and expands it (album page,
  playlist) on a worker thread;
- each transition to a new item bumps a *generation* counter and hands the
  item to a fresh play worker, which resolves it, starts mpv and watches it
  until it exits. A worker whose generation is no longer current gives up and
  quits whatever it started, so skipping fast never leaves two players up;
- ``status()`` only reads cached state plus mpv's IPC, never a resolver, so
  the TUI can poll it ten times a second.

No sockets here: the daemon (``tunetape/daemon.py``) maps protocol methods onto
these calls and forwards what the ``listener(type, data)`` callback emits as
``event`` notifications (docs/protocol.md §4.4).
"""

import itertools
import math
import threading
import time
import uuid
from datetime import datetime, timezone

from tunetape import ai, config, debug, history, sources
from tunetape.player import MPVController

MAX_CONSECUTIVE_FAILURES = 5
MAX_REQUESTS = 100
# mpv reports silence as -inf dBFS, which JSON can't carry (protocol §4.2).
SILENCE_DB = -200.0
_VOLUME_MAX = 130.0

ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_NOTHING_PLAYING = 1012
ERR_NOT_FOUND = 1013
ERR_REQUEST_FAILED = 1014
ERR_AI_NOT_CONFIGURED = 1020
ERR_AI_FAILED = 1021

# History entry type for each single-track kind (protocol §6).
_HISTORY_TYPES = {"search": "ai", "youtube": "youtube", "direct": "direct",
                  "file": "file", "generic": "generic"}


class EngineError(Exception):
    """A failure with a protocol error code (docs/protocol.md §5)."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Request:
    def __init__(self, text, canonical, kind, at, title, start_index):
        self.id = "r-" + uuid.uuid4().hex[:6]
        self.input = text
        self.canonical = canonical
        self.kind = kind
        self.at = at
        self.title_param = title
        self.start_index = start_index
        self.title = None
        self.status = "pending"
        self.error = None
        self.item_ids = []
        self.created_at = now_iso()
        self.now_seq = 0
        self.recorded = False  # history entry written yet?
        self.settled = threading.Event()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "input": self.input, "kind": self.kind,
            "title": self.title, "status": self.status, "error": self.error,
            "item_ids": list(self.item_ids), "created_at": self.created_at,
        }


class _Item:
    def __init__(self, item_id, request, track, title, collection):
        self.id = item_id
        self.request = request
        self.track = track
        self.title = title
        self.collection = collection
        self.status = "queued"
        self.error = None
        self.duration = None

    @property
    def kind(self):
        return self.request.kind

    def to_dict(self) -> dict:
        return {
            "id": self.id, "request_id": self.request.id, "title": self.title,
            "kind": self.kind, "input": self.track.resolve_hint,
            "collection": dict(self.collection) if self.collection else None,
            "status": self.status, "error": self.error, "duration": self.duration,
        }


def _finite(value, default=0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _db(value) -> float:
    """A dBFS reading safe for JSON: -inf (silence) and NaN become SILENCE_DB."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return SILENCE_DB
    return SILENCE_DB if math.isnan(value) else max(SILENCE_DB, value)


def _int_param(name, value, default=None):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError(ERR_INVALID_PARAMS, f"Invalid params: '{name}' must be an integer.")
    return value


def _num_param(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EngineError(ERR_INVALID_PARAMS, f"Invalid params: '{name}' must be a number.")
    return float(value)


class Engine:
    """The queue and the player. Every public method is thread-safe."""

    def __init__(self, listener=None, *, controller_factory=MPVController):
        self._listener = listener
        self._factory = controller_factory
        # One lock guards all state below. It is never held across a resolver,
        # an mpv launch or an mpv quit — those run on worker threads.
        self._lock = threading.RLock()
        self._requests = []          # oldest first
        self._items = []
        self._index = -1
        self._state = "idle"
        self._message = None
        self._controller = None
        self._paused = False
        self._volume = config.get_volume()
        self._muted = False
        self._gen = 0                # bumped on every transition / stop
        self._failures = 0           # consecutive, for fail-forward
        self._now_seq = 0            # last at=now request posted …
        self._now_applied = 0        # … and the last one that took over the queue
        self._closed = False
        self._threads = set()
        self._item_ids = itertools.count(1)
        # Serializes launches so a superseded worker's mpv is gone before the
        # next one starts, and event publishing so snapshots go out in order.
        self._spawn_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._published = None
        self._track_seq = 0          # bumped whenever an item (re)starts

    # -- events -----------------------------------------------------------------

    def _emit(self, type_, data) -> None:
        if self._listener is None:
            return
        try:
            self._listener(type_, data)
        except Exception as e:  # a listener bug must not stall playback
            debug.exception("Event listener failed", e)

    def _publish(self, queue=False) -> None:
        """Emit ``queue`` when asked, and ``state`` / ``track`` when they changed."""
        with self._publish_lock:
            if queue:
                self._emit("queue", self.queue_list())
            with self._lock:
                track = (self._track_seq, self._index)
            status = self.status()
            key = (status["state"], status["message"], status["volume"], status["muted"])
            last_key, last_track = self._published or (None, None)
            self._published = (key, track)
            if track != last_track:
                self._emit("track", status)
            if key != last_key:
                self._emit("state", status)

    def _request_changed(self, req) -> None:
        with self._lock:
            data = {"request": req.to_dict()}
        self._emit("request", data)

    # -- threads ----------------------------------------------------------------

    def _spawn(self, target, *args) -> None:
        def run():
            try:
                target(*args)
            except Exception as e:  # never let a worker die silently
                debug.exception(f"Engine worker {target.__name__} crashed", e)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        t = threading.Thread(target=run, name=f"tunetape-{target.__name__}", daemon=True)
        with self._lock:
            self._threads.add(t)
        t.start()

    @staticmethod
    def _quit(controller) -> None:
        if controller is not None:
            try:
                controller.quit()
            except Exception as e:
                debug.exception("Stopping mpv failed", e)

    # -- requests ---------------------------------------------------------------

    def add(self, input, at="end", title=None, start_index=0, wait=False) -> dict:
        """Post an input (protocol ``queue.add``). Returns ``{"request": Request}``."""
        if not isinstance(input, str):
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'input' must be a string.")
        if at not in ("end", "next", "now"):
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'at' must be end, next or now.")
        if title is not None and not isinstance(title, str):
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'title' must be a string.")
        start_index = _int_param("start_index", start_index, 0)
        text = input.strip()
        try:
            kind = sources.classify(text)
            sources.check_dependencies(kind)
            canonical = sources.canonical_input(text, kind)
        except sources.SourceError as e:
            raise EngineError(e.code, e.message)

        req = _Request(text, canonical, kind, at, (title or "").strip() or None, start_index)
        with self._lock:
            if self._closed:
                raise EngineError(ERR_INTERNAL, "The daemon is shutting down.")
            if at == "now":
                self._now_seq += 1
                req.now_seq = self._now_seq
            self._requests.append(req)
            del self._requests[:-MAX_REQUESTS]
        debug.log(f"Request {req.id}: {kind} {canonical}")
        self._request_changed(req)

        if kind in sources.COLLECTION_KINDS:
            self._spawn(self._expand, req)
        else:
            self._expand(req)  # string work only: no reason to defer it

        if wait:
            req.settled.wait()
            with self._lock:
                if req.status == "failed":
                    raise EngineError(ERR_REQUEST_FAILED, req.error or "The request failed.",
                                      {"request": req.to_dict()})
        with self._lock:
            return {"request": req.to_dict()}

    def _expand(self, req) -> None:
        with self._lock:
            req.status = "expanding"
        self._request_changed(req)
        try:
            title, tracks = sources.expand(req.canonical, req.kind)
            if not tracks:
                raise ValueError("Nothing playable was found there.")
        except Exception as e:
            message = str(e).strip() or type(e).__name__
            debug.log(f"Request {req.id} failed: {message}", "ERROR")
            with self._lock:
                req.status = "failed"
                req.error = message
            self._request_changed(req)
            req.settled.set()
            return
        self._apply(req, title, tracks)

    def _apply(self, req, title, tracks) -> None:
        """Put an expanded request's items in the queue and start them if due."""
        collection = req.kind in sources.COLLECTION_KINDS
        if req.title_param:
            title = req.title_param
        old = None
        with self._lock:
            req.title = title
            items = []
            for i, track in enumerate(tracks):
                name = track.name if collection else (req.title_param or track.name)
                coll = {"title": title, "index": i, "count": len(tracks)} if collection else None
                items.append(_Item("i-%04x" % next(self._item_ids), req, track, name, coll))
            req.item_ids = [it.id for it in items]

            at = req.at
            if at == "now" and req.now_seq < self._now_applied:
                at = "end"  # a later `play` already took over; don't stomp on it
            start = None
            if self._closed:
                pass
            elif at == "now":
                self._now_applied = req.now_seq
                old = self._leave_current_locked()
                self._gen += 1
                self._state = "idle"
                self._items = items
                self._index = -1
                start = max(0, min(req.start_index, len(items) - 1))
            else:
                pos = len(self._items)
                if at == "next" and self._index >= 0:
                    pos = self._index + 1
                self._items[pos:pos] = items
                if pos <= self._index:
                    self._index += len(items)
                if self._state == "idle":
                    start = pos
            req.status = "done"
            if start is not None:
                self._failures = 0
                self._begin_locked(start, old)
                old = None
        self._quit(old)
        self._request_changed(req)
        req.settled.set()
        self._publish(queue=True)

    def requests_list(self) -> dict:
        with self._lock:
            return {"requests": [r.to_dict() for r in reversed(self._requests)]}

    def request_get(self, request_id) -> dict:
        with self._lock:
            for r in self._requests:
                if r.id == request_id:
                    return {"request": r.to_dict()}
        raise EngineError(ERR_NOT_FOUND, f"No request with id {request_id!r}.")

    # -- the queue --------------------------------------------------------------

    def queue_list(self) -> dict:
        with self._lock:
            return {"items": [it.to_dict() for it in self._items], "index": self._index}

    def jump(self, index) -> dict:
        index = _int_param("index", index)
        if index is None:
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'index' is required.")
        with self._lock:
            if not 0 <= index < len(self._items):
                raise EngineError(ERR_NOT_FOUND, f"There is no track {index + 1} in the queue.")
            self._failures = 0
            self._begin_locked(index)
        self._publish(queue=True)
        return self.status()

    def remove(self, item_id) -> dict:
        old = None
        with self._lock:
            pos = next((i for i, it in enumerate(self._items) if it.id == item_id), None)
            if pos is None:
                raise EngineError(ERR_NOT_FOUND, f"No queue item with id {item_id!r}.")
            current = pos == self._index
            active = self._state != "idle"
            if current:
                old = self._leave_current_locked()
            del self._items[pos]
            if pos < self._index:
                self._index -= 1
            elif current:
                if active and pos < len(self._items):
                    self._begin_locked(pos, old)
                    old = None
                else:
                    if active:
                        self._gen += 1
                        self._set_idle_locked()
                    self._index = min(pos, len(self._items) - 1)
        self._quit(old)
        self._publish(queue=True)
        return self.queue_list()

    def clear(self) -> dict:
        with self._lock:
            old = self._leave_current_locked()
            self._gen += 1
            self._set_idle_locked()
            self._items = []
            self._index = -1
        self._quit(old)
        self._publish(queue=True)
        return self.queue_list()

    # -- the state machine ------------------------------------------------------

    def _set_idle_locked(self, message=None) -> None:
        self._state = "idle"
        self._message = message
        self._paused = False

    def _leave_current_locked(self):
        """Detach the current item and its mpv. Returns the controller to quit."""
        if 0 <= self._index < len(self._items):
            item = self._items[self._index]
            if item.status == "playing":
                item.status = "played"
            elif item.status == "resolving":
                item.status = "queued"
        old, self._controller = self._controller, None
        return old

    def _loading_message(self, item) -> str:
        if item.collection:
            return f"Loading track {item.collection['index'] + 1}/{item.collection['count']}…"
        return f"Loading {item.title}…"

    def _begin_locked(self, index, old=None) -> None:
        """Make item ``index`` current and start a play worker for it."""
        previous = self._leave_current_locked()
        self._gen += 1
        self._track_seq += 1
        self._index = index
        self._paused = False
        item = self._items[index]
        item.status = "resolving"
        item.error = None
        self._state = "loading"
        self._message = self._loading_message(item)
        self._spawn(self._play_worker, self._gen, item, [c for c in (old, previous) if c])

    def _play_worker(self, gen, item, to_quit) -> None:
        for c in to_quit:
            self._quit(c)

        try:
            stream = sources.resolve(item.track, item.kind)
        except Exception as e:
            self._fail(gen, item, f"Could not load track: {str(e).strip() or type(e).__name__}")
            return
        req = item.request
        with self._lock:
            if gen != self._gen:
                return
            if item.track.resolved_title and not req.title_param and not item.collection:
                item.title = item.track.resolved_title
                req.title = item.title

        # Read the settings when the item starts, as the TUI always has, so a
        # change made elsewhere (Settings screen, another client) applies next.
        normalize = bool(config.get_setting("normalize_volume"))
        volume = config.get_volume()
        with self._spawn_lock:
            with self._lock:
                if gen != self._gen:
                    return
            try:
                controller = self._factory(stream, normalize=normalize, volume=volume)
            except Exception as e:
                debug.exception("Track playback failed to start", e)
                self._fail(gen, item, f"Could not start player: {e}")
                return

        with self._lock:
            current = gen == self._gen and not self._closed
            if current:
                self._controller = controller
                self._state = "playing"
                self._message = None
                self._volume = volume
                self._failures = 0
                item.status = "playing"
                muted = self._muted
        if not current:
            self._quit(controller)
            return
        if muted:
            controller.set_mute(True)
        debug.log(f"Now playing: {item.title}")
        self._record_history(item)
        self._publish(queue=True)

        while True:
            with self._lock:
                if gen != self._gen:
                    return  # whoever moved on owns quitting this controller
            code = controller.exit_code()
            if code is not None:
                break
            time.sleep(0.1)
        self._finished(gen, item, controller, code)

    def _finished(self, gen, item, controller, code) -> None:
        """mpv exited on its own: the item ended, or mpv gave up on it."""
        with self._lock:
            if gen != self._gen:
                return
            self._controller = None
        self._quit(controller)  # closes the IPC endpoint, copies mpv's log
        if code != 0:
            self._fail(gen, item, f"Playback failed (mpv exited with status {code}).")
            return
        with self._lock:
            if gen != self._gen:
                return
            item.status = "played"
            if self._index + 1 < len(self._items):
                self._begin_locked(self._index + 1)
            else:
                self._gen += 1
                self._set_idle_locked()
        self._publish(queue=True)

    def _fail(self, gen, item, message) -> None:
        """Fail forward: mark the item, tell the user, move on unless it's hopeless."""
        with self._lock:
            if gen != self._gen:
                return
            item.status = "failed"
            item.error = message
            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._gen += 1
                self._set_idle_locked(
                    f"Stopped after {self._failures} tracks in a row failed. {message}")
            elif self._index + 1 >= len(self._items):
                self._gen += 1
                self._set_idle_locked(message)
            else:
                self._begin_locked(self._index + 1)
        debug.log(f"{item.title}: {message}", "ERROR")
        self._emit("error", {"message": message, "item_id": item.id})
        self._publish(queue=True)

    def _record_history(self, item) -> None:
        req = item.request
        with self._lock:
            first = not req.recorded
            req.recorded = True
        if item.collection:
            if first:
                history.record(req.kind, req.canonical, req.title,
                               track_count=item.collection["count"],
                               last_index=item.collection["index"])
            else:
                history.set_last_index(req.canonical, item.collection["index"])
        elif first:
            history.record(_HISTORY_TYPES.get(req.kind, req.kind), req.canonical, item.title)

    # -- the player -------------------------------------------------------------

    def status(self, levels=False, stats=False) -> dict:
        """The protocol Status. Never waits on a resolver; may ask mpv over IPC."""
        with self._lock:
            controller = self._controller
            state = self._state
            index = self._index
            item = self._items[index] if 0 <= index < len(self._items) else None
            length = len(self._items)
            base = {
                "state": "paused" if state == "playing" and self._paused else state,
                "message": self._message,
                "volume": self._volume,
                "muted": self._muted,
            }
        position = duration = 0.0
        if controller is not None:
            position = _finite(controller.get_position())
            duration = _finite(controller.get_duration())
            if duration > 0 and item is not None:
                with self._lock:
                    item.duration = duration
        elif item is not None and item.duration:
            duration = item.duration
        with self._lock:
            current = item.to_dict() if item is not None else None
        result = {
            "state": base["state"], "message": base["message"],
            "position": position, "duration": duration,
            "volume": base["volume"], "muted": base["muted"],
            "current": current, "index": index if item is not None else -1,
            "length": length,
            "has_next": item is not None and index < length - 1,
            "has_prev": item is not None and index > 0,
        }
        if levels:
            raw = controller.get_levels() if controller is not None else None
            result["levels"] = [[_db(rms), _db(peak)] for rms, peak in raw] if raw else None
        if stats:
            result["stats"] = controller.get_stats() if controller is not None else None
        return result

    def _playing_controller(self):
        with self._lock:
            if self._controller is None:
                raise EngineError(ERR_NOTHING_PLAYING, "Nothing is playing.")
            return self._controller

    def _set_paused(self, paused) -> dict:
        controller = self._playing_controller()
        controller.set_pause(paused)
        with self._lock:
            if self._controller is controller:
                self._paused = paused
        self._publish()
        return self.status()

    def pause(self) -> dict:
        return self._set_paused(True)

    def resume(self) -> dict:
        return self._set_paused(False)

    def toggle(self) -> dict:
        self._playing_controller()
        with self._lock:
            paused = self._paused
        return self._set_paused(not paused)

    def next(self) -> dict:
        with self._lock:
            if not (0 <= self._index < len(self._items) - 1):
                raise EngineError(ERR_NOT_FOUND, "There is no next track.")
            self._failures = 0
            self._begin_locked(self._index + 1)
        self._publish(queue=True)
        return self.status()

    def prev(self) -> dict:
        with self._lock:
            if not (0 < self._index < len(self._items)):
                raise EngineError(ERR_NOT_FOUND, "There is no previous track.")
            self._failures = 0
            self._begin_locked(self._index - 1)
        self._publish(queue=True)
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            old = self._leave_current_locked()
            self._gen += 1
            self._set_idle_locked()
        self._quit(old)
        self._publish(queue=True)
        return self.status()

    def seek(self, seconds, relative=True) -> dict:
        seconds = _num_param("seconds", seconds)
        controller = self._playing_controller()
        controller.seek(seconds, relative=bool(relative))
        return self.status()

    def set_volume(self, value=None, delta=None) -> dict:
        if (value is None) == (delta is None):
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: give either 'value' or 'delta'.")
        with self._lock:
            target = _num_param("value", value) if value is not None else (
                self._volume + _num_param("delta", delta))
            target = max(0.0, min(target, _VOLUME_MAX))
            self._volume = target
            controller = self._controller
        if controller is not None:
            controller.set_volume(target)
        config.set_volume(target)
        self._publish()
        return self.status()

    def mute(self, muted=None) -> dict:
        if muted is not None and not isinstance(muted, bool):
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'muted' must be a boolean.")
        controller = self._playing_controller()
        with self._lock:
            target = (not self._muted) if muted is None else muted
            self._muted = target
        controller.set_mute(target)
        self._publish()
        return self.status()

    # -- discovery and history --------------------------------------------------

    def discover(self, request) -> dict:
        if not isinstance(request, str) or not request.strip():
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'request' must be a non-empty string.")
        if not ai.is_configured():
            raise EngineError(ERR_AI_NOT_CONFIGURED,
                              "AI discovery isn't set up yet. Add an API key in Settings → "
                              "AI discovery (or set ANTHROPIC_API_KEY).")
        try:
            tracks = ai.suggest_tracks(request)
        except (RuntimeError, ConnectionError) as e:
            raise EngineError(ERR_AI_FAILED, str(e))
        history.record("ai_search", request.strip(), request.strip())
        return {"tracks": [{"title": t.name, "input": t.resolve_hint} for t in tracks]}

    def history_list(self, limit=50) -> dict:
        limit = _int_param("limit", limit, 50)
        if limit < 0:
            raise EngineError(ERR_INVALID_PARAMS, "Invalid params: 'limit' must be >= 0.")
        return {"entries": history.load()[:limit]}

    # -- lifecycle --------------------------------------------------------------

    def is_idle(self) -> bool:
        """Nothing playing or loading and no request still expanding."""
        with self._lock:
            return self._state == "idle" and not any(
                r.status in ("pending", "expanding") for r in self._requests)

    def shutdown(self, timeout=8.0) -> None:
        """Stop playback for good and wait (bounded) for the workers to wind down."""
        with self._lock:
            self._closed = True
            old = self._leave_current_locked()
            self._gen += 1
            self._set_idle_locked()
        self._quit(old)
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                pending = [t for t in self._threads if t is not threading.current_thread()]
            if not pending or time.monotonic() >= deadline:
                break
            pending[0].join(max(0.0, min(0.5, deadline - time.monotonic())))
