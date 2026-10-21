import asyncio
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai_codex.errors import InvalidRequestError, TransportClosedError

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
        self.steered = []
        self.unregistered = []

    async def skills(self, manifest):
        pass

    async def prepare(self, session, profile, mode, instructions):
        self.prepared_instructions = instructions
        self.prepared_model = profile
        return session or "codex-session", profile or "fixture-model"

    async def turn(self, session, inputs, mode, profile, model, manifest):
        self.sent.append((inputs, mode))
        return SimpleNamespace(turn=SimpleNamespace(id=f"turn-{len(self.sent)}"))

    def turn_steer(self, session, turn, inputs):
        self.steered.append((session, turn, inputs))

    def next_turn_notification(self, turn):
        result = self.messages.get(timeout=3)
        if isinstance(result, Exception):
            raise result
        return result

    def unregister_turn_notifications(self, turn):
        self.unregistered.append(turn)

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
        Profile("auto"),
        runtime=runtime,
        persist=lambda state: saved.append(state.session_id),
    )
    await runner.start()
    return runner, runtime, saved


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_startup_steering(tmp_path):
    runner, runtime, saved = await make(tmp_path)
    await runner.send("first")
    assert await runner.send("second") == 1
    assert await runner.send("third") == 2
    await wait_for(lambda: len(runtime.steered) == 2)
    assert runtime.steered == [
        ("codex-session", "turn-1", "second"),
        ("codex-session", "turn-1", "third"),
    ]
    assert saved == ["codex-session"]
    with pytest.raises(ValueError):
        await runner.set_permission_mode("plan")
    runtime.finish()
    await runner._pump
    assert len(runtime.sent) == 1
    assert not runner.busy
    await runner.set_permission_mode("plan")
    assert runner.state.permission_mode == "plan"
    assert runtime.released == ["codex-session"]
    await runner.close()


async def test_active_steering(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    await runner.send("first")
    await wait_for(lambda: runner._turn_id is not None)
    image = [{"type": "text", "text": "look"}, {"type": "localImage", "path": "/tmp/a.png"}]
    assert await runner.send(image) is None
    assert runtime.steered == [("codex-session", "turn-1", image)]
    assert not runner.state.queue
    runtime.finish()
    await runner._pump
    assert len(runtime.sent) == 1
    await runner.close()


async def test_waiting_steering(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    runtime.slots = asyncio.Semaphore(0)
    await runner.send("first")
    assert await runner.send("second") == 1
    assert not runtime.sent
    runtime.slots.release()
    await wait_for(lambda: bool(runtime.steered))
    assert runtime.steered == [("codex-session", "turn-1", "second")]
    runtime.finish()
    await runner._pump
    assert len(runtime.sent) == 1
    await runner.close()


async def test_cleanup_queue(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def cleanup():
        cleaning.set()
        await release.wait()

    runner._publish_diffs = cleanup
    await runner.send("first")
    await wait_for(lambda: runner._turn_id is not None)
    runtime.finish()
    await cleaning.wait()
    assert await runner.send("second") == 1
    release.set()
    await wait_for(lambda: len(runtime.sent) == 2)
    runtime.finish()
    await runner._pump
    assert runtime.sent == [("first", "auto"), ("second", "auto")]
    assert not runtime.steered
    await runner.close()


@pytest.mark.parametrize("startup", [False, True])
async def test_steering_finished_turn(tmp_path, startup):
    runner, runtime, _ = await make(tmp_path)
    runtime.turn_steer = Mock(side_effect=InvalidRequestError(-32600, "no active turn to steer"))
    await runner.send("first")
    if not startup:
        await wait_for(lambda: runner._turn_id is not None)
    assert await runner.send("second") == 1
    await wait_for(lambda: runtime.turn_steer.called)
    assert await runner.send("third") == 2
    runtime.turn_steer = FakeRuntime.turn_steer.__get__(runtime)
    runtime.finish()
    await wait_for(lambda: len(runtime.steered) == 1)
    assert runtime.sent == [("first", "auto"), ("second", "auto")]
    assert runtime.steered == [("codex-session", "turn-2", "third")]
    runtime.finish()
    await runner._pump
    assert runtime.unregistered == ["turn-1", "turn-2"]
    await runner.close()


async def test_completion_during_steer(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def steer(session, turn, inputs):
        entered.set()
        assert release.wait(2)
        runtime.steered.append((session, turn, inputs))

    runtime.turn_steer = steer
    await runner.send("first")
    await wait_for(lambda: runner._turn_id is not None)
    sending = asyncio.create_task(runner.send("second"))
    await wait_for(entered.is_set)
    runtime.finish()
    await asyncio.sleep(0.02)
    assert not runner._pump.done()
    release.set()
    assert await sending is None
    await runner._pump
    assert len(runtime.sent) == 1
    assert len(runtime.steered) == 1
    await runner.close()


@pytest.mark.parametrize(
    "error",
    [TransportClosedError("uncertain"), InvalidRequestError(-32600, "input must not be empty")],
)
async def test_steering_no_replay(tmp_path, error):
    runner, runtime, _ = await make(tmp_path)
    await runner.send("first")
    await wait_for(lambda: runner._turn_id is not None)
    runtime.turn_steer = Mock(side_effect=error)
    with pytest.raises(type(error)):
        await runner.send("second")
    assert not runner.state.queue
    runtime.finish()
    await runner._pump
    assert len(runtime.sent) == 1
    await runner.close()


async def test_model_switch(tmp_path):
    runner, runtime, _ = await make(tmp_path)
    await runner.start(resume="existing-session")
    await runner.set_model("model-a")
    await runner.send("first")
    with pytest.raises(ValueError, match="正在执行"):
        await runner.set_model("model-b")
    await wait_for(lambda: bool(runtime.sent))
    assert runtime.prepared_model == "model-a"
    runtime.finish()
    await runner._pump
    await runner.set_model("model-b")
    await runner.send("second")
    await wait_for(lambda: len(runtime.sent) == 2)
    assert runtime.prepared_model == "model-b"
    assert runner.state.session_id == "existing-session"
    runtime.finish()
    await runner._pump
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


async def test_initial_prompt(tmp_path):
    from antares_agent.manager import ThreadManager
    from antares_agent.store import Store

    settings = Settings(workspace=tmp_path, db_path=tmp_path / "db")
    store = Store(settings.db_path)
    manager = ThreadManager(settings, store)
    manager.runtime = FakeRuntime()
    store.set_prompt("7", "PROMPT_ALPHA")
    row = await manager.create(chat_id="7")
    store.set_prompt("7", "PROMPT_BETA")
    runner = await manager.runner(row.thread_id)
    await runner.send("first")
    await wait_for(lambda: len(manager.runtime.sent) == 1)
    assert "PROMPT_ALPHA" in manager.runtime.prepared_instructions
    assert "PROMPT_BETA" not in manager.runtime.prepared_instructions
    store.set_prompt("7", "")
    manager.runtime.finish()
    await runner._pump
    await runner.send("second")
    await wait_for(lambda: len(manager.runtime.sent) == 2)
    assert manager.runtime.prepared_instructions.count("PROMPT_ALPHA") == 1
    manager.runtime.finish()
    await runner._pump
    await manager.close_thread(row.thread_id)
    revived = await manager.runner(row.thread_id)
    assert revived.prompt == "PROMPT_ALPHA"
    fresh = await manager.create(chat_id="7")
    assert fresh.prompt == ""
    await manager.shutdown()
    store.close()
