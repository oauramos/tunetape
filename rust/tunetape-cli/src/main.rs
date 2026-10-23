//! `tunetape-cli`: a small, fast client for the tunetape daemon.
//!
//! Speaks docs/protocol.md directly (NDJSON / JSON-RPC over a Unix socket, or
//! TCP + token on Windows) and mirrors the Python `tunetape` subcommands and
//! their output (docs/architecture.md, "Command line").

mod args;
mod client;
mod discovery;
mod format;
mod shlex;

use std::path::Path;
use std::process::ExitCode;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use args::{Command, Invocation, Simple, UsageError, Volume};
use client::{CallError, Conn, ConnectError};

const EXIT_DAEMON_ERROR: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_UNAVAILABLE: u8 = 3;

/// The protocol's code for a request that failed (§5).
const REQUEST_FAILED: i64 = 1014;

const CALL_TIMEOUT: Duration = Duration::from_secs(30);
/// `play`/`add` wait for the request to expand (an album page, a playlist).
const POST_TIMEOUT: Duration = Duration::from_secs(180);
/// How long `play` waits for its first track to start before it reports
/// `Loading` and returns: long enough for a yt-dlp lookup (as the Python CLI).
const START_WAIT: Duration = Duration::from_secs(45);
const START_POLL: Duration = Duration::from_millis(200);

fn main() -> ExitCode {
    ExitCode::from(run(std::env::args_os().skip(1)))
}

fn run(argv: impl Iterator<Item = std::ffi::OsString>) -> u8 {
    let Invocation { command, json } = match args::parse(argv) {
        Ok(inv) => inv,
        Err(UsageError(msg)) => {
            eprintln!("tunetape-cli: {msg}\n\n{}", args::USAGE);
            return EXIT_USAGE;
        }
    };
    match command {
        Command::Help => {
            println!("{}", args::USAGE);
            return 0;
        }
        Command::Version => {
            println!("tunetape-cli {}", env!("CARGO_PKG_VERSION"));
            return 0;
        }
        _ => {}
    }

    // Starting a daemon only to stop it again is pointless.
    let spawn = command != Command::Shutdown;
    let mut conn = match client::connect(spawn) {
        Ok(conn) => conn,
        Err(ConnectError::NotRunning) => {
            println!("tunetape daemon is not running");
            return 0;
        }
        Err(ConnectError::Protocol(msg)) => {
            eprintln!(
                "tunetape-cli: the daemon does not speak protocol {}: {msg}",
                client::PROTOCOL
            );
            return EXIT_UNAVAILABLE;
        }
        Err(ConnectError::Unavailable(msg)) => {
            eprintln!("tunetape-cli: {msg}");
            return EXIT_UNAVAILABLE;
        }
    };

    match execute(&mut conn, &command, json) {
        Ok(out) => {
            if !out.is_empty() {
                println!("{out}");
            }
            0
        }
        Err(CallError::Rpc { message, .. }) => {
            eprintln!("tunetape-cli: {message}");
            EXIT_DAEMON_ERROR
        }
        Err(CallError::Io(msg)) => {
            eprintln!("tunetape-cli: lost the daemon: {msg}");
            EXIT_UNAVAILABLE
        }
    }
}

/// Whole numbers go on the wire as integers (`90`, not `90.0`).
fn num(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        json!(v as i64)
    } else {
        json!(v)
    }
}

fn call(conn: &mut Conn, method: &str, params: Value) -> Result<Value, CallError> {
    conn.call(method, params, CALL_TIMEOUT)
}

fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).unwrap_or_else(|_| v.to_string())
}

/// The daemon runs in its own cwd, so a relative path to a file that exists
/// here is sent as an absolute one (as the Python CLI does).
fn input_arg(input: &str) -> String {
    let p = Path::new(input);
    if !p.is_absolute() && p.is_file() {
        if let Ok(abs) = std::path::absolute(p) {
            return abs.to_string_lossy().into_owned();
        }
    }
    input.to_string()
}

fn post_params(input: &str, at: Option<&str>, title: &Option<String>) -> Value {
    let mut p = Map::new();
    p.insert("input".into(), json!(input_arg(input)));
    if let Some(at) = at {
        p.insert("at".into(), json!(at));
    }
    if let Some(title) = title {
        p.insert("title".into(), json!(title));
    }
    p.insert("wait".into(), json!(true));
    Value::Object(p)
}

/// The queue's items that belong to `request`, in queue order.
fn request_items(conn: &mut Conn, request: &Value) -> Result<Option<Vec<Value>>, CallError> {
    let ids: Vec<&str> = request["item_ids"]
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let queue = call(conn, "queue.list", json!({}))?;
    let items: Vec<Value> = queue["items"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter(|it| it["id"].as_str().is_some_and(|id| ids.contains(&id)))
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    Ok(if ids.is_empty() { None } else { Some(items) })
}

/// Resolution is lazy, so a dead link only shows once the daemon tries to
/// play it: wait until one of `play`'s items is playing, or all of them
/// failed. Returns the item that started, if any.
fn wait_started(conn: &mut Conn, request: &Value) -> Result<Option<Value>, CallError> {
    let deadline = Instant::now() + START_WAIT;
    while Instant::now() < deadline {
        let Some(items) = request_items(conn, request)? else {
            return Ok(None);
        };
        if items.is_empty() {
            return Ok(None); // replaced by a later play
        }
        let status = |it: &Value| it["status"].as_str().unwrap_or("").to_string();
        if let Some(it) = items
            .iter()
            .find(|it| matches!(status(it).as_str(), "playing" | "played"))
        {
            return Ok(Some(it.clone()));
        }
        let failed: Vec<&Value> = items.iter().filter(|it| status(it) == "failed").collect();
        if let Some(last) = failed.last() {
            let idle = call(conn, "player.status", json!({}))?["state"] == "idle";
            if failed.len() == items.len() || idle {
                let error = last["error"].as_str().unwrap_or("Playback failed.");
                return Err(CallError::Rpc {
                    code: REQUEST_FAILED,
                    message: format!(
                        "Could not play {}: {error}",
                        format::request_label(request, None)
                    ),
                });
            }
        }
        thread::sleep(START_POLL);
    }
    Ok(None)
}

/// Run one command; return what to print on stdout.
fn execute(conn: &mut Conn, cmd: &Command, json: bool) -> Result<String, CallError> {
    let show = |v: &Value, human: fn(&Value) -> String| if json { pretty(v) } else { human(v) };
    Ok(match cmd {
        Command::Help | Command::Version => String::new(),
        Command::Play { input, title } => {
            let result = conn.call("play", post_params(input, None, title), POST_TIMEOUT)?;
            let item = wait_started(conn, &result["request"])?;
            if json {
                pretty(&result)
            } else {
                format::play_line(&result["request"], item.as_ref())
            }
        }
        Command::Add { input, next, title } => {
            let at = if *next { "next" } else { "end" };
            let result = conn.call(
                "queue.add",
                post_params(input, Some(at), title),
                POST_TIMEOUT,
            )?;
            if json {
                pretty(&result)
            } else {
                let items = request_items(conn, &result["request"])?.unwrap_or_default();
                let first = items.first().and_then(|it| it["title"].as_str());
                format::queued_line(&result["request"], first)
            }
        }
        Command::Simple(Simple::Clear) => {
            let r = call(conn, Simple::Clear.method(), json!({}))?;
            show(&r, |_| "Queue cleared".to_string())
        }
        Command::Simple(s) => show(&call(conn, s.method(), json!({}))?, format::status_line),
        Command::Status => show(
            &call(conn, "player.status", json!({}))?,
            format::status_line,
        ),
        Command::Queue => show(&call(conn, "queue.list", json!({}))?, format::queue_lines),
        Command::Jump { index } => show(
            &call(conn, "queue.jump", json!({ "index": index }))?,
            format::status_line,
        ),
        Command::Volume(v) => {
            let status = match v {
                Volume::Show => call(conn, "player.status", json!({}))?,
                Volume::Set(v) => call(conn, "player.volume", json!({ "value": num(*v) }))?,
                Volume::Delta(d) => call(conn, "player.volume", json!({ "delta": num(*d) }))?,
            };
            show(&status, format::volume_line)
        }
        Command::Seek { seconds, relative } => show(
            &call(
                conn,
                "player.seek",
                json!({ "seconds": num(*seconds), "relative": relative }),
            )?,
            format::status_line,
        ),
        Command::History { limit } => show(
            &call(conn, "history.list", json!({ "limit": limit }))?,
            format::history_lines,
        ),
        Command::Discover { request } => show(
            &conn.call("ai.discover", json!({ "request": request }), POST_TIMEOUT)?,
            format::discover_lines,
        ),
        Command::Shutdown => {
            call(conn, "daemon.shutdown", json!({}))?;
            "tunetape daemon stopped.".to_string()
        }
    })
}
