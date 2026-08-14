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
