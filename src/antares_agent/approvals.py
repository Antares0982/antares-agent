"""Out-of-band approval requests.

`can_use_tool` blocks on a Future while the request travels out over SSE and
the decision comes back over HTTP. Several background subagents can be waiting
at once, so every request carries its own short id and the client must be able
to show more than one at a time.

Nothing here is persisted. V1 measured what happens when the process dies
holding a pending approval: the CLI synthesises the tool call as a failure,
the turn ends, and the session stays consistent. What is needed on restart is
a retry entry point, not a durable queue.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ToolPermissionContext

from .eventlog import EventLog
from .events import Event, EventType, ThreadStatus
from .permissions import Verdict

log = logging.getLogger(__name__)


class UnknownApproval(KeyError):
    pass


@dataclass
class Pending:
    approval_id: str
    tool: str
    tool_input: dict[str, Any]
    reason: str
    future: asyncio.Future[Verdict]


class ApprovalBroker:
    def __init__(self, thread_id: str, log_: EventLog, timeout_s: int = 600) -> None:
        self.thread_id = thread_id
        self._log = log_
        self._timeout = timeout_s
        self._pending: dict[str, Pending] = {}

    @property
    def pending(self) -> list[Pending]:
        return list(self._pending.values())

    async def ask(
        self,
        tool: str,
        tool_input: dict[str, Any],
        reason: str,
        context: ToolPermissionContext | None = None,
    ) -> Verdict:
        # Short on purpose: this id has to survive a Telegram callback_data
        # payload, which is capped at 64 bytes.
        approval_id = "apr_" + secrets.token_hex(3)
        future: asyncio.Future[Verdict] = asyncio.get_running_loop().create_future()
        entry = Pending(approval_id, tool, tool_input, reason, future)
        self._pending[approval_id] = entry

        self._log.publish(
            Event(
                type=EventType.APPROVAL_REQUIRED,
                thread_id=self.thread_id,
                data={
                    "approval_id": approval_id,
                    "tool_use_id": getattr(context, "tool_use_id", None),
                    "tool": tool,
                    "input": tool_input,
                    "reason": reason,
                },
            )
        )
        self._publish_status()

        try:
            verdict = await asyncio.wait_for(asyncio.shield(future), self._timeout)
        except TimeoutError:
            verdict = Verdict(allow=False, message="审批超时，已自动拒绝。可以换一个方案再试。")
            self._log.publish(
                Event(
                    type=EventType.ERROR,
                    thread_id=self.thread_id,
                    data={"code": "approval_timeout", "message": f"{tool} 等待审批超时"},
                )
            )
        except asyncio.CancelledError:
            self._pending.pop(approval_id, None)
            self._publish_status()
            raise
        finally:
            self._pending.pop(approval_id, None)

        self._log.publish(
            Event(
                type=EventType.APPROVAL_RESOLVED,
                thread_id=self.thread_id,
                data={
                    "approval_id": approval_id,
                    "decision": "allow" if verdict.allow else "deny",
                    "message": verdict.message,
                },
            )
        )
        self._publish_status()
        return verdict

    def resolve(self, approval_id: str, allow: bool, message: str = "") -> None:
        entry = self._pending.get(approval_id)
        if entry is None:
            raise UnknownApproval(approval_id)
        if not entry.future.done():
            entry.future.set_result(Verdict(allow=allow, message=message))

    def cancel_all(self, message: str = "线程已关闭") -> None:
        """Release every waiter so the SDK's tool calls can unwind."""
        for entry in list(self._pending.values()):
            if not entry.future.done():
                entry.future.set_result(Verdict(allow=False, message=message))

    def _publish_status(self) -> None:
        status = ThreadStatus.AWAITING_APPROVAL if self._pending else ThreadStatus.BUSY
        self._log.publish(
            Event(
                type=EventType.THREAD_STATUS,
                thread_id=self.thread_id,
                data={"status": str(status), "pending_approvals": len(self._pending)},
            )
        )
