from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import CommandExecResponse, SkillsListResponse

from .runtime import prepare_home, runtime_config


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class PreflightReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_requirements(path=Path("/etc/codex/requirements.toml")):
    rules = tomllib.loads(path.read_text())
    if rules.get("allowed_approval_policies") != ["on-request"]:
        raise ValueError("必须强制 on-request 审批策略")
    if rules.get("allowed_approvals_reviewers") != ["auto_review"]:
        raise ValueError("必须强制 auto_review 审查器")
    if set(rules.get("allowed_sandbox_modes", [])) != {"read-only", "workspace-write"}:
        raise ValueError("必须限制可用沙箱模式")
    for path in rules.get("permissions", {}).get("filesystem", {}).get("deny_read", []):
        if any(char in path for char in "*?[") or (Path(path).exists() and not Path(path).is_dir()):
            raise ValueError("deny-read 必须使用目录路径")


def check_sandbox(settings):
    prepare_home(settings)
    if settings.auth_socket:
        check_requirements()
    binary = str(settings.cli_path or bundled_codex_path())
    with ExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(dir=settings.workspace))
        root = Path(directory)
        skills = root / ".agents" / "skills"
        skills.mkdir(parents=True)
        policy = {
            "type": "workspaceWrite",
            "networkAccess": True,
            "writableRoots": [directory, str(skills)],
        }
        denied = [settings.db_path, settings.profiles_dir, settings.codex_home / "auth.json"]
        if settings.auth_socket:
            if not settings.auth_socket.is_socket():
                raise ValueError("认证 socket 尚未就绪")
            denied.append(settings.auth_socket)
            for parent in (settings.db_path.parent, settings.codex_home):
                parent.mkdir(parents=True, exist_ok=True)
                canary = stack.enter_context(
                    tempfile.NamedTemporaryFile(dir=parent, prefix=".preflight-")
                )
                canary.write(b"sandbox probe")
                canary.flush()
                denied.append(Path(canary.name))
        probe = """import json, os, socket, sys
from pathlib import Path
Path('probe').write_text('ok')
Path('.agents/skills/probe').write_text('ok')
for path in json.loads(sys.argv[2]):
    assert Path(path).read_bytes(), path
if sys.argv[1]:
    for path in sys.argv[3:]:
        assert not os.access(path, os.R_OK), path
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        try:
            connection.connect(sys.argv[1])
        except OSError:
            pass
        else:
            raise RuntimeError('认证 socket 未隔离')
"""
        readable = []
        config = runtime_config(settings)
        with CodexClient(
            config=config, approval_handler=lambda m, p: {"decision": "decline"}
        ) as client:
            client.initialize()
            started = client.thread_start(
                {
                    "cwd": directory,
                    "ephemeral": True,
                    "sandbox": "workspace-write",
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "auto_review",
                }
            )
            if started.approvals_reviewer.value != "auto_review":
                raise ValueError("自动审批未生效")
            if settings.resources_dir:
                listed = client.request(
                    "skills/list",
                    {"cwds": [directory], "forceReload": True},
                    response_model=SkillsListResponse,
                ).model_dump(by_alias=True, mode="json")
                readable = [
                    skill["path"]
                    for entry in listed["data"]
                    for skill in entry["skills"]
                    if skill["name"] == "imagegen"
                ]
                if not readable or any(
                    not Path(path).is_relative_to(settings.resources_dir) for path in readable
                ):
                    raise ValueError("原生技能没有使用独立资源目录")
                resource = stack.enter_context(
                    tempfile.NamedTemporaryFile(dir=settings.resources_dir / "plugins/cache")
                )
                resource.write(b"resource probe")
                resource.flush()
                readable.append(resource.name)
            command = [
                sys.executable,
                "-c",
                probe,
                str(settings.auth_socket or ""),
                json.dumps(readable),
                *map(str, denied),
            ]
            for mode in ("workspaceWrite", "readOnly"):
                result = client.request(
                    "command/exec",
                    {
                        "command": command,
                        "cwd": directory,
                        "sandboxPolicy": policy
                        if mode == "workspaceWrite"
                        else {"type": "readOnly"},
                        "timeoutMs": 10000,
                    },
                    response_model=CommandExecResponse,
                )
                if (mode == "workspaceWrite") != (result.exit_code == 0):
                    raise ValueError(f"{mode} 沙箱自检失败：{result.stderr}")
        subprocess.run(
            [
                binary,
                *[arg for value in config.config_overrides for arg in ("-c", value)],
                "-c",
                "sandbox_workspace_write.writable_roots=" + json.dumps([str(skills)]),
                "sandbox",
                "--",
                *command,
            ],
            cwd=root,
            env={**os.environ, "CODEX_HOME": str(settings.codex_home)},
            check=True,
            capture_output=True,
            timeout=20,
        )


def run(settings):
    if not settings.workspace.is_dir():
        raise SandboxUnavailable(f"工作区不存在：{settings.workspace}")
    if not settings.require_sandbox:
        return PreflightReport(True, notes=["开发模式：未运行沙箱自检"])
    try:
        check_sandbox(settings)
    except Exception as exc:
        raise SandboxUnavailable("Codex 沙箱自检失败，拒绝启动") from exc
    return PreflightReport(True, notes=["Codex 沙箱与自动审批配置自检通过"])
