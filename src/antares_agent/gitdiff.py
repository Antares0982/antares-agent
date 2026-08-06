"""Per-repo diff at the end of a turn.

Deliberately not derived from `Edit`/`Write` tool events: `sed -i`, a python
script, or `git apply` change files just as thoroughly and emit no edit event
at all. Tool events are for watching progress; this is for checking what
actually happened.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .manifest import Manifest

log = logging.getLogger(__name__)

PATCH_LIMIT = 60_000


@dataclass(frozen=True)
class RepoDiff:
    repo: str
    stat: str
    patch: str
    truncated: bool


async def _git(cwd: str, *args: str, timeout: float = 20.0) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.pager=cat",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("git unavailable in %s: %s", cwd, exc)
        return None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        log.warning("git %s timed out in %s", args[0], cwd)
        return None
    if proc.returncode != 0:
        log.debug("git %s failed in %s: %s", args[0], cwd, err.decode(errors="replace")[:200])
        return None
    return out.decode(errors="replace")


async def collect(manifest: Manifest) -> list[RepoDiff]:
    """Diff every declared repo that is a git working tree with changes.

    `--no-ext-diff` and an explicit `--` guard against a repo-local diff
    driver or a path that looks like a revision.
    """
    results = await asyncio.gather(
        *(_one(manifest, repo.name) for repo in manifest.repos), return_exceptions=True
    )
    out: list[RepoDiff] = []
    for item in results:
        if isinstance(item, RepoDiff):
            out.append(item)
        elif isinstance(item, BaseException):
            log.warning("diff failed: %s", item)
    return out


async def _one(manifest: Manifest, name: str) -> RepoDiff | None:
    repo = manifest.repo(name)
    if repo is None:
        return None
    cwd = str(repo.abspath(manifest.root))

    if await _git(cwd, "rev-parse", "--is-inside-work-tree") is None:
        return None

    stat = await _git(cwd, "diff", "--no-ext-diff", "--stat", "HEAD", "--")
    if not stat or not stat.strip():
        return None

    patch = await _git(cwd, "diff", "--no-ext-diff", "HEAD", "--") or ""
    truncated = len(patch) > PATCH_LIMIT
    return RepoDiff(
        repo=name,
        stat=stat.strip().splitlines()[-1].strip(),
        patch=patch[:PATCH_LIMIT],
        truncated=truncated,
    )
