//! `tunetape-cli`: a small, fast client for the tunetape daemon.
//!
//! Speaks docs/protocol.md directly (NDJSON / JSON-RPC over a Unix socket, or
//! TCP + token on Windows) and mirrors the Python `tunetape` subcommands.

mod args;
mod client;
mod discovery;
mod format;
mod shlex;

use std::process::ExitCode;
use std::time::Duration;

use serde_json::{json, Map, Value};

use args::{Command, Simple, UsageError, Volume};
use client::{CallError, Conn, ConnectError};

const EXIT_DAEMON_ERROR: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_UNAVAILABLE: u8 = 3;

const CALL_TIMEOUT: Duration = Duration::from_secs(30);
/// `play`/`add` wait for the request to expand (an album page, a playlist).
const POST_TIMEOUT: Duration = Duration::from_secs(180);

fn main() -> ExitCode {
    ExitCode::from(run(std::env::args_os().skip(1)))
}

fn run(argv: impl Iterator<Item = std::ffi::OsString>) -> u8 {
    let cmd = match args::parse(argv) {
        Ok(cmd) => cmd,
        Err(UsageError(msg)) => {
            eprintln!("tunetape-cli: {msg}\n\n{}", args::USAGE);
            return EXIT_USAGE;
        }
    };
    match cmd {
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
    let spawn = cmd != Command::Shutdown;
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

    match execute(&mut conn, &cmd) {
        Ok(out) => {
            if !out.is_empty() {
                println!("{out}");
            }
            0
        }
        Err(CallError::Rpc { message, .. }) => {
            eprintln!("{message}");
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

fn post_params(input: &str, at: Option<&str>, title: &Option<String>) -> Value {
    let mut p = Map::new();
    p.insert("input".into(), json!(input));
    if let Some(at) = at {
        p.insert("at".into(), json!(at));
    }
    if let Some(title) = title {
        p.insert("title".into(), json!(title));
    }
    p.insert("wait".into(), json!(true));
    Value::Object(p)
}

/// Run one command; return what to print on stdout.
fn execute(conn: &mut Conn, cmd: &Command) -> Result<String, CallError> {
    Ok(match cmd {
        Command::Help | Command::Version => String::new(),
        Command::Play { input, title } => {
            let r = conn.call("play", post_params(input, None, title), POST_TIMEOUT)?;
            format::posted_line("Playing", &r["request"])
        }
        Command::Add { input, next, title } => {
            let at = if *next { "next" } else { "end" };
            let r = conn.call(
                "queue.add",
                post_params(input, Some(at), title),
                POST_TIMEOUT,
            )?;
            format::posted_line("Queued", &r["request"])
        }
        Command::Simple(Simple::Clear) => {
            call(conn, Simple::Clear.method(), json!({}))?;
            "Queue cleared".to_string()
        }
        Command::Simple(s) => format::status_line(&call(conn, s.method(), json!({}))?),
        Command::Status { json } => {
            let s = call(conn, "player.status", json!({}))?;
            if *json {
                pretty(&s)
            } else {
                format::status_line(&s)
            }
        }
        Command::Queue { json } => {
            let q = call(conn, "queue.list", json!({}))?;
            if *json {
                pretty(&q)
            } else {
                format::queue_lines(&q)
            }
        }
        Command::Jump { index } => {
            format::status_line(&call(conn, "queue.jump", json!({ "index": index }))?)
        }
        Command::Volume(Volume::Show) => {
            format::volume_line(&call(conn, "player.status", json!({}))?)
        }
        Command::Volume(Volume::Set(v)) => {
            format::volume_line(&call(conn, "player.volume", json!({ "value": num(*v) }))?)
        }
        Command::Volume(Volume::Delta(d)) => {
            format::volume_line(&call(conn, "player.volume", json!({ "delta": num(*d) }))?)
        }
        Command::Seek { seconds, relative } => format::status_line(&call(
            conn,
            "player.seek",
            json!({ "seconds": num(*seconds), "relative": relative }),
        )?),
        Command::History { limit, json } => {
            let params = match limit {
                Some(n) => json!({ "limit": n }),
                None => json!({}),
            };
            let h = call(conn, "history.list", params)?;
            if *json {
                pretty(&h)
            } else {
                format::history_lines(&h)
            }
        }
        Command::Discover { request, json } => {
            let r = conn.call("ai.discover", json!({ "request": request }), POST_TIMEOUT)?;
            if *json {
                pretty(&r)
            } else {
                format::discover_lines(&r)
            }
        }
        Command::Shutdown => {
            call(conn, "daemon.shutdown", json!({}))?;
            "tunetape daemon stopped".to_string()
        }
    })
}
