"""Permission arbitration.

Three tiers, in the order the CLI evaluates them:

1. **deny rules** (`disallowed_tools`) -- shapes that are never worth asking
   about. These run *before* our callback, and F24 showed the CLI decomposes
   shell structure when matching them, so they are not bypassed by `;` or
   `bash -c`.
2. **sandbox auto-allow** -- a command confined by bubblewrap + seccomp cannot
   do damage regardless of what the model intended, so it runs silently. The
   event is still emitted; the client folds it.
3. **ask** -- anything that escapes the sandbox, plus a small set of shapes
   that are destructive but legitimate often enough that denying them outright
   would be worse than a prompt.

Two constraints fall out of the spikes and are easy to violate by accident:

- `allowed_tools` must stay **empty** (F8). Allow rules are evaluated ahead of
  `can_use_tool` and silently short-circuit it, which disables tiers 2 and 3.
- `can_use_tool` must always be supplied (F16). Without it the CLI falls back
  to a non-interactive path that rejects writes inside cwd with a misleading
  message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    SandboxSettings,
    ToolPermissionContext,
)

from .config import Settings
from .shlex_split import matches, split_commands

log = logging.getLogger(__name__)

#: Never allowed, at any tier. Kept deliberately short: every entry here is a
#: shape with no legitimate use from an unattended agent.
HARD_DENY: tuple[str, ...] = (
    "Bash(git push --force:*)",
    "Bash(git push -f:*)",
    "Bash(git remote set-url:*)",
    "Bash(git remote remove:*)",
    "Bash(chmod -R:*)",
    "Bash(chown -R:*)",
    "Bash(sudo:*)",
    "Bash(doas:*)",
    "Bash(mkfs:*)",
    "Bash(dd:*)",
    "Bash(shutdown:*)",
    "Bash(reboot:*)",
)

#: Destructive but sometimes right. Escalated to the user rather than refused.
#: Matched against every decomposed command, not just the first.
ASK_SHAPES: tuple[tuple[str, ...], ...] = (
    ("git", "push"),
    ("git", "reset", "--hard"),
    ("git", "clean", "-f"),
    ("git", "commit", "--amend"),
    ("git", "rebase"),
    ("git", "checkout", "--"),
    ("git", "branch", "-D"),
    ("git", "tag", "-d"),
    ("git", "stash", "drop"),
    ("git", "stash", "clear"),
    ("gh", "pr", "merge"),
    ("gh", "release"),
    ("npm", "publish"),
    ("cargo", "publish"),
    ("uv", "publish"),
    ("twine", "upload"),
    ("systemctl",),
    ("crontab",),
)


class Tier(StrEnum):
    SANDBOXED = "sandboxed"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    tier: Tier
    reason: str = ""


#: The model can request an unconfined Bash call by setting this on the tool
#: input; that request is exactly what tier 3 exists to catch.
ESCAPE_FLAG = "dangerouslyDisableSandbox"

#: Pseudo-tool the CLI raises when sandboxed code tries to reach the network.
NETWORK_TOOL = "SandboxNetworkAccess"

#: Tools that only ever touch the workspace and are covered by the sandbox.
_ALWAYS_SANDBOXED = frozenset({"Read", "Glob", "Grep", "TodoWrite", "Agent", "Task"})


def will_sandbox(tool: str, tool_input: dict[str, Any]) -> bool:
    """Whether this call runs inside the sandbox.

    A pure function of the request, so the event translator can label
    `tool.call` without waiting for the permission round-trip.
    """
    if tool == NETWORK_TOOL:
        return False
    if tool in _ALWAYS_SANDBOXED:
        return True
    if tool == "Bash":
        return not tool_input.get(ESCAPE_FLAG, False)
    return True


def secret_path_hit(text: str, secret_paths: tuple[str, ...]) -> str | None:
    """Does a command or path reference a credential store?

    F19: the sandbox blocks writes outside cwd but not reads, so `cat
    ~/.ssh/id_rsa` succeeds silently with no approval. Deny rules cover the
    file tools; arbitrary shell needs this substring pass.
    """
    home = str(Path.home())
    haystack = text.replace("$HOME", home).replace("${HOME}", home)
    for raw in secret_paths:
        expanded = str(Path(raw).expanduser())
        if expanded in haystack or raw in haystack:
            return raw
    return None


def classify(tool: str, tool_input: dict[str, Any], settings: Settings) -> Decision:
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        leaked = secret_path_hit(command, settings.secret_paths)
        if leaked:
            return Decision(Tier.DENY, f"命令引用了受保护的凭据路径 {leaked}")
        for argv in split_commands(command):
            for shape in ASK_SHAPES:
                if matches(argv, list(shape)):
                    return Decision(Tier.ASK, f"命令包含需确认的操作：{' '.join(shape)}")
        if tool_input.get(ESCAPE_FLAG):
            return Decision(Tier.ASK, "命令要求在沙箱之外运行")
        return Decision(Tier.SANDBOXED)

    if tool == NETWORK_TOOL:
        target = tool_input.get("host") or tool_input.get("url") or "未知目标"
        return Decision(Tier.ASK, f"需要访问未授权的网络目标 {target}")

    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            leaked = secret_path_hit(value, settings.secret_paths)
            if leaked:
                return Decision(Tier.DENY, f"路径位于受保护的凭据目录 {leaked}")

    if will_sandbox(tool, tool_input):
        return Decision(Tier.SANDBOXED)
    return Decision(Tier.ASK, f"{tool} 在沙箱之外执行")


#: Called by the arbiter when a decision needs a human. Returns True to allow.
AskFn = Callable[[str, dict[str, Any], str, ToolPermissionContext], Awaitable["Verdict"]]


@dataclass(frozen=True)
class Verdict:
    allow: bool
    message: str = ""


class Arbiter:
    """Wraps `classify` into the SDK's `can_use_tool` signature."""

    def __init__(self, settings: Settings, ask: AskFn) -> None:
        self._settings = settings
        self._ask = ask

    async def __call__(
        self, tool: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        decision = classify(tool, tool_input, self._settings)

        if decision.tier is Tier.SANDBOXED:
            return PermissionResultAllow()
        if decision.tier is Tier.DENY:
            log.warning("denied %s: %s", tool, decision.reason)
            return PermissionResultDeny(message=decision.reason)

        verdict = await self._ask(tool, tool_input, decision.reason, context)
        if verdict.allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=verdict.message or "用户拒绝了该操作")


def denied_tools(settings: Settings) -> list[str]:
    """The `disallowed_tools` list. Also denies file tools on secret paths."""
    rules = list(HARD_DENY)
    for raw in settings.secret_paths:
        expanded = Path(raw).expanduser()
        for tool in ("Read", "Edit", "Write", "NotebookEdit"):
            rules.append(f"{tool}(//{str(expanded).lstrip('/')}/**)")
            rules.append(f"{tool}(//{str(expanded).lstrip('/')})")
    rules.extend(settings.extra_denied_tools)
    return rules


def sandbox_settings() -> SandboxSettings:
    """Sandbox config. Network is closed by default; the gateway is reachable
    only by the CLI's own process, not by sandboxed Bash (F22)."""
    return SandboxSettings(
        enabled=True,
        # Tier 2: a confined command does not need a prompt (D3).
        autoAllowBashIfSandboxed=True,
        # The model may still ask to escape; that request goes to tier 3.
        allowUnsandboxedCommands=True,
        network={"allowUnixSockets": [], "allowAllUnixSockets": False,
                 "allowLocalBinding": False},
    )
