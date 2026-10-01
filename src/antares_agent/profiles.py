from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from typing import Any, Literal

from .config import Settings

log = logging.getLogger(__name__)

PermissionMode = Literal["plan", "auto"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

ORCHESTRATION = """你是多仓库工作区的编排者。
先读取工作区索引及相关仓库的 AGENTS.md。
复杂任务先调研、明确契约，再修改和验证；小任务直接完成。
可以派遣子 Agent；汇总前必须等待子任务完成。
需要用户决定时用普通中文消息提问并结束本轮，等用户回复后继续。
新技能写到工作区或仓库的 .agents/skills/<name>/SKILL.md。
"""

_BUILTIN: dict[str, dict[str, Any]] = {
    "quick": {
        "description": "日常小改动，直接执行。",
        "permission_mode": "auto",
        "model": None,
        "effort": "low",
        "append": "",
    },
    "deep": {
        "description": "跨仓库任务。先出计划，再动手。",
        "permission_mode": "plan",
        "model": None,
        "effort": "high",
        "append": "",
    },
}


@dataclass(frozen=True)
class Profile:
    name: str
    description: str = ""
    append: str = ""
    permission_mode: PermissionMode = "auto"
    model: str | None = None
    effort: Effort | None = None


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

        base = profiles.get(name) or Profile(name=name)

        append = base.append
        append_file = raw.get("append_file")
        if append_file:
            candidate = directory / str(append_file)
            try:
                append = candidate.read_text(encoding="utf-8")
            except OSError as exc:
                log.error("profile %s: cannot read %s: %s", name, candidate, exc)
        elif "append" in raw:
            append = str(raw["append"])

        mode = raw.get("permission_mode", base.permission_mode)
        if mode not in {"plan", "auto"}:
            raise ValueError(f"{path}: 模式必须是 plan 或 auto，请迁移旧 profile")
        profiles[name] = Profile(
            name=name,
            description=str(raw.get("description", base.description)),
            append=append,
            permission_mode=mode,
            model=raw.get("model", base.model),
            effort=raw.get("effort", base.effort),
        )
    return profiles


def _from_spec(name: str, spec: dict[str, Any]) -> Profile:
    return Profile(
        name=name,
        description=spec["description"],
        append=spec["append"],
        permission_mode=spec["permission_mode"],
        model=spec["model"],
        effort=spec["effort"],
    )
