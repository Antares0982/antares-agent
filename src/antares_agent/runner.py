from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace

from . import gitdiff, index
from .artifacts import Artifacts, signature
from .config import Settings
from .eventlog import EventLog
from .events import Event, EventType, ThreadStatus
from .manifest import Manifest
from .profiles import ORCHESTRATION, Profile
from .runtime import Runtime
from .translate import Translator

log = logging.getLogger(__name__)

OUTBOX_HINT = """
要把文件发给用户，请将副本放进 `{path}`。每轮结束后台发送，成功接收后移走副本。
保留之后还要使用的原件。单文件上限 {limit} 字节，接收失败时保留副本并报告原因。
"""


@dataclass
class ThreadState:
    thread_id: str
    profile: Profile
    status: ThreadStatus = ThreadStatus.IDLE
    queue: deque = field(default_factory=deque)
    session_id: str | None = None
    summary: str = ""
    permission_mode: str = ""
    model: str | None = None


class ThreadRunner:
    def __init__(
        self,
        thread_id: str,
        settings: Settings,
        manifest: Manifest,
        profile: Profile,
        event_log=None,
        runtime=None,
        artifacts=None,
        persist=None,
    ):
        self.settings = settings
        self.manifest = manifest
        self.state = ThreadState(thread_id, profile)
        self.log = event_log or EventLog(thread_id, capacity=settings.event_buffer)
        self.runtime = runtime or Runtime(settings)
        self._owns_runtime = runtime is None
        self.artifacts = artifacts
        self._owns_artifacts = artifacts is None
        self.persist = persist or (lambda state: None)
        self._outbox_errors: dict[str, str] = {}
        self.translator = Translator(thread_id, manifest, settings)
        self.last_active = time.monotonic()
        self._pump: asyncio.Task | None = None
        self._turn_id: str | None = None
        self._stopping = False
        self._waiting = True

    @property
    def thread_id(self):
        return self.state.thread_id

    @property
    def busy(self):
        return self.state.status is not ThreadStatus.IDLE

    @property
    def outbox(self):
        return self.settings.workspace / self.manifest.scratch / "outbox" / self.thread_id

    async def start(self, resume=None):
        self.state.session_id = resume
        if not self.state.permission_mode:
            self.state.permission_mode = self.state.profile.permission_mode
        skills = self.settings.workspace / ".agents" / "skills"
        for path in (skills, self.outbox):
            if not path.resolve().is_relative_to(self.settings.workspace.resolve()):
                raise ValueError(f"路径超出工作区：{path}")
        self.outbox.mkdir(parents=True, exist_ok=True)
        skills.mkdir(parents=True, exist_ok=True)

    async def send(self, text):
        if self.busy:
            self.state.queue.append(text)
            position = len(self.state.queue)
            self.log.publish(Event(EventType.QUEUED, self.thread_id, {"position": position}))
            return position
        self._stopping = False
        self._waiting = True
        self._set_status(ThreadStatus.BUSY)
        self._pump = asyncio.create_task(self._run(text))
        return None

    async def set_model(self, model: str):
        if self.busy:
            raise ValueError("会话正在执行，请结束或停止任务后再切换模型")
        self.state.model = model
        self.state.profile = replace(self.state.profile, model=model)
        self.persist(self.state)

    async def _run(self, inputs):
        try:
            async with self.runtime.slots:
                self._waiting = False
                while inputs is not None and not self._stopping:
                    await asyncio.to_thread(index.write, self.manifest)
                    await self.runtime.skills(self.manifest)
                    profile = self.state.profile
                    instructions = (
                        ORCHESTRATION
                        + "\n"
                        + profile.append
                        + OUTBOX_HINT.format(path=self.outbox, limit=self.settings.max_file_bytes)
                    )
                    session, model = await self.runtime.prepare(
                        self.state.session_id, profile, self.state.permission_mode, instructions
                    )
                    self.state.session_id = session
                    self.state.model = model
                    self.state.profile = replace(profile, model=model)
                    if not self.state.summary:
                        text = (
                            inputs
                            if isinstance(inputs, str)
                            else " ".join(item.get("text", "") for item in inputs)
                        )
                        self.state.summary = " ".join(text.split())[:40]
                    self.persist(self.state)
                    if self._stopping:
                        await self.runtime.release(session)
                        break
                    self.translator.reset()
                    started = await self.runtime.turn(
                        session, inputs, self.state.permission_mode, profile, model, self.manifest
                    )
                    self._turn_id = started.turn.id
                    if self._stopping:
                        await asyncio.to_thread(
                            self.runtime.client.turn_interrupt, session, self._turn_id
                        )
                    client = self.runtime.client
                    assert client is not None
                    try:
                        while True:
                            notification = await asyncio.to_thread(
                                client.next_turn_notification, self._turn_id
                            )
                            if notification.method == "turn/completed":
                                self.translator.usage = getattr(self.runtime, "usage", {}).get(
                                    session, self.translator.usage
                                )
                            for event in self.translator.handle(notification):
                                self.log.publish(event)
                            if notification.method == "turn/completed":
                                break
                    finally:
                        client.unregister_turn_notifications(self._turn_id)
                    self._turn_id = None
                    await self._publish_diffs()
                    await self._publish_outbox()
                    await self.runtime.release(session)
                    self.persist(self.state)
                    inputs = self.state.queue.popleft() if self.state.queue else None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Codex turn failed for %s", self.thread_id)
            self.state.queue.clear()
            self.log.publish(
                Event(
                    EventType.ERROR,
                    self.thread_id,
                    {
                        "code": "runtime_failed",
                        "message": "Codex 任务失败，未自动重试；请检查服务及登录状态。",
                    },
                )
            )
            await self.runtime.close()
        finally:
            self._turn_id = None
            self._set_status(ThreadStatus.IDLE)

    async def interrupt(self):
        self.state.queue.clear()
        self._stopping = True
        if not self.busy or self._pump is None:
            return
        try:
            if self._turn_id and self.runtime.client:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.runtime.client.turn_interrupt, self.state.session_id, self._turn_id
                    ),
                    10,
                )
                await asyncio.wait_for(asyncio.shield(self._pump), 10)
            elif self._waiting:
                self._pump.cancel()
                await asyncio.gather(self._pump, return_exceptions=True)
            else:
                await asyncio.wait_for(asyncio.shield(self._pump), 10)
        except TimeoutError:
            await self.runtime.close()
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
        if self.busy:
            self._set_status(ThreadStatus.IDLE)
        self.log.publish(
            Event(
                EventType.ERROR,
                self.thread_id,
                {"code": "interrupted", "message": "已打断当前任务并清空队列"},
            )
        )

    async def set_permission_mode(self, mode):
        if mode not in {"plan", "auto"}:
            raise ValueError("模式必须是 plan 或 auto")
        if self.busy:
            raise ValueError("任务运行中，请先打断再切换模式")
        self.state.permission_mode = mode
        self.persist(self.state)
        self._set_status(self.state.status)

    async def close(self):
        if self.busy:
            await self.interrupt()
        with contextlib.suppress(Exception):
            await self.runtime.release(self.state.session_id)
        if self._owns_runtime:
            await self.runtime.close()
        self.log.close()
        if self._owns_artifacts and self.artifacts is not None:
            self.artifacts.close()

    def _set_status(self, status):
        self.state.status = status
        self.last_active = time.monotonic()
        self.log.publish(
            Event(
                EventType.THREAD_STATUS,
                self.thread_id,
                {
                    "status": str(status),
                    "background_agents": len(self.translator.background_tasks),
                    "permission_mode": self.state.permission_mode,
                    "approvals_reviewer": "auto_review",
                },
            )
        )

    async def _publish_diffs(self) -> None:
        for diff in await gitdiff.collect(self.manifest):
            self.log.publish(
                Event(
                    type=EventType.DIFF,
                    thread_id=self.thread_id,
                    data={
                        "repo": diff.repo,
                        "stat": diff.stat,
                        "patch": diff.patch,
                        "truncated": diff.truncated,
                    },
                )
            )

    async def _publish_outbox(self) -> None:
        if not self.outbox.is_dir():
            return
        for path in sorted(self.outbox.iterdir()):
            if path.is_dir() and not path.is_symlink():
                continue
            stamp = ""
            try:
                stamp = signature(path)
                if self.artifacts is None:
                    self.artifacts = Artifacts(self.settings.artifact_path)
                row = await self.artifacts.collect(
                    self.thread_id, path, self.settings.max_file_bytes
                )
                if not row["published"]:
                    self.log.publish(
                        Event(
                            type=EventType.FILE,
                            thread_id=self.thread_id,
                            data=json.loads(row["metadata"]),
                        )
                    )
                    self.artifacts.published(row["id"])
                if signature(path) == row["signature"]:
                    path.unlink()
                self._outbox_errors.pop(str(path), None)
            except (OSError, ValueError) as exc:
                if self._outbox_errors.get(str(path)) != stamp:
                    self.log.publish(
                        Event(
                            type=EventType.ERROR,
                            thread_id=self.thread_id,
                            data={"code": "file_failed", "message": f"{path.name} 未接收：{exc}"},
                        )
                    )
                    self._outbox_errors[str(path)] = stamp
