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
/// (`⏸` paused, `…` loading, `■` idle). Same rules as the Python CLI's
/// `format_status`.
pub fn status_line(status: &Value) -> String {
    let state = str_of(status, "state").unwrap_or("idle");
    let current = status.get("current").filter(|c| c.is_object());
    let icon = match state {
        "playing" => "▶",
        "paused" => "⏸",
        "loading" => "…",
        _ => "■",
    };
    let volume = volume_label(status);
    let message = str_of(status, "message").filter(|m| !m.is_empty());
    if let (Some(message), "loading") = (message, state) {
        return format!("{icon} {message}  {volume}");
    }
    let Some(item) = current else {
        return format!("{icon} Nothing playing  {volume}");
    };
    let mut parts = vec![format!("{icon} {}", track_label(item))];
    if state == "playing" || state == "paused" {
        let mut clock_part = clock(num_of(status, "position").unwrap_or(0.0));
        let dur = num_of(status, "duration").unwrap_or(0.0);
        if dur > 0.0 {
            clock_part.push_str(&format!(" / {}", clock(dur)));
        }
        parts.push(clock_part);
    } else if let Some(message) = message {
        parts.push(message.to_string());
    }
    parts.push(volume);
    parts.join("  ")
}

/// `volume`: `Volume 80%`, plus ` (muted)`.
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

const COLLECTION_KINDS: [&str; 3] = ["khinsider", "spotify", "youtube_playlist"];

fn is_collection(request: &Value) -> bool {
    str_of(request, "kind").is_some_and(|k| COLLECTION_KINDS.contains(&k))
}

/// A request's label: its title (else `fallback`, the first item's title,
/// else its input), plus ` (N tracks)` for a collection.
pub fn request_label(request: &Value, fallback: Option<&str>) -> String {
    let title = str_of(request, "title")
        .filter(|t| !t.is_empty())
        .or(fallback.filter(|t| !t.is_empty()))
        .or_else(|| str_of(request, "input"))
        .unwrap_or("?");
    if is_collection(request) {
        let count = request
            .get("item_ids")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        format!("{title} ({count} tracks)")
    } else {
        title.to_string()
    }
}

/// `add`: `Queued: <label>`.
pub fn queued_line(request: &Value, first_title: Option<&str>) -> String {
    format!("Queued: {}", request_label(request, first_title))
}

/// `play`: `Playing: <title>` once an item of the request is playing (else
/// `Loading: …`); a collection shows `<track> · <album> (N tracks)`.
pub fn play_line(request: &Value, item: Option<&Value>) -> String {
    let label = if is_collection(request) {
        let label = request_label(request, None);
        match item {
            Some(it) => format!("{} · {label}", item_title(it)),
            None => label,
        }
    } else {
        item.and_then(|it| str_of(it, "title"))
            .filter(|t| !t.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| request_label(request, None))
    };
    let playing = item.and_then(|it| str_of(it, "status")) == Some("playing");
    let verb = if playing { "Playing" } else { "Loading" };
    format!("{verb}: {label}")
}

/// `queue`: `▶ 1. Title  4:11` for the current item, two spaces for the others.
pub fn queue_lines(queue: &Value) -> String {
    let items = queue
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if items.is_empty() {
        return "The queue is empty.".to_string();
    }
    let current = int_of(queue, "index").unwrap_or(-1);
    let mut lines = Vec::with_capacity(items.len());
    for (i, item) in items.iter().enumerate() {
        let mark = if i as i64 == current { "▶" } else { " " };
        let mut line = format!("{mark} {}. {}", i + 1, item_title(item));
        if let Some(d) = num_of(item, "duration").filter(|d| *d > 0.0) {
            line.push_str(&format!("  {}", clock(d)));
        }
        if str_of(item, "status") == Some("failed") {
            let err = str_of(item, "error").unwrap_or("unknown error");
            line.push_str(&format!("  (failed: {err})"));
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
        return "No listening history yet.".to_string();
    }
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
            format!("{}. {title}  [{tag}]", i + 1)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// `discover`: `1. Title  (input)`; nothing at all for no picks.
pub fn discover_lines(result: &Value) -> String {
    let tracks = result
        .get("tracks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    tracks
        .iter()
        .enumerate()
        .map(|(i, t)| {
            format!(
                "{}. {}  ({})",
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
        // A loading message replaces the title, as in the Python CLI.
        let mut s = status("loading", chrono());
        s["message"] = json!("Loading track 5/64…");
        assert_eq!(status_line(&s), "… Loading track 5/64…  vol 80%");
        // Without one, the item is shown without a clock.
        assert_eq!(
            status_line(&status("loading", chrono())),
            "… Corridors of Time · Chrono Trigger OST (5/64)  vol 80%"
        );
        assert_eq!(
            status_line(&status("loading", Value::Null)),
            "… Nothing playing  vol 80%"
        );
    }

    #[test]
    fn request_and_play_lines() {
        let one = json!({"title": "Around the World", "kind": "youtube", "input": "x",
                         "item_ids": ["i-1"]});
        assert_eq!(queued_line(&one, None), "Queued: Around the World");
        let album = json!({"title": "Chrono Trigger OST", "kind": "khinsider", "input": "x",
                           "item_ids": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"]});
        assert_eq!(
            queued_line(&album, None),
            "Queued: Chrono Trigger OST (12 tracks)"
        );
        let untitled = json!({"title": null, "kind": "file", "input": "/m/a.mp3",
                              "item_ids": ["i-1"]});
        assert_eq!(queued_line(&untitled, Some("a")), "Queued: a");
        assert_eq!(queued_line(&untitled, None), "Queued: /m/a.mp3");

        let mut playing = item("Corridors of Time", Value::Null);
        assert_eq!(
            play_line(&album, Some(&playing)),
            "Playing: Corridors of Time · Chrono Trigger OST (12 tracks)"
        );
        assert_eq!(
            play_line(&one, Some(&playing)),
            "Playing: Corridors of Time"
        );
        playing["status"] = json!("resolving");
        assert_eq!(
            play_line(&one, Some(&playing)),
            "Loading: Corridors of Time"
        );
        assert_eq!(play_line(&untitled, None), "Loading: /m/a.mp3");
    }

    #[test]
    fn queue_listing() {
        assert_eq!(
            queue_lines(&json!({"items": [], "index": -1})),
            "The queue is empty."
        );
        let mut failed = item("Broken", Value::Null);
        failed["status"] = json!("failed");
        failed["error"] = json!("Could not load track");
        failed["duration"] = Value::Null;
        let q = json!({"items": [item("One", Value::Null), item("Two", chrono()["collection"].clone()), failed], "index": 1});
        assert_eq!(
            queue_lines(&q),
            "  1. One  4:11\n▶ 2. Two  4:11\n  3. Broken  (failed: Could not load track)"
        );
    }

    #[test]
    fn history_listing() {
        assert_eq!(
            history_lines(&json!({"entries": []})),
            "No listening history yet."
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
        assert_eq!(discover_lines(&json!({"tracks": []})), "");
    }
}
