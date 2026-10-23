"""§6 behaviour the daemon owns: auto-advance, fail forward, history.

Uses the fake mpv: a track "plays" for FAKE_MPV_DURATION seconds, and a media
path containing ``fake-fail`` fails to start.
"""

from .wire import add, first_item, item_status, queue_items, wait_for, wait_idle, wait_playing


def test_auto_advance_to_the_end_keeps_the_queue(daemon_factory, media):
    d = daemon_factory(fake_duration=1)
    conn = d.connect()
    conn.result("events.subscribe", {"types": ["track"]})
    reqs = [add(conn, media(f"adv{i}.mp3")) for i in range(3)]
    ids = [first_item(r) for r in reqs]

    def all_played():
        st = item_status(conn)
        return all(st.get(i) == "played" for i in ids)

    wait_for(all_played, timeout=30, what="every item to be played")
    s = wait_idle(conn)
    assert s["length"] == 3
    assert [it["id"] for it in queue_items(conn)] == ids

    order = []
    for ev in conn.events:
        cur = ev["data"]["current"] if ev["type"] == "track" else None
        if cur and (not order or order[-1] != cur["id"]):
            order.append(cur["id"])
    assert order == ids, f"track events should follow the queue: {order}"


def _queue_behind_a_paused_track(conn, media, names):
    """Play a first track, pause it, queue ``names`` behind it; return their item ids."""
    first = add(conn, media("first.mp3"))
    wait_playing(conn, first_item(first))
    conn.result("player.pause")
    return first_item(first), [first_item(add(conn, media(n))) for n in names]


def test_fail_forward(conn, media):
    conn.result("events.subscribe", {"types": ["error", "queue"]})
    _, (bad, good) = _queue_behind_a_paused_track(conn, media, ["fake-fail.mp3", "after.mp3"])
    conn.result("player.next")
    wait_playing(conn, good, timeout=15)
    items = {it["id"]: it for it in queue_items(conn)}
    assert items[bad]["status"] == "failed"
    assert items[bad]["error"]
    ev = conn.wait_event("error", lambda data: data["item_id"] == bad)
    assert ev["message"]


def test_failure_on_the_last_item_stops(conn, media):
    _, (bad,) = _queue_behind_a_paused_track(conn, media, ["last-fake-fail.mp3"])
    conn.result("player.next")
    wait_for(lambda: item_status(conn)[bad] == "failed", what="the item to fail")
    s = wait_idle(conn)
    assert s["length"] == 2


def test_five_consecutive_failures_stop(conn, media):
    names = [f"bad{i}-fake-fail.mp3" for i in range(6)] + ["never.mp3"]
    _, ids = _queue_behind_a_paused_track(conn, media, names)
    conn.result("player.next")
    wait_for(lambda: item_status(conn)[ids[4]] == "failed", timeout=20, what="five failures")
    wait_idle(conn)
    st = item_status(conn)
    assert [st[i] for i in ids[:5]] == ["failed"] * 5
    assert st[ids[5]] == "queued", "the player stops after 5 consecutive failures"
    assert st[ids[6]] == "queued"


def test_played_file_is_recorded_in_history(conn, media):
    path = media("history.mp3")
    req = add(conn, path, title="History Track")
    wait_playing(conn, first_item(req))

    def entry():
        for e in conn.result("history.list")["entries"]:
            if e["url"] == path or e["url"].endswith("history.mp3"):
                return e
        return None

    e = wait_for(entry, timeout=5, what="a history entry for the played file")
    assert e["type"] == "file"
    assert e["title"] == "History Track"
    assert e["play_count"] >= 1


def test_history_limit(conn, media):
    for i in range(3):
        req = add(conn, media(f"h{i}.mp3"), at="now")
        wait_playing(conn, first_item(req))
    wait_for(lambda: len(conn.result("history.list")["entries"]) >= 3, timeout=5, what="3 history entries")
    assert len(conn.result("history.list", {"limit": 2})["entries"]) == 2
