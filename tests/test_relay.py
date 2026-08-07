from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from antares_agent.relay import Relay


def sse(frames: list[tuple[str, dict[str, Any]]]) -> bytes:
    """Serialise like sse-starlette does, keepalive comment included."""
    out = [": ping\n\n"]
    for type_, payload in frames:
        out.append(
            f"event: {type_}\r\nid: {payload['id']}\r\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\r\n\r\n"
        )
    return "".join(out).encode()


def event(event_id: int, type_: str, **data: Any) -> tuple[str, dict[str, Any]]:
    return type_, {"id": f"evt_{event_id:06d}", "thread_id": "thr_x", **data}


class FakeRelay(Relay):
    """Captures what would have gone onto the bus."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__(http)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def _publish(self, type_: str, payload: dict[str, Any]) -> None:
        self.published.append((type_, payload))


def relay_over(handler: Any) -> FakeRelay:
    transport = httpx.MockTransport(handler)
    return FakeRelay(httpx.AsyncClient(transport=transport, base_url="http://agent"))


# --- following -----------------------------------------------------------


async def test_follow_stops_at_idle_and_forwards_everything() -> None:
    frames = [
        event(2, "thread.status", status="busy"),
        event(3, "text", content="hi"),
        event(4, "thread.status", status="idle"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            assert request.url.params["after"] == "1"
            return httpx.Response(200, content=sse(frames))
        return httpx.Response(200, json={"status": "idle"})

    relay = relay_over(handler)
    await relay._follow_until_idle("thr_x", after=1)

    assert [t for t, _ in relay.published] == ["thread.status", "text", "thread.status"]
    assert relay.published[1][1]["content"] == "hi"


async def test_a_stale_idle_in_the_replayed_gap_does_not_end_the_follow() -> None:
    """The failure this guards against is silent and total.

    `?after=` replays from the bot's cursor, which can predate the idle that
    ended the *previous* turn. Treating that as this turn's idle would stop
    the follow one event in, and everything the turn goes on to produce --
    including any `approval.required` -- would never reach the bot.
    """
    frames = [
        event(2, "thread.status", status="idle"),  # the previous turn's
        event(3, "thread.status", status="busy"),  # this turn's
        event(4, "text", content="working"),
        event(5, "thread.status", status="idle"),  # this turn's
        event(6, "text", content="never reached"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=sse(frames))
        return httpx.Response(200, json={"status": "idle"})

    relay = relay_over(handler)
    await relay._follow_until_idle("thr_x", after=1)

    assert [p.get("content") for _, p in relay.published] == [None, None, "working", None]


async def test_a_thread_already_busy_needs_no_fresh_busy_event() -> None:
    # After a relay restart there is no new `busy` to see: the turn started
    # before this process did. Without the status probe the follower would
    # never stop, and the thread would be followed (and so kept live) forever.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=sse([event(9, "thread.status", status="idle")]))
        return httpx.Response(200, json={"status": "busy"})

    relay = relay_over(handler)
    await relay._follow_until_idle("thr_x", after=8)

    assert len(relay.published) == 1


async def test_one_follower_per_thread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=sse([event(2, "thread.status", status="idle")]))
        return httpx.Response(200, json={"status": "busy"})

    relay = relay_over(handler)
    relay._follow("thr_x", 1)
    first = relay._followers["thr_x"]
    relay._follow("thr_x", 1)
    assert relay._followers["thr_x"] is first
    await first


# --- commands ------------------------------------------------------------


async def test_resume_replays_from_sqlite_without_following_an_idle_thread() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "events": [{"type": "text", "payload": {"id": "evt_000002", "content": "m"}}],
                "last_event_id": 2,
                "status": "idle",
            },
        )

    relay = relay_over(handler)
    await relay._dispatch({"op": "resume", "thread_id": "thr_x", "after": 1})

    assert relay.published == [("text", {"id": "evt_000002", "content": "m"})]
    assert calls == ["/v1/threads/thr_x/events/replay"]  # never opened the stream
    assert relay._followers == {}


async def test_an_evicted_thread_reports_409_rather_than_failing_silently() -> None:
    # The bot has to be able to tell the user why the button did nothing.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "thread is not running"})

    relay = relay_over(handler)

    class FakeIncoming:
        body = json.dumps(
            {"op": "approve", "thread_id": "thr_x", "approval_id": "apr_1", "decision": "allow"}
        ).encode()

        def process(self, requeue: bool = True) -> Any:
            class Ctx:
                async def __aenter__(self) -> None: ...
                async def __aexit__(self, *exc: Any) -> None: ...

            return Ctx()

    await relay._on_command(FakeIncoming())  # type: ignore[arg-type]

    assert len(relay.published) == 1
    type_, payload = relay.published[0]
    assert type_ == "relay.cmd_failed"
    assert payload["op"] == "approve" and "409" in payload["detail"]


async def test_message_posts_then_follows() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"status": "accepted", "position": None})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, content=sse([event(3, "thread.status", status="idle")]))
        return httpx.Response(200, json={"status": "busy"})

    relay = relay_over(handler)
    await relay._dispatch({"op": "message", "thread_id": "thr_x", "text": "go", "after": 2})
    await relay._followers["thr_x"]

    assert posted == [{"text": "go"}]
    assert relay.published == [
        ("thread.status", {"id": "evt_000003", "thread_id": "thr_x", "status": "idle"})
    ]


async def test_unknown_op_is_reported_not_swallowed() -> None:
    relay = relay_over(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="unknown op"):
        await relay._dispatch({"op": "teleport", "thread_id": "thr_x"})
