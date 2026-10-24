"""§3.1–§3.3 and §4.1: inputs, requests, the queue."""

import os
from pathlib import Path

import pytest

from .wire import add, first_item, queue_ids, queue_items, status, wait_for, wait_idle, wait_playing


def test_add_local_file_and_wait(conn, media):
    path = media("one.mp3")
    req = add(conn, path)
    assert req["kind"] == "file"
    assert req["input"] == path
    assert req["error"] is None
    assert len(req["item_ids"]) == 1
    items = queue_items(conn)
    assert [it["id"] for it in items] == req["item_ids"]
    item = items[0]
    assert item["request_id"] == req["id"]
    assert item["kind"] == "file"
    assert item["collection"] is None


def test_add_to_idle_queue_starts_playback(conn, media):
    req = add(conn, media("starts.mp3"))
    s = wait_playing(conn, first_item(req))
    assert s["index"] == 0
    assert s["length"] == 1
    assert s["current"]["status"] == "playing"


def test_add_without_wait_returns_at_once(conn, media):
    req = add(conn, media("nowait.mp3"), wait=False)
    assert req["status"] in ("pending", "expanding", "done")
    def finished():
        r = conn.result("requests.get", {"id": req["id"]})["request"]
        return r if r["status"] == "done" else None

    done = wait_for(finished, what="request to finish expanding")
    assert done["id"] == req["id"]
    assert len(done["item_ids"]) == 1


def test_file_url_input(conn, media):
    path = media("as-url.mp3")
    uri = Path(path).as_uri()
    req = add(conn, uri)
    assert req["kind"] == "file"
    assert req["input"] == uri


def test_title_param_names_the_item(conn, media):
    req = add(conn, media("titled.mp3"), title="A Custom Title")
    item = {it["id"]: it for it in queue_items(conn)}[first_item(req)]
    assert item["title"] == "A Custom Title"


def test_at_end_appends(conn, media):
    a = add(conn, media("a.mp3"))
    b = add(conn, media("b.mp3"))
    c = add(conn, media("c.mp3"), at="end")
    assert queue_ids(conn) == [first_item(a), first_item(b), first_item(c)]


def test_at_next_inserts_after_current(conn, media):
    a = add(conn, media("a.mp3"))
    wait_playing(conn, first_item(a))
    b = add(conn, media("b.mp3"))
    c = add(conn, media("c.mp3"), at="next")
    assert queue_ids(conn) == [first_item(a), first_item(c), first_item(b)]


def test_at_now_replaces_the_queue_and_plays(conn, media):
    add(conn, media("a.mp3"))
    add(conn, media("b.mp3"))
    now = add(conn, media("now.mp3"), at="now")
    assert queue_ids(conn) == now["item_ids"]
    wait_playing(conn, first_item(now))


def test_play_is_add_at_now(conn, media):
    add(conn, media("a.mp3"))
    add(conn, media("b.mp3"))
    req = conn.result("play", {"input": media("played.mp3"), "wait": True}, timeout=20)["request"]
    assert req["status"] == "done"
    assert queue_ids(conn) == req["item_ids"]
    wait_playing(conn, first_item(req))


@pytest.mark.parametrize("bad", [
    "ftp://example.com/song.mp3",
    "av://lavfi:sine",
    "lavfi:sine=f=440",
    "memory://abc",
    "Re:Zero",  # no whitespace + a 2+ char scheme: a URL, not words (§3.1)
])
def test_unrecognized_scheme_fails_1010(conn, bad):
    err = conn.error("queue.add", {"input": bad}, code=1010)
    assert err["message"]
    conn.error("play", {"input": bad}, code=1010)
    assert status(conn)["length"] == 0


@pytest.mark.parametrize("missing", [
    "/nonexistent-tunetape-dir/song.mp3",
    "./no-such-tunetape-file.mp3",
    "../no-such-tunetape-file.mp3",
    "~/no-such-tunetape-file-9f2c.mp3",
    "file:///nonexistent-tunetape-dir/song.mp3",
])
def test_path_to_no_file_fails_1010(conn, missing):
    # A path-looking input that names no file is never a search (§3.1 rule 6).
    err = conn.error("queue.add", {"input": missing}, code=1010)
    assert err["message"]
    assert status(conn)["length"] == 0


@pytest.mark.parametrize("needs_ytdlp", [
    "ytsearch1:daft punk around the world",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/playlist?list=PL0123456789abcdef",
    "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    "https://example.com/some/page",
    "daft punk around the world",
    # Whitespace makes these plain words (a search), not a scheme: §3.1.
    "artist: song",
    "Re:Zero opening",
])
def test_missing_ytdlp_fails_1011_synchronously(conn, needs_ytdlp):
    # The suite points TUNETAPE_YTDLP at a program that does not exist, which
    # counts as missing (docs/architecture.md, "Test seams").
    err = conn.error("queue.add", {"input": needs_ytdlp}, code=1011)
    assert err["message"]
    assert status(conn)["length"] == 0


def test_requests_list_is_most_recent_first(conn, media):
    r1 = add(conn, media("r1.mp3"))
    r2 = add(conn, media("r2.mp3"))
    reqs = conn.result("requests.list")["requests"]
    ids = [r["id"] for r in reqs]
    assert ids.index(r2["id"]) < ids.index(r1["id"])
    assert ids[0] == r2["id"]


def test_requests_get(conn, media):
    req = add(conn, media("get.mp3"))
    got = conn.result("requests.get", {"id": req["id"]})["request"]
    assert got["id"] == req["id"]
    assert got["status"] == "done"
    assert got["item_ids"] == req["item_ids"]


def test_requests_get_unknown_fails_1013(conn):
    conn.error("requests.get", {"id": "r-does-not-exist"}, code=1013)


def test_queue_list_on_empty_queue(conn):
    res = conn.result("queue.list")
    assert res["items"] == []
    assert res["index"] == -1


def test_queue_jump(conn, media):
    reqs = [add(conn, media(f"j{i}.mp3")) for i in range(3)]
    s = conn.result("queue.jump", {"index": 2})
    assert s["index"] == 2
    wait_playing(conn, first_item(reqs[2]))


@pytest.mark.parametrize("index", [3, 99, -1])
def test_queue_jump_out_of_range_fails_1013(conn, media, index):
    for i in range(3):
        add(conn, media(f"r{i}.mp3"))
    conn.error("queue.jump", {"index": index}, code=1013)


def test_queue_remove_other_item(conn, media):
    a, b, c = (add(conn, media(f"{n}.mp3")) for n in "abc")
    wait_playing(conn, first_item(a))
    res = conn.result("queue.remove", {"id": first_item(b)})
    assert [it["id"] for it in res["items"]] == [first_item(a), first_item(c)]
    assert res["index"] == 0
    assert status(conn)["current"]["id"] == first_item(a)


def test_queue_remove_current_skips_to_next(conn, media):
    a, b = (add(conn, media(f"{n}.mp3")) for n in "ab")
    wait_playing(conn, first_item(a))
    res = conn.result("queue.remove", {"id": first_item(a)})
    assert [it["id"] for it in res["items"]] == [first_item(b)]
    wait_playing(conn, first_item(b))


def test_queue_remove_unknown_fails_1013(conn, media):
    add(conn, media("x.mp3"))
    conn.error("queue.remove", {"id": "i-does-not-exist"}, code=1013)


def test_queue_clear_stops_playback(conn, media):
    req = add(conn, media("clear.mp3"))
    wait_playing(conn, first_item(req))
    res = conn.result("queue.clear")
    assert res["items"] == [] and res["index"] == -1
    s = wait_idle(conn)
    assert s["length"] == 0
    assert s["current"] is None
    assert s["index"] == -1
    assert conn.result("queue.list")["items"] == []


def test_items_carry_the_posted_input(conn, media):
    path = media("input.mp3")
    req = add(conn, path)
    item = queue_items(conn)[0]
    assert item["id"] == first_item(req)
    assert os.path.basename(item["input"]).endswith("input.mp3")
