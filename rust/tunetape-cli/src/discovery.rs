//! Where the daemon lives: the data dir and `daemon.json` (protocol §1.2).

use std::env;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::Deserialize;

/// The endpoint a discovery file names.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "transport", rename_all = "lowercase")]
pub enum Endpoint {
    /// POSIX: a Unix domain socket.
    Unix { path: PathBuf },
    /// Windows: TCP on loopback plus the token `hello` must present.
    Tcp {
        host: String,
        port: u16,
        token: String,
    },
}

/// `<data_dir>/daemon.json`. Unknown fields are ignored (protocol §7).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Discovery {
    pub protocol: u32,
    #[serde(default)]
    pub version: String,
    pub pid: u32,
    #[serde(flatten)]
    pub endpoint: Endpoint,
}

impl Discovery {
    pub fn parse(text: &str) -> Result<Discovery, String> {
        serde_json::from_str(text).map_err(|e| format!("bad daemon.json: {e}"))
    }

    /// The token to send in `hello`, if the transport needs one.
    pub fn token(&self) -> Option<&str> {
        match &self.endpoint {
            Endpoint::Tcp { token, .. } => Some(token),
            Endpoint::Unix { .. } => None,
        }
    }
}

/// tunetape's data dir, from an environment lookup (protocol §1.2):
/// `TUNETAPE_DATA_DIR` when absolute; else `%LOCALAPPDATA%\tunetape` on
/// Windows, `$XDG_DATA_HOME/tunetape` or `~/.local/share/tunetape` elsewhere.
pub fn data_dir_from(windows: bool, var: impl Fn(&str) -> Option<OsString>) -> PathBuf {
    let absolute = |name: &str| {
        var(name)
            .map(PathBuf::from)
            .filter(|p| is_absolute(p, windows))
    };
    if let Some(dir) = absolute("TUNETAPE_DATA_DIR") {
        return dir;
    }
    let home = || {
        let name = if windows { "USERPROFILE" } else { "HOME" };
        var(name).map(PathBuf::from).unwrap_or_default()
    };
    if windows {
        let base = absolute("LOCALAPPDATA").unwrap_or_else(|| home().join("AppData").join("Local"));
        base.join("tunetape")
    } else {
        let base = absolute("XDG_DATA_HOME").unwrap_or_else(|| home().join(".local").join("share"));
        base.join("tunetape")
    }
}

/// `Path::is_absolute`, but for the target platform rather than the host, so
/// the Windows rules can be unit-tested anywhere.
fn is_absolute(p: &Path, windows: bool) -> bool {
    if windows == cfg!(windows) {
        return p.is_absolute();
    }
    let s = p.to_string_lossy();
    if windows {
        let b = s.as_bytes();
        (b.len() >= 3
            && b[0].is_ascii_alphabetic()
            && b[1] == b':'
            && (b[2] == b'\\' || b[2] == b'/'))
            || s.starts_with("\\\\")
    } else {
        s.starts_with('/')
    }
}

pub fn data_dir() -> PathBuf {
    data_dir_from(cfg!(windows), |name| env::var_os(name))
}

pub fn discovery_path(dir: &Path) -> PathBuf {
    dir.join("daemon.json")
}

/// Read and parse `daemon.json`; any failure means "no usable daemon".
pub fn read(dir: &Path) -> io::Result<Discovery> {
    let text = fs::read_to_string(discovery_path(dir))?;
    Discovery::parse(&text).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<OsString> {
        let map: HashMap<String, OsString> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), OsString::from(v)))
            .collect();
        move |k| map.get(k).cloned()
    }

    #[test]
    fn parses_unix_discovery() {
        let d = Discovery::parse(
            r#"{"protocol":1,"version":"0.10.0","pid":41237,"transport":"unix",
                "path":"/Users/me/.local/share/tunetape/daemon.sock",
                "started_at":"2026-10-23T14:02:11Z","future_field":[1,2]}"#,
        )
        .unwrap();
        assert_eq!(d.protocol, 1);
        assert_eq!(d.pid, 41237);
        assert_eq!(d.version, "0.10.0");
        assert_eq!(
            d.endpoint,
            Endpoint::Unix {
                path: PathBuf::from("/Users/me/.local/share/tunetape/daemon.sock")
            }
        );
        assert_eq!(d.token(), None);
    }

    #[test]
    fn parses_tcp_discovery() {
        let token = "d1f0c4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
        let d = Discovery::parse(&format!(
            r#"{{"protocol":1,"version":"0.10.0","pid":9120,"transport":"tcp",
                "host":"127.0.0.1","port":53817,"token":"{token}",
                "started_at":"2026-10-23T14:02:11Z"}}"#
        ))
        .unwrap();
        assert_eq!(
            d.endpoint,
            Endpoint::Tcp {
                host: "127.0.0.1".into(),
                port: 53817,
                token: token.into()
            }
        );
        assert_eq!(d.token(), Some(token));
    }

    #[test]
    fn rejects_broken_discovery() {
        assert!(Discovery::parse("").is_err());
        assert!(Discovery::parse("{\"protocol\":1,\"pid\":3}").is_err());
        assert!(Discovery::parse(r#"{"protocol":1,"pid":3,"transport":"pigeon"}"#).is_err());
        assert!(Discovery::parse(r#"{"protocol":1,"pid":3,"transport":"unix"}"#).is_err());
        assert!(
            Discovery::parse(r#"{"protocol":1,"pid":3,"transport":"tcp","host":"127.0.0.1","port":99999,"token":"x"}"#)
                .is_err()
        );
    }

    #[test]
    fn data_dir_env_override_must_be_absolute() {
        let posix = |pairs: &[(&str, &str)]| data_dir_from(false, env_of(pairs));
        assert_eq!(
            posix(&[("TUNETAPE_DATA_DIR", "/tmp/tt"), ("HOME", "/home/me")]),
            PathBuf::from("/tmp/tt")
        );
        assert_eq!(
            posix(&[("TUNETAPE_DATA_DIR", "relative/dir"), ("HOME", "/home/me")]),
            Path::new("/home/me")
                .join(".local")
                .join("share")
                .join("tunetape")
        );
    }

    #[test]
    fn data_dir_posix_defaults() {
        let posix = |pairs: &[(&str, &str)]| data_dir_from(false, env_of(pairs));
        assert_eq!(
            posix(&[("XDG_DATA_HOME", "/xdg"), ("HOME", "/home/me")]),
            Path::new("/xdg").join("tunetape")
        );
        assert_eq!(
            posix(&[("XDG_DATA_HOME", "rel"), ("HOME", "/home/me")]),
            Path::new("/home/me")
                .join(".local")
                .join("share")
                .join("tunetape")
        );
        assert_eq!(
            posix(&[("HOME", "/home/me")]),
            Path::new("/home/me")
                .join(".local")
                .join("share")
                .join("tunetape")
        );
    }

    #[test]
    fn data_dir_windows_defaults() {
        let win = |pairs: &[(&str, &str)]| data_dir_from(true, env_of(pairs));
        assert_eq!(
            win(&[("LOCALAPPDATA", r"C:\Users\me\AppData\Local")]),
            Path::new(r"C:\Users\me\AppData\Local").join("tunetape")
        );
        assert_eq!(
            win(&[("TUNETAPE_DATA_DIR", r"D:\tt"), ("LOCALAPPDATA", r"C:\x")]),
            PathBuf::from(r"D:\tt")
        );
        assert_eq!(
            win(&[("USERPROFILE", r"C:\Users\me")]),
            Path::new(r"C:\Users\me")
                .join("AppData")
                .join("Local")
                .join("tunetape")
        );
    }
}
