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
import os
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
    #
    # `--proc /proc` is not decoration. It is the step that actually fails, and
    # it fails for a reason nothing else here would catch: any option that
    # over-mounts part of /proc -- `ProtectKernelTunables=yes` binds /proc/sys
    # read-only -- makes /proc no longer "fully visible", and the kernel then
    # refuses to let an unprivileged user namespace mount a fresh procfs at
    # all. Probing without it passes on a host where every real sandboxed
    # command dies (F30).
    try:
        proc = subprocess.run(
            ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--proc", "/proc",
             "--die-with-parent", "true"],
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
            "bwrap could not build a sandbox (exit "
            f"{proc.returncode}): {stderr[-1] if stderr else 'no output'}. "
            "Check RestrictNamespaces= lists every namespace, SystemCallFilter= "
            "includes @mount, and -- if the message names /proc -- that nothing "
            "over-mounts part of /proc (ProtectKernelTunables=yes does)."
        )
    else:
        report.notes.append("bwrap namespace probe: ok")
    return report


def check_shell_path(shell: str | None = None) -> PreflightReport:
    """The same two binaries again, looked up where they are actually looked up.

    `shutil.which` above answers for *this* process. The Bash tool does not
    run in this process: the CLI hands each command to the login shell, and a
    login shell is free to rewrite PATH before anything else happens --
    NixOS's `/etc/zshenv` replaces it outright with the system PATH, so a
    `path =` in a systemd unit reaches the server and nothing it spawns
    through a shell.

    Untested, that gap costs one `command not found: bwrap` per Bash call
    forever. It fails closed, so nothing escapes -- but the model reads the
    127 as "the sandbox is broken", asks for `dangerouslyDisableSandbox`
    instead, and the user gets trained to approve exactly the thing the
    sandbox exists to prevent.
    """
    report = PreflightReport(ok=True)
    shell = shell or os.environ.get("SHELL") or ""
    if not shell:
        report.notes.append("SHELL unset; skipping the login-shell PATH check")
        return report

    missing: list[str] = []
    for binary in REQUIRED_BINARIES:
        try:
            proc = subprocess.run(
                [shell, "-c", f"command -v {binary}"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.notes.append(f"could not probe {shell}: {exc}")
            return report
        if proc.returncode != 0:
            missing.append(binary)

    if missing:
        report.ok = False
        report.problems.append(
            f"{shell} cannot find {', '.join(missing)}, though this process can. "
            "The Bash tool runs through the login shell, and this one rewrites PATH "
            "(on NixOS /etc/zshenv replaces it with the system PATH), so every "
            "sandboxed command will die with 'command not found'. Put them on the "
            "system PATH rather than only in the unit's."
        )
    else:
        report.notes.append(f"{shell} resolves the sandbox binaries")
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
    for part in (check_workspace(settings), check_sandbox(), check_shell_path()):
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
