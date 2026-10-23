# tunetape protocol

The daemon protocol as a contract. [`docs/protocol.md`](../docs/protocol.md) is
the authoritative prose; this folder is its machine-readable form plus the way
to check an implementation against it.

| file | what |
|---|---|
| `v1.schema.json` | JSON Schema (draft 2020-12) for protocol version 1 |

## The schema

Everything lives under `$defs`, addressed by JSON pointer:

| pointer | |
|---|---|
| `#/$defs/envelope/request`, `…/success`, `…/error`, `…/event` | the four JSON-RPC message shapes (§1.1) |
| `#/$defs/serverMessage` | anything the daemon may send (a response or an event) |
| `#/$defs/Discovery` | `daemon.json`, either transport (§1.2) |
| `#/$defs/Request`, `Item`, `Status`, `HistoryEntry`, `QueueSnapshot` | the data types (§3) |
| `#/$defs/methods/<method>/params`, `…/result` | every method, e.g. `#/$defs/methods/queue.add/params` (§4) |
| `#/$defs/events/<type>/data` | every event's payload (§4.4) |
| `#/$defs/ErrorCode`, `#/$defs/errors/<code>/data` | the error codes and the ones that carry `data` (§5) |

Results and payloads allow properties the schema does not list: the spec says
clients must ignore fields they do not know, so a daemon may add fields without
a protocol bump. An event of an unknown `type` validates as long as its
envelope does.

## Versioning

The file name carries the protocol version. Compatible changes (new methods,
optional params, result fields, event types, error codes; protocol §7) are
edits to `v1.schema.json`. An incompatible change bumps `protocol` and adds
`v2.schema.json` next to it; daemons that speak both keep both files valid.

## Conformance suite

`tests/conformance/` is a black-box pytest suite: it starts a daemon process in
a temp `TUNETAPE_DATA_DIR` with the fake mpv (`tests/fakes/fake_mpv.py`) as
`TUNETAPE_MPV`, reads the endpoint from `daemon.json` and speaks raw NDJSON to
it — never through `tunetape.client` — validating every response and event
against `v1.schema.json`. No network, no yt-dlp, no audio device; the suite
strips `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from the daemon's environment and
never touches your real data dir.

```sh
pip install -e ".[test]"
pytest tests/conformance            # the Python daemon: python -m tunetape daemon --idle-timeout 0
```

Point it at any other implementation with `TUNETAPE_CONFORMANCE_DAEMON`, a
command line split shell-style. The daemon must honour `TUNETAPE_DATA_DIR`,
`TUNETAPE_MPV` and `TUNETAPE_YTDLP`, and run in the foreground:

```sh
TUNETAPE_CONFORMANCE_DAEMON="/path/to/my-daemon --idle-timeout 0" pytest tests/conformance
```

The suite skips as a whole, with the reason, when `jsonschema`,
`tests/fakes/fake_mpv.py` or (for the default command) `tunetape.daemon` is
missing. On Windows the tests that play audio through the fake mpv are skipped
unless `TUNETAPE_CONFORMANCE_PLAYBACK=1`; the transport, handshake and framing
tests still run there over TCP.
