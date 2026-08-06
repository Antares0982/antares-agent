"""One thread: one `ClaudeSDKClient`, one message loop, one queue.

The loop reads `receive_messages()`, never `receive_response()`. F25: the
latter returns at the first `ResultMessage`, but background subagents outlive
the turn and the SDK then starts a fresh turn on its own to fold their results
in. Reading the bounded stream truncates the work mid-flight and the client
sees a confident, wrong "done".

So the loop runs for the life of the thread and idleness is inferred, not
awaited: a `ResultMessage` counts only when nothing is still outstanding.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolPermissionContext,
)

from . import gitdiff
from .approvals import ApprovalBroker
from .config import Settings
from .eventlog import EventLog
from .events import Event, EventType, ThreadStatus
from .manifest import Manifest
from .permissions import Arbiter, Verdict, denied_tools, sandbox_settings
from .profiles import Profile
from .translate import Translator

log = logging.getLogger(__name__)

#: Grace period after a ResultMessage before believing the thread is idle.
#: `background_tasks_changed` can trail the ResultMessage it belongs to, and a
#: spurious `idle` makes the client hide its progress indicator and start
#: dequeuing. Cheap insurance; the spawn bookkeeping is the real check.
IDLE_SETTLE_S = 0.4


class ClientFactory(Protocol):
    """Injectable so tests can drive the loop without a CLI subprocess."""

    def __call__(self, *, options: ClaudeAgentOptions) -> Any: ...


@dataclass
class ThreadState:
    thread_id: str
    profile: Profile
    status: ThreadStatus = ThreadStatus.IDLE
    queue: deque[str] = field(default_factory=deque)
    session_id: str | None = None
    summary: str = ""


class ThreadRunner:
    def __init__(
        self,
        thread_id: str,
        settings: Settings,
        manifest: Manifest,
        profile: Profile,
        event_log: EventLog | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.state = ThreadState(thread_id=thread_id, profile=profile)
        self.log = event_log or EventLog(thread_id, capacity=settings.event_buffer)
        self.approvals = ApprovalBroker(thread_id, self.log, settings.approval_timeout_s)
        self.translator = Translator(thread_id, manifest, settings)

        self._new_client: ClientFactory = client_factory or ClaudeSDKClient
        self._client: ClaudeSDKClient | None = None
        self._pump: asyncio.Task[None] | None = None
        self._settle: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self, resume: str | None = None) -> None:
        self._client = self._new_client(options=self._options(resume))
        await self._client.connect()
        self._pump = asyncio.create_task(self._read_forever(), name=f"pump-{self.thread_id}")

    async def close(self) -> None:
        self.approvals.cancel_all("线程已关闭")
        self._cancel_settle()
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self.log.close()

    @property
    def thread_id(self) -> str:
        return self.state.thread_id

    @property
    def busy(self) -> bool:
        return self.state.status is not ThreadStatus.IDLE

    # -- input -----------------------------------------------------------

    async def send(self, text: str) -> int | None:
        """Queue or dispatch. Returns the queue position when queued (D1).

        The SDK refuses new input while a turn is running, so the queue lives
        here. Queuing rather than rejecting matches how Claude Code behaves
        and means a user typing a follow-up never loses it.
        """
        if self.busy:
            self.state.queue.append(text)
            position = len(self.state.queue)
            self.log.publish(
                Event(type=EventType.QUEUED, thread_id=self.thread_id,
                      data={"position": position})
            )
            return position

        await self._dispatch(text)
        return None

    async def interrupt(self) -> None:
        if self._client is None or not self.busy:
            return
        with contextlib.suppress(Exception):
            await self._client.interrupt()
        self.state.queue.clear()
        self.log.publish(
            Event(type=EventType.ERROR, thread_id=self.thread_id,
                  data={"code": "interrupted", "message": "已打断当前任务"})
        )

    async def set_permission_mode(self, mode: str) -> None:
        """Hot, unlike the profile: this never enters a model request (D7)."""
        if self._client is None:
            return
        await self._client.set_permission_mode(mode)  # type: ignore[arg-type]

    def resolve_approval(self, approval_id: str, allow: bool, message: str = "") -> None:
        self.approvals.resolve(approval_id, allow, message)

    # -- internals -------------------------------------------------------

    async def _dispatch(self, text: str) -> None:
        assert self._client is not None
        self._turn_active = True
        self._set_status(ThreadStatus.BUSY)
        if not self.state.summary:
            self.state.summary = " ".join(text.split())[:40]
        await self._client.query(text)

    async def _read_forever(self) -> None:
        assert self._client is not None
        try:
            async for message in self._client.receive_messages():
                for event in self.translator.handle(message):
                    self.log.publish(event)
                if isinstance(message, ResultMessage):
                    self.state.session_id = message.session_id or self.state.session_id
                    await self._on_result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("thread %s message loop failed", self.thread_id)
            self.log.publish(
                Event(type=EventType.ERROR, thread_id=self.thread_id,
                      data={"code": "internal", "message": str(exc)})
            )
            self._set_status(ThreadStatus.IDLE)

    async def _on_result(self) -> None:
        await self._publish_diffs()
        self._cancel_settle()

        if not self.translator.idle_possible:
            # Background subagents are still running; the SDK will start
            # another turn by itself once they report (F25).
            return

        self._settle = asyncio.create_task(self._settle_then_idle())

    async def _settle_then_idle(self) -> None:
        """Wait out the grace period *without* blocking the message loop.

        Sleeping inline would freeze the pump, so the re-check below would
        read exactly the same state it just read and the wait would buy
        nothing. As its own task, the pump keeps draining and a late
        `background_tasks_changed` can still change the answer.
        """
        await asyncio.sleep(IDLE_SETTLE_S)
        if not self.translator.idle_possible:
            return
        if self.state.queue:
            await self._dispatch(self.state.queue.popleft())
            return
        self._set_status(ThreadStatus.IDLE)

    def _cancel_settle(self) -> None:
        if self._settle is not None and not self._settle.done():
            self._settle.cancel()
        self._settle = None

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

    def _set_status(self, status: ThreadStatus) -> None:
        self.state.status = status
        self.log.publish(
            Event(
                type=EventType.THREAD_STATUS,
                thread_id=self.thread_id,
                data={
                    "status": str(status),
                    "background_agents": len(self.translator.background_tasks),
                },
            )
        )

    async def _ask(
        self,
        tool: str,
        tool_input: dict[str, Any],
        reason: str,
        context: ToolPermissionContext,
    ) -> Verdict:
        previous = self.state.status
        self.state.status = ThreadStatus.AWAITING_APPROVAL
        try:
            return await self.approvals.ask(tool, tool_input, reason, context)
        finally:
            self.state.status = previous

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        profile = self.state.profile
        env: dict[str, str] = {}
        if self.settings.gateway_base_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.gateway_base_url
        if self.settings.gateway_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.settings.gateway_auth_token
            env["ANTHROPIC_API_KEY"] = self.settings.gateway_auth_token

        return ClaudeAgentOptions(
            cwd=str(self.settings.workspace),
            cli_path=self.settings.cli_path,
            model=profile.model,
            effort=profile.effort,
            max_turns=profile.max_turns,
            env=env,
            resume=resume,
            setting_sources=["project"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": profile.append},
            permission_mode=profile.permission_mode,
            # Must stay empty: allow rules are evaluated ahead of
            # can_use_tool and would silently short-circuit it (F8).
            allowed_tools=[],
            disallowed_tools=denied_tools(self.settings),
            can_use_tool=Arbiter(self.settings, self._ask),
            sandbox=sandbox_settings(),
            # Only settable at session creation -- it cannot be added to a
            # session that already exists (D8).
            enable_file_checkpointing=True,
        )
