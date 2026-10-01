from types import SimpleNamespace

from antares_agent.translate import Translator


def event(method, **payload):
    return SimpleNamespace(method=method, payload=payload)


def test_codex_translation():
    translator = Translator("thr_1")
    item = {"type": "agentMessage", "id": "a", "text": "完成"}
    assert not translator.handle(event("item/started", item=item))
    message = event("item/completed", item=item)
    assert translator.handle(message)[0].data == {"content": "完成", "delta": False}
    assert not translator.handle(message)
    call = {"type": "commandExecution", "id": "b", "command": "ls", "status": "inProgress"}
    assert translator.handle(event("item/started", item=call))[0].data["input"]["command"] == "ls"
    call.update(status="failed", exitCode=1, aggregatedOutput="failed")
    assert translator.handle(event("item/completed", item=call))[0].data["is_error"]
    done = translator.handle(
        event("turn/completed", turn={"status": "failed", "error": {"message": "失败"}})
    )
    assert [str(e.type) for e in done] == ["error", "turn.done"]
    assert "cost_usd" not in done[-1].data


def test_child_completion():
    translator = Translator("thr_1")
    item = {
        "type": "collabAgentToolCall",
        "id": "spawn",
        "tool": "spawnAgent",
        "agentsStates": {"child": {"status": "running"}},
        "prompt": "调研",
    }
    assert translator.handle(event("item/completed", item=item))[-1].type == "agent.spawn"
    item = {
        **item,
        "id": "wait",
        "agentsStates": {"child": {"status": "completed", "message": "完成"}},
    }
    assert translator.handle(event("item/completed", item=item))[-1].type == "agent.done"
    assert not translator.background_tasks
