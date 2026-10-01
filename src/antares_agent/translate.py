from __future__ import annotations

import json
import time

from .events import Event, EventType


class Translator:
    def __init__(self, thread_id, manifest=None, settings=None):
        self.thread_id = thread_id
        self.background_tasks: set[str] = set()
        self.reset()

    def reset(self):
        self.seen: set[tuple[str, str]] = set()
        self.background_tasks.clear()
        self.usage = {}
        self.started = time.monotonic()

    def event(self, kind, data):
        return Event(kind, self.thread_id, data)

    def handle(self, notification):
        method = notification.method
        payload = notification.payload
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(by_alias=True, mode="json")
        elif hasattr(payload, "params"):
            payload = payload.params
        if method == "thread/tokenUsage/updated":
            self.usage = payload["tokenUsage"]["last"]
            return []
        if method == "error":
            if payload.get("willRetry"):
                return []
            return [
                self.event(
                    EventType.ERROR, {"code": "codex_error", "message": payload["error"]["message"]}
                )
            ]
        if method == "turn/completed":
            turn = payload["turn"]
            events = []
            if turn.get("error"):
                events.append(
                    self.event(
                        EventType.ERROR,
                        {"code": "codex_error", "message": turn["error"]["message"]},
                    )
                )
            events.append(
                self.event(
                    EventType.TURN_DONE,
                    {
                        "subtype": turn["status"],
                        "usage": self.usage,
                        "duration_ms": int((time.monotonic() - self.started) * 1000),
                    },
                )
            )
            return events
        if method not in {"item/started", "item/completed"}:
            return []
        item = payload["item"]
        key = (method, item["id"])
        if key in self.seen:
            return []
        self.seen.add(key)
        done = method == "item/completed"
        kind = item["type"]
        if kind in {"agentMessage", "plan"}:
            text = item.get("text", "")
            return (
                [self.event(EventType.TEXT, {"content": text, "delta": False})]
                if done and text.strip()
                else []
            )
        tools = {
            "commandExecution": "commandExecution",
            "fileChange": "fileChange",
            "mcpToolCall": "mcpToolCall",
            "collabAgentToolCall": "collabAgentToolCall",
            "webSearch": "webSearch",
            "imageView": "imageView",
            "imageGeneration": "imageGeneration",
            "dynamicToolCall": "dynamicToolCall",
        }
        if kind not in tools:
            return []
        events = []
        if done:
            preview = item.get("aggregatedOutput") or item.get("result") or ""
            if not isinstance(preview, str):
                preview = json.dumps(preview, ensure_ascii=False)
            events.append(
                self.event(
                    EventType.TOOL_RESULT,
                    {
                        "tool_use_id": item["id"],
                        "tool": kind,
                        "is_error": item.get("status") in {"failed", "declined"}
                        or bool(item.get("exitCode")),
                        "preview": preview[:500],
                    },
                )
            )
        else:
            details = {
                key: item[key]
                for key in ("command", "cwd", "changes", "server", "tool", "arguments", "prompt")
                if key in item
            }
            events.append(
                self.event(
                    EventType.TOOL_CALL,
                    {
                        "tool_use_id": item["id"],
                        "tool": kind,
                        "input": details,
                        "render": "diff" if kind == "fileChange" else "summary",
                    },
                )
            )
        if kind == "collabAgentToolCall" and done:
            for agent, state in item.get("agentsStates", {}).items():
                if state.get("status") in {"completed", "errored", "shutdown", "notFound"}:
                    if agent in self.background_tasks:
                        self.background_tasks.discard(agent)
                        events.append(
                            self.event(
                                EventType.AGENT_DONE,
                                {
                                    "agent_id": agent,
                                    "tool_use_id": item["id"],
                                    "summary": state.get("message", ""),
                                },
                            )
                        )
                elif agent not in self.background_tasks:
                    self.background_tasks.add(agent)
                    events.append(
                        self.event(
                            EventType.AGENT_SPAWN,
                            {
                                "agent_id": agent,
                                "tool_use_id": item["id"],
                                "subagent_type": "codex",
                                "task": item.get("prompt", ""),
                            },
                        )
                    )
        return events
