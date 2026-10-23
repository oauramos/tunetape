//! Runs the real `tunetape-cli` binary against an in-process mock daemon: a
//! small NDJSON server (a Unix socket on POSIX, TCP + token on Windows) that
//! records every request and answers from a per-test script.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

const BIN: &str = env!("CARGO_BIN_EXE_tunetape-cli");
const TOKEN: &str = "c2VjcmV0LXRva2VuLWZvci10aGUtbW9jay1kYWVtb24";

/// What the mock answers to a method: a result or an error.
enum Reply {
    Ok(Value),
    Err(i64, &'static str),
}

type Script = dyn Fn(&str, &Value) -> Reply + Send + Sync;

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> TempDir {
        static N: AtomicUsize = AtomicUsize::new(0);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .subsec_nanos();
        let name = format!(
            "ttcli-{}-{}-{nanos}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        );
        let mut base = std::env::temp_dir();
        // Keep <dir>/daemon.sock under the ~104-byte sun_path limit.
        if cfg!(unix) && base.join(&name).join("daemon.sock").as_os_str().len() >= 100 {
            base = PathBuf::from("/tmp");
        }
        let dir = base.join(name);
        fs::create_dir_all(&dir).unwrap();
        TempDir(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Mock {
    dir: TempDir,
    requests: Arc<Mutex<Vec<Value>>>,
    discovery: Value,
}

impl Mock {
    /// Start listening and write daemon.json.
    fn start(script: impl Fn(&str, &Value) -> Reply + Send + Sync + 'static) -> Mock {
        let mock = Mock::listen(script);
        mock.write_discovery(&mock.dir.path().join("daemon.json"));
        mock
    }

    /// Start listening without writing daemon.json (for the spawn test).
    fn listen(script: impl Fn(&str, &Value) -> Reply + Send + Sync + 'static) -> Mock {
        let dir = TempDir::new();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let script: Arc<Script> = Arc::new(script);
        let discovery = serve(dir.path(), requests.clone(), script);
        Mock {
            dir,
            requests,
            discovery,
        }
    }

    fn write_discovery(&self, path: &Path) {
        fs::write(path, self.discovery.to_string()).unwrap();
    }

    fn data_dir(&self) -> &Path {
        self.dir.path()
    }

    /// Every request received, `hello` included, as (method, params).
    fn calls(&self) -> Vec<(String, Value)> {
        self.requests
            .lock()
            .unwrap()
            .iter()
            .map(|r| {
                (
                    r["method"].as_str().unwrap_or("").to_string(),
                    r.get("params").cloned().unwrap_or(Value::Null),
                )
            })
            .collect()
    }

    /// The requests after `hello`, as (method, params).
    fn commands(&self) -> Vec<(String, Value)> {
        self.calls()
            .into_iter()
            .filter(|(m, _)| m != "hello")
            .collect()
    }

    fn run(&self, args: &[&str]) -> Output {
        run_in(self.data_dir(), args, &[])
    }
}

fn run_in(data_dir: &Path, args: &[&str], env: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(BIN);
    cmd.args(args)
        .env("TUNETAPE_DATA_DIR", data_dir)
        // A spawn that should not happen fails loudly instead of starting a
        // real daemon.
        .env(
            "TUNETAPE_DAEMON_CMD",
            "/nonexistent/tunetape-daemon-for-tests",
        );
    for (k, v) in env {
        cmd.env(k, v);
    }
    cmd.output().unwrap()
}

fn stdout(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}

fn stderr(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).into_owned()
}

fn handle(
    stream: impl std::io::Read + Write,
    requests: Arc<Mutex<Vec<Value>>>,
    script: Arc<Script>,
) {
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    while reader.read_line(&mut line).map(|n| n > 0).unwrap_or(false) {
        let req: Value = serde_json::from_str(&line).expect("client sent invalid JSON");
        line.clear();
        assert_eq!(req["jsonrpc"], "2.0", "not JSON-RPC 2.0: {req}");
        assert!(req.get("id").is_some(), "request without id: {req}");
        requests.lock().unwrap().push(req.clone());
        let method = req["method"].as_str().unwrap_or("").to_string();
        let params = req.get("params").cloned().unwrap_or(json!({}));
        let reply = if method == "hello" {
            let token_ok = cfg!(unix) || params["token"] == TOKEN;
            if !token_ok {
                Reply::Err(1001, "Not authenticated")
            } else {
                script(&method, &params)
            }
        } else {
            script(&method, &params)
        };
        let out = reader.get_mut();
        // Clients must skip notifications they did not ask for.
        let noise = json!({"jsonrpc": "2.0", "method": "event",
                           "params": {"type": "brand-new-event", "data": {}}});
        let msg = match reply {
            Reply::Ok(result) => json!({"jsonrpc": "2.0", "id": req["id"], "result": result}),
            Reply::Err(code, message) => json!({"jsonrpc": "2.0", "id": req["id"],
                "error": {"code": code, "message": message, "data": null}}),
        };
        if out
            .write_all(format!("{noise}\n{msg}\n").as_bytes())
            .is_err()
        {
            return;
        }
    }
}

#[cfg(unix)]
fn serve(dir: &Path, requests: Arc<Mutex<Vec<Value>>>, script: Arc<Script>) -> Value {
    use std::os::unix::net::UnixListener;
    let path = dir.join("daemon.sock");
    let listener = UnixListener::bind(&path).unwrap();
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let (r, s) = (requests.clone(), script.clone());
            thread::spawn(move || handle(stream, r, s));
        }
    });
    json!({"protocol": 1, "version": "0.10.0-mock", "pid": std::process::id(),
           "transport": "unix", "path": path, "started_at": "2026-10-23T14:02:11Z"})
}

#[cfg(not(unix))]
fn serve(_dir: &Path, requests: Arc<Mutex<Vec<Value>>>, script: Arc<Script>) -> Value {
    use std::net::TcpListener;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let (r, s) = (requests.clone(), script.clone());
            thread::spawn(move || handle(stream, r, s));
        }
    });
    json!({"protocol": 1, "version": "0.10.0-mock", "pid": std::process::id(),
           "transport": "tcp", "host": "127.0.0.1", "port": port, "token": TOKEN,
           "started_at": "2026-10-23T14:02:11Z"})
}

// ---------------------------------------------------------------- canned data

fn hello() -> Reply {
    Reply::Ok(json!({"protocol": 1, "server": "tunetape/0.10.0-mock", "pid": 1}))
}

fn chrono_status(state: &str) -> Value {
    json!({
        "state": state, "message": null, "position": 83.4, "duration": 251.0,
        "volume": 80.0, "muted": false, "index": 4, "length": 64,
        "has_next": true, "has_prev": true,
        "current": {"id": "i-01a9", "request_id": "r-7f3a2c", "title": "Corridors of Time",
                    "kind": "khinsider", "input": "https://downloads.khinsider.com/x.mp3",
                    "collection": {"title": "Chrono Trigger OST", "index": 4, "count": 64},
                    "status": "playing", "error": null, "duration": 251.0},
        "some_future_field": {"ignored": true}
    })
}

fn request(title: &str, items: usize) -> Value {
    let ids: Vec<String> = (0..items).map(|i| format!("i-{i}")).collect();
    json!({"request": {"id": "r-1", "input": "x", "kind": "search", "title": title,
                       "status": "done", "error": null, "item_ids": ids,
                       "created_at": "2026-10-23T14:05:00Z"}})
}

/// A daemon that answers everything with a playing status.
fn player() -> Mock {
    Mock::start(|method, _| match method {
        "hello" => hello(),
        "play" => Reply::Ok(request("lofi hip hop radio", 1)),
        "queue.add" => Reply::Ok(request("Chrono Trigger OST", 12)),
        "queue.clear" => Reply::Ok(json!({"items": [], "index": -1})),
        "daemon.shutdown" => Reply::Ok(json!({})),
        _ => Reply::Ok(chrono_status("playing")),
    })
}

// ---------------------------------------------------------------- tests

#[test]
fn hello_comes_first_and_names_the_client() {
    let mock = player();
    let out = mock.run(&["status"]);
    assert!(out.status.success(), "{}", stderr(&out));
    let calls = mock.calls();
    assert_eq!(calls[0].0, "hello");
    assert_eq!(calls[0].1["protocol"], 1);
    assert!(calls[0].1["client"]
        .as_str()
        .unwrap()
        .starts_with("tunetape-cli/"));
    if cfg!(windows) {
        assert_eq!(calls[0].1["token"], TOKEN);
    }
    assert_eq!(mock.commands(), [("player.status".to_string(), json!({}))]);
}

#[test]
fn status_prints_one_line() {
    let mock = player();
    let out = mock.run(&["status"]);
    assert_eq!(out.status.code(), Some(0));
    assert_eq!(
        stdout(&out),
        "▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%\n"
    );
}

#[test]
fn status_json_prints_the_raw_result() {
    let mock = player();
    let out = mock.run(&["status", "--json"]);
    assert_eq!(out.status.code(), Some(0));
    let printed: Value = serde_json::from_str(&stdout(&out)).unwrap();
    assert_eq!(printed, chrono_status("playing"));
    // Key order is the daemon's.
    assert!(stdout(&out).trim_start().starts_with("{\n  \"state\""));
}

#[test]
fn play_joins_words_and_waits() {
    let mock = player();
    let out = mock.run(&["play", "lofi", "hip", "hop"]);
    assert_eq!(out.status.code(), Some(0), "{}", stderr(&out));
    assert_eq!(stdout(&out), "Playing: lofi hip hop radio\n");
    assert_eq!(
        mock.commands(),
        [(
            "play".to_string(),
            json!({"input": "lofi hip hop", "wait": true})
        )]
    );
}

#[test]
fn add_next_with_title() {
    let mock = player();
    let out = mock.run(&[
        "add",
        "--next",
        "--title",
        "Chrono",
        "https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger",
    ]);
    assert_eq!(out.status.code(), Some(0), "{}", stderr(&out));
    assert_eq!(stdout(&out), "Queued: Chrono Trigger OST (12 tracks)\n");
    assert_eq!(
        mock.commands(),
        [(
            "queue.add".to_string(),
            json!({"input": "https://downloads.khinsider.com/game-soundtracks/album/chrono-trigger",
                   "at": "next", "title": "Chrono", "wait": true})
        )]
    );
}

#[test]
fn add_defaults_to_the_end() {
    let mock = player();
    assert!(mock.run(&["add", "/music/a.mp3"]).status.success());
    assert_eq!(
        mock.commands(),
        [(
            "queue.add".to_string(),
            json!({"input": "/music/a.mp3", "at": "end", "wait": true})
        )]
    );
}

#[test]
fn player_commands_send_their_methods() {
    let mock = player();
    for (cmd, method) in [
        ("pause", "player.pause"),
        ("resume", "player.resume"),
        ("toggle", "player.toggle"),
        ("next", "player.next"),
        ("prev", "player.prev"),
        ("stop", "player.stop"),
        ("mute", "player.mute"),
    ] {
        let out = mock.run(&[cmd]);
        assert_eq!(out.status.code(), Some(0), "{cmd}: {}", stderr(&out));
        assert!(stdout(&out).starts_with("▶ Corridors of Time"), "{cmd}");
        assert_eq!(
            mock.commands().last().unwrap(),
            &(method.to_string(), json!({}))
        );
    }
    let out = mock.run(&["clear"]);
    assert_eq!(stdout(&out), "Queue cleared\n");
    assert_eq!(mock.commands().last().unwrap().0, "queue.clear");
}

#[test]
fn jump_is_one_based() {
    let mock = player();
    assert!(mock.run(&["jump", "3"]).status.success());
    assert_eq!(
        mock.commands(),
        [("queue.jump".to_string(), json!({"index": 2}))]
    );
}

#[test]
fn seek_forms_on_the_wire() {
    let mock = player();
    for args in [
        ["seek", "+10"],
        ["seek", "-10"],
        ["seek", "90"],
        ["seek", "1:30"],
    ] {
        let out = mock.run(&args);
        assert_eq!(out.status.code(), Some(0), "{args:?}: {}", stderr(&out));
    }
    let params: Vec<Value> = mock.commands().into_iter().map(|(_, p)| p).collect();
    assert_eq!(
        params,
        [
            json!({"seconds": 10, "relative": true}),
            json!({"seconds": -10, "relative": true}),
            json!({"seconds": 90, "relative": false}),
            json!({"seconds": 90, "relative": false}),
        ]
    );
}

#[test]
fn volume_forms_on_the_wire() {
    let mock = player();
    let out = mock.run(&["volume"]);
    assert_eq!(stdout(&out), "Volume 80%\n");
    mock.run(&["volume", "+5"]);
    mock.run(&["volume", "-5"]);
    mock.run(&["volume", "65"]);
    assert_eq!(
        mock.commands(),
        [
            ("player.status".to_string(), json!({})),
            ("player.volume".to_string(), json!({"delta": 5})),
            ("player.volume".to_string(), json!({"delta": -5})),
            ("player.volume".to_string(), json!({"value": 65})),
        ]
    );
}

#[test]
fn queue_and_history_listings() {
    let mock = Mock::start(|method, params| match method {
        "hello" => hello(),
        "queue.list" => Reply::Ok(json!({"index": 0, "items": [
            {"id": "i-1", "request_id": "r-1", "title": "One", "kind": "file", "input": "/a.mp3",
             "collection": null, "status": "playing", "error": null, "duration": 61.0},
            {"id": "i-2", "request_id": "r-2", "title": "Two", "kind": "file", "input": "/b.mp3",
             "collection": null, "status": "queued", "error": null, "duration": null}
        ]})),
        "history.list" => {
            assert_eq!(params["limit"], 2);
            Reply::Ok(json!({"entries": [
                {"type": "khinsider", "url": "u", "title": "Chrono Trigger OST",
                 "last_played": "2026-10-23T14:05:00Z", "play_count": 2,
                 "track_count": 64, "last_index": 4}
            ]}))
        }
        _ => Reply::Err(-32601, "Method not found"),
    });
    let out = mock.run(&["queue"]);
    assert_eq!(stdout(&out), "▶ 1. One  1:01\n  2. Two\n");
    let out = mock.run(&["history", "--limit", "2"]);
    assert_eq!(out.status.code(), Some(0), "{}", stderr(&out));
    assert_eq!(stdout(&out), "1. Chrono Trigger OST  [khinsider, 5/64]\n");
}

#[test]
fn daemon_error_goes_to_stderr_with_status_1() {
    let mock = Mock::start(|method, _| match method {
        "hello" => hello(),
        _ => Reply::Err(1013, "Already at the last track"),
    });
    let out = mock.run(&["next"]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stdout(&out), "");
    assert_eq!(stderr(&out).trim(), "Already at the last track");
}

#[test]
fn failed_request_is_reported() {
    let mock = Mock::start(|method, _| match method {
        "hello" => hello(),
        _ => Reply::Err(1014, "Could not fetch the album page"),
    });
    let out = mock.run(&["play", "https://example.com/album"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(stderr(&out).contains("Could not fetch the album page"));
}

#[test]
fn usage_errors_exit_2_without_connecting() {
    let mock = player();
    for args in [
        &["jump", "zero"][..],
        &["seek"],
        &["volume", "loud"],
        &["dance"],
        &[],
        &["play"],
    ] {
        let out = mock.run(args);
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        assert!(!stderr(&out).is_empty());
    }
    assert!(
        mock.calls().is_empty(),
        "usage errors must not reach the daemon"
    );
}

#[test]
fn version_needs_no_daemon() {
    let dir = TempDir::new();
    let out = run_in(dir.path(), &["--version"], &[]);
    assert_eq!(out.status.code(), Some(0));
    assert_eq!(
        stdout(&out),
        format!("tunetape-cli {}\n", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn unreachable_daemon_exits_3() {
    let dir = TempDir::new();
    let started = Instant::now();
    let out = run_in(dir.path(), &["status"], &[]);
    assert_eq!(out.status.code(), Some(3), "{}", stderr(&out));
    assert!(stderr(&out).contains("could not start the daemon"));
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[test]
fn unsupported_protocol_exits_3_without_spawning() {
    let mock = Mock::start(|_, _| Reply::Err(1002, "Unsupported protocol version"));
    let marker = mock.data_dir().join("spawned");
    let spawn_cmd = marker_cmd(&marker);
    let out = run_in(
        mock.data_dir(),
        &["status"],
        &[("TUNETAPE_DAEMON_CMD", &spawn_cmd)],
    );
    assert_eq!(out.status.code(), Some(3));
    assert!(stderr(&out).contains("protocol"));
    thread::sleep(Duration::from_millis(300));
    assert!(!marker.exists(), "1002 must not trigger a spawn");
}

#[test]
fn shutdown_does_not_start_a_daemon() {
    let dir = TempDir::new();
    let marker = dir.path().join("spawned");
    let spawn_cmd = marker_cmd(&marker);
    let out = run_in(
        dir.path(),
        &["shutdown"],
        &[("TUNETAPE_DAEMON_CMD", &spawn_cmd)],
    );
    assert_eq!(out.status.code(), Some(0));
    assert_eq!(stdout(&out), "tunetape daemon is not running\n");
    thread::sleep(Duration::from_millis(300));
    assert!(!marker.exists());
}

#[test]
fn shutdown_calls_the_daemon() {
    let mock = player();
    let out = mock.run(&["shutdown"]);
    assert_eq!(out.status.code(), Some(0));
    assert_eq!(stdout(&out), "tunetape daemon stopped\n");
    assert_eq!(
        mock.commands(),
        [("daemon.shutdown".to_string(), json!({}))]
    );
}

/// A stale daemon.json (nothing listening) makes the CLI spawn
/// $TUNETAPE_DAEMON_CMD and retry; here the "daemon" just drops the real
/// discovery file in place, as a daemon would once it listens.
#[test]
fn stale_discovery_spawns_and_retries() {
    let mock = Mock::listen(|method, _| match method {
        "hello" => hello(),
        _ => Reply::Ok(chrono_status("paused")),
    });
    let dir = mock.data_dir();
    let staged = dir.join("staged.json");
    mock.write_discovery(&staged);
    fs::write(dir.join("daemon.json"), stale_discovery(dir)).unwrap();

    let target = dir.join("daemon.json");
    let spawn_cmd = copy_cmd(&staged, &target);
    let out = run_in(dir, &["status"], &[("TUNETAPE_DAEMON_CMD", &spawn_cmd)]);
    assert_eq!(out.status.code(), Some(0), "{}", stderr(&out));
    assert!(stdout(&out).starts_with("⏸ Corridors of Time"));
    assert_eq!(mock.commands(), [("player.status".to_string(), json!({}))]);
}

#[cfg(unix)]
fn stale_discovery(dir: &Path) -> String {
    // A socket path nobody listens on.
    json!({"protocol": 1, "version": "0", "pid": 999999, "transport": "unix",
           "path": dir.join("dead.sock"), "started_at": "2026-01-01T00:00:00Z"})
    .to_string()
}

#[cfg(not(unix))]
fn stale_discovery(_dir: &Path) -> String {
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port(); // the listener is dropped: nothing listens there now
    json!({"protocol": 1, "version": "0", "pid": 999999, "transport": "tcp",
           "host": "127.0.0.1", "port": port, "token": TOKEN,
           "started_at": "2026-01-01T00:00:00Z"})
    .to_string()
}

fn quote(p: &Path) -> String {
    format!("\"{}\"", p.display())
}

#[cfg(unix)]
fn marker_cmd(marker: &Path) -> String {
    format!("mkdir {}", quote(marker))
}

#[cfg(not(unix))]
fn marker_cmd(marker: &Path) -> String {
    format!("cmd /c mkdir {}", quote(marker))
}

#[cfg(unix)]
fn copy_cmd(from: &Path, to: &Path) -> String {
    format!("cp {} {}", quote(from), quote(to))
}

#[cfg(not(unix))]
fn copy_cmd(from: &Path, to: &Path) -> String {
    format!("cmd /c copy /y {} {}", quote(from), quote(to))
}
