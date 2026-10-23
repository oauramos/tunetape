# tunetape architecture

```
 tunetape (TUI) ─┐
 tunetape play … ├── NDJSON / JSON-RPC over a local socket ──▶ tunetape daemon ──▶ mpv (one per item)
 tunetape mcp ───┤        (docs/protocol.md)                    │
 tunetape-cli ───┘                                              ├─ queue, requests, history
                                                                └─ resolvers: yt-dlp, KHInsider, Spotify, AI
```

The daemon is the only process that starts mpv, resolves sources or writes
listening history. Clients only speak the protocol in
[`protocol.md`](protocol.md).

## Python modules

| module | role |
|---|---|
| `tunetape/sources.py` | `classify(input) -> kind`, `expand(input, kind) -> (title, [Track])`, `resolve(track, kind) -> stream_url`; wraps `player`, `khinsider`, `spotify`, the new `direct`/`file`/`generic` kinds |
| `tunetape/engine.py` | the queue, requests, playback state machine, auto-advance, fail-forward, history and volume persistence; no sockets |
| `tunetape/daemon.py` | `tunetape daemon`: socket server, handshake, method dispatch onto the engine, events, discovery file, idle exit |
| `tunetape/client.py` | `Client`: discovery, spawn-on-demand, thread-safe calls, event subscription |
| `tunetape/cli.py` | `tunetape play/add/pause/…` subcommands |
| `tunetape/mcp_server.py` | `tunetape mcp`: MCP over stdio, a thin adapter onto `Client` |
| `tunetape/ui.py`, `screens.py`, `player_screen.py` | the TUI, a `Client` user |

## Python client API

`tunetape.client` is what the TUI, the CLI and the MCP server use. It is the
in-repo reference client for the protocol.

```python
class DaemonError(Exception):
    code: int          # protocol error code (docs/protocol.md §5)
    message: str       # user-facing
    data: object       # error.data or None

class DaemonUnavailable(DaemonError):  # code 0: could not reach or start the daemon
    ...

class DaemonTimeout(DaemonError):      # code -1: no answer within the call's timeout
    ...

class Client:
    @classmethod
    def connect(cls, *, spawn: bool = True, timeout: float = 10.0,
                name: str = "tunetape-python") -> "Client": ...
    def call(self, method: str, params: dict = None, *, timeout: float = 30.0): ...
    def subscribe(self, callback, types: list = None) -> None: ...  # callback(type, data) on a reader thread
    def close(self) -> None: ...
    # thin wrappers, each returning the method's result
    def play(self, input, *, title=None, start_index=0, wait=False) -> dict: ...
    def add(self, input, *, at="end", title=None, wait=False) -> dict: ...
    def status(self, *, levels=False, stats=False) -> dict: ...
    def queue(self) -> dict: ...
    def jump(self, index) -> dict: ...
    def remove(self, item_id) -> dict: ...
    def clear(self) -> dict: ...
    def pause(self) -> dict: ...
    def resume(self) -> dict: ...
    def toggle(self) -> dict: ...
    def next(self) -> dict: ...
    def prev(self) -> dict: ...
    def stop(self) -> dict: ...
    def seek(self, seconds, *, relative=True) -> dict: ...
    def volume(self, *, value=None, delta=None) -> dict: ...
    def mute(self, muted=None) -> dict: ...
    def requests(self) -> dict: ...
    def request(self, request_id) -> dict: ...
    def discover(self, request) -> dict: ...
    def history(self, limit=50) -> dict: ...
    def info(self) -> dict: ...
    def log(self, limit=200) -> dict: ...
    def shutdown(self) -> dict: ...
```

`call` is safe from several threads at once: one reader thread routes
responses by `id` and hands `event` notifications to the subscriber callback.
A dropped connection makes pending and later calls raise `DaemonUnavailable`.
A call that gets no answer within `timeout` seconds raises `DaemonTimeout`
(code `-1`); the connection stays usable. Every wrapper also takes an optional
`timeout=` keyword, passed to `call` (`None` means the method's default: 30 s,
longer for `wait=True` and `discover`).

Constraints that apply everywhere: Python ≥ 3.9 (no `match`, no `X | Y`
annotations evaluated at runtime), standard library only for the daemon, the
client and the MCP server, and macOS, Linux and Windows all supported.

## Command line

The Python CLI (`tunetape …`) and the Rust `tunetape-cli …` expose the same
subcommands, so either can be used interchangeably. Both start the daemon on
demand (protocol §1.3).

```
tunetape                              the TUI
tunetape play <input…> [--title T]    replace the queue and play (words are joined with spaces)
tunetape add <input…> [--next] [--title T]
tunetape pause | resume | toggle | next | prev | stop | mute | clear
tunetape status [--json]
tunetape queue [--json]
tunetape jump <n>                     1-based for humans; sends index n-1
tunetape volume [<n> | +<n> | -<n>]   no argument prints the volume
tunetape seek <s | +s | -s | mm:ss>   a sign means relative, bare means absolute
tunetape history [--limit N] [--json]
tunetape discover <words…> [--json]
tunetape shutdown                     stop the daemon
tunetape daemon [--idle-timeout S]    run the daemon in the foreground
tunetape mcp                          MCP server on stdio (Python CLI only)
```

`play` and `add` post with `wait: true` and print what was queued
(`Playing: <title>` / `Queued: <title> (12 tracks)`), so a bad link is reported
right away. `status` prints one line, e.g.
`▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%`
(`⏸` paused, `…` loading, `■` idle). `--json` prints the method's raw result,
pretty-printed with a 2-space indent in the daemon's key order.

The other commands print, so both CLIs match:

- player commands (`pause`, `next`, `seek`, …): the status line; `clear`: `Queue cleared`
- `volume`: `Volume 80%`, plus ` (muted)` when muted
- `queue`: `▶ 1. Title  4:11` for the current item, two spaces instead of `▶`
  for the others (the duration only once known)
- `history`: `1. Title  [khinsider, 5/64]` (`[youtube]` for a single track)
- `discover`: `1. Title  (input)`
- `shutdown` never starts a daemon; with none running it prints
  `tunetape daemon is not running` and exits `0`

Exit status: `0` ok, `1` the daemon returned an error (its message goes to
stderr), `2` usage error, `3` the daemon could not be reached or started (a
daemon answering `hello` with `1002` counts as unreachable; no new one is
started).

## Test seams

- `TUNETAPE_DATA_DIR` isolates a daemon and its clients (protocol §1.2).
- `TUNETAPE_MPV` / `TUNETAPE_YTDLP`: a command line used instead of the `mpv` /
  `yt-dlp` executables, split with `shlex.split` (`posix=False` on Windows),
  e.g. `TUNETAPE_MPV="/usr/bin/python3 tests/fakes/fake_mpv.py"`. An override
  whose program (its first word, looked up on `PATH` or as a path) does not
  exist counts as missing (`1011`), never as the real tool, so a test can
  simulate an uninstalled yt-dlp.
- `tests/fakes/fake_mpv.py` is a stand-in for mpv that needs no audio device:
  it accepts mpv's command line, serves mpv's JSON IPC on the
  `--input-ipc-server` endpoint (`get_property`/`set_property`/`cycle`/`seek`/
  `quit` for `time-pos`, `duration`, `pause`, `volume`, `mute`,
  `af-metadata/…`, `audio-params`, …), "plays" for `FAKE_MPV_DURATION` seconds
  (default `2`) and then exits 0. A media argument containing `fake-fail` exits
  with status 2 before opening the IPC endpoint; one containing
  `fake-dur-<seconds>` plays that long instead; `FAKE_MPV_LOG=<file>` appends
  each launch's argv as a JSON line.
- `tests/fakes/fake_ytdlp.py` answers `--get-url --get-title` and
  `--flat-playlist` without the network (input containing `fake-unavailable`
  fails), for `TUNETAPE_YTDLP`.
- `TUNETAPE_DAEMON_TRANSPORT=tcp` makes a POSIX daemon use the Windows
  transport (loopback TCP + token), so that path is tested everywhere.

With those, the engine, the daemon, the CLIs and the conformance suite run
against local `file` inputs with no network, no yt-dlp and no audio device.

## Protocol and clients

- [`protocol/v1.schema.json`](../protocol/v1.schema.json) is the protocol as
  JSON Schema (draft 2020-12): envelopes, data types, every method's params
  and result, every event and the error codes.
  [`protocol/README.md`](../protocol/README.md) explains its layout and
  versioning.
- `tests/conformance/` checks a daemon against it, black-box: it starts the
  daemon in a temp `TUNETAPE_DATA_DIR` with the fake mpv, speaks raw NDJSON to
  the endpoint in `daemon.json` and validates every message against the
  schema. It runs in the default `pytest`; `TUNETAPE_CONFORMANCE_DAEMON` points
  it at another implementation.
- [`rust/tunetape-cli/`](../rust/tunetape-cli/README.md) is a second client,
  in Rust, with the same subcommands, output and exit codes as the Python CLI
  and its own implementation of discovery and spawn-on-demand (§1.2–§1.3).
