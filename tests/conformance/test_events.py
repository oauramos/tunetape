"""§4.4 events: subscription, filtering and every event type's payload."""

from .wire import EVENT_TYPES, add, first_item, wait_playing


def test_subscribe_all(conn):
    res = conn.result("events.subscribe")
    assert set(EVENT_TYPES) <= set(res["subscribed"])


def test_subscribe_filtered(conn, media):
    res = conn.result("events.subscribe", {"types": ["queue"]})
    assert res["subscribed"] == ["queue"]
    req = add(conn, media("filtered.mp3"))
    wait_playing(conn, first_item(req))  # state and track change meanwhile
    conn.wait_event("queue")
    conn.pump(0.5)
    assert {e["type"] for e in conn.events} <= {"queue"}, [e["type"] for e in conn.events]


def test_no_events_without_subscribing(daemon, media):
    quiet = daemon.connect()
    loud = daemon.connect()
    add(loud, media("quiet.mp3"))
    quiet.pump(1.0)
    assert quiet.events == []


def test_queue_event(conn, media):
    conn.result("events.subscribe")
    req = add(conn, media("q.mp3"))
    data = conn.wait_event("queue", lambda d: first_item(req) in [it["id"] for it in d["items"]])
    assert data["index"] >= -1


def test_request_event(conn, media):
    conn.result("events.subscribe", {"types": ["request"]})
    req = add(conn, media("r.mp3"), wait=False)
    data = conn.wait_event("request", lambda d: d["request"]["id"] == req["id"] and d["request"]["status"] == "done")
    assert data["request"]["item_ids"]


def test_request_events_move_forward(conn, media):
    conn.result("events.subscribe", {"types": ["request"]})
    req = add(conn, media("order.mp3"), wait=False)
    conn.wait_event("request", lambda d: d["request"]["id"] == req["id"] and d["request"]["status"] == "done")
    # The events (not the queue.add result, which may be a later snapshot)
    # must never move a request backwards: pending -> expanding -> done.
    rank = {"pending": 0, "expanding": 1, "done": 2, "failed": 2}
    seen = [rank[e["data"]["request"]["status"]] for e in conn.events
            if e["data"]["request"]["id"] == req["id"]]
    seen.append(2)  # the "done" event wait_event consumed
    assert seen == sorted(seen), seen


def test_track_event(conn, media):
    conn.result("events.subscribe", {"types": ["track"]})
    req = add(conn, media("t.mp3"))
    data = conn.wait_event("track", lambda d: d["current"] is not None and d["current"]["id"] == first_item(req))
    assert data["length"] == 1


def test_state_event_on_pause(conn, media):
    req = add(conn, media("s.mp3"))
    wait_playing(conn, first_item(req))
    conn.result("events.subscribe", {"types": ["state"]})
    conn.result("player.pause")
    conn.wait_event("state", lambda d: d["state"] == "paused")
    conn.result("player.volume", {"value": 61})
    conn.wait_event("state", lambda d: abs(d["volume"] - 61) < 0.01)


def test_error_event_on_failed_item(conn, media):
    conn.result("events.subscribe", {"types": ["error"]})
    first = add(conn, media("ok.mp3"))
    wait_playing(conn, first_item(first))
    conn.result("player.pause")
    bad = add(conn, media("e-fake-fail.mp3"))
    conn.result("player.next")
    data = conn.wait_event("error", lambda d: d["item_id"] == first_item(bad), timeout=15)
    assert data["message"]


def test_shutdown_event(daemon):
    watcher = daemon.connect()
    watcher.result("events.subscribe", {"types": ["shutdown"]})
    daemon.connect().result("daemon.shutdown")
    watcher.wait_event("shutdown")
    assert watcher.closed_by_daemon(10)
    daemon.wait_exit(10)
