"""What a crash leaves behind, and what startup writes over it.

The failure being guarded against is silent: a thread that looks like it is
still working, or an approval button the user is waiting on that nobody is
listening for. Both look identical to "busy" from the client's side.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from antares_agent.config import Settings
from antares_agent.events import Event, EventType
from antares_agent.manager import ThreadManager
from antares_agent.store import Store


@pytest.fixture
def manager(tmp_path: Path) -> ThreadManager:
    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(
        workspace=workspace,
        db_path=tmp_path / "antares.db",
        profiles_dir=tmp_path / "profiles",
        require_sandbox=False,
    )
    return ThreadManager(settings, Store(settings.db_path))


def append(store: Store, thread_id: str, type_: EventType, **data: object) -> None:
    """Write an event the way a running thread would have."""
    event = Event(type=type_, thread_id=thread_id, data=data)
    store.append_event(event.with_id(store.last_event_id(thread_id) + 1))


def types(store: Store, thread_id: str) -> list[str]:
    return [row["type"] for row in store.events_since(thread_id, 0)]


def crashed_awaiting_approval(store: Store) -> str:
    """A thread stopped dead between asking and being answered."""
    store.create_thread("thr_a", "default")
    append(store, "thr_a", EventType.THREAD_STATUS, status="busy")
    append(store, "thr_a", EventType.APPROVAL_REQUIRED, approval_id="apr_111", tool="Bash")
    append(store, "thr_a", EventType.THREAD_STATUS, status="awaiting_approval")
    return "thr_a"


def test_a_lost_approval_is_reported_and_the_thread_returned_to_idle(
    manager: ThreadManager,
) -> None:
    thread_id = crashed_awaiting_approval(manager.store)

    assert manager.recover() == [thread_id]

    assert types(manager.store, thread_id)[-2:] == ["error", "thread.status"]
    events = manager.store.events_since(thread_id, 3)
    assert events[0]["payload"]["code"] == "approval_lost"
    # Named, so the client can retire exactly the buttons that died.
    assert events[0]["payload"]["approval_ids"] == ["apr_111"]
    assert events[1]["payload"]["status"] == "idle"


def test_recovery_continues_the_event_sequence(manager: ThreadManager) -> None:
    # A client reconnecting with `?after=` has to see these as new events. A
    # reused id would be filtered out as already-seen and the thread would go
    # on looking busy -- the exact failure this whole pass exists to fix.
    thread_id = crashed_awaiting_approval(manager.store)
    manager.recover()

    ids = [row["payload"]["id"] for row in manager.store.events_since(thread_id, 0)]
    assert ids == ["evt_000001", "evt_000002", "evt_000003", "evt_000004", "evt_000005"]


def test_running_twice_does_not_report_the_same_loss_twice(manager: ThreadManager) -> None:
    # Recovery writes an `idle`, which is what makes the second pass a no-op.
    # Without that, every restart would re-announce a loss the user has
    # already been told about and already moved on from.
    thread_id = crashed_awaiting_approval(manager.store)
    manager.recover()
    before = types(manager.store, thread_id)

    assert manager.recover() == []
    assert types(manager.store, thread_id) == before


def test_a_crash_with_no_approval_pending_only_clears_the_status(
    manager: ThreadManager,
) -> None:
    manager.store.create_thread("thr_b", "default")
    append(manager.store, "thr_b", EventType.THREAD_STATUS, status="busy")
    append(manager.store, "thr_b", EventType.TEXT, content="half a thought")

    assert manager.recover() == ["thr_b"]
    assert types(manager.store, "thr_b") == ["thread.status", "text", "thread.status"]


def test_an_answered_approval_is_not_a_lost_one(manager: ThreadManager) -> None:
    thread_id = crashed_awaiting_approval(manager.store)
    append(
        manager.store,
        thread_id,
        EventType.APPROVAL_RESOLVED,
        approval_id="apr_111",
        decision="allow",
    )
    append(manager.store, thread_id, EventType.THREAD_STATUS, status="busy")

    # Still recovered -- it died mid-turn -- but with nothing to apologise for.
    assert manager.recover() == [thread_id]
    assert "error" not in types(manager.store, thread_id)


def test_one_lost_approval_among_several_is_still_named(manager: ThreadManager) -> None:
    # Background subagents ask concurrently (F25), so "the last approval" is
    # not the same question as "the unanswered one".
    thread_id = crashed_awaiting_approval(manager.store)
    append(manager.store, thread_id, EventType.APPROVAL_REQUIRED, approval_id="apr_222")
    append(
        manager.store,
        thread_id,
        EventType.APPROVAL_RESOLVED,
        approval_id="apr_222",
        decision="deny",
    )
    manager.recover()

    lost = [
        row
        for row in manager.store.events_since(thread_id, 0)
        if row["payload"].get("code") == "approval_lost"
    ]
    assert lost[0]["payload"]["approval_ids"] == ["apr_111"]


def test_an_idle_thread_is_left_alone(manager: ThreadManager) -> None:
    manager.store.create_thread("thr_c", "default")
    append(manager.store, "thr_c", EventType.THREAD_STATUS, status="busy")
    append(manager.store, "thr_c", EventType.TURN_DONE, subtype="success")
    append(manager.store, "thr_c", EventType.THREAD_STATUS, status="idle")

    assert manager.recover() == []
    assert len(manager.store.events_since("thr_c", 0)) == 3


def test_a_thread_that_never_ran_is_left_alone(manager: ThreadManager) -> None:
    # No events at all: created and never spoken to. There is nothing to
    # correct, and announcing `idle` would be the client's first ever event.
    manager.store.create_thread("thr_d", "default")

    assert manager.recover() == []
    assert manager.store.events_since("thr_d", 0) == []
