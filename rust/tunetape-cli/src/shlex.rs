//! Shell-style splitting for `TUNETAPE_DAEMON_CMD`, like Python's
//! `shlex.split` (POSIX rules; on Windows backslashes stay literal so paths
//! survive, matching `shlex.split(posix=False)` closely enough for commands).

/// Split `line` into words. Single and double quotes group; outside single
/// quotes a backslash escapes the next character when `backslash_escapes`.
pub fn split(line: &str, backslash_escapes: bool) -> Result<Vec<String>, String> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut in_word = false;
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        match c {
            '\'' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(ch) => word.push(ch),
                        None => return Err("unterminated single quote".into()),
                    }
                }
            }
            '"' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') if backslash_escapes => match chars.next() {
                            Some(ch @ ('"' | '\\')) => word.push(ch),
                            Some(ch) => {
                                word.push('\\');
                                word.push(ch);
                            }
                            None => return Err("unterminated double quote".into()),
                        },
                        Some(ch) => word.push(ch),
                        None => return Err("unterminated double quote".into()),
                    }
                }
            }
            '\\' if backslash_escapes => {
                in_word = true;
                match chars.next() {
                    Some(ch) => word.push(ch),
                    None => return Err("trailing backslash".into()),
                }
            }
            c if c.is_whitespace() => {
                if in_word {
                    words.push(std::mem::take(&mut word));
                    in_word = false;
                }
            }
            c => {
                in_word = true;
                word.push(c);
            }
        }
    }
    if in_word {
        words.push(word);
    }
    Ok(words)
}

#[cfg(test)]
mod tests {
    use super::split;

    fn posix(s: &str) -> Vec<String> {
        split(s, true).unwrap()
    }

    #[test]
    fn splits_plain_words() {
        assert_eq!(posix("tunetape daemon"), ["tunetape", "daemon"]);
        assert_eq!(posix("  a   b\tc  "), ["a", "b", "c"]);
        assert!(posix("").is_empty());
    }

    #[test]
    fn honours_quotes_and_escapes() {
        assert_eq!(
            posix(r#"/usr/bin/python3 -m tunetape daemon --idle-timeout 0"#),
            [
                "/usr/bin/python3",
                "-m",
                "tunetape",
                "daemon",
                "--idle-timeout",
                "0"
            ]
        );
        assert_eq!(
            posix(r#"'/opt/my app/tt' "a b" c\ d"#),
            ["/opt/my app/tt", "a b", "c d"]
        );
        assert_eq!(posix(r#""say \"hi\"" 'it''s'"#), [r#"say "hi""#, "its"]);
        assert_eq!(posix(r#"x"" ''"#), ["x", ""]);
    }

    #[test]
    fn windows_keeps_backslashes() {
        assert_eq!(
            split(r#""C:\Program Files\tunetape\tunetape.exe" daemon"#, false).unwrap(),
            [r"C:\Program Files\tunetape\tunetape.exe", "daemon"]
        );
        assert_eq!(
            split(r"C:\tt\tunetape.exe daemon", false).unwrap(),
            [r"C:\tt\tunetape.exe", "daemon"]
        );
    }

    #[test]
    fn rejects_unterminated_quotes() {
        assert!(split("'oops", true).is_err());
        assert!(split("\"oops", true).is_err());
        assert!(split("oops\\", true).is_err());
    }
}
