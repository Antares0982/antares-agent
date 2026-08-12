"""Renders `.agent/workspace-index.md` -- the one file the agent reads to learn
what repos exist before deciding which to touch.

Not injected into the system prompt, on purpose: the system prompt sits in the
cache prefix, the index grows with the workspace, and most tasks never need it.
The root CLAUDE.md carries a one-line pointer instead and the agent pays a
single Read when it actually cares (see 01-workspace-manifest.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest, Repo

POINTER = "本目录是多仓库工作区。动手前先读 `.agent/workspace-index.md` 了解仓库划分与跨仓库约束。"
_POINTER_MARK = "<!-- antares-agent:workspace-pointer -->"

_KIND_LABEL = {
    "http": "HTTP 调用",
    "library": "库依赖",
    "schema": "共享 schema",
    "build": "构建依赖",
    "deploy": "部署",
}


class DuplicateSkillError(Exception):
    """Two repos ship a skill with the same name.

    F2: the loser is shadowed silently and there is no working syntax to
    disambiguate, so this has to be a hard error rather than a warning.
    """


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    repo: str
    path: Path


def scan_skills(repo: Repo, root: Path) -> list[Skill]:
    """Read `<repo>/.claude/skills/*/SKILL.md` frontmatter."""
    skills_dir = repo.abspath(root) / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    found: list[Skill] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta = _frontmatter(skill_md)
        found.append(
            Skill(
                name=str(meta.get("name") or skill_md.parent.name),
                description=str(meta.get("description") or "").strip(),
                repo=repo.name,
                path=skill_md,
            )
        )
    return found


def scan_all_skills(manifest: Manifest) -> dict[str, list[Skill]]:
    """Skills per repo, raising if any name is claimed twice (F2)."""
    by_repo: dict[str, list[Skill]] = {}
    owners: dict[str, Skill] = {}
    clashes: list[str] = []
    for repo in manifest.repos:
        skills = scan_skills(repo, manifest.root)
        by_repo[repo.name] = skills
        for skill in skills:
            prior = owners.get(skill.name)
            if prior is not None:
                clashes.append(
                    f"skill {skill.name!r} declared in both {prior.repo!r} ({prior.path}) "
                    f"and {skill.repo!r} ({skill.path})"
                )
            else:
                owners[skill.name] = skill
    if clashes:
        raise DuplicateSkillError(
            "duplicate skill names shadow each other silently and cannot be qualified:\n"
            + "\n".join(f"  - {c}" for c in clashes)
        )
    return by_repo


def render(manifest: Manifest, skills: dict[str, list[Skill]] | None = None) -> str:
    if skills is None:
        skills = scan_all_skills(manifest)

    out: list[str] = ["# 工作区索引", ""]

    if not manifest.repos:
        out += ["工作区尚未在 `workspace.toml` 中登记任何仓库。", ""]
    else:
        out += ["## 仓库", ""]
        for repo in manifest.repos:
            out += [f"### {repo.name}  (`{repo.path}/`)", ""]
            if repo.description:
                out += [repo.description.strip(), ""]
            repo_skills = skills.get(repo.name) or []
            if repo_skills:
                out += ["可用 skill（进入该目录后自动可用）："]
                out += [
                    f"- `{s.name}`" + (f" — {_one_line(s.description)}" if s.description else "")
                    for s in repo_skills
                ]
                out += [""]

    if manifest.relations:
        out += ["## 跨仓库约束", ""]
        for rel in manifest.relations:
            label = _KIND_LABEL.get(rel.kind, rel.kind)
            head = f"- **{rel.src} → {rel.dst}** ({label})"
            if rel.contract:
                head += f"，契约见 `{rel.contract}`"
            out.append(head)
            if rel.note:
                out += [f"  {line}" for line in rel.note.strip().splitlines()]
        out += [""]

    return "\n".join(out).rstrip() + "\n"


def write(manifest: Manifest, skills: dict[str, list[Skill]] | None = None) -> Path:
    """Regenerate the index and make sure the root CLAUDE.md points at it.

    Called when a thread is created; existing threads keep the index they
    started with.
    """
    target = manifest.scratch_dir / "workspace-index.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(manifest, skills), encoding="utf-8")
    ensure_pointer(manifest.root)
    return target


def ensure_pointer(root: Path) -> None:
    """Idempotently keep the pointer line in the workspace CLAUDE.md.

    Anything the user wrote by hand is preserved; only the marked block is
    ours to rewrite.
    """
    claude_md = root / "CLAUDE.md"
    block = f"{_POINTER_MARK}\n{POINTER}\n"
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    if _POINTER_MARK in existing:
        updated = re.sub(
            rf"{re.escape(_POINTER_MARK)}\n.*?(?=\n\n|\Z)", block.rstrip(), existing, flags=re.S
        )
    else:
        updated = block + ("\n" + existing.lstrip() if existing.strip() else "")
    if updated != existing:
        claude_md.write_text(updated, encoding="utf-8")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _frontmatter(path: Path) -> dict[str, str]:
    """Minimal YAML frontmatter reader for SKILL.md.

    Handles `key: value`, quoted values, and `|`/`>` blocks -- which is all
    SKILL.md actually uses. Not worth a yaml dependency on a Pi.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    _, _, rest = text.partition("\n")
    body, sep, _ = rest.partition("\n---")
    if not sep:
        return {}

    meta: dict[str, str] = {}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            continue
        key, sep2, value = line.partition(":")
        if not sep2:
            continue
        key, value = key.strip(), value.strip()
        if value in {"|", ">", "|-", ">-"}:
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i].strip())
                i += 1
            value = ("\n" if value.startswith("|") else " ").join(block).strip()
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    return meta
