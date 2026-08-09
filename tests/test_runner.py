from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from antares_agent import runner as runner_mod
from antares_agent.config import Settings
from antares_agent.events import EventType, ThreadStatus
from antares_agent.manifest import empty as empty_manifest
from antares_agent.profiles import Profile
from antares_agent.runner import ThreadRunner


class FakeClient:
    """Stands in for ClaudeSDKClient: a queue in, a queue out."""

    def __init__(self, *, options: Any) -> None:
        self.options = options
        self.queries: list[str] = []
        self.interrupted = 0
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def set_permission_mode(self, mode: str) -> None: ...

    def emit(self, message: Any) -> None:
        self._inbox.put_nowait(message)

    def fail(self, exc: BaseException) -> None:
        self._inbox.put_nowait(exc)

    async def receive_messages(self):
        while True:
            message = await self._inbox.get()
            if isinstance(message, BaseException):
                raise message
            yield message


def result(session_id: str = "s1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        stop_reason=None,
        total_cost_usd=0.0,
        usage={},
    )


def spawn(tool_use_id: str = "toolu_a") -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id=tool_use_id,
                name="Agent",
                input={"subagent_type": "Explore", "prompt": "look around"},
            )
        ],
        model="m",
        parent_tool_use_id=None,
    )


def spawn_result(tool_use_id: str = "toolu_a") -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_use_id, content="done", is_error=False)]
    )


def background(*task_ids: str) -> SystemMessage:
    return SystemMessage(
        subtype="background_tasks_changed",
        data={"tasks": [{"task_id": t, "task_type": "local_agent"} for t in task_ids]},
    )


@pytest.fixture
def fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "IDLE_SETTLE_S", 0.01)


async def make(tmp_path: Path) -> tuple[ThreadRunner, FakeClient]:
    clients: list[FakeClient] = []

    def factory(*, options: Any) -> FakeClient:
        client = FakeClient(options=options)
        clients.append(client)
        return client

    r = ThreadRunner(
        thread_id="thr_1",
        settings=Settings(workspace=tmp_path),
        manifest=empty_manifest(tmp_path),
        profile=Profile(name="deep"),
        client_factory=factory,
    )
    await r.start()
    return r, clients[0]


async def settle(times: int = 6) -> None:
    for _ in range(times):
        await asyncio.sleep(0.02)


def kinds(r: ThreadRunner) -> list[str]:
    return [str(e.type) for e in r.log.since(0)]


# --- the F25 semantics, end to end ---------------------------------------


async def test_result_with_a_live_subagent_does_not_go_idle(
    tmp_path: Path, fast_settle: None
) -> None:
    r, client = await make(tmp_path)
    try:
        await r.send("do the thing")
        client.emit(spawn())
        client.emit(result())  # the SDK's first ResultMessage, work still running
        await settle()

        assert r.state.status is ThreadStatus.BUSY
        assert ThreadStatus.IDLE not in [
            e.data.get("status") for e in r.log.since(0) if e.type == EventType.THREAD_STATUS
        ]
    finally:
        await r.close()


async def test_idle_only_once_the_subagent_reports(tmp_path: Path, fast_settle: None) -> None:
    r, client = await make(tmp_path)
    try:
        await r.send("go")
        client.emit(spawn())
        client.emit(result())
        await settle()
        assert r.busy

        client.emit(spawn_result())
        client.emit(background())
        client.emit(result())
        await settle()

        assert r.state.status is ThreadStatus.IDLE
    finally:
        await r.close()


async def test_a_late_background_list_still_prevents_idle(
    tmp_path: Path, fast_settle: None
) -> None:
    # The settle window only helps if the message loop keeps running during
    # it. Emit the task list *after* the ResultMessage, as in the F25 trace.
    r, client = await make(tmp_path)
    try:
        await r.send("go")
        client.emit(result())
        client.emit(background("task_1"))
        await settle()

        assert r.busy
    finally:
        await r.close()


async def test_plain_turn_goes_idle(tmp_path: Path, fast_settle: None) -> None:
    r, client = await make(tmp_path)
    try:
        await r.send("hello")
        client.emit(AssistantMessage(content=[TextBlock(text="hi")], model="m",
                                     parent_tool_use_id=None))
        client.emit(result())
        await settle()

        assert r.state.status is ThreadStatus.IDLE
        assert EventType.TURN_DONE in kinds(r)
    finally:
        await r.close()


# --- queueing (D1) -------------------------------------------------------


async def test_messages_queue_while_busy_and_dispatch_on_idle(
    tmp_path: Path, fast_settle: None
) -> None:
    r, client = await make(tmp_path)
    try:
        await r.send("first")
        assert client.queries == ["first"]

        position = await r.send("second")
        assert position == 1
        assert client.queries == ["first"]
        assert EventType.QUEUED in kinds(r)

        client.emit(result())
        await settle()

        assert client.queries == ["first", "second"]
        assert r.busy  # the queued turn is now the live one
    finally:
        await r.close()


async def test_interrupt_clears_the_queue(tmp_path: Path, fast_settle: None) -> None:
    r, client = await make(tmp_path)
    try:
        await r.send("first")
        await r.send("second")
        await r.interrupt()

        assert client.interrupted == 1
        assert not r.state.queue
    finally:
        await r.close()


# --- approvals -----------------------------------------------------------


async def test_approval_round_trip(tmp_path: Path, fast_settle: None) -> None:
    r, _ = await make(tmp_path)
    try:
        task = asyncio.create_task(
            r._ask("Bash", {"command": "git push"}, "需要确认", None)  # type: ignore[arg-type]
        )
        await settle(2)

        required = [e for e in r.log.since(0) if e.type == EventType.APPROVAL_REQUIRED]
        assert len(required) == 1
        approval_id = required[0].data["approval_id"]
        assert len(approval_id) <= 20  # must fit a Telegram callback_data

        r.resolve_approval(approval_id, allow=True)
        verdict = await asyncio.wait_for(task, 1)

        assert verdict.allow
        assert EventType.APPROVAL_RESOLVED in kinds(r)
    finally:
        await r.close()


async def test_concurrent_approvals_are_independent(tmp_path: Path, fast_settle: None) -> None:
    # F25 confirmed background subagents really do run in parallel, so two
    # tools can be waiting at the same time.
    r, _ = await make(tmp_path)
    try:
        a = asyncio.create_task(r._ask("Bash", {"command": "git push"}, "a", None))  # type: ignore[arg-type]
        b = asyncio.create_task(r._ask("Bash", {"command": "git rebase"}, "b", None))  # type: ignore[arg-type]
        await settle(2)

        ids = [e.data["approval_id"] for e in r.log.since(0)
               if e.type == EventType.APPROVAL_REQUIRED]
        assert len(set(ids)) == 2

        r.resolve_approval(ids[1], allow=False, message="不要")
        second = await asyncio.wait_for(b, 1)
        assert second.allow is False
        assert not a.done()

        r.resolve_approval(ids[0], allow=True)
        assert (await asyncio.wait_for(a, 1)).allow
    finally:
        await r.close()


async def test_close_releases_waiting_approvals(tmp_path: Path, fast_settle: None) -> None:
    r, _ = await make(tmp_path)
    task = asyncio.create_task(r._ask("Bash", {"command": "git push"}, "x", None))  # type: ignore[arg-type]
    await settle(2)

    await r.close()
    verdict = await asyncio.wait_for(task, 1)
    assert verdict.allow is False


async def test_approval_timeout_denies_and_reports(tmp_path: Path, fast_settle: None) -> None:
    r, _ = await make(tmp_path)
    r.approvals._timeout = 0.05
    try:
        verdict = await asyncio.wait_for(
            r._ask("Bash", {"command": "git push"}, "x", None), 2  # type: ignore[arg-type]
        )
        assert verdict.allow is False
        codes = [e.data.get("code") for e in r.log.since(0) if e.type == EventType.ERROR]
        assert "approval_timeout" in codes
    finally:
        await r.close()


# --- options wiring ------------------------------------------------------


async def test_options_respect_the_measured_constraints(tmp_path: Path) -> None:
    r, client = await make(tmp_path)
    try:
        opts = client.options
        assert opts.allowed_tools == []  # F8: allow rules short-circuit the callback
        assert opts.can_use_tool is not None  # F16: absence triggers a bad fallback
        assert opts.enable_file_checkpointing is True  # D8: session-creation only
        assert opts.sandbox is not None and opts.sandbox["enabled"] is True
        assert any("git push --force" in rule for rule in opts.disallowed_tools)
        assert opts.cwd == str(tmp_path)
    finally:
        await r.close()


# --- permission mode -----------------------------------------------------


async def test_a_mode_switch_is_in_the_event_log(tmp_path: Path) -> None:
    """F30: `auto` and friends stop the CLI calling `can_use_tool` at all, so
    approvals just cease. The log has to say who turned them off and when."""
    r, _ = await make(tmp_path)
    try:
        assert r.state.permission_mode == "acceptEdits"  # the profile's
        before = [e for e in r.log.since(0) if e.type == EventType.THREAD_STATUS]

        await r.set_permission_mode("auto")

        after = [e for e in r.log.since(0) if e.type == EventType.THREAD_STATUS]
        assert len(after) == len(before) + 1
        assert after[-1].data["permission_mode"] == "auto"
        assert after[-1].data["status"] == str(ThreadStatus.IDLE)  # unchanged
    finally:
        await r.close()


async def test_a_bypass_does_not_survive_a_restart(tmp_path: Path) -> None:
    """`start()` rebuilds the client from the profile, so the mode reverts --
    and the reported value must revert with it rather than keep claiming it."""
    r, _ = await make(tmp_path)
    try:
        await r.set_permission_mode("bypassPermissions")
        assert r.state.permission_mode == "bypassPermissions"

        await r.start(resume="s1")
        assert r.state.permission_mode == "acceptEdits"
    finally:
        await r.close()


async def test_a_sigtermed_cli_is_not_reported_as_an_internal_error(
    tmp_path: Path,
) -> None:
    """A `systemctl restart` kills the whole control group, so the CLI child
    often dies before close() cancels the pump. That is a restart, not a
    fault, and the chat must not learn to ignore red lines."""
    r, client = await make(tmp_path)
    try:
        client.fail(ProcessError("Command failed", exit_code=143))
        await settle()

        assert [e for e in r.log.since(0) if e.type == EventType.ERROR] == []
        assert r.state.status is ThreadStatus.IDLE
    finally:
        await r.close()


async def test_a_real_crash_still_reaches_the_user(tmp_path: Path) -> None:
    r, client = await make(tmp_path)
    try:
        client.fail(ProcessError("Command failed", exit_code=1))
        await settle()

        errors = [e for e in r.log.since(0) if e.type == EventType.ERROR]
        assert [e.data["code"] for e in errors] == ["internal"]
    finally:
        await r.close()
