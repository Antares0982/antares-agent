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
import base64
import contextlib
import logging
import mimetypes
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
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

#: 128 + SIGTERM, the shell convention the CLI reports its child's death with.
_SIGTERM_EXIT = 143


def _is_shutdown(exc: BaseException) -> bool:
    """Did the CLI die because someone asked it to, rather than break?"""
    return getattr(exc, "exit_code", None) == _SIGTERM_EXIT


#: Grace period after a ResultMessage before believing the thread is idle.
#: `background_tasks_changed` can trail the ResultMessage it belongs to, and a
#: spurious `idle` makes the client hide its progress indicator and start
#: dequeuing. Cheap insurance; the spawn bookkeeping is the real check.
IDLE_SETTLE_S = 0.4

#: Cap on one outgoing file. Mirrors the inbound ceiling: the bytes cross the
#: bus base64-encoded and land in the thread's event log on the way, so this is
#: paid twice whether or not the user keeps the file.
MAX_OUTBOX_BYTES = 10 * 1024 * 1024

#: Where the model puts things it wants delivered. Said in the system prompt
#: because the path is per thread and there is nowhere else the model could
#: learn its own id -- the same reason `_stash` names the inbox path inline.
OUTBOX_HINT = """
要把文件（图片、报告、构建产物）发给用户，就复制一份到 `{path}`。
每轮结束时该目录里的文件会被发出并移走，所以放进去的必须是副本，不是你之后还要读的原件。
超过 10MB 的不会发出，只会告诉用户它太大了。
"""


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
    #: Live value; the profile supplies only the initial one (D7). Deliberately
    #: not persisted -- `start()` rebuilds the client from the profile, so a
    #: `bypassPermissions` from one session must not follow the thread into the
    #: next one.
    permission_mode: str = ""


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
        # The hot mode belongs to the client, and this builds a new one from
        # the profile. The reported value has to follow it back, or the thread
        # would keep claiming a bypass that is no longer in effect.
        self.state.permission_mode = self.state.profile.permission_mode
        # Created here rather than left to the model: a `cp` into a directory
        # that does not exist fails, and the prompt would then be describing
        # something that only works from tools which happen to mkdir -p.
        self.outbox.mkdir(parents=True, exist_ok=True)
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

    @property
    def outbox(self) -> Path:
        """Per thread, like the inbox: an event carries a thread id and that is
        what tells the client which chat the file belongs to."""
        return self.settings.workspace / self.manifest.scratch / "outbox" / self.thread_id

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
                Event(type=EventType.QUEUED, thread_id=self.thread_id, data={"position": position})
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
            Event(
                type=EventType.ERROR,
                thread_id=self.thread_id,
                data={"code": "interrupted", "message": "已打断当前任务"},
            )
        )

    async def set_permission_mode(self, mode: str) -> None:
        """Hot, unlike the profile: this never enters a model request (D7).

        The switch is logged because `auto`, `dontAsk` and `bypassPermissions`
        stop the CLI from calling `can_use_tool` at all -- and every tier past
        the deny rules lives in there, including tier 3 and the Bash
        credential-path check in `classify`. F30: without this the event log
        shows approvals simply ceasing, with nothing recording why.
        """
        if self._client is None:
            return
        await self._client.set_permission_mode(mode)  # type: ignore[arg-type]
        self.state.permission_mode = mode
        self._set_status(self.state.status)

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
            if _is_shutdown(exc):
                # systemd's default KillMode is control-group, so a `systemctl
                # restart` signals the CLI children and this process at the
                # same instant. The child usually loses that race, and the loop
                # sees its death before `close()` gets to cancel it. Reporting
                # a routine restart to the user as a red internal error is the
                # same mistake as the four `[Errno 2]` resume failures: the
                # chat learns to ignore the one channel that should mean
                # something.
                log.info("thread %s: CLI terminated on shutdown", self.thread_id)
                self._set_status(ThreadStatus.IDLE)
                return
            log.exception("thread %s message loop failed", self.thread_id)
            self.log.publish(
                Event(
                    type=EventType.ERROR,
                    thread_id=self.thread_id,
                    data={"code": "internal", "message": str(exc)},
                )
            )
            self._set_status(ThreadStatus.IDLE)

    async def _on_result(self) -> None:
        await self._publish_diffs()
        self._publish_outbox()
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

    def _publish_outbox(self) -> None:
        """Send whatever the turn left in the outbox, then take it out again.

        Leaving the directory *is* the record of having been sent. A second
        ledger -- a table, an mtime cursor -- would only have to be kept in
        step with the filesystem, which already knows; and this way a restart
        cannot replay the directory, because nothing outside a turn ever looks
        at it and a finished turn leaves it empty.

        Published first and removed after, so a crash in between costs a
        duplicate rather than the file itself.
        """
        if not self.outbox.is_dir():
            return
        for path in sorted(p for p in self.outbox.iterdir() if p.is_file()):
            try:
                size = path.stat().st_size
                # ponytail: the base64 lands in sqlite with the event, so a
                # sent file is paid for once more in the log; point the event
                # at a served path instead if the db gets fat.
                data = b"" if size > MAX_OUTBOX_BYTES else path.read_bytes()
            except OSError as exc:
                log.warning("outbox: cannot read %s: %s", path, exc)
                continue
            self.log.publish(
                Event(
                    type=EventType.FILE,
                    thread_id=self.thread_id,
                    data={
                        "name": path.name,
                        "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        "size": size,
                        **(
                            {"reason": "too_large"}
                            if size > MAX_OUTBOX_BYTES
                            else {"data_b64": base64.b64encode(data).decode()}
                        ),
                    },
                )
            )
            # The oversized ones leave too. Left in place they would be retried
            # at the end of every turn from here on, forever.
            with contextlib.suppress(OSError):
                path.unlink()

    def _set_status(self, status: ThreadStatus) -> None:
        self.state.status = status
        self.log.publish(
            Event(
                type=EventType.THREAD_STATUS,
                thread_id=self.thread_id,
                data={
                    "status": str(status),
                    "background_agents": len(self.translator.background_tasks),
                    # On every status event, not only on a switch: this is the
                    # field that answers "was the arbiter on at time T", and it
                    # can only answer that if it is present throughout.
                    "permission_mode": self.state.permission_mode,
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
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                # Thread-specific, but still fixed for the thread's life, so
                # D6 holds: nothing here changes after the session is created.
                "append": profile.append + OUTBOX_HINT.format(path=self.outbox),
            },
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
