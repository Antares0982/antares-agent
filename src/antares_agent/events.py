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
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    DIFF = "diff"
    FILE = "file"
    QUEUED = "queued"
    TURN_DONE = "turn.done"
    ERROR = "error"


class ThreadStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    AWAITING_APPROVAL = "awaiting_approval"


class ErrorCode(StrEnum):
    INTERRUPTED = "interrupted"
    INTERNAL = "internal"
    APPROVAL_TIMEOUT = "approval_timeout"
    #: A pending approval died with the process. The CLI synthesises the tool
    #: call as a failure and the session stays consistent (V1), but the request
    #: is not re-sent on resume, so the user needs a retry entry point.
    APPROVAL_LOST = "approval_lost"


#: Server-side default folding policy. Clients may override wholesale (a
#: `/verbose` toggle) but should never need to maintain this table themselves.
_RENDER: dict[str, Render] = {
    "Read": "none",
    "Glob": "none",
    "Grep": "none",
    "WebFetch": "none",
    "WebSearch": "none",
    "TodoWrite": "none",
    "Edit": "diff",
    "Write": "diff",
    "NotebookEdit": "diff",
    "Bash": "summary",
    # `system:init` advertises this tool as `Task`, but the tool_use block's
    # name is `Agent` (F5). Both are mapped so neither spelling falls through.
    "Agent": "summary",
    "Task": "summary",
}

DEFAULT_RENDER: Render = "summary"


def render_for(tool: str) -> Render:
    """Unknown tools -- MCP servers, plugins -- get the conservative default."""
    return _RENDER.get(tool, DEFAULT_RENDER)


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


class AgentRegistry:
    """Maps a spawning tool_use_id to a stable agent identity.

    Subagents run in the background and their events interleave, so the client
    needs a key it can group by; `parent_tool_use_id` is that key and this just
    gives it a readable name and a short id.
    """

    def __init__(self) -> None:
        self._by_tool_use: dict[str, AgentRef] = {}
        self._n = 0

    def register(self, tool_use_id: str, subagent_type: str, repo_hint: str | None) -> AgentRef:
        self._n += 1
        name = f"{subagent_type}:{repo_hint}" if repo_hint else subagent_type
        ref = AgentRef(id=f"agt_{self._n}", name=name, parent_tool_use_id=tool_use_id)
        self._by_tool_use[tool_use_id] = ref
        return ref

    def resolve(self, parent_tool_use_id: str | None) -> AgentRef:
        if not parent_tool_use_id:
            return ROOT_AGENT
        known = self._by_tool_use.get(parent_tool_use_id)
        if known is not None:
            return known
        # An event arrived before (or without) its spawn -- still give the
        # client a groupable identity rather than folding it into root.
        return AgentRef(
            id=f"agt_{parent_tool_use_id[-6:]}",
            name="subagent",
            parent_tool_use_id=parent_tool_use_id,
        )
