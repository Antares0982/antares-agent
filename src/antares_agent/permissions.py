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
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
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
from .shlex_split import matches, split_commands, tokenize

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

#: Tools that put bytes on disk at a path the model chooses.
_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

#: Settings files whose `permissions.allow` entries are read back by the CLI.
#: An allow rule is evaluated *ahead* of `can_use_tool` (F8), so a thread that
#: can write one of these can grant itself everything tiers 2 and 3 exist to
#: withhold -- including unsandboxed Bash. The sandbox does not stop it: these
#: live inside cwd, which is the one place writes are permitted.
_SETTINGS_NAMES = frozenset({"settings.json", "settings.local.json"})


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


def _normalisations(token: str, cwd: Path | None) -> list[Path]:
    """Every form a path token might really denote.

    Both `..` and symlinks have to collapse before comparison: F28 showed the
    CLI's own Bash path analysis misses `<dir>/../.ssh/id_rsa`, and a symlink
    planted inside cwd would defeat a purely lexical check.
    """
    path = Path(os.path.expandvars(token)).expanduser()
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    forms = [Path(os.path.normpath(str(path)))]
    with suppress(OSError, RuntimeError):  # broken symlink loop, permission denied
        forms.append(path.resolve())
    return forms


def _path_tokens(text: str) -> list[str]:
    try:
        tokens = tokenize(text)
    except ValueError:
        tokens = text.split()
    out: list[str] = []
    for token in tokens:
        # `--config=/path`, `-o/path`
        for part in (token, token.partition("=")[2]):
            if part and ("/" in part or part.startswith("~")):
                out.append(part.lstrip("-"))
    return out


def secret_path_hit(
    text: str, secret_paths: tuple[str, ...], cwd: Path | None = None
) -> str | None:
    """Does a command or path reference a credential store?

    F19: the sandbox blocks writes outside cwd but not reads, so `cat
    ~/.ssh/id_rsa` succeeds silently with no approval. Deny rules cover the
    file tools and, per F27, most Bash spellings too -- but not all of them,
    so this pass has to stand on its own rather than assume the CLI got there
    first.
    """
    roots: list[tuple[str, list[Path]]] = []
    for raw in secret_paths:
        expanded = Path(raw).expanduser()
        forms = [Path(os.path.normpath(str(expanded)))]
        with suppress(OSError, RuntimeError):
            forms.append(expanded.resolve())
        roots.append((raw, forms))

    for token in _path_tokens(text) or [text]:
        for candidate in _normalisations(token, cwd):
            for raw, forms in roots:
                for root in forms:
                    if candidate == root or root in candidate.parents:
                        return raw
    return None


def _write_target(value: str, settings: Settings) -> Decision | None:
    """Guard the two write destinations the sandbox does not cover.

    The bubblewrap boundary only constrains Bash; `Edit` and `Write` are
    executed by the CLI itself and land wherever the service uid can write.
    Two shapes are worth intercepting, and neither is caught by a path deny
    rule -- F27's `//`-anchored syntax needs an absolute path, and a repo's
    `.claude/` directory can appear anywhere under cwd.
    """
    forms = _normalisations(value, settings.workspace)
    for form in forms:
        if form.name in _SETTINGS_NAMES and form.parent.name == ".claude":
            return Decision(
                Tier.DENY, "不允许改写 .claude 设置文件（allow 规则会绕过审批）"
            )

    roots = [Path(os.path.normpath(str(settings.workspace)))]
    with suppress(OSError, RuntimeError):
        roots.append(settings.workspace.resolve())

    # Every form has to land inside, not just one: a symlink planted in the
    # workspace normalises to an inside path lexically and an outside path
    # after resolution, and it is the second one that gets written.
    def contained(form: Path) -> bool:
        return any(root == form or root in form.parents for root in roots)

    if not all(contained(form) for form in forms):
        return Decision(Tier.ASK, f"写入 workspace 之外的路径 {forms[0]}")
    return None


def classify(tool: str, tool_input: dict[str, Any], settings: Settings) -> Decision:
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        leaked = secret_path_hit(command, settings.secret_paths, cwd=settings.workspace)
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
        if not isinstance(value, str):
            continue
        leaked = secret_path_hit(value, settings.secret_paths, cwd=settings.workspace)
        if leaked:
            return Decision(Tier.DENY, f"路径位于受保护的凭据目录 {leaked}")
        if tool in _WRITE_TOOLS:
            verdict = _write_target(value, settings)
            if verdict is not None:
                return verdict

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


def path_rule(tool: str, path: Path) -> str:
    """A path deny rule the CLI actually honours.

    The leading `//` is load-bearing (F27). `Read(/abs/**)` with a single
    slash and `Read(**/.ssh/**)` with no anchor both parse fine, match
    nothing, and leak silently -- so this spelling is centralised here rather
    than formatted at each call site.
    """
    return f"{tool}(//{str(path).lstrip('/')})"


def denied_tools(settings: Settings) -> list[str]:
    """The `disallowed_tools` list. Also denies file tools on secret paths."""
    rules = list(HARD_DENY)
    for raw in settings.secret_paths:
        expanded = Path(raw).expanduser()
        for tool in ("Read", "Edit", "Write", "NotebookEdit"):
            rules.append(path_rule(tool, expanded / "**"))
            rules.append(path_rule(tool, expanded))
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
