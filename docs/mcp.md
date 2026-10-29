# tunetape for agents (MCP)

`tunetape mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
server on stdio. It lets an agent such as Claude Code or Claude Desktop play
music, manage the queue and ask for AI suggestions through the same daemon the
TUI and the CLI use, so whatever the agent queues shows up in an open
`tunetape` window, and the other way round.

It is a thin adapter: every tool is one call through `tunetape.client`
([architecture](architecture.md)), and the daemon is started on the first tool
call if it is not running yet. It needs nothing beyond tunetape itself (no MCP
SDK) and works on Python 3.9+ on macOS, Linux and Windows.

## Register it

Press **m** on tunetape's menu: it shows both setups below with the exact path
of the `tunetape` you are running, copies either one, and when Claude Code's
`claude` command is on your `PATH` it can run the `claude mcp add` for you.

Claude Code:

```bash
claude mcp add tunetape -- tunetape mcp
```

Add `--scope user` to have it in every project, not just the current one.

Claude Desktop and other clients that take an `mcpServers` config
(`claude_desktop_config.json` for Claude Desktop):

```json
{
  "mcpServers": {
    "tunetape": {
      "command": "tunetape",
      "args": ["mcp"]
    }
  }
}
```

GUI apps may not see your shell's `PATH`; if the server fails to start, use the
absolute path from `which tunetape` (`where tunetape` on Windows) as `command`.

The server writes its diagnostics to stderr, which clients show as the server
log. The daemon's own log is `daemon.log` in tunetape's data directory.

## Tools

An `input` is anything tunetape plays: a YouTube video or playlist, a Spotify
track/album/playlist, a KHInsider album page, a direct audio URL, a local file
path, or plain words, which are searched on YouTube.

| tool | what it does |
|---|---|
| `play` `{input, title?}` | replace the queue with `input` and play it now |
| `enqueue` `{input, play_next?, title?}` | add to the end of the queue, or right after the current track |
| `now_playing` | state, current track, position, volume, place in the queue |
| `list_queue` | the queue with 1-based positions and item ids |
| `pause` / `resume` | pause or resume playback |
| `next_track` / `previous_track` | skip forward or back one track |
| `stop` | stop playback, keep the queue |
| `set_volume` `{value \| delta}` | absolute percent (0–130) or a relative change |
| `seek` `{seconds, relative?}` | relative by default; `relative: false` seeks to a position |
| `jump_to` `{position}` | play the track at a 1-based queue position |
| `remove_from_queue` `{item_id}` | drop one queue item |
| `clear_queue` | stop and empty the queue |
| `discover` `{request}` | AI suggestions; returns picks with an `input` and `title` for `play`/`enqueue` |
| `history` `{limit?}` | recently played, with the original URL to replay |

`play` and `enqueue` wait until the link is resolved, so a dead link or an
empty search comes back as a tool error straight away rather than as silence.
Errors from the daemon ("Nothing is playing", "Unrecognized input", a missing
`yt-dlp`) reach the agent word for word. On MCP 2025-06-18 and later every
result also carries the daemon's raw JSON as `structuredContent`.

## Example

```
You:    play lofi hip hop
Claude: → play {input: "lofi hip hop"}
        Playing: lofi hip hop radio 📚 beats to relax/study to

You:    queue this KHInsider album after it:
        https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger
Claude: → enqueue {input: "https://downloads.khinsider.com/…/chrono-trigger"}
        Queued: Chrono Trigger OST (64 tracks)

You:    what's playing?
Claude: → now_playing
        Playing: lofi hip hop radio 📚 beats to relax/study to (1/65)  12:04 (live)  vol 80%

You:    skip to Corridors of Time, and a bit quieter
Claude: → list_queue             (finds it at position 6)
        → jump_to {position: 6}
        → set_volume {delta: -10}
        Playing: Corridors of Time · Chrono Trigger OST (6/65)  0:00 / 4:11  vol 70%

You:    something like this but more upbeat
Claude: → discover {request: "upbeat 90s JRPG soundtrack"}
        → enqueue {input: "ytsearch1:…", title: "…"}  for the picks you like
```
