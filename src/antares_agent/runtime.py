from __future__ import annotations

import asyncio
import json
import logging
import shutil

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    SkillsExtraRootsSetResponse,
    SkillsListResponse,
    ThreadUnsubscribeResponse,
)

from .auth import TokenClient
from .config import Settings

log = logging.getLogger(__name__)


def prepare_home(settings):
    settings.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if settings.resources_dir is None:
        return
    for relative in ("skills", "plugins/cache"):
        source = settings.codex_home / relative
        target = settings.resources_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.is_symlink():
            if source.resolve() != target.resolve():
                raise ValueError(f"资源链接冲突：{source}")
            continue
        if source.exists():
            if target.exists():
                raise ValueError(f"资源目录冲突：{source} → {target}")
            shutil.move(str(source), str(target))
        else:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(target, target_is_directory=True)


def runtime_config(settings: Settings) -> CodexConfig:
    overrides = (
        'approval_policy="on-request"',
        'approvals_reviewer="auto_review"',
        'sandbox_mode="workspace-write"',
        "sandbox_workspace_write.network_access=true",
        "features.shell_snapshot=false",
        "tools.experimental_request_user_input.enabled=false",
        f'projects.{json.dumps(str(settings.workspace))}.trust_level="trusted"',
    )
    if settings.auth_socket:
        overrides += ('cli_auth_credentials_store="ephemeral"', 'forced_login_method="chatgpt"')
    return CodexConfig(
        codex_bin=str(settings.cli_path) if settings.cli_path else None,
        cwd=str(settings.workspace),
        env={"CODEX_HOME": str(settings.codex_home)},
        config_overrides=overrides,
        client_name="antares_agent",
    )


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: CodexClient | None = None
        self.tokens = TokenClient(settings.auth_socket) if settings.auth_socket else None
        self.slots = asyncio.Semaphore(settings.max_live_threads)
        self.lock = asyncio.Lock()
        self.notifications: asyncio.Task | None = None
        self.usage: dict[str, dict] = {}
        self.default_model: str | None = None
        self.skill_roots: list[str] = []

    def handle_request(self, method, params):
        if method == "account/chatgptAuthTokens/refresh" and self.tokens:
            return self.tokens.fetch(True, (params or {}).get("previousAccountId"))
        log.error("Unexpected Codex request: %s", method)
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return {"decision": "decline"}

    async def start(self):
        async with self.lock:
            if self.client is not None:
                return self.client
            prepare_home(self.settings)
            client = CodexClient(runtime_config(self.settings), self.handle_request)
            try:
                await asyncio.to_thread(client.start)
                await asyncio.to_thread(client.initialize)
                if self.tokens:
                    tokens = await asyncio.to_thread(self.tokens.fetch)
                    await asyncio.to_thread(
                        client.account_login_start, {"type": "chatgptAuthTokens", **tokens}
                    )
            except BaseException:
                await asyncio.to_thread(client.close)
                raise
            self.client = client
            self.notifications = asyncio.create_task(self._watch(client))
            return client

    async def _watch(self, client):
        try:
            while True:
                event = await asyncio.to_thread(client.next_notification)
                if event.method == "thread/tokenUsage/updated":
                    value = event.payload.model_dump(by_alias=True, mode="json")
                    self.usage[value["threadId"]] = value["tokenUsage"]["last"]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Codex notification stream closed")

    async def skills(self, manifest):
        client = await self.start()
        roots = self.skill_paths(manifest)
        self.skill_roots = [str(path) for path in roots if path.is_dir()]
        await asyncio.to_thread(
            client.request,
            "skills/extraRoots/set",
            {"extraRoots": self.skill_roots},
            response_model=SkillsExtraRootsSetResponse,
        )
        await asyncio.to_thread(
            client.request,
            "skills/list",
            {"cwds": [str(self.settings.workspace)], "forceReload": True},
            response_model=SkillsListResponse,
        )

    def skill_paths(self, manifest):
        roots = [self.settings.workspace / ".agents" / "skills"]
        roots += [r.abspath(manifest.root) / ".agents" / "skills" for r in manifest.repos]
        for path in roots:
            if not path.resolve().is_relative_to(self.settings.workspace.resolve()):
                raise ValueError(f"技能路径超出工作区：{path}")
        return list(dict.fromkeys(roots))

    def sandbox(self, mode, manifest):
        if mode == "plan":
            return {"type": "readOnly"}
        roots = self.skill_paths(manifest)
        return {
            "type": "workspaceWrite",
            "networkAccess": True,
            "writableRoots": [str(self.settings.workspace), *map(str, roots)],
        }

    async def prepare(self, session, profile, mode, instructions):
        client = await self.start()
        options = {
            "cwd": str(self.settings.workspace),
            "sandbox": "read-only" if mode == "plan" else "workspace-write",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "developerInstructions": instructions,
        }
        if profile.model:
            options["model"] = profile.model
        if session:
            result = await asyncio.to_thread(client.thread_resume, session, options)
        else:
            result = await asyncio.to_thread(client.thread_start, options)
        if result.approvals_reviewer.value != "auto_review":
            raise RuntimeError("Codex 自动审批未生效")
        self.default_model = result.model
        return result.thread.id, result.model

    async def turn(self, session, inputs, mode, profile, model, manifest):
        client = await self.start()
        self.usage.pop(session, None)
        options = {
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandboxPolicy": self.sandbox(mode, manifest),
            "collaborationMode": {
                "mode": "plan" if mode == "plan" else "default",
                "settings": {
                    "model": model,
                    "reasoning_effort": profile.effort,
                    "developer_instructions": None,
                },
            },
        }
        return await asyncio.to_thread(client.turn_start, session, inputs, options)

    async def release(self, session):
        if self.client is not None and session:
            await asyncio.to_thread(
                self.client.request,
                "thread/unsubscribe",
                {"threadId": session},
                response_model=ThreadUnsubscribeResponse,
            )
            self.usage.pop(session, None)

    async def close(self):
        async with self.lock:
            if self.client is not None:
                await asyncio.to_thread(self.client.close)
                self.client = None
            if self.notifications is not None:
                self.notifications.cancel()
                await asyncio.gather(self.notifications, return_exceptions=True)
                self.notifications = None
