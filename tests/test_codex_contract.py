import base64
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace

import pytest
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import (
    CommandExecResponse,
    SkillsExtraRootsSetResponse,
    SkillsListResponse,
)

from antares_agent.config import Settings
from antares_agent.preflight import check_sandbox
from antares_agent.runtime import runtime_config

pytestmark = pytest.mark.skipif(os.getenv("ANTARES_CONTRACT") != "1", reason="real CLI opt-in")


def test_runtime_contract(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "codex"
    home.mkdir()
    settings = Settings(workspace=work, codex_home=home, db_path=tmp_path / "db")
    check_sandbox(settings)
    claims = {
        "exp": 4102444800,
        "email": "fixture@example.invalid",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "fixture",
            "chatgpt_plan_type": "plus",
        },
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = "eyJhbGciOiJub25lIn0." + body + ".fixture"
    legacy = json.dumps(
        {
            "tokens": {
                "id_token": token,
                "access_token": token,
                "refresh_token": "fixture",
                "account_id": "fixture",
            }
        }
    )
    auth = home / "auth.json"
    auth.write_text(legacy)
    config = runtime_config(replace(settings, auth_socket=tmp_path / "unused.sock"))
    with CodexClient(config, lambda m, p: {"decision": "decline"}) as client:
        client.initialize()
        assert client.account_read().account is None
        client.account_login_start(
            {"type": "chatgptAuthTokens", "accessToken": token, "chatgptAccountId": "fixture"}
        )
        assert auth.read_text() == legacy
        models = client.model_list().data
        assert len(models) >= 2
        options = {
            "cwd": str(work),
            "model": models[0].model,
            "config": {
                "model_provider": "fixture",
                "model_providers.fixture": {
                    "name": "fixture",
                    "base_url": "http://127.0.0.1:1",
                    "wire_api": "responses",
                    "request_max_retries": 0,
                    "stream_max_retries": 0,
                },
            },
        }
        started = client.thread_start(options)
        turn = client.turn_start(started.thread.id, [{"type": "text", "text": "fixture"}])
        while client.next_turn_notification(turn.turn.id).method != "turn/completed":
            pass
        resumed = client.thread_resume(started.thread.id, {**options, "model": models[1].model})
        assert resumed.thread.id == started.thread.id
        turn = client.turn_start(
            resumed.thread.id,
            [{"type": "text", "text": "fixture switch"}],
            {"model": models[1].model},
        )
        while client.next_turn_notification(turn.turn.id).method != "turn/completed":
            pass
        resumed = client.thread_resume(started.thread.id, options)
        assert resumed.model == models[1].model
        skills = work / ".agents/skills"
        skills.mkdir(parents=True)
        client.request(
            "skills/extraRoots/set",
            {"extraRoots": [str(skills)]},
            response_model=SkillsExtraRootsSetResponse,
        )
        result = client.request(
            "command/exec",
            {
                "cwd": str(work),
                "command": [
                    "/bin/sh",
                    "-ec",
                    "mkdir -p .agents/skills/probe; "
                    "printf -- '---\nname: probe\ndescription: fixture\n---\nfixture\n' "
                    "> .agents/skills/probe/SKILL.md",
                ],
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(work), str(skills)],
                    "networkAccess": True,
                },
                "timeoutMs": 10000,
            },
            response_model=CommandExecResponse,
        )
        assert result.exit_code == 0, result.stderr
        found = client.request(
            "skills/list",
            {"cwds": [str(work)], "forceReload": True},
            response_model=SkillsListResponse,
        )
        assert any(skill.name == "probe" for entry in found.data for skill in entry.skills)


def test_managed_sandbox(tmp_path):
    runtime_tmp = tmp_path / "runtime-tmp"
    runtime_tmp.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "codex"
    home.mkdir()
    helpers = home / "tmp" / "arg0"
    helpers.mkdir(parents=True)
    resources = tmp_path / "resources"
    state = tmp_path / "state"
    state.mkdir()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    auth = auth_dir / "auth.sock"
    rules = tmp_path / "requirements.toml"
    rules.write_text(
        'allowed_approval_policies = ["on-request"]\n'
        'allowed_approvals_reviewers = ["auto_review"]\n'
        'allowed_sandbox_modes = ["read-only", "workspace-write"]\n'
        "[permissions.filesystem]\ndeny_read = "
        + json.dumps([str(state), str(home), str(auth_dir)])
        + "\n"
    )
    with socket.socket(socket.AF_UNIX) as broker:
        broker.bind(str(auth))
        broker.listen(1)
        result = subprocess.run(
            [
                "bwrap",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/etc",
                "--dir",
                "/etc/codex",
                "--ro-bind",
                str(rules),
                "/etc/codex/requirements.toml",
                "--bind",
                str(tmp_path),
                str(tmp_path),
                "--ro-bind",
                str(helpers),
                str(helpers),
                "--",
                sys.executable,
                "-c",
                "from antares_agent.config import Settings; "
                "from antares_agent.preflight import check_sandbox; "
                "check_sandbox(Settings.from_env())",
            ],
            env={
                **os.environ,
                "TMPDIR": str(runtime_tmp),
                "ANTARES_WORKSPACE": str(work),
                "ANTARES_CODEX_HOME": str(home),
                "ANTARES_DB_PATH": str(state / "db"),
                "ANTARES_PROFILES_DIR": str(state / "profiles"),
                "ANTARES_AUTH_SOCKET": str(auth),
                "ANTARES_RESOURCES_DIR": str(resources),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
