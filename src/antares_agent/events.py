"""The SSE event vocabulary.

Every event carries its own agent attribution and its own render hint, so an
IM adapter can build the orchestrator/subagent tree and decide what to fold
without understanding a single Agent SDK concept -- including MCP tools that
did not exist when the adapter was written.

See docs/design/02-sse-api.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

Render = Literal["none", "summary", "diff", "full"]


class EventType(StrEnum):
    THREAD_STATUS = "thread.status"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    AGENT_SPAWN = "agent.spawn"
    AGENT_DONE = "agent.done"
    DIFF = "diff"
    FILE = "file"
    QUEUED = "queued"
    TURN_DONE = "turn.done"
    ERROR = "error"


class ThreadStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"


class ErrorCode(StrEnum):
    INTERRUPTED = "interrupted"
    INTERNAL = "internal"


@dataclass(frozen=True)
class AgentRef:
    """Who produced an event. `id` is stable for the life of the thread."""

    id: str = "root"
    name: str = "orchestrator"
    parent_tool_use_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "parent_tool_use_id": self.parent_tool_use_id}


ROOT_AGENT = AgentRef()


@dataclass(frozen=True)
class Event:
    type: EventType
    thread_id: str
    data: dict[str, Any] = field(default_factory=dict)
    agent: AgentRef = ROOT_AGENT
    id: int = 0
    ts: str = ""

    def with_id(self, event_id: int) -> Event:
        """Assigned by the thread's event log, which owns the sequence."""
        return Event(
            type=self.type,
            thread_id=self.thread_id,
            data=self.data,
            agent=self.agent,
            id=event_id,
            ts=self.ts or datetime.now(UTC).isoformat(timespec="milliseconds"),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "id": f"evt_{self.id:06d}",
            "thread_id": self.thread_id,
            "ts": self.ts,
            "agent": self.agent.as_dict(),
            **self.data,
        }

    def sse(self) -> dict[str, str]:
        """Fields for sse_starlette's `EventSourceResponse`."""
        return {
            "event": str(self.type),
            "id": str(self.id),
            "data": json.dumps(self.payload(), ensure_ascii=False),
        }
