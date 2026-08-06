"""Startup self-check.

F23: when `bwrap` or `socat` is missing from PATH the CLI does not fail --- it
prints one warning line and runs every Bash call unconfined, while
`sandbox.enabled` stays True. That is the only failure mode in this design
where the configuration looks entirely correct and the security boundary
simply is not there, so the process refuses to start until it has watched the
mechanism work.

The probe drives bubblewrap directly rather than asking the model to attempt a
forbidden write: it costs no tokens, it runs in milliseconds, and it tests the
mechanism instead of the model's willingness to try.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

REQUIRED_BINARIES = ("bwrap", "socat")


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class PreflightReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_sandbox() -> PreflightReport:
    report = PreflightReport(ok=True)

    for binary in REQUIRED_BINARIES:
        found = shutil.which(binary)
        if found is None:
            report.ok = False
            report.problems.append(
                f"{binary} not found on PATH -- the SDK sandbox fails open without it (F23)"
            )
        else:
            report.notes.append(f"{binary}: {found}")

    if not report.ok:
        return report

    # Namespace creation is what systemd hardening most often breaks:
    # `RestrictNamespaces=user` means *only* user, and @system-service omits
    # mount/pivot_root (see 00-overview.md).
    try:
        proc = subprocess.run(
            ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--die-with-parent", "true"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.ok = False
        report.problems.append(f"bwrap probe could not run: {exc}")
        return report

    if proc.returncode != 0:
        report.ok = False
        stderr = proc.stderr.decode(errors="replace").strip().splitlines()
        report.problems.append(
            "bwrap could not create namespaces (exit "
            f"{proc.returncode}): {stderr[-1] if stderr else 'no output'}. "
            "Check RestrictNamespaces= lists every namespace and SystemCallFilter= includes @mount."
        )
    else:
        report.notes.append("bwrap namespace probe: ok")
    return report


def check_workspace(settings: Settings) -> PreflightReport:
    report = PreflightReport(ok=True)
    if not settings.workspace.is_dir():
        report.ok = False
        report.problems.append(f"workspace {settings.workspace} does not exist")
    if settings.cli_path is not None and not Path(settings.cli_path).exists():
        report.ok = False
        report.problems.append(f"cli_path {settings.cli_path} does not exist")
    if settings.cli_path is None:
        report.notes.append(
            "cli_path unset -- the SDK will prefer its bundled binary over PATH (F9); "
            "pin it explicitly in production"
        )
    return report


def run(settings: Settings) -> PreflightReport:
    """Raise unless the sandbox is demonstrably working."""
    combined = PreflightReport(ok=True)
    for part in (check_workspace(settings), check_sandbox()):
        combined.ok = combined.ok and part.ok
        combined.problems += part.problems
        combined.notes += part.notes

    for note in combined.notes:
        log.info("preflight: %s", note)

    if combined.ok:
        return combined

    detail = "\n".join(f"  - {p}" for p in combined.problems)
    if settings.require_sandbox:
        raise SandboxUnavailable(f"preflight failed:\n{detail}")
    log.warning("preflight failed but ANTARES_REQUIRE_SANDBOX is off:\n%s", detail)
    return combined
