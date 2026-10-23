//! Command line parsing. Same subcommands as the Python CLI
//! (docs/architecture.md, "Command line"), minus the TUI, `daemon` and `mcp`.

use std::ffi::OsString;
use std::fmt;

use lexopt::prelude::*;

pub const USAGE: &str = "\
usage: tunetape-cli <command> [args] [--json]

  play <input…> [--title T]    replace the queue and play (words are joined with spaces)
  add <input…> [--next] [--title T]
  pause | resume | toggle | next | prev | stop | mute | clear
  status
  queue
  jump <n>                     play track n (1-based)
  volume [<n> | +<n> | -<n>]   no argument prints the volume
  seek <s | +s | -s | mm:ss>   a sign means relative, bare means absolute
  history [--limit N]
  discover <words…>
  shutdown                     stop the daemon (never starts one)

  --json         print the daemon's raw result (every command but shutdown)
  -h, --help     show this help
  -V, --version  show the version

The daemon is started on demand. Environment: TUNETAPE_DATA_DIR,
TUNETAPE_DAEMON_CMD (default: tunetape daemon).
Exit status: 0 ok, 1 the daemon returned an error, 2 usage error,
3 the daemon could not be reached or started.";

/// Player and queue commands that take no arguments.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Simple {
    Pause,
    Resume,
    Toggle,
    Next,
    Prev,
    Stop,
    Mute,
    Clear,
}

impl Simple {
    fn from_name(name: &str) -> Option<Simple> {
        Some(match name {
            "pause" => Simple::Pause,
            "resume" => Simple::Resume,
            "toggle" => Simple::Toggle,
            "next" => Simple::Next,
            "prev" => Simple::Prev,
            "stop" => Simple::Stop,
            "mute" => Simple::Mute,
            "clear" => Simple::Clear,
            _ => return None,
        })
    }

    pub fn method(self) -> &'static str {
        match self {
            Simple::Pause => "player.pause",
            Simple::Resume => "player.resume",
            Simple::Toggle => "player.toggle",
            Simple::Next => "player.next",
            Simple::Prev => "player.prev",
            Simple::Stop => "player.stop",
            Simple::Mute => "player.mute",
            Simple::Clear => "queue.clear",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Volume {
    Show,
    Set(f64),
    Delta(f64),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Command {
    Help,
    Version,
    Play {
        input: String,
        title: Option<String>,
    },
    Add {
        input: String,
        next: bool,
        title: Option<String>,
    },
    Simple(Simple),
    Status,
    Queue,
    /// 0-based index to send.
    Jump {
        index: u64,
    },
    Volume(Volume),
    Seek {
        seconds: f64,
        relative: bool,
    },
    History {
        limit: u64,
    },
    Discover {
        request: String,
    },
    Shutdown,
}

/// A parsed command line: the command, and whether to print raw JSON.
#[derive(Debug, Clone, PartialEq)]
pub struct Invocation {
    pub command: Command,
    pub json: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageError(pub String);

impl fmt::Display for UsageError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl From<lexopt::Error> for UsageError {
    fn from(e: lexopt::Error) -> Self {
        UsageError(e.to_string())
    }
}

fn usage(msg: impl Into<String>) -> UsageError {
    UsageError(msg.into())
}

fn string(v: OsString) -> Result<String, UsageError> {
    v.into_string().map_err(|v| {
        usage(format!(
            "argument is not valid UTF-8: {}",
            v.to_string_lossy()
        ))
    })
}

/// The options and positional words after a subcommand.
#[derive(Default)]
struct Rest {
    words: Vec<String>,
    json: bool,
    next: bool,
    title: Option<String>,
    limit: Option<String>,
}

/// Read what follows a subcommand, allowing only the options in `allowed`.
fn rest(p: &mut lexopt::Parser, allowed: &[&str]) -> Result<Rest, UsageError> {
    let mut r = Rest::default();
    while let Some(arg) = p.next()? {
        match arg {
            Long(name) if allowed.contains(&name) => match name {
                "json" => r.json = true,
                "next" => r.next = true,
                "title" => r.title = Some(string(p.value()?)?),
                "limit" => r.limit = Some(string(p.value()?)?),
                _ => unreachable!("option {name} is allowed but not handled"),
            },
            Value(v) => r.words.push(string(v)?),
            other => return Err(other.unexpected().into()),
        }
    }
    Ok(r)
}

/// The remaining arguments unparsed, so `-10` is a number and not an option;
/// `--json` may still appear among them.
fn raw_rest(p: &mut lexopt::Parser) -> Result<(Vec<String>, bool), UsageError> {
    let mut json = false;
    let mut words = Vec::new();
    for arg in p.raw_args()? {
        let arg = string(arg)?;
        if arg == "--json" {
            json = true;
        } else {
            words.push(arg);
        }
    }
    Ok((words, json))
}

fn no_words(r: &Rest, name: &str) -> Result<(), UsageError> {
    match r.words.first() {
        None => Ok(()),
        Some(w) => Err(usage(format!("{name} takes no arguments (got {w})"))),
    }
}

pub fn parse<I>(args: I) -> Result<Invocation, UsageError>
where
    I: IntoIterator,
    I::Item: Into<OsString>,
{
    let mut p = lexopt::Parser::from_args(args);
    let name = match p.next()? {
        None => return Err(usage("missing command")),
        Some(Short('h') | Long("help")) => return Ok(plain(Command::Help)),
        Some(Short('V') | Long("version")) => return Ok(plain(Command::Version)),
        Some(Value(v)) => string(v)?,
        Some(other) => return Err(other.unexpected().into()),
    };
    let (command, json) = match name.as_str() {
        "help" => (Command::Help, false),
        "play" | "add" => {
            let play = name == "play";
            let allowed: &[&str] = if play {
                &["json", "title"]
            } else {
                &["json", "title", "next"]
            };
            let r = rest(&mut p, allowed)?;
            if r.words.is_empty() {
                return Err(usage(format!(
                    "{name} needs a link, a file or some words to search"
                )));
            }
            let input = r.words.join(" ");
            let cmd = if play {
                Command::Play {
                    input,
                    title: r.title,
                }
            } else {
                Command::Add {
                    input,
                    next: r.next,
                    title: r.title,
                }
            };
            (cmd, r.json)
        }
        "status" | "queue" => {
            let r = rest(&mut p, &["json"])?;
            no_words(&r, &name)?;
            let cmd = if name == "status" {
                Command::Status
            } else {
                Command::Queue
            };
            (cmd, r.json)
        }
        "jump" => {
            let r = rest(&mut p, &["json"])?;
            let index = match r.words.as_slice() {
                [n] => match n.parse::<u64>() {
                    Ok(n) if n >= 1 => n - 1,
                    _ => return Err(usage(format!("jump: not a track number (1, 2, …): {n}"))),
                },
                _ => return Err(usage("jump takes one track number (1-based)")),
            };
            (Command::Jump { index }, r.json)
        }
        "volume" => {
            let (words, json) = raw_rest(&mut p)?;
            let v = match words.as_slice() {
                [] => Volume::Show,
                [v] => parse_volume(v)?,
                _ => return Err(usage("volume takes at most one argument")),
            };
            (Command::Volume(v), json)
        }
        "seek" => {
            let (words, json) = raw_rest(&mut p)?;
            let (seconds, relative) = match words.as_slice() {
                [s] => parse_seek(s)?,
                _ => return Err(usage("seek takes a position: 90, 1:30, +10 or -10")),
            };
            (Command::Seek { seconds, relative }, json)
        }
        "history" => {
            let r = rest(&mut p, &["json", "limit"])?;
            no_words(&r, "history")?;
            let limit = match &r.limit {
                None => 50,
                Some(v) => v
                    .parse::<u64>()
                    .map_err(|_| usage(format!("--limit: not a number: {v}")))?,
            };
            (Command::History { limit }, r.json)
        }
        "discover" => {
            let r = rest(&mut p, &["json"])?;
            if r.words.is_empty() {
                return Err(usage(
                    "discover needs a description, e.g. discover rainy day jazz",
                ));
            }
            (
                Command::Discover {
                    request: r.words.join(" "),
                },
                r.json,
            )
        }
        "shutdown" => {
            let r = rest(&mut p, &[])?;
            no_words(&r, "shutdown")?;
            (Command::Shutdown, false)
        }
        "daemon" | "mcp" => {
            return Err(usage(format!(
                "`{name}` is only in the Python CLI; run `tunetape {name}`"
            )))
        }
        other => match Simple::from_name(other) {
            Some(s) => {
                let r = rest(&mut p, &["json"])?;
                no_words(&r, other)?;
                (Command::Simple(s), r.json)
            }
            None => return Err(usage(format!("unknown command: {other}"))),
        },
    };
    Ok(Invocation { command, json })
}

fn plain(command: Command) -> Invocation {
    Invocation {
        command,
        json: false,
    }
}

/// `80` sets, `+5` / `-5` adjust. A trailing `%` is allowed.
pub fn parse_volume(s: &str) -> Result<Volume, UsageError> {
    let bad = || usage(format!("volume: expected 80, +5 or -5, got {s}"));
    let t = s.trim().trim_end_matches('%');
    let (sign, digits) = match t.as_bytes().first() {
        Some(b'+') => (Some(1.0), &t[1..]),
        Some(b'-') => (Some(-1.0), &t[1..]),
        _ => (None, t),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
        return Err(bad());
    }
    let n: f64 = digits.parse().map_err(|_| bad())?;
    Ok(match sign {
        Some(sign) => Volume::Delta(sign * n),
        None => Volume::Set(n),
    })
}

/// `90` / `1:30` / `1:02:03` are absolute; a leading `+` or `-` makes it relative.
pub fn parse_seek(s: &str) -> Result<(f64, bool), UsageError> {
    let bad = || usage(format!("seek: expected 90, 1:30, +10 or -10, got {s}"));
    let t = s.trim();
    let (sign, rest, relative) = match t.as_bytes().first() {
        Some(b'+') => (1.0, &t[1..], true),
        Some(b'-') => (-1.0, &t[1..], true),
        _ => (1.0, t, false),
    };
    let parts: Vec<&str> = rest.split(':').collect();
    if parts.len() > 3 || parts.iter().any(|p| p.is_empty()) {
        return Err(bad());
    }
    let mut total = 0.0;
    for (i, part) in parts.iter().enumerate() {
        let last = i == parts.len() - 1;
        let ok = part
            .bytes()
            .all(|b| b.is_ascii_digit() || (last && b == b'.'));
        if !ok {
            return Err(bad());
        }
        let v: f64 = part.parse().map_err(|_| bad())?;
        if i > 0 && v >= 60.0 {
            return Err(bad());
        }
        total = total * 60.0 + v;
    }
    Ok((sign * total, relative))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(args: &[&str]) -> Result<Command, UsageError> {
        parse(args.iter().copied()).map(|i| i.command)
    }

    fn json(args: &[&str]) -> bool {
        parse(args.iter().copied()).unwrap().json
    }

    #[test]
    fn jump_is_one_based_for_humans() {
        assert_eq!(p(&["jump", "3"]), Ok(Command::Jump { index: 2 }));
        assert_eq!(p(&["jump", "1"]), Ok(Command::Jump { index: 0 }));
        assert!(p(&["jump", "0"]).is_err());
        assert!(p(&["jump", "-1"]).is_err());
        assert!(p(&["jump", "three"]).is_err());
        assert!(p(&["jump"]).is_err());
        assert!(p(&["jump", "1", "2"]).is_err());
    }

    #[test]
    fn seek_forms() {
        let seek = |s: &str| match p(&["seek", s]) {
            Ok(Command::Seek { seconds, relative }) => (seconds, relative),
            other => panic!("{s}: {other:?}"),
        };
        assert_eq!(seek("+10"), (10.0, true));
        assert_eq!(seek("-10"), (-10.0, true));
        assert_eq!(seek("90"), (90.0, false));
        assert_eq!(seek("1:30"), (90.0, false));
        assert_eq!(seek("+1:30"), (90.0, true));
        assert_eq!(seek("1:02:03"), (3723.0, false));
        assert_eq!(seek("12.5"), (12.5, false));
        assert_eq!(seek("0"), (0.0, false));
        for bad in [
            "", "abc", "1:", ":30", "1:75", "1:2:3:4", "+", "--5", "inf", "NaN", "1e3",
        ] {
            assert!(parse_seek(bad).is_err(), "{bad:?} should be rejected");
        }
        assert!(p(&["seek"]).is_err());
    }

    #[test]
    fn volume_forms() {
        assert_eq!(p(&["volume"]), Ok(Command::Volume(Volume::Show)));
        assert_eq!(
            p(&["volume", "+5"]),
            Ok(Command::Volume(Volume::Delta(5.0)))
        );
        assert_eq!(
            p(&["volume", "-5"]),
            Ok(Command::Volume(Volume::Delta(-5.0)))
        );
        assert_eq!(p(&["volume", "80"]), Ok(Command::Volume(Volume::Set(80.0))));
        assert_eq!(
            p(&["volume", "80%"]),
            Ok(Command::Volume(Volume::Set(80.0)))
        );
        assert!(p(&["volume", "loud"]).is_err());
        assert!(p(&["volume", "+"]).is_err());
        assert!(p(&["volume", "1", "2"]).is_err());
    }

    #[test]
    fn play_joins_words() {
        assert_eq!(
            p(&["play", "lofi", "hip", "hop"]),
            Ok(Command::Play {
                input: "lofi hip hop".into(),
                title: None
            })
        );
        assert_eq!(
            p(&["play", "--title", "Night Drive", "https://youtu.be/x"]),
            Ok(Command::Play {
                input: "https://youtu.be/x".into(),
                title: Some("Night Drive".into())
            })
        );
        assert_eq!(
            p(&["play", "a", "--title=T", "b"]),
            Ok(Command::Play {
                input: "a b".into(),
                title: Some("T".into())
            })
        );
        assert!(p(&["play"]).is_err());
        assert!(p(&["play", "--next", "x"]).is_err());
        assert!(p(&["play", "--bogus", "x"]).is_err());
    }

    #[test]
    fn add_flags() {
        assert_eq!(
            p(&["add", "--next", "daft", "punk"]),
            Ok(Command::Add {
                input: "daft punk".into(),
                next: true,
                title: None
            })
        );
        assert_eq!(
            p(&["add", "/music/a.mp3", "--title", "A"]),
            Ok(Command::Add {
                input: "/music/a.mp3".into(),
                next: false,
                title: Some("A".into())
            })
        );
        // `--` lets an input start with a dash.
        assert_eq!(
            p(&["add", "--", "-weird name-"]),
            Ok(Command::Add {
                input: "-weird name-".into(),
                next: false,
                title: None
            })
        );
    }

    #[test]
    fn simple_commands() {
        for (name, method) in [
            ("pause", "player.pause"),
            ("resume", "player.resume"),
            ("toggle", "player.toggle"),
            ("next", "player.next"),
            ("prev", "player.prev"),
            ("stop", "player.stop"),
            ("mute", "player.mute"),
            ("clear", "queue.clear"),
        ] {
            match p(&[name]) {
                Ok(Command::Simple(s)) => assert_eq!(s.method(), method),
                other => panic!("{name}: {other:?}"),
            }
            assert!(p(&[name, "extra"]).is_err());
            assert!(json(&[name, "--json"]));
        }
    }

    #[test]
    fn listing_commands() {
        assert_eq!(p(&["status"]), Ok(Command::Status));
        assert!(!json(&["status"]));
        assert!(json(&["status", "--json"]));
        assert!(json(&["queue", "--json"]));
        assert_eq!(p(&["history"]), Ok(Command::History { limit: 50 }));
        assert_eq!(
            p(&["history", "--limit", "5", "--json"]),
            Ok(Command::History { limit: 5 })
        );
        assert!(json(&["history", "--limit", "5", "--json"]));
        assert!(p(&["history", "--limit", "many"]).is_err());
        assert_eq!(
            p(&["discover", "rainy", "day", "jazz", "--json"]),
            Ok(Command::Discover {
                request: "rainy day jazz".into()
            })
        );
        assert!(p(&["discover"]).is_err());
        assert_eq!(p(&["shutdown"]), Ok(Command::Shutdown));
        assert!(p(&["shutdown", "--json"]).is_err());
    }

    #[test]
    fn json_on_every_command() {
        assert!(json(&["play", "x", "--json"]));
        assert!(json(&["add", "--json", "x"]));
        assert!(json(&["jump", "2", "--json"]));
        assert!(json(&["volume", "-5", "--json"]));
        assert_eq!(
            p(&["volume", "-5", "--json"]),
            Ok(Command::Volume(Volume::Delta(-5.0)))
        );
        assert!(json(&["volume", "--json"]));
        assert!(json(&["seek", "--json", "-10"]));
        assert_eq!(
            p(&["seek", "-10", "--json"]),
            Ok(Command::Seek {
                seconds: -10.0,
                relative: true
            })
        );
    }

    #[test]
    fn globals_and_errors() {
        assert_eq!(p(&["--version"]), Ok(Command::Version));
        assert_eq!(p(&["-V"]), Ok(Command::Version));
        assert_eq!(p(&["--help"]), Ok(Command::Help));
        assert_eq!(p(&["help"]), Ok(Command::Help));
        assert!(p(&[]).is_err());
        assert!(p(&["dance"]).is_err());
        assert!(p(&["daemon"]).is_err());
        assert!(p(&["mcp"]).is_err());
        assert!(p(&["--frobnicate"]).is_err());
    }
}
