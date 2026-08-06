"""Turn SDK messages into SSE events.

Stateful on purpose: `tool.result` has to remember which tool it belongs to,
`agent.done` has to remember which subagent was spawned, and thread status has
to remember how many background tasks are still alive. Keeping that here means
the runner only has to own the loop.
"""

from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .config import Settings
from .events import AgentRegistry, Event, EventType, ThreadStatus, render_for
from .manifest import Manifest
from .permissions import will_sandbox

#: The Agent tool result ends with a line naming the subagent, which is what a
#: later `resume` needs.
_AGENT_ID_RE = re.compile(r"agentId:\s*(\S+)")

#: `system:init` advertises the spawn tool as `Task`; the tool_use block calls
#: it `Agent` (F5). Accept both spellings everywhere.
SPAWN_TOOLS = frozenset({"Agent", "Task"})

PREVIEW_LIMIT = 500


class Translator:
    def __init__(self, thread_id: str, manifest: Manifest, settings: Settings) -> None:
        self.thread_id = thread_id
        self.manifest = manifest
        self.settings = settings
        self.agents = AgentRegistry()
        #: tool_use_id -> tool name, so tool.result can name its tool
        self._tools: dict[str, str] = {}
        #: tool_use_id of Agent calls, so their results become agent.done
        self._spawns: dict[str, str] = {}
        #: Live background tasks. Empty *and* a ResultMessage means idle (F25).
        self.background_tasks: set[str] = set()

    # -- public ----------------------------------------------------------

    @property
    def idle_possible(self) -> bool:
        """A ResultMessage only ends the thread's work if nothing is left.

        Checking `background_tasks` alone is not enough. In the F25 trace the
        *first* `background_tasks_changed` arrived only after the first
        `ResultMessage`, so at the moment we have to make the call the task
        list is still empty and would wrongly read as idle. An Agent tool call
        with no result yet is known earlier and says the same thing, so both
        are consulted.
        """
        return not self.background_tasks and not self._spawns

    def handle(self, message: Message) -> list[Event]:
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        if isinstance(message, ResultMessage):
            return self._result(message)
        if isinstance(message, TaskStartedMessage):
            self.background_tasks.add(message.task_id)
            return [self._status(ThreadStatus.BUSY)]
        if isinstance(message, TaskUpdatedMessage):
            return self._task_updated(message)
        if isinstance(message, TaskNotificationMessage):
            self.background_tasks.discard(message.task_id)
            # A subagent that died without producing a tool_result would
            # otherwise pin the thread to `busy` forever.
            if message.tool_use_id:
                self._spawns.pop(message.tool_use_id, None)
            return [self._status(ThreadStatus.BUSY)]
        if isinstance(message, SystemMessage):
            return self._system(message)
        return []

    # -- per message type ------------------------------------------------

    def _assistant(self, message: AssistantMessage) -> list[Event]:
        agent = self.agents.resolve(message.parent_tool_use_id)
        events: list[Event] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    events.append(
                        self._event(EventType.TEXT, {"content": block.text, "delta": False}, agent)
                    )
            elif isinstance(block, ThinkingBlock):
                events.append(
                    self._event(EventType.THINKING, {"content": block.thinking}, agent)
                )
            elif isinstance(block, ToolUseBlock):
                events.extend(self._tool_use(block, agent))
        return events

    def _tool_use(self, block: ToolUseBlock, agent: Any) -> list[Event]:
        self._tools[block.id] = block.name
        events = [
            self._event(
                EventType.TOOL_CALL,
                {
                    "tool_use_id": block.id,
                    "tool": block.name,
                    "input": block.input,
                    "render": render_for(block.name),
                    "sandboxed": will_sandbox(block.name, block.input),
                },
                agent,
            )
        ]
        if block.name in SPAWN_TOOLS:
            subagent_type = str(block.input.get("subagent_type") or "general-purpose")
            prompt = str(block.input.get("prompt") or block.input.get("description") or "")
            hint = self._repo_hint(prompt)
            child = self.agents.register(block.id, subagent_type, hint)
            self._spawns[block.id] = subagent_type
            payload: dict[str, Any] = {
                "tool_use_id": block.id,
                "subagent_type": subagent_type,
                "agent_id": child.id,
                "task": prompt,
            }
            if hint:
                payload["repo_hint"] = hint
            events.append(self._event(EventType.AGENT_SPAWN, payload, agent))
        return events

    def _user(self, message: UserMessage) -> list[Event]:
        if isinstance(message.content, str):
            return []
        agent = self.agents.resolve(message.parent_tool_use_id)
        events: list[Event] = []
        for block in message.content:
            if not isinstance(block, ToolResultBlock):
                continue
            tool = self._tools.pop(block.tool_use_id, "unknown")
            text = _as_text(block.content)
            events.append(
                self._event(
                    EventType.TOOL_RESULT,
                    {
                        "tool_use_id": block.tool_use_id,
                        "tool": tool,
                        "is_error": bool(block.is_error),
                        "preview": _truncate(text),
                    },
                    agent,
                )
            )
            subagent_type = self._spawns.pop(block.tool_use_id, None)
            if subagent_type is not None:
                child = self.agents.resolve(block.tool_use_id)
                match = _AGENT_ID_RE.search(text)
                events.append(
                    self._event(
                        EventType.AGENT_DONE,
                        {
                            "tool_use_id": block.tool_use_id,
                            "subagent_type": subagent_type,
                            "agent_id": match.group(1) if match else child.id,
                            "summary": _truncate(text),
                        },
                        agent,
                    )
                )
        return events

    def _task_updated(self, message: TaskUpdatedMessage) -> list[Event]:
        if message.status in {"completed", "failed", "killed"}:
            self.background_tasks.discard(message.task_id)
        elif message.status in {"running", "pending", "paused"}:
            self.background_tasks.add(message.task_id)
        return [self._status(ThreadStatus.BUSY)]

    def _system(self, message: SystemMessage) -> list[Event]:
        if message.subtype != "background_tasks_changed":
            return []
        # Authoritative: the CLI sends the full list, so replace rather than
        # merge. An empty list is the only true "everything finished" signal.
        tasks = message.data.get("tasks") or []
        self.background_tasks = {
            str(t.get("task_id")) for t in tasks if isinstance(t, dict) and t.get("task_id")
        }
        return [self._status(ThreadStatus.BUSY)]

    def _result(self, message: ResultMessage) -> list[Event]:
        return [
            self._event(
                EventType.TURN_DONE,
                {
                    "subtype": message.subtype,
                    "usage": message.usage or {},
                    "cost_usd": message.total_cost_usd,
                    # F15: the CLI prices every turn against the Anthropic
                    # rate card, so a DeepSeek turn reported $0.149. Ship the
                    # raw token counts and let the client do its own maths.
                    "cost_trusted": self.settings.gateway_base_url is None,
                    "duration_ms": message.duration_ms,
                },
            )
        ]

    # -- helpers ---------------------------------------------------------

    def _status(self, status: ThreadStatus) -> Event:
        return self._event(
            EventType.THREAD_STATUS,
            {"status": str(status), "background_agents": len(self.background_tasks)},
        )

    def _event(self, kind: EventType, data: dict[str, Any], agent: Any = None) -> Event:
        return Event(
            type=kind,
            thread_id=self.thread_id,
            data=data,
            agent=agent if agent is not None else self.agents.resolve(None),
        )

    def _repo_hint(self, text: str) -> str | None:
        """Best-effort repo attribution for a spawn, purely for client grouping.

        D9 means the subagent type is always a built-in (`Explore`,
        `general-purpose`), so the repo has to be recovered from the prompt
        text. Longest path wins; no match means no hint.
        """
        best: str | None = None
        best_len = 0
        for repo in self.manifest.repos:
            needle = repo.path.rstrip("/")
            if (f"{needle}/" in text or f"`{needle}`" in text) and len(needle) > best_len:
                best, best_len = repo.name, len(needle)
        return best


def _as_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [str(b.get("text", "")) for b in content if isinstance(b, dict)]
    return "\n".join(p for p in parts if p)


def _truncate(text: str, limit: int = PREVIEW_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
