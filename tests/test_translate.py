from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from antares_agent.config import Settings
from antares_agent.events import EventType
from antares_agent.manifest import Manifest, Repo
from antares_agent.translate import Translator

MANIFEST = Manifest(
    root=Path("/ws"),
    scratch=".agent",
    default_profile="deep",
    repos=(
        Repo("api", "api", "backend"),
        Repo("web", "web", "frontend"),
    ),
    relations=(),
)


def make(gateway: str | None = None) -> Translator:
    return Translator("thr_1", MANIFEST, Settings(gateway_base_url=gateway))


def assistant(*blocks: Any, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="m", parent_tool_use_id=parent)


def kinds(events: list[Any]) -> list[str]:
    return [str(e.type) for e in events]


def test_text_and_tool_call() -> None:
    t = make()
    events = t.handle(
        assistant(
            TextBlock(text="先看一下接口定义"),
            ToolUseBlock(id="toolu_1", name="Read", input={"file_path": "api/routes.py"}),
        )
    )
    assert kinds(events) == [EventType.TEXT, EventType.TOOL_CALL]
    assert events[1].data["render"] == "none"
    assert events[1].data["sandboxed"] is True
    assert events[0].agent.id == "root"


def test_empty_text_blocks_are_dropped() -> None:
    assert make().handle(assistant(TextBlock(text="   \n"))) == []


def test_bash_escaping_the_sandbox_is_labelled() -> None:
    t = make()
    events = t.handle(
        assistant(
            ToolUseBlock(
                id="t", name="Bash", input={"command": "id", "dangerouslyDisableSandbox": True}
            )
        )
    )
    assert events[0].data["sandboxed"] is False
    assert events[0].data["render"] == "summary"


def test_spawn_gets_a_repo_hint_and_a_child_agent() -> None:
    t = make()
    events = t.handle(
        assistant(
            ToolUseBlock(
                id="toolu_a",
                name="Agent",
                input={"subagent_type": "Explore", "prompt": "Search the `api/` directory"},
            )
        )
    )
    assert kinds(events) == [EventType.TOOL_CALL, EventType.AGENT_SPAWN]
    spawn = events[1].data
    assert spawn["subagent_type"] == "Explore"
    assert spawn["repo_hint"] == "api"

    # events produced *inside* that subagent group under it
    inner = t.handle(assistant(TextBlock(text="found it"), parent="toolu_a"))
    assert inner[0].agent.parent_tool_use_id == "toolu_a"
    assert inner[0].agent.id == spawn["agent_id"]


def test_spawn_without_a_recognisable_repo_omits_the_hint() -> None:
    t = make()
    events = t.handle(
        assistant(
            ToolUseBlock(id="x", name="Agent", input={"subagent_type": "Explore", "prompt": "hi"})
        )
    )
    assert "repo_hint" not in events[1].data


def test_tool_result_recovers_the_tool_name_and_agent_done() -> None:
    t = make()
    t.handle(
        assistant(
            ToolUseBlock(
                id="toolu_a",
                name="Agent",
                input={"subagent_type": "Explore", "prompt": "look at web/"},
            )
        )
    )
    events = t.handle(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_a", content="调研完成\n\nagentId: agt_xyz", is_error=False
                )
            ]
        )
    )
    assert kinds(events) == [EventType.TOOL_RESULT, EventType.AGENT_DONE]
    assert events[0].data["tool"] == "Agent"
    assert events[1].data["agent_id"] == "agt_xyz"


def test_preview_is_truncated() -> None:
    t = make()
    t.handle(assistant(ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})))
    events = t.handle(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="x" * 2000)]))
    assert len(events[0].data["preview"]) == 501


# --- the F25 semantics ---------------------------------------------------


def _result(subtype: str = "success") -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason=None,
        total_cost_usd=0.149,
        usage={"input_tokens": 10, "output_tokens": 2},
    )


def _bg(*task_ids: str) -> SystemMessage:
    return SystemMessage(
        subtype="background_tasks_changed",
        data={"tasks": [{"task_id": tid, "task_type": "local_agent"} for tid in task_ids]},
    )


def test_result_message_alone_does_not_mean_idle() -> None:
    t = make()
    t.handle(_bg("task_1", "task_2"))
    assert t.idle_possible is False

    t.handle(_bg("task_2"))
    assert t.idle_possible is False

    events = t.handle(_bg())
    assert t.idle_possible is True
    assert events[0].data["background_agents"] == 0


def test_background_task_list_replaces_rather_than_merges() -> None:
    t = make()
    t.handle(_bg("a", "b"))
    t.handle(_bg("c"))
    assert t.background_tasks == {"c"}


def test_turn_done_carries_usage_and_distrusts_cost_behind_a_gateway() -> None:
    native = make().handle(_result())[0]
    assert native.data["cost_trusted"] is True

    behind = make(gateway="http://127.0.0.1:8080").handle(_result())[0]
    assert behind.data["cost_trusted"] is False
    assert behind.data["usage"] == {"input_tokens": 10, "output_tokens": 2}


def test_event_serialises_to_sse() -> None:
    t = make()
    event = t.handle(assistant(TextBlock(text="hi")))[0].with_id(7)
    frame = event.sse()
    assert frame["event"] == "text"
    assert frame["id"] == "7"
    payload = json.loads(frame["data"])
    assert payload["id"] == "evt_000007"
    assert payload["agent"]["id"] == "root"
    assert payload["content"] == "hi"
