"""Profiles: the cold half of a thread's configuration.

A profile is fixed when the thread is created and never changes (D6). The
system prompt sits in the cache prefix, so editing it mid-thread invalidates
both the system and message layers; `/new deep` is cheaper and clearer than
switching. Permission mode is the exception -- it never reaches the model, so
it stays hot (D7).

The orchestration text lives in a file rather than a string constant because
it is the asset that will be edited most often, and because it needs to be
tunable per provider: F26 found sonnet holds back from fanning out where
deepseek-v4-pro reaches for it eagerly.

Profiles carry no repo list. Capabilities are discovered lazily (F1) and the
repo picture comes from the index file, so one profile works for any
workspace and adding a repo never touches profile config (D9).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from typing import Any, Literal

from .config import Settings

log = logging.getLogger(__name__)

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

ORCHESTRATION = """\
你是多仓库工作区的编排者。

按 **上下文体积 × 耦合度** 切分任务，而不是按仓库边界：

- 调研阶段体积大、耦合低 → 用 Agent 工具并行扇出，每个仓库一个 `Explore`，
  在 prompt 里点名该仓库的路径
- 定契约阶段体积小、耦合极高 → 你自己做，不要外包
- 修改阶段 → 契约写进 `.agent/contract.md` 之后才可并行
- 验证阶段 → 用未参与修改的新鲜上下文

交接一律用文件，不要用散文复述：调研 agent 写 `.agent/findings-<repo>.md`，
你综合成 `.agent/contract.md`，修改 agent 只需被指向该文件的相应章节。
subagent 拿不到你的对话历史，只能拿到 Agent 工具的 prompt 字符串。

**任务小就自己做。** 只涉及一个仓库、或改动不超过几个文件时，扇出的成本高于收益。
"""

_BUILTIN: dict[str, dict[str, Any]] = {
    "quick": {
        "description": "日常小改动。不扇出，直接改。",
        "permission_mode": "acceptEdits",
        "effort": "low",
        "append": "",
    },
    "deep": {
        "description": "跨仓库任务。先出计划，再动手。",
        "permission_mode": "plan",
        "effort": "high",
        "append": ORCHESTRATION,
    },
}


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    #: Appended to the `claude_code` preset, never replacing it.
    append: str = ""
    permission_mode: PermissionMode = "acceptEdits"
    model: str | None = None
    effort: Effort | None = None
    max_turns: int | None = None


def materialise(settings: Settings) -> None:
    """Write the built-in profiles to disk once, so they can be edited."""
    directory = settings.profiles_dir
    directory.mkdir(parents=True, exist_ok=True)
    for name, spec in _BUILTIN.items():
        toml_path = directory / f"{name}.toml"
        if toml_path.exists():
            continue
        lines = [
            f'description = "{spec["description"]}"',
            f'permission_mode = "{spec["permission_mode"]}"',
            f'effort = "{spec["effort"]}"',
        ]
        if spec["append"]:
            append_path = directory / f"{name}.md"
            append_path.write_text(spec["append"], encoding="utf-8")
            lines.append(f'append_file = "{append_path.name}"')
        toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load(settings: Settings) -> dict[str, Profile]:
    """Disk profiles, falling back to the built-ins for anything absent."""
    profiles = {name: _from_spec(name, spec) for name, spec in _BUILTIN.items()}

    directory = settings.profiles_dir
    if not directory.is_dir():
        return profiles

    for path in sorted(directory.glob("*.toml")):
        name = path.stem
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("ignoring profile %s: %s", path, exc)
            continue

        append = str(raw.get("append", ""))
        append_file = raw.get("append_file")
        if append_file:
            candidate = directory / str(append_file)
            try:
                append = candidate.read_text(encoding="utf-8")
            except OSError as exc:
                log.error("profile %s: cannot read %s: %s", name, candidate, exc)

        profiles[name] = Profile(
            name=name,
            description=str(raw.get("description", "")),
            append=append,
            permission_mode=raw.get("permission_mode", "acceptEdits"),
            model=raw.get("model"),
            effort=raw.get("effort"),
            max_turns=raw.get("max_turns"),
        )
    return profiles


def _from_spec(name: str, spec: dict[str, Any]) -> Profile:
    return Profile(
        name=name,
        description=spec["description"],
        append=spec["append"],
        permission_mode=spec["permission_mode"],
        effort=spec["effort"],
    )
