from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from .manifest import load


def migrate(root: Path, apply=False):
    root = root.resolve()
    manifest = load(root)
    moves = []
    for folder in dict.fromkeys([root, *(r.abspath(root) for r in manifest.repos)]):
        instruction = folder / "CLAUDE.md"
        if instruction.exists():
            moves.append((instruction, folder / "AGENTS.md"))
        skills = folder / ".claude" / "skills"
        if skills.is_dir():
            moves.extend(
                (skill, folder / ".agents" / "skills" / skill.name)
                for skill in sorted(skills.iterdir())
            )
    problems = []
    targets = set()
    for source, target in moves:
        if target.exists() or target.is_symlink() or target in targets:
            problems.append(f"目标已存在：{target}")
        targets.add(target)
        paths = [source, target]
        if source.is_dir():
            paths.extend(source.rglob("*"))
        if any(not path.resolve().is_relative_to(root) for path in paths):
            problems.append(f"路径超出工作区：{source} → {target}")
    if problems:
        raise ValueError("迁移未执行：\n" + "\n".join(problems))
    if not apply or not moves:
        return moves, None
    backup = root / ".agent" / "migrations" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    if not backup.resolve().is_relative_to(root):
        raise ValueError("备份路径超出工作区")
    backup.mkdir(parents=True, exist_ok=False)
    for source, _ in moves:
        saved = backup / source.relative_to(root)
        saved.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, saved, symlinks=True)
        else:
            shutil.copy2(source, saved, follow_symlinks=False)
    completed = []
    try:
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ValueError(f"目标已存在：{target}")
            source.rename(target)
            completed.append((source, target))
    except Exception:
        for source, target in reversed(completed):
            target.rename(source)
        raise
    return moves, backup
