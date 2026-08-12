"""`workspace.toml` -- what repos exist and how they constrain each other.

The manifest deliberately does *not* register capabilities. Skills and
CLAUDE.md under a repo are discovered lazily the moment the agent touches that
subtree (F1/F4), so listing them here would only pay their context cost up
front. What the SDK cannot supply at all is the cross-repo picture: a subagent
receives nothing but the Agent tool's prompt string, so `[[relation]]` is the
one thing that has to be written down.

See docs/design/01-workspace-manifest.md.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RELATION_KINDS = frozenset({"http", "library", "schema", "build", "deploy"})

_NAME_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")


class ManifestError(Exception):
    """Raised with every problem found, not just the first one."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("workspace.toml is invalid:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Repo:
    name: str
    path: str  # relative to the workspace root
    description: str

    def abspath(self, root: Path) -> Path:
        return root / self.path


@dataclass(frozen=True)
class Relation:
    src: str  # `from` in TOML; `from` is a Python keyword
    dst: str
    kind: str
    note: str
    contract: str | None = None


@dataclass(frozen=True)
class Manifest:
    root: Path
    scratch: str
    default_profile: str
    repos: tuple[Repo, ...]
    relations: tuple[Relation, ...]

    @property
    def scratch_dir(self) -> Path:
        return self.root / self.scratch

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def repo_for_path(self, path: str | Path) -> Repo | None:
        """Which repo does a path belong to? Longest prefix wins.

        Used to attribute SSE events and diffs. Accepts absolute paths and
        paths relative to the workspace root; anything outside returns None.
        """
        p = Path(path)
        if p.is_absolute():
            try:
                p = p.relative_to(self.root)
            except ValueError:
                return None
        parts = p.parts
        best: Repo | None = None
        for r in self.repos:
            rparts = Path(r.path).parts
            if parts[: len(rparts)] == rparts and (
                best is None or len(rparts) > len(Path(best.path).parts)
            ):
                best = r
        return best


def empty(root: Path) -> Manifest:
    """A workspace with no manifest is legal -- it just has no index to read."""
    return Manifest(root=root, scratch=".agent", default_profile="quick", repos=(), relations=())


def load(root: Path, path: Path | None = None) -> Manifest:
    """Parse and validate the manifest. Missing file yields an empty manifest."""
    path = path or root / "workspace.toml"
    if not path.exists():
        return empty(root)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError([f"{path}: {exc}"]) from exc
    return parse(raw, root)


def parse(raw: dict[str, Any], root: Path) -> Manifest:
    problems: list[str] = []
    ws = raw.get("workspace") or {}

    repos: list[Repo] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw.get("repo") or []):
        where = f"[[repo]] #{i + 1}"
        name = str(entry.get("name", "")).strip()
        rel = str(entry.get("path", "")).strip()
        desc = str(entry.get("description", "")).strip()

        if not name:
            problems.append(f"{where}: missing `name`")
        elif not _NAME_RE.match(name):
            problems.append(f"{where}: name {name!r} must match {_NAME_RE.pattern}")
        elif name in seen:
            problems.append(f"{where}: duplicate name {name!r}")
        seen.add(name)

        if not rel:
            problems.append(f"{where} ({name}): missing `path`")
        else:
            err = _check_contained(rel, root, where=f"{where} ({name}) path")
            if err:
                problems.append(err)
            elif not (root / rel).is_dir():
                problems.append(f"{where} ({name}): {root / rel} does not exist")

        if not desc:
            problems.append(
                f"{where} ({name}): missing `description` -- say *when* to reach for "
                "this repo, not what it is"
            )

        if name and rel:
            repos.append(Repo(name=name, path=rel.strip("/"), description=desc))

    relations: list[Relation] = []
    for i, entry in enumerate(raw.get("relation") or []):
        where = f"[[relation]] #{i + 1}"
        src, dst = str(entry.get("from", "")).strip(), str(entry.get("to", "")).strip()
        kind = str(entry.get("kind", "")).strip()
        note = str(entry.get("note", "")).strip()
        contract = entry.get("contract")

        for label, value in (("from", src), ("to", dst)):
            if not value:
                problems.append(f"{where}: missing `{label}`")
            elif value not in seen:
                problems.append(f"{where}: `{label}` = {value!r} is not a declared repo")

        if kind not in RELATION_KINDS:
            problems.append(f"{where}: kind {kind!r} not one of {sorted(RELATION_KINDS)}")
        if not note:
            problems.append(
                f"{where}: missing `note` -- this is the main input to cross-repo "
                "decisions and has no other source"
            )
        if contract:
            err = _check_contained(str(contract), root, where=f"{where} contract")
            if err:
                problems.append(err)

        if src and dst and kind in RELATION_KINDS:
            relations.append(
                Relation(
                    src=src,
                    dst=dst,
                    kind=kind,
                    note=note,
                    contract=str(contract) if contract else None,
                )
            )

    if problems:
        raise ManifestError(problems)

    return Manifest(
        root=root,
        scratch=str(ws.get("scratch", ".agent")),
        default_profile=str(ws.get("default_profile", "quick")),
        repos=tuple(repos),
        relations=tuple(relations),
    )


def _check_contained(rel: str, root: Path, *, where: str) -> str | None:
    """D2: everything the agent works on must live under the workspace root.

    Not a security boundary -- the sandbox is -- but a path outside the root
    silently loses lazy skill discovery and hierarchical CLAUDE.md, so it is
    worth refusing loudly.
    """
    if Path(rel).is_absolute():
        return f"{where}: {rel!r} must be relative to the workspace root"
    resolved = (root / rel).resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        return f"{where}: {rel!r} escapes the workspace root"
    return None
