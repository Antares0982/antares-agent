import asyncio
import queue
from types import SimpleNamespace

import pytest

from antares_agent.config import Settings
from antares_agent.manifest import empty
from antares_agent.profiles import Profile
from antares_agent.runner import ThreadRunner


class FakeRuntime:
    def __init__(self):
        self.slots = asyncio.Semaphore(2)
        self.client = self
        self.messages = queue.Queue()
        self.sent = []
        self.released = []
        self.closed = False
        self.interrupts = []

    async def skills(self, manifest):
        pass

    async def prepare(self, session, profile, mode, instructions):
        return session or "codex-session", "fixture-model"

    async def turn(self, session, inputs, mode, profile, model, manifest):
        self.sent.append((inputs, mode))
        return SimpleNamespace(turn=SimpleNamespace(id="turn-1"))

    def next_turn_notification(self, turn):
        result = self.messages.get(timeout=3)
        if isinstance(result, Exception):
            raise result
        return result

    def unregister_turn_notifications(self, turn):
        pass

    def finish(self, status="completed"):
        self.messages.put(
            SimpleNamespace(method="turn/completed", payload={"turn": {"status": status}})
        )

    def turn_interrupt(self, session, turn):
        self.interrupts.append(turn)
        self.finish("interrupted")

    async def release(self, session):
        self.released.append(session)

    async def close(self):
        self.closed = True


async def make(tmp_path):
    settings = Settings(workspace=tmp_path, db_path=tmp_path / "db", require_sandbox=False)
    runtime = FakeRuntime()
    saved = []
    runner = ThreadRunner(
        "thr_1",
        settings,
        empty(tmp_path),
        Profile("quick"),
        runtime=runtime,
        persist=lambda state: saved.append(state.session_id),
    )
    await runner.start()
    return runner, runtime, saved


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_queue_and_resume(tmp_path):
    runner, runtime, saved = await make(tmp_path)
    await runner.send("first")
    assert await runner.send("second") == 1
    await wait_for(lambda: len(runtime.sent) == 1)
    assert saved == ["codex-session"]
    with pytest.raises(ValueError):
        await runner.set_permission_mode("plan")
    runtime.finish()
    await wait_for(lambda: len(runtime.sent) == 2)
    runtime.finish()
    await runner._pump
    assert not runner.busy
    await runner.set_permission_mode("plan")
    assert runner.state.permission_mode == "plan"
    assert runtime.released == ["codex-session", "codex-session"]
    await runner.close()


async def test_interrupt_and_failure(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    await runner.send("first")
    await runner.send("queued")
    await wait_for(lambda: bool(runtime.sent))
    await runner.interrupt()
    assert runtime.interrupts == ["turn-1"]
    assert len(runtime.sent) == 1
    assert not runner.busy
    await runner.send("next")
    runtime.messages.put(RuntimeError("fixture failure"))
    await runner._pump
    assert runtime.closed
    assert not runner.busy
    assert any(
        e.type == "error" and e.data["code"] == "runtime_failed" for e in runner.log.since(0)
    )
    await runner.close()


async def test_waiting_interrupt(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    runtime.slots = asyncio.Semaphore(0)
    await runner.send("queued globally")
    await runner.interrupt()
    assert not runtime.closed
    assert not runtime.sent
    assert not runner.busy
    await runner.close()


async def test_outbox_once(tmp_path):
    runner, _, _ = await make(tmp_path)
    path = runner.outbox / "report.txt"
    path.write_text("fixture")
    await runner._publish_outbox()
    await runner._publish_outbox()
    assert not path.exists()
    assert len([e for e in runner.log.since(0) if e.type == "file"]) == 1
    await runner.close()
