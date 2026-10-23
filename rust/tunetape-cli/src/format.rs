//! Human-readable output, matching the Python CLI (docs/architecture.md).

use serde_json::Value;

fn str_of<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.get(key).and_then(Value::as_str)
}

fn num_of(v: &Value, key: &str) -> Option<f64> {
    v.get(key).and_then(Value::as_f64)
}

fn int_of(v: &Value, key: &str) -> Option<i64> {
    v.get(key).and_then(Value::as_i64)
}

/// `m:ss`, or `h:mm:ss` from an hour up.
pub fn clock(seconds: f64) -> String {
    let s = if seconds.is_finite() && seconds > 0.0 {
        seconds as u64
    } else {
        0
    };
    let (h, m, s) = (s / 3600, (s % 3600) / 60, s % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

fn percent(v: f64) -> String {
    format!("{}%", v.round() as i64)
}

/// An item's display title: its title, else its input.
fn item_title(item: &Value) -> &str {
    str_of(item, "title")
        .filter(|t| !t.is_empty())
        .or_else(|| str_of(item, "input"))
        .unwrap_or("?")
}

/// `Corridors of Time · Chrono Trigger OST (5/64)`.
fn track_label(item: &Value) -> String {
    let mut out = item_title(item).to_string();
    if let Some(col) = item.get("collection").filter(|c| c.is_object()) {
        let index = int_of(col, "index").unwrap_or(0) + 1;
        let count = int_of(col, "count").unwrap_or(0);
        match str_of(col, "title").filter(|t| !t.is_empty()) {
            Some(title) => out.push_str(&format!(" · {title} ({index}/{count})")),
            None => out.push_str(&format!(" ({index}/{count})")),
        }
    }
    out
}

fn volume_label(status: &Value) -> String {
    let mut out = format!("vol {}", percent(num_of(status, "volume").unwrap_or(0.0)));
    if status.get("muted").and_then(Value::as_bool) == Some(true) {
        out.push_str(" (muted)");
    }
    out
}

/// The one-line status:
/// `▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%`
/// (`⏸` paused, `…` loading, `■` idle).
pub fn status_line(status: &Value) -> String {
    let state = str_of(status, "state").unwrap_or("idle");
    let current = status.get("current").filter(|c| c.is_object());
    let icon = match state {
        "playing" => "▶",
        "paused" => "⏸",
        "loading" => "…",
        _ => "■",
    };
    let loading = str_of(status, "message")
        .filter(|m| !m.is_empty())
        .unwrap_or("Loading…");
    let mut parts = Vec::new();
    match current {
        Some(item) => parts.push(format!("{icon} {}", track_label(item))),
        None if state == "loading" => parts.push(format!("{icon} {loading}")),
        None => parts.push(format!("{icon} Nothing playing")),
    }
    match state {
        "playing" | "paused" => {
            let pos = clock(num_of(status, "position").unwrap_or(0.0));
            let dur = num_of(status, "duration").unwrap_or(0.0);
            if dur > 0.0 {
                parts.push(format!("{pos} / {}", clock(dur)));
            } else {
                parts.push(pos);
            }
        }
        "loading" if current.is_some() => parts.push(loading.to_string()),
        _ => {}
    }
    parts.push(volume_label(status));
    parts.join("  ")
}

/// `volume` with no argument.
pub fn volume_line(status: &Value) -> String {
    let mut out = format!(
        "Volume {}",
        percent(num_of(status, "volume").unwrap_or(0.0))
    );
    if status.get("muted").and_then(Value::as_bool) == Some(true) {
        out.push_str(" (muted)");
    }
    out
}

/// `Playing: <title>` / `Queued: <title> (12 tracks)` for a posted request.
pub fn posted_line(verb: &str, request: &Value) -> String {
    let title = str_of(request, "title")
        .filter(|t| !t.is_empty())
        .or_else(|| str_of(request, "input"))
        .unwrap_or("?");
    let count = request
        .get("item_ids")
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    if count > 1 {
        format!("{verb}: {title} ({count} tracks)")
    } else {
        format!("{verb}: {title}")
    }
}

fn width(n: usize) -> usize {
    n.max(1).to_string().len()
}

/// `queue`: one line per item, the current one marked `▶`, failed ones `✗`.
pub fn queue_lines(queue: &Value) -> String {
    let items = queue
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if items.is_empty() {
        return "Queue is empty".to_string();
    }
    let current = int_of(queue, "index").unwrap_or(-1);
    let w = width(items.len());
    let mut lines = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let mark = if i as i64 == current {
            "▶"
        } else if str_of(item, "status") == Some("failed") {
            "✗"
        } else {
            " "
        };
        let mut line = format!("{mark} {:>w$}. {}", i + 1, track_label(item));
        if let Some(d) = num_of(item, "duration").filter(|d| *d > 0.0) {
            line.push_str(&format!("  {}", clock(d)));
        }
        if let Some(err) = str_of(item, "error").filter(|e| !e.is_empty()) {
            line.push_str(&format!("  ({err})"));
        }
        lines.push(line);
    }
    lines.join("\n")
}

/// `history`: `1. Chrono Trigger OST  [khinsider, 5/64]`.
pub fn history_lines(history: &Value) -> String {
    let entries = history
        .get("entries")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if entries.is_empty() {
        return "No listening history yet".to_string();
    }
    let w = width(entries.len());
    entries
        .iter()
        .enumerate()
        .map(|(i, e)| {
            let title = str_of(e, "title")
                .filter(|t| !t.is_empty())
                .or_else(|| str_of(e, "url"))
                .unwrap_or("?");
            let mut tag = str_of(e, "type").unwrap_or("?").to_string();
            if let Some(count) = int_of(e, "track_count").filter(|c| *c > 0) {
                let at = int_of(e, "last_index").unwrap_or(0) + 1;
                tag.push_str(&format!(", {at}/{count}"));
            }
            format!("{:>w$}. {title}  [{tag}]", i + 1)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// `discover`: numbered picks with the input that plays each one.
pub fn discover_lines(result: &Value) -> String {
    let tracks = result
        .get("tracks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if tracks.is_empty() {
        return "No suggestions".to_string();
    }
    let w = width(tracks.len());
    tracks
        .iter()
        .enumerate()
        .map(|(i, t)| {
            format!(
                "{:>w$}. {}  ({})",
                i + 1,
                str_of(t, "title").unwrap_or("?"),
                str_of(t, "input").unwrap_or("")
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn item(title: &str, collection: Value) -> Value {
        json!({"id": "i-1", "request_id": "r-1", "title": title, "kind": "khinsider",
               "input": "https://x/y.mp3", "collection": collection, "status": "playing",
               "error": null, "duration": 251.0})
    }

    fn status(state: &str, current: Value) -> Value {
        json!({"state": state, "message": null, "position": 83.4, "duration": 251.0,
               "volume": 80.0, "muted": false, "current": current, "index": 4,
               "length": 64, "has_next": true, "has_prev": true})
    }

    fn chrono() -> Value {
        item(
            "Corridors of Time",
            json!({"title": "Chrono Trigger OST", "index": 4, "count": 64}),
        )
    }

    #[test]
    fn clock_format() {
        assert_eq!(clock(0.0), "0:00");
        assert_eq!(clock(83.4), "1:23");
        assert_eq!(clock(251.0), "4:11");
        assert_eq!(clock(3723.0), "1:02:03");
        assert_eq!(clock(-5.0), "0:00");
        assert_eq!(clock(f64::NAN), "0:00");
    }

    #[test]
    fn playing_line_with_collection() {
        assert_eq!(
            status_line(&status("playing", chrono())),
            "▶ Corridors of Time · Chrono Trigger OST (5/64)  1:23 / 4:11  vol 80%"
        );
    }

    #[test]
    fn paused_line_single_track() {
        let s = status("paused", item("Around the World", Value::Null));
        assert_eq!(status_line(&s), "⏸ Around the World  1:23 / 4:11  vol 80%");
    }

    #[test]
    fn loading_line_shows_message() {
        let mut s = status("loading", chrono());
        s["message"] = json!("Loading track 5/64…");
        assert_eq!(
            status_line(&s),
            "… Corridors of Time · Chrono Trigger OST (5/64)  Loading track 5/64…  vol 80%"
        );
        let mut s = status("loading", Value::Null);
        s["message"] = Value::Null;
        assert_eq!(status_line(&s), "… Loading…  vol 80%");
    }

    #[test]
    fn idle_lines() {
        let mut s = status("idle", Value::Null);
        s["volume"] = json!(64.6);
        assert_eq!(status_line(&s), "■ Nothing playing  vol 65%");
        // Stopped with a queue kept: the item is still shown, without a clock.
        assert_eq!(
            status_line(&status("idle", chrono())),
            "■ Corridors of Time · Chrono Trigger OST (5/64)  vol 80%"
        );
    }

    #[test]
    fn live_stream_and_mute() {
        let mut s = status("playing", item("Lofi Radio", Value::Null));
        s["duration"] = json!(0);
        s["muted"] = json!(true);
        assert_eq!(status_line(&s), "▶ Lofi Radio  1:23  vol 80% (muted)");
        assert_eq!(volume_line(&s), "Volume 80% (muted)");
    }

    #[test]
    fn untitled_item_falls_back_to_input() {
        let mut it = item("", Value::Null);
        it["title"] = Value::Null;
        assert_eq!(
            status_line(&status("playing", it)),
            "▶ https://x/y.mp3  1:23 / 4:11  vol 80%"
        );
        let col = json!({"title": null, "index": 0, "count": 3});
        assert_eq!(
            status_line(&status("playing", item("T", col))),
            "▶ T (1/3)  1:23 / 4:11  vol 80%"
        );
    }

    #[test]
    fn posted_lines() {
        let one = json!({"title": "Around the World", "input": "x", "item_ids": ["i-1"]});
        assert_eq!(posted_line("Playing", &one), "Playing: Around the World");
        let many = json!({"title": "Chrono Trigger OST", "input": "x",
                          "item_ids": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"]});
        assert_eq!(
            posted_line("Queued", &many),
            "Queued: Chrono Trigger OST (12 tracks)"
        );
        let untitled = json!({"title": null, "input": "lofi hip hop", "item_ids": []});
        assert_eq!(posted_line("Playing", &untitled), "Playing: lofi hip hop");
    }

    #[test]
    fn queue_listing() {
        assert_eq!(
            queue_lines(&json!({"items": [], "index": -1})),
            "Queue is empty"
        );
        let mut failed = item("Broken", Value::Null);
        failed["status"] = json!("failed");
        failed["error"] = json!("Could not load track");
        failed["duration"] = Value::Null;
        let q = json!({"items": [item("One", Value::Null), item("Two", Value::Null), failed], "index": 1});
        assert_eq!(
            queue_lines(&q),
            "  1. One  4:11\n▶ 2. Two  4:11\n✗ 3. Broken  (Could not load track)"
        );
    }

    #[test]
    fn history_listing() {
        assert_eq!(
            history_lines(&json!({"entries": []})),
            "No listening history yet"
        );
        let h = json!({"entries": [
            {"type": "khinsider", "url": "u", "title": "Chrono Trigger OST", "last_played": "t",
             "play_count": 3, "track_count": 64, "last_index": 4},
            {"type": "youtube", "url": "https://youtu.be/x", "title": "", "last_played": "t", "play_count": 1}
        ]});
        assert_eq!(
            history_lines(&h),
            "1. Chrono Trigger OST  [khinsider, 5/64]\n2. https://youtu.be/x  [youtube]"
        );
    }

    #[test]
    fn discover_listing() {
        let r = json!({"tracks": [{"title": "So What", "input": "ytsearch1:miles davis so what"}]});
        assert_eq!(
            discover_lines(&r),
            "1. So What  (ytsearch1:miles davis so what)"
        );
        assert_eq!(discover_lines(&json!({"tracks": []})), "No suggestions");
    }
}
