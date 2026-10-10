"""The pool's own bookkeeping: what gets closed, and what a close must keep.

The failure guarded against is invisible from the outside -- a thread that
looks idle in every event stream while its CLI spins a core (F31), or one that
was closed without its session id reaching the store and so resumes as a blank
conversation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from antares_agent.config import Settings
from antares_agent.manager import ThreadManager
from antares_agent.store import Store


@dataclass
class FakeState:
    session_id: str | None = "sess_1"


@dataclass
class FakeRunner:
    """Only what the reaper touches. A real one needs a CLI subprocess."""

    busy: bool = False
    last_active: float = 0.0
    state: FakeState = field(default_factory=FakeState)
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


def make(tmp_path: Path, ttl: int) -> ThreadManager:
    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(
        workspace=workspace,
        db_path=tmp_path / "antares.db",
        profiles_dir=tmp_path / "profiles",
        require_sandbox=False,
        idle_ttl_s=ttl,
    )
    return ThreadManager(settings, Store(settings.db_path))


def park(manager: ThreadManager, thread_id: str, *, idle_for: float, busy: bool = False) -> None:
    manager.store.create_thread(thread_id, "default")
    manager._live[thread_id] = FakeRunner(  # type: ignore[assignment]
        busy=busy, last_active=time.monotonic() - idle_for
    )


async def test_a_long_idle_thread_is_closed(tmp_path: Path) -> None:
    manager = make(tmp_path, ttl=60)
    park(manager, "thr_old", idle_for=120)

    assert await manager.reap_idle() == ["thr_old"]
    assert "thr_old" not in manager._live


async def test_a_recently_active_thread_is_left_alone(tmp_path: Path) -> None:
    manager = make(tmp_path, ttl=60)
    park(manager, "thr_new", idle_for=5)

    assert await manager.reap_idle() == []
    assert "thr_new" in manager._live


async def test_a_busy_thread_is_never_reaped(tmp_path: Path) -> None:
    # Idle *time* is not idleness: a turn that runs past the TTL without a
    # status event would be torn down mid-flight, abandoning the work.
    manager = make(tmp_path, ttl=60)
    park(manager, "thr_working", idle_for=600, busy=True)

    assert await manager.reap_idle() == []
    assert "thr_working" in manager._live


async def test_reaping_records_the_session_before_dropping_the_client(tmp_path: Path) -> None:
    # Losing this is losing the conversation: `resume` has nothing to name.
    manager = make(tmp_path, ttl=60)
    park(manager, "thr_old", idle_for=120)
    runner = manager._live["thr_old"]

    await manager.reap_idle()

    row = manager.store.get_thread("thr_old")
    assert row is not None and row.session_id == "sess_1"
    assert runner.closed  # type: ignore[attr-defined]


@pytest.mark.parametrize("ttl", [0, -1])
async def test_the_reaper_can_be_turned_off(tmp_path: Path, ttl: int) -> None:
    manager = make(tmp_path, ttl=ttl)
    park(manager, "thr_ancient", idle_for=10_000)

    assert await manager.reap_idle() == []
    assert "thr_ancient" in manager._live


async def test_expired_cleanup(tmp_path):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from antares_agent.events import Event, EventType

    manager = make(tmp_path, ttl=60)
    manager.runtime.delete_thread = AsyncMock(side_effect=RuntimeError("offline"))
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="milliseconds")
    for thread in ("old", "busy", "recent", "legacy", "transfer"):
        manager.store.create_thread(thread, "default", "7")
    for thread in ("old", "busy", "legacy", "transfer"):
        manager.store._db.execute(
            "UPDATE threads SET last_active_at=? WHERE thread_id=?", (cutoff, thread)
        )
    manager.store._db.commit()
    manager.store.touch("old", session_id="session", activity=False)
    manager.store.append_event(Event(EventType.TEXT, "old", {"content": "secret"}).with_id(1))
    manager.store.begin_request("request", "old", "hash")
    manager._live["busy"] = FakeRunner(busy=True)
    source = tmp_path / "work" / "keep.txt"
    source.write_text("work survives")
    old_artifact = await manager.artifacts.collect("old", source)
    active_artifact = await manager.artifacts.collect("transfer", source)
    manager.artifacts.active.add(active_artifact["id"])
    assert await manager.purge_expired() == ["legacy"]
    assert manager.store.get_thread("old") is None
    assert manager.store.events_since("old", 0)
    assert manager.store.get_thread("busy") is not None
    assert manager.store.get_thread("recent") is not None
    assert manager.store.get_thread("transfer") is not None
    manager.runtime.delete_thread.side_effect = None
    assert await manager.purge_expired() == ["old"]
    assert not manager.store.events_since("old", 0)
    assert manager.store.request("request") is None
    assert not manager.artifacts.path(old_artifact["id"]).exists()
    assert manager.artifacts.get(old_artifact["id"]) is None
    assert source.read_text() == "work survives"
    assert manager.runtime.delete_thread.await_count == 2
    assert await manager.purge_expired() == []
    manager.artifacts.close()
    manager.store.close()


async def test_retirement_activity(tmp_path):
    manager = make(tmp_path, ttl=60)
    park(manager, "old", idle_for=120)
    before = manager.store.get_thread("old").last_active_at
    await manager.reap_idle()
    assert manager.store.get_thread("old").last_active_at == before
    manager.artifacts.close()
    manager.store.close()


async def test_cleanup_lock(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    manager = make(tmp_path, ttl=60)
    manager.store.create_thread("old", "default")
    manager.store._db.execute("UPDATE threads SET last_active_at='2000-01-01T00:00:00.000+00:00'")
    manager.store._db.commit()
    async with manager.activity_lock:
        task = asyncio.create_task(manager.purge_expired())
        await asyncio.sleep(0)
        assert not task.done()
        manager.store.touch("old")
    assert await task == []
    manager.runtime.delete_thread = AsyncMock()
    manager.store.mark_deleting("old")
    assert await manager.purge_expired() == ["old"]
    manager.runtime.delete_thread.assert_not_awaited()
    manager.artifacts.close()
    manager.store.close()


async def test_retention_boundary(tmp_path):
    manager = make(tmp_path, ttl=60)
    for thread, stamp in (
        ("expired", "2026-01-01T00:00:00.000+00:00"),
        ("recent", "2026-01-01T00:00:00.001+00:00"),
    ):
        manager.store.create_thread(thread, "default")
        manager.store._db.execute(
            "UPDATE threads SET last_active_at=? WHERE thread_id=?", (stamp, thread)
        )
    manager.store._db.commit()
    assert [
        row.thread_id for row in manager.store.expired_threads("2026-01-01T00:00:00.000+00:00")
    ] == ["expired"]
    manager.artifacts.close()
    manager.store.close()


async def test_cleanup_refresh(tmp_path):
    from unittest.mock import AsyncMock

    manager = make(tmp_path, ttl=60)
    for thread in ("first", "finished"):
        manager.store.create_thread(thread, "default")
        manager.store.touch(thread, session_id=thread)
    manager.store._db.execute("UPDATE threads SET last_active_at='2000-01-01T00:00:00.000+00:00'")
    manager.store._db.commit()
    manager.runtime.delete_thread = AsyncMock(
        side_effect=lambda session: manager.store.touch("finished")
    )
    assert await manager.purge_expired() == ["first"]
    assert manager.store.get_thread("finished") is not None
    manager.artifacts.close()
    manager.store.close()
