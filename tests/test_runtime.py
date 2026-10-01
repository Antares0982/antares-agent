from types import SimpleNamespace

import pytest

from antares_agent.config import Settings
from antares_agent.manifest import empty
from antares_agent.profiles import Profile
from antares_agent.runtime import Runtime, prepare_home


async def test_forced_policy(tmp_path):
    settings = Settings(workspace=tmp_path, codex_home=tmp_path / "codex")
    runtime = Runtime(settings)
    assert runtime.handle_request("item/commandExecution/requestApproval", {}) == {
        "decision": "decline"
    }
    assert runtime.handle_request("item/fileChange/requestApproval", {}) == {"decision": "decline"}
    calls = []

    class Client:
        def thread_resume(self, session, options):
            return SimpleNamespace(
                thread=SimpleNamespace(id=session),
                model="previous-model",
                approvals_reviewer=SimpleNamespace(value="auto_review"),
            )

        def turn_start(self, session, inputs, options):
            calls.append(options)
            return SimpleNamespace(turn=SimpleNamespace(id="turn"))

    runtime.client = Client()
    session, model = await runtime.prepare(
        "thread", Profile("quick", model="fixture-model"), "auto", ""
    )
    assert (session, model) == ("thread", "fixture-model")
    for mode in ("plan", "auto"):
        await runtime.turn(
            "thread", "hello", mode, Profile("quick"), "fixture-model", empty(tmp_path)
        )
    assert all(
        c["approvalsReviewer"] == "auto_review" and c["approvalPolicy"] == "on-request"
        for c in calls
    )
    assert calls[0]["sandboxPolicy"]["type"] == "readOnly"
    assert calls[0]["collaborationMode"]["mode"] == "plan"
    assert calls[1]["sandboxPolicy"]["networkAccess"]
    assert calls[1]["model"] == "fixture-model"
    assert str(tmp_path / ".agents/skills") in calls[1]["sandboxPolicy"]["writableRoots"]


async def test_model_catalog(tmp_path):
    runtime = Runtime(Settings(workspace=tmp_path))
    calls = []

    class Client:
        def request(self, method, params, response_model):
            calls.append(params)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        model="model-b" if params["cursor"] else "model-a",
                        display_name="Model",
                        is_default=not params["cursor"],
                        hidden=False,
                    ),
                    SimpleNamespace(hidden=True),
                ],
                next_cursor=None if params["cursor"] else "page-2",
            )

    runtime.client = Client()
    assert [m["id"] for m in await runtime.models()] == ["model-a", "model-b"]
    assert calls == [
        {"includeHidden": False, "cursor": None},
        {"includeHidden": False, "cursor": "page-2"},
    ]


def test_resource_layout(tmp_path):
    settings = Settings(codex_home=tmp_path / "private", resources_dir=tmp_path / "resources")
    skill = settings.codex_home / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill")
    prepare_home(settings)
    prepare_home(settings)
    assert (settings.codex_home / "skills").is_symlink()
    assert (settings.resources_dir / "skills/example/SKILL.md").read_text() == "skill"
    (settings.codex_home / "skills").unlink()
    (settings.codex_home / "skills").mkdir()
    with pytest.raises(ValueError, match="冲突"):
        prepare_home(settings)
