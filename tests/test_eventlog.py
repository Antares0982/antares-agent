from __future__ import annotations

import asyncio

import pytest

from antares_agent.eventlog import EventLog
from antares_agent.events import Event, EventType


def text(thread_id: str, body: str) -> Event:
    return Event(type=EventType.TEXT, thread_id=thread_id, data={"content": body})


def make(capacity: int = 8) -> EventLog:
    return EventLog("thr_1", capacity=capacity)


async def collect(log: EventLog, after: int | None, want: int, timeout: float = 1.0) -> list[str]:
    out: list[str] = []

    async def reader() -> None:
        async for event in log.stream(after):
            out.append(event.data["content"])
            if len(out) >= want:
                return

    await asyncio.wait_for(reader(), timeout)
    return out


def test_ids_are_assigned_in_order() -> None:
    log = make()
    first = log.publish(text("thr_1", "a"))
    second = log.publish(text("thr_1", "b"))
    assert (first.id, second.id) == (1, 2)
    assert log.last_id == 2


def test_since_is_exclusive() -> None:
    log = make()
    for body in "abc":
        log.publish(text("thr_1", body))
    assert [e.data["content"] for e in log.since(1)] == ["b", "c"]
    assert log.since(3) == []
    assert log.since(None) == []


def test_buffer_is_bounded() -> None:
    log = make(capacity=2)
    for body in "abcd":
        log.publish(text("thr_1", body))
    assert [e.data["content"] for e in log.since(0)] == ["c", "d"]


async def test_stream_replays_then_follows_live() -> None:
    log = make()
    log.publish(text("thr_1", "old"))

    async def publish_later() -> None:
        await asyncio.sleep(0.02)
        log.publish(text("thr_1", "new"))

    task = asyncio.create_task(publish_later())
    assert await collect(log, after=0, want=2) == ["old", "new"]
    await task


async def test_an_event_published_during_replay_is_not_lost() -> None:
    """The gap between replaying and subscribing is where approvals vanish.

    Subscribing first means a concurrent publish lands in the queue instead of
    between the two steps; the id filter then drops the duplicate.
    """
    log = make()
    log.publish(text("thr_1", "a"))

    seen: list[str] = []
    stream = log.stream(after=0)
    seen.append((await anext(stream)).data["content"])

    log.publish(text("thr_1", "b"))
    seen.append((await anext(stream)).data["content"])

    assert seen == ["a", "b"]
    await stream.aclose()


async def test_replayed_events_are_not_delivered_twice() -> None:
    log = make()
    for body in "ab":
        log.publish(text("thr_1", body))
    # Both are in the buffer *and* will be pushed to the new subscriber's
    # queue if published concurrently; the cursor must suppress the repeat.
    assert await collect(log, after=0, want=2) == ["a", "b"]


async def test_close_ends_open_streams() -> None:
    log = make()
    log.publish(text("thr_1", "a"))

    async def reader() -> list[str]:
        return [e.data["content"] async for e in log.stream(after=0)]

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.02)
    log.close()

    assert await asyncio.wait_for(task, 1) == ["a"]


async def test_a_reopened_log_can_be_followed_again() -> None:
    # The log outlives the runner: eviction closes it, the next message revives
    # the thread on the same log. Without `reopen` every later stream would end
    # right after its replay and the turn would run with nobody following it.
    log = make()
    log.publish(text("thr_1", "before"))
    log.close()

    log.reopen()
    task = asyncio.create_task(collect(log, after=1, want=1))
    await asyncio.sleep(0.02)
    log.publish(text("thr_1", "after"))

    assert await asyncio.wait_for(task, 1) == ["after"]


async def test_a_stalled_subscriber_never_blocks_publishing() -> None:
    log = make()
    log.publish(text("thr_1", "first"))
    stream = log.stream(after=0)
    await anext(stream)  # subscribe, then stop reading

    for body in "abcdefgh":
        log.publish(text("thr_1", body))  # must not raise or block

    assert log.last_id == 9  # the seed event plus all eight
    await stream.aclose()


def test_sink_receives_every_event_with_its_id() -> None:
    captured: list[int] = []
    log = EventLog("thr_1", capacity=4, sink=lambda e: captured.append(e.id))
    for body in "abc":
        log.publish(text("thr_1", body))
    assert captured == [1, 2, 3]


@pytest.mark.parametrize("after", [None, 0, 5])
def test_since_never_raises_on_odd_cursors(after: int | None) -> None:
    log = make()
    log.publish(text("thr_1", "a"))
    assert isinstance(log.since(after), list)
