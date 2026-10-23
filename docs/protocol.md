# tunetape daemon protocol — version 1

tunetape runs as a small background server (the **daemon**) that owns the play
queue, the mpv process and every source resolver (YouTube, KHInsider, Spotify,
AI discovery, direct audio links). Everything else is a **client** that talks to
it over a local socket:

- the Textual TUI (`tunetape`)
- the command line (`tunetape play <url>`, `tunetape add <url>`, …)
- the MCP server (`tunetape mcp`) that agents such as Claude Code connect to
- any other implementation of this document (the Rust `tunetape-cli`, say)

This file is the contract. A client written against it must work with any
daemon that implements it, in any language. Behaviour not written here is not
part of the contract.

## 1. Transport

### 1.1 Framing

A stream socket carrying **newline-delimited JSON** (NDJSON): every message is
one JSON object encoded as UTF-8 on a single line, terminated by `\n`. Messages
never contain a raw newline (JSON escapes them). Receivers must accept lines up
to 1 MiB and may close the connection on a longer one.

Messages follow [JSON-RPC 2.0](https://www.jsonrpc.org/specification) without
batches:

```json
{"jsonrpc":"2.0","id":1,"method":"player.status","params":{}}
{"jsonrpc":"2.0","id":1,"result":{"state":"idle", "...": "..."}}
{"jsonrpc":"2.0","id":2,"error":{"code":1010,"message":"Unrecognized input","data":null}}
{"jsonrpc":"2.0","method":"event","params":{"type":"track","data":{"...": "..."}}}
```

- `id` is an integer or string chosen by the client; responses echo it.
- `params` is always an object (omitted or `{}` when a method takes none).
- The daemon answers requests **in any order**; clients match by `id`.
- Requests from one connection may be processed concurrently.
- The daemon never sends requests to clients, only responses and `event`
  notifications.

### 1.2 Endpoint discovery

The daemon writes a **discovery file** at `<data_dir>/daemon.json` once it is
listening, atomically (write temp + rename), mode `0600` on POSIX, and removes
it on clean exit:

```json
{
  "protocol": 1,
  "version": "0.10.0",
  "pid": 41237,
  "transport": "unix",
  "path": "/Users/me/.local/share/tunetape/daemon.sock",
  "started_at": "2026-10-23T14:02:11Z"
}
```

```json
{
  "protocol": 1, "version": "0.10.0", "pid": 9120,
  "transport": "tcp", "host": "127.0.0.1", "port": 53817,
  "token": "d1f0c4…(43 url-safe base64 chars)",
  "started_at": "2026-10-23T14:02:11Z"
}
```

`<data_dir>` is the existing tunetape data directory (`tunetape.paths.data_dir`):

| platform | data_dir |
|---|---|
| any, when `TUNETAPE_DATA_DIR` is set to an absolute path | that path |
| Windows | `%LOCALAPPDATA%\tunetape` |
| macOS / Linux | `$XDG_DATA_HOME/tunetape`, else `~/.local/share/tunetape` |

`TUNETAPE_DATA_DIR` is new in protocol 1; it exists so tests and parallel
instances can be isolated, and every client must honour it.

Transports:

- **POSIX (`unix`)**: a Unix domain socket. The daemon uses
  `<data_dir>/daemon.sock` when that path is shorter than 100 bytes, otherwise
  `<tmpdir>/tunetape-<uid>/<hash>.sock` (directory mode `0700`; `<hash>` is
  the first 12 hex digits of the SHA-256 of the data dir path, so isolated
  instances never share a socket). The socket file is `0600`. Clients always
  take the path from the discovery file and never compute it.
- **Windows (`tcp`)**: TCP on `127.0.0.1`, an OS-assigned port, and a random
  32-byte token (url-safe base64, no padding) that the client must present in
  `hello`. The daemon never binds anything other than the loopback address.

### 1.3 Connecting, and starting the daemon on demand

1. Read `daemon.json`. If it is missing or unreadable, go to 4.
2. Connect to the endpoint it names and send `hello`.
3. If the connection is refused, or `hello` fails with anything other than
   `1002`, treat the file as stale and go to 4.
4. Unless the caller asked not to, **spawn** `tunetape daemon` detached from the
   client (new session / process group, stdio to the daemon log, not to the
   terminal) and retry 1–2 every 100 ms for up to 10 s.

Two clients racing to spawn are harmless: the daemon guarantees a single
instance (§1.4), and the loser exits on its own.

### 1.4 Daemon lifecycle

- `tunetape daemon` runs in the foreground; clients spawn it in the background.
  Flags: `--idle-timeout SECONDS` (default `600`, `0` disables).
- **Single instance.** On start the daemon reads an existing discovery file and
  sends it `hello`; if a live daemon answers, the new one prints a message and
  exits with status 0. Otherwise it removes the stale socket/file and listens.
  So that two daemons starting at the same moment can't both listen, a daemon
  holds an exclusive OS lock (`flock` / `msvcrt.locking`) on
  `<data_dir>/daemon.lock` for its whole life and only touches the socket and
  discovery file while holding it.
- **Idle exit.** When the player is `idle` and no client is connected for
  `--idle-timeout` seconds, the daemon exits cleanly.
- **Clean exit** (`daemon.shutdown`, `SIGTERM`, `SIGINT`, idle exit) stops mpv,
  sends a `shutdown` event to subscribers, closes connections and removes the
  socket and discovery file.
- The daemon writes its log to `<data_dir>/daemon.log` (truncated at start).

## 2. Handshake

The first request on every connection must be `hello`. Any other method before
it fails with `1001` and the daemon closes the connection.

`hello` params:

| field | type | |
|---|---|---|
| `protocol` | integer | protocol version the client speaks; `1` |
| `client` | string | free-form name, e.g. `"tunetape-tui/0.10.0"` |
| `token` | string | required on the `tcp` transport, ignored on `unix` |

Result:

```json
{"protocol": 1, "server": "tunetape/0.10.0", "pid": 41237}
```

A `protocol` the daemon does not speak fails with `1002` (`data`:
`{"supported":[1]}`). A missing or wrong token fails with `1001`.

## 3. Data types

### 3.1 Input

Every way to queue audio takes a single **input** string. The daemon classifies
it in this order (first match wins):

| # | input | kind |
|---|---|---|
| 1 | starts with `ytsearch` (e.g. `ytsearch1:daft punk around the world`) | `search` |
| 2 | Spotify URL or `spotify:` URI (track / album / playlist) | `spotify` |
| 3 | YouTube playlist URL | `youtube_playlist` |
| 4 | YouTube video URL | `youtube` |
| 5 | KHInsider album URL | `khinsider` |
| 6 | a path to an existing local file: it starts with `/`, `~/`, `./`, `../` or `file://`, or on Windows `X:\` or `\\`. A path that names no file fails with `1010` (`No such file: …`) | `file` |
| 7 | `http(s)` URL whose path ends in an audio extension (`.mp3 .m4a .aac .flac .ogg .oga .opus .wav .webm .mka .wma .aiff`) | `direct` |
| 8 | any other `http(s)` URL — handed to yt-dlp's generic extractors | `generic` |
| 9 | anything else that is not a URL — plain words | `search` (`ytsearch1:<input>`) |

Any other URL scheme (`ftp:`, `av:`, `lavfi:`, `memory:`, …) fails with `1010`.
An input is a URL with a scheme when it contains no whitespace and matches
`^[A-Za-z][A-Za-z0-9+.-]+:` (at least two scheme characters, so a Windows drive
`C:\` is a path, not a scheme). Schemes other than `http`, `https`, `file` and
`spotify` fail with `1010`; input with whitespace, such as `artist: song` or
`Re:Zero opening`, is plain words (rule 9).
`spotify`, `youtube_playlist` and `khinsider` expand to several items; the rest
to one. Every kind needs mpv; `search`, `youtube`, `youtube_playlist`, `spotify`
and `generic` also need yt-dlp. A missing binary fails with `1011`.

### 3.2 Request

A **request** is one posted input: the "post" the queue is built from. Expanding
it (fetching the album page, the playlist) happens on a daemon worker, so
posting returns at once.

```json
{
  "id": "r-7f3a2c",
  "input": "https://downloads.khinsider.com/game-soundtracks/album/…",
  "kind": "khinsider",
  "title": "Chrono Trigger OST",
  "status": "done",
  "error": null,
  "item_ids": ["i-01a9", "i-01aa", "…"],
  "created_at": "2026-10-23T14:05:00Z"
}
```

`status`: `pending` → `expanding` → `done` | `failed`. `title` is `null` until
known. `error` is a user-facing message when `failed`.

### 3.3 Item

One playable track in the queue.

```json
{
  "id": "i-01a9",
  "request_id": "r-7f3a2c",
  "title": "Corridors of Time",
  "kind": "khinsider",
  "input": "https://downloads.khinsider.com/…/corridors-of-time.mp3",
  "collection": {"title": "Chrono Trigger OST", "index": 4, "count": 64},
  "status": "queued",
  "error": null,
  "duration": null
}
```

- `title` is `null` until known; clients then show `input`.
- `collection` is `null` for single-track requests. `index` is 0-based.
- `status`: `queued` → `resolving` → `playing` → `played`, or `failed`.
  An item that was played and is played again goes back to `playing`.
- `duration` is seconds once mpv knows it, else `null`.

### 3.4 Status

```json
{
  "state": "playing",
  "message": null,
  "position": 83.4,
  "duration": 251.0,
  "volume": 80.0,
  "muted": false,
  "current": { "...": "Item" },
  "index": 4,
  "length": 64,
  "has_next": true,
  "has_prev": true
}
```

- `state`: `idle` (nothing playing), `loading` (resolving or starting the
  current item; `message` says what, e.g. `"Loading track 5/64…"`), `playing`,
  `paused`.
- `position` and `duration` are seconds; `duration` is `0` when unknown (live).
- `volume` is percent, `0`–`130`.
- `current` is `null` and `index` is `-1` when the queue is empty.

## 4. Methods

Every method below except `hello` requires a completed handshake. "→" shows the
result. Unknown params are ignored, so new optional params can be added without
a protocol bump.

### 4.1 Queue

**`queue.add`** — post an input.

| param | type | default | |
|---|---|---|---|
| `input` | string | required | §3.1 |
| `at` | `"end"` \| `"next"` \| `"now"` | `"end"` | `next` inserts after the current item; `now` clears the queue and plays the request's first item as soon as it is expanded |
| `title` | string | `null` | display title for a single-track request (AI picks use it so the item is not shown under yt-dlp's raw title) |
| `start_index` | integer | `0` | for a collection, the track to start at when `at` is `now` (history resume) |
| `wait` | boolean | `false` | block until the request is `done` or `failed` |

→ `{"request": Request}`. Classification errors (`1010`, `1011`) are raised
synchronously; expansion errors arrive as `status: "failed"` (and, with
`wait: true`, as error `1014` carrying the request in `data`).

When the queue was idle and `at` is `end` or `next`, playback starts with the
request's first item.

**`play`** — shorthand for `queue.add` with `at: "now"`. Same params and result.

**`queue.list`** → `{"items": [Item], "index": 4}`

**`queue.jump`** `{index}` → `Status`. Plays item `index` (0-based). Out of
range fails with `1013`.

**`queue.remove`** `{id}` → `{"items": [Item], "index": n}`. Removing the
current item skips to the next one.

**`queue.clear`** → `{"items": [], "index": -1}`. Stops playback.

**`requests.list`** → `{"requests": [Request]}` (most recent first, last 100).

**`requests.get`** `{id}` → `{"request": Request}`. Unknown id fails with `1013`.

### 4.2 Player

| method | params | → |
|---|---|---|
| `player.status` | `levels` bool = false, `stats` bool = false | `Status`, plus `levels` and/or `stats` when asked |
| `player.pause` | | `Status` |
| `player.resume` | | `Status` |
| `player.toggle` | | `Status` |
| `player.next` | | `Status` |
| `player.prev` | | `Status` |
| `player.stop` | | `Status` (stops mpv, keeps the queue, state `idle`) |
| `player.seek` | `seconds` number, `relative` bool = true | `Status` |
| `player.volume` | `value` number (absolute) **or** `delta` number | `Status` |
| `player.mute` | `muted` bool (omit to toggle) | `Status` |

`player.pause/resume/toggle/seek/mute` with nothing playing fail with `1012`.
`player.next` on the last item and `player.prev` on the first fail with `1013`.
Volume is clamped to 0–130 and persisted to the user config, as today.
`player.volume` also works with nothing playing: it sets the level the next
item starts at.

`levels` is `[[rms_dbfs, peak_dbfs], …]` per channel, or `null` (silence is
`-Infinity` in mpv; on the wire it is sent as `-200.0`). `stats` is the object
`MPVController.get_stats()` returns today (`codec`, `bitrate`, `samplerate`,
`channels`, `container`, `ahead`, `cached`, `speed`, `normalized`; any may be
`null`). The TUI polls `player.status` with these flags ~10×/s, so the daemon
must answer it without blocking on resolvers.

### 4.3 Discovery and history

**`ai.discover`** `{request}` → `{"tracks": [{"title": "...", "input": "ytsearch1:..."}]}`.
Records the request as an `ai_search` history entry. Not configured: `1020`;
provider or network failure: `1021`. Playing a pick is `queue.add` with its
`input` and `title`.

**`history.list`** `{limit = 50}` → `{"entries": [HistoryEntry]}` — the entries
of `history.json` as stored today (`type`, `url`, `title`, `last_played`,
`play_count`, and `track_count`/`last_index` for collections).

### 4.4 Events

**`events.subscribe`** `{types?: [string]}` → `{"subscribed": [string]}`. After
it, the daemon pushes notifications on this connection:

```json
{"jsonrpc":"2.0","method":"event","params":{"type":"state","data":{"...":"Status"}}}
```

| type | data | when |
|---|---|---|
| `state` | Status | `state`, `volume`, `muted` or `message` changed (not on every position tick) |
| `track` | Status | the current item changed |
| `queue` | `{"items": [Item], "index": n}` | items added, removed, reordered or changed status |
| `request` | `{"request": Request}` | a request changed status |
| `error` | `{"message": str, "item_id": str\|null}` | a playback failure the user should see |
| `shutdown` | `{}` | the daemon is about to exit |

Omitting `types` subscribes to all. Events are best effort: a client that
cannot keep up may be disconnected.

### 4.5 Daemon

**`daemon.info`** → `{"protocol": 1, "version": str, "pid": int, "started_at": str, "clients": int, "data_dir": str}`

**`daemon.log`** `{limit = 200}` → `{"entries": [{"time": iso8601, "level": str, "message": str}]}`
(the daemon's in-memory debug log, oldest first, for the TUI's Debug screen).

**`daemon.shutdown`** → `{}`; the daemon then exits as in §1.4.

## 5. Errors

Standard JSON-RPC codes plus tunetape's own. `message` is user-facing English
and safe to show as is.

| code | meaning |
|---|---|
| -32700 | parse error (the daemon closes the connection) |
| -32600 | invalid request |
| -32601 | method not found |
| -32602 | invalid params |
| -32603 | internal error |
| 1001 | not authenticated: `hello` missing, or bad token |
| 1002 | unsupported protocol version |
| 1010 | unrecognized or unsupported input |
| 1011 | missing dependency (`mpv`, `yt-dlp`); `message` carries the install hint |
| 1012 | nothing is playing |
| 1013 | not found / out of range (index, item id, request id) |
| 1014 | request failed (`data`: `{"request": Request}`) |
| 1020 | AI discovery is not configured |
| 1021 | AI discovery failed |

## 6. Behaviour the daemon owns

These move out of the TUI's flow (`tunetape/__main__.py`) into the daemon and
keep working as they do today:

- **One mpv per item**, started with the user's volume and the normalize
  setting read from `config.json` when the item starts; volume carries across
  items and is persisted.
- **Auto-advance**: when mpv exits at the end of an item, the next item starts;
  after the last one the player goes `idle` and the queue is kept.
- **Fail forward**: an item that fails to resolve or start is marked `failed`
  with an `error` event and playback moves on; after 5 consecutive failures, or
  on the last item, the player stops.
- **History**: a single-track request is recorded when it starts playing
  (`youtube`, `ai` for `search` inputs, `direct`/`file`/`generic` for the new
  kinds, keeping the `title` param when given); a collection is recorded once
  with `track_count`, and its `last_index` follows the current item.
- **Resolution is lazy**: an item's stream URL is resolved when it becomes
  current, never at post time.

## 7. Versioning

`protocol` is bumped only for incompatible changes. Adding methods, optional
params, result fields, event types or error codes is compatible: clients must
ignore fields and event types they do not know.
