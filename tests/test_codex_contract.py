import base64
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace

import pytest
from openai_codex.client import CodexClient
from openai_codex.errors import InvalidRequestError
from openai_codex.generated.v2_all import (
    CommandExecResponse,
    SkillsExtraRootsSetResponse,
    SkillsListResponse,
)

from antares_agent.config import Settings
from antares_agent.preflight import check_sandbox
from antares_agent.runtime import Runtime, runtime_config

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
        with pytest.raises(InvalidRequestError, match="no active turn to steer"):
            client.turn_steer(started.thread.id, "finished-turn", "follow-up")
        turn = client.turn_start(started.thread.id, [{"type": "text", "text": "fixture"}])
        while client.next_turn_notification(turn.turn.id).method != "turn/completed":
            pass
        with pytest.raises(InvalidRequestError, match="no active turn to steer"):
            client.turn_steer(started.thread.id, turn.turn.id, "follow-up")
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


async def test_delete_contract(tmp_path):
    from openai_codex.errors import InvalidRequestError
    from openai_codex.generated.v2_all import ThreadReadResponse

    work = tmp_path / "work"
    work.mkdir()
    runtime = Runtime(Settings(workspace=work, codex_home=tmp_path / "codex"))
    client = await runtime.start()
    try:
        started = client.thread_start({"cwd": str(work), "persistExtendedHistory": True})
        session = started.thread.id
        await runtime.delete_thread(session)
        with pytest.raises(InvalidRequestError):
            client.request("thread/read", {"threadId": session}, response_model=ThreadReadResponse)
        await runtime.delete_thread(session)
        assert work.is_dir()
    finally:
        await runtime.close()


async def test_initial_prompt_contract(tmp_path):
    import asyncio
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from antares_agent.manifest import empty

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            events = [
                {"type": "response.created", "response": {"id": "probe"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "id": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "fixture response"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "probe",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                },
            ]
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    settings = Settings(workspace=tmp_path, codex_home=tmp_path / "codex")
    settings.codex_home.mkdir()
    config = runtime_config(settings)
    config = replace(
        config,
        config_overrides=(
            *config.config_overrides,
            'model_provider="fixture"',
            'model_providers.fixture.name="Local probe"',
            f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}"',
            'model_providers.fixture.wire_api="responses"',
            "model_providers.fixture.requires_openai_auth=false",
            "model_providers.fixture.supports_websockets=false",
            "model_providers.fixture.request_max_retries=0",
            "model_providers.fixture.stream_max_retries=0",
        ),
    )
    runtime = Runtime(settings)
    try:
        runtime.client = CodexClient(config, runtime.handle_request)
        await asyncio.to_thread(runtime.client.start)
        await asyncio.to_thread(runtime.client.initialize)
        models = await runtime.models()
        info = next(m for m in models if m["is_default"])
        session = None
        for index in range(3):
            if index == 2:
                await runtime.close()
                runtime.client = CodexClient(config, runtime.handle_request)
                await asyncio.to_thread(runtime.client.start)
                await asyncio.to_thread(runtime.client.initialize)
            session, model = await runtime.prepare(session, info["id"], "auto", "INITIAL_PROMPT")
            turn = await runtime.turn(session, "hello", "auto", None, model, empty(tmp_path))
            while True:
                notification = await asyncio.to_thread(
                    runtime.client.next_turn_notification, turn.turn.id
                )
                if notification.method == "turn/completed":
                    assert notification.payload.turn.status.value == "completed"
                    break
            runtime.client.unregister_turn_notifications(turn.turn.id)
            if index == 1:
                await asyncio.to_thread(runtime.client.thread_compact, session)
                async with asyncio.timeout(20):
                    while True:
                        read = await asyncio.to_thread(runtime.client.thread_read, session, True)
                        latest = read.thread.turns[-1]
                        if latest.id != turn.turn.id and latest.status.value == "completed":
                            break
                        await asyncio.sleep(0.05)
            await runtime.release(session)
        assert len(requests) == 4
        for request in requests:
            text = json.dumps(request["input"])
            assert text.count("INITIAL_PROMPT") == 1, [
                json.dumps(r["input"]).count("INITIAL_PROMPT") for r in requests
            ]
    finally:
        await runtime.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        server_thread.join()
