# tunetape-cli

A small, fast command-line client for the tunetape daemon, written in Rust. It
speaks [the protocol](../../docs/protocol.md) directly — NDJSON / JSON-RPC over
a Unix socket, or TCP plus a token on Windows — and has the same subcommands,
output and exit codes as the Python `tunetape` CLI, so either works in scripts,
key bindings or status bars. It starts the daemon on demand.

It does not include the TUI, `daemon` or `mcp`: those stay in Python, and the
daemon itself is still `tunetape daemon`.

## Build and install

Stable Rust (edition 2021):

```sh
cargo build --release --manifest-path rust/tunetape-cli/Cargo.toml
cargo install --path rust/tunetape-cli         # puts tunetape-cli in ~/.cargo/bin
```

Dependencies are `serde`, `serde_json` and `lexopt`; no async runtime, no
network crates.

## Usage

```
tunetape-cli play <input…> [--title T]    replace the queue and play (words are joined with spaces)
tunetape-cli add <input…> [--next] [--title T]
tunetape-cli pause | resume | toggle | next | prev | stop | mute | clear
tunetape-cli status [--json]
tunetape-cli queue [--json]
tunetape-cli jump <n>                     1-based; sends index n-1
tunetape-cli volume [<n> | +<n> | -<n>]   no argument prints the volume
tunetape-cli seek <s | +s | -s | mm:ss>   a sign means relative, bare means absolute
tunetape-cli history [--limit N] [--json]
tunetape-cli discover <words…> [--json]
tunetape-cli shutdown                     stop the daemon (never starts one)

--json on any command but shutdown prints the raw result
```

```console
$ tunetape-cli play lofi hip hop
Playing: lofi hip hop radio - beats to relax/study to
$ tunetape-cli status
▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%
```

`status` shows `▶` playing, `⏸` paused, `…` loading and `■` idle. Every
command but `shutdown` takes `--json` to print the method's raw result
(2-space indent, the daemon's key order). Player commands print the resulting
status line. `play` waits (up to 45 s) for its first track to start, so a dead
link is reported right away (`Could not play …`, exit 1), and prints
`Loading: …` if it is still resolving. A relative path to a local file is sent
as an absolute one. The output strings are the Python CLI's
(docs/architecture.md, "Command line").

Exit status: `0` ok, `1` the daemon returned an error (its message goes to
stderr), `2` usage error, `3` the daemon could not be reached or started.

## Environment

| variable | |
|---|---|
| `TUNETAPE_DATA_DIR` | data dir holding `daemon.json` (absolute path); default `%LOCALAPPDATA%\tunetape` on Windows, `$XDG_DATA_HOME/tunetape` or `~/.local/share/tunetape` elsewhere |
| `TUNETAPE_DAEMON_CMD` | command that starts the daemon, split shell-style; default `tunetape daemon` |

When `daemon.json` is missing or stale the CLI spawns the daemon detached (a
new session on POSIX; `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on
Windows; stdio to null) and retries every 100 ms for up to 10 s.

## Tests

```sh
cd rust/tunetape-cli
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
```

Unit tests cover argument parsing, output formatting, discovery parsing and
the data dir rules. `tests/cli.rs` runs the built binary against an in-process
mock daemon and asserts the exact requests it sends, its stdout and its exit
status, including spawn-on-demand from a stale `daemon.json`.
