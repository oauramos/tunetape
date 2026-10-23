//! A blocking protocol client: connect, `hello`, call (protocol §1.3, §2).

use std::env;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::discovery::{self, Discovery, Endpoint};
use crate::shlex;

pub const PROTOCOL: u64 = 1;
const SPAWN_WAIT: Duration = Duration::from_secs(10);
const RETRY_EVERY: Duration = Duration::from_millis(100);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// Receivers must accept lines up to 1 MiB (§1.1); allow more for big queues.
const MAX_LINE: u64 = 64 * 1024 * 1024;

/// Why a call did not produce a result.
#[derive(Debug)]
pub enum CallError {
    /// The daemon answered with a JSON-RPC error.
    Rpc { code: i64, message: String },
    /// The connection broke or timed out.
    Io(String),
}

impl CallError {
    fn io(e: impl std::fmt::Display) -> CallError {
        CallError::Io(e.to_string())
    }
}

enum Stream {
    #[cfg(unix)]
    Unix(UnixStream),
    Tcp(TcpStream),
}

impl Stream {
    fn open(endpoint: &Endpoint) -> io::Result<Stream> {
        match endpoint {
            #[cfg(unix)]
            Endpoint::Unix { path } => Ok(Stream::Unix(UnixStream::connect(path)?)),
            #[cfg(not(unix))]
            Endpoint::Unix { .. } => Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "unix sockets are not available on this platform",
            )),
            Endpoint::Tcp { host, port, .. } => {
                let addr = (host.as_str(), *port);
                let addr = std::net::ToSocketAddrs::to_socket_addrs(&addr)?
                    .next()
                    .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "bad daemon address"))?;
                let s = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT)?;
                s.set_nodelay(true)?;
                Ok(Stream::Tcp(s))
            }
        }
    }

    fn try_clone(&self) -> io::Result<Stream> {
        Ok(match self {
            #[cfg(unix)]
            Stream::Unix(s) => Stream::Unix(s.try_clone()?),
            Stream::Tcp(s) => Stream::Tcp(s.try_clone()?),
        })
    }

    fn set_read_timeout(&self, t: Option<Duration>) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Stream::Unix(s) => s.set_read_timeout(t),
            Stream::Tcp(s) => s.set_read_timeout(t),
        }
    }
}

impl io::Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Stream::Unix(s) => s.read(buf),
            Stream::Tcp(s) => s.read(buf),
        }
    }
}

impl io::Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Stream::Unix(s) => s.write(buf),
            Stream::Tcp(s) => s.write(buf),
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Stream::Unix(s) => s.flush(),
            Stream::Tcp(s) => s.flush(),
        }
    }
}

/// One connection to the daemon, after a successful `hello`.
pub struct Conn {
    reader: BufReader<Stream>,
    writer: Stream,
    next_id: u64,
}

impl Conn {
    fn open(disc: &Discovery) -> io::Result<Conn> {
        let stream = Stream::open(&disc.endpoint)?;
        let writer = stream.try_clone()?;
        Ok(Conn {
            reader: BufReader::new(stream),
            writer,
            next_id: 0,
        })
    }

    /// Send one request and wait for its response. Anything else on the
    /// connection (events, other ids) is skipped.
    pub fn call(
        &mut self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, CallError> {
        self.next_id += 1;
        let id = self.next_id;
        let mut line = serde_json::to_vec(&json!({
            "jsonrpc": "2.0", "id": id, "method": method, "params": params,
        }))
        .map_err(CallError::io)?;
        line.push(b'\n');
        self.writer.write_all(&line).map_err(CallError::io)?;
        self.writer.flush().map_err(CallError::io)?;

        let deadline = Instant::now() + timeout;
        let mut buf = Vec::new();
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(CallError::Io(format!(
                    "no answer to {method} within {}s",
                    timeout.as_secs()
                )));
            }
            self.reader
                .get_ref()
                .set_read_timeout(Some(left))
                .map_err(CallError::io)?;
            buf.clear();
            let n = (&mut self.reader)
                .take(MAX_LINE)
                .read_until(b'\n', &mut buf)
                .map_err(|e| match e.kind() {
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut => CallError::Io(format!(
                        "no answer to {method} within {}s",
                        timeout.as_secs()
                    )),
                    _ => CallError::io(e),
                })?;
            if n == 0 {
                return Err(CallError::Io("the daemon closed the connection".into()));
            }
            let msg: Value = match serde_json::from_slice(&buf) {
                Ok(v) => v,
                Err(e) => return Err(CallError::Io(format!("bad message from the daemon: {e}"))),
            };
            if msg.get("id").and_then(Value::as_u64) != Some(id) {
                continue; // an event, or something that is not ours
            }
            if let Some(err) = msg.get("error") {
                return Err(CallError::Rpc {
                    code: err.get("code").and_then(Value::as_i64).unwrap_or(-32603),
                    message: err
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("the daemon returned an error")
                        .to_string(),
                });
            }
            return Ok(msg.get("result").cloned().unwrap_or(Value::Null));
        }
    }

    fn hello(&mut self, disc: &Discovery) -> Result<Value, CallError> {
        let mut params = Map::new();
        params.insert("protocol".into(), json!(PROTOCOL));
        params.insert(
            "client".into(),
            json!(concat!("tunetape-cli/", env!("CARGO_PKG_VERSION"))),
        );
        if let Some(token) = disc.token() {
            params.insert("token".into(), json!(token));
        }
        self.call("hello", Value::Object(params), Duration::from_secs(5))
    }
}

/// Could not get a working connection.
#[derive(Debug)]
pub enum ConnectError {
    /// No daemon and we were told not to start one.
    NotRunning,
    /// A daemon answered but does not speak our protocol (1002).
    Protocol(String),
    /// Spawning failed or the daemon never came up.
    Unavailable(String),
}

enum Attempt {
    Connected(Conn),
    Absent,
    Fatal(String),
}

fn attempt(dir: &Path) -> Attempt {
    let disc = match discovery::read(dir) {
        Ok(d) => d,
        Err(_) => return Attempt::Absent,
    };
    let mut conn = match Conn::open(&disc) {
        Ok(c) => c,
        Err(_) => return Attempt::Absent, // refused: a stale file
    };
    match conn.hello(&disc) {
        Ok(_) => Attempt::Connected(conn),
        Err(CallError::Rpc {
            code: 1002,
            message,
        }) => Attempt::Fatal(message),
        Err(_) => Attempt::Absent,
    }
}

/// The command that starts the daemon: `$TUNETAPE_DAEMON_CMD`, split
/// shell-style, else `tunetape daemon`.
pub fn daemon_command() -> Result<Vec<String>, String> {
    match env::var("TUNETAPE_DAEMON_CMD") {
        Ok(raw) if !raw.trim().is_empty() => {
            let argv = shlex::split(&raw, !cfg!(windows))
                .map_err(|e| format!("TUNETAPE_DAEMON_CMD: {e}"))?;
            if argv.is_empty() {
                return Err("TUNETAPE_DAEMON_CMD is empty".into());
            }
            Ok(argv)
        }
        _ => Ok(vec!["tunetape".into(), "daemon".into()]),
    }
}

/// Start the daemon detached from this process and its terminal.
fn spawn(argv: &[String]) -> io::Result<()> {
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        extern "C" {
            fn setsid() -> i32;
        }
        // SAFETY: setsid is async-signal-safe and touches no Rust state.
        unsafe {
            cmd.pre_exec(|| {
                setsid();
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }
    // The child is not waited for: it outlives us and is reparented.
    cmd.spawn().map(drop)
}

/// Protocol §1.3: read daemon.json, connect, hello; on a missing or stale
/// file, spawn the daemon (when `spawn_daemon`) and retry every 100 ms for 10 s.
pub fn connect(spawn_daemon: bool) -> Result<Conn, ConnectError> {
    let dir: PathBuf = discovery::data_dir();
    match attempt(&dir) {
        Attempt::Connected(c) => return Ok(c),
        Attempt::Fatal(m) => return Err(ConnectError::Protocol(m)),
        Attempt::Absent => {}
    }
    if !spawn_daemon {
        return Err(ConnectError::NotRunning);
    }
    let argv = daemon_command().map_err(ConnectError::Unavailable)?;
    spawn(&argv).map_err(|e| {
        ConnectError::Unavailable(format!(
            "could not start the daemon ({}): {e}",
            argv.join(" ")
        ))
    })?;
    let deadline = Instant::now() + SPAWN_WAIT;
    loop {
        thread::sleep(RETRY_EVERY);
        match attempt(&dir) {
            Attempt::Connected(c) => return Ok(c),
            Attempt::Fatal(m) => return Err(ConnectError::Protocol(m)),
            Attempt::Absent if Instant::now() >= deadline => {
                return Err(ConnectError::Unavailable(format!(
                    "the daemon did not start within {}s; see {}",
                    SPAWN_WAIT.as_secs(),
                    dir.join("daemon.log").display()
                )))
            }
            Attempt::Absent => {}
        }
    }
}
