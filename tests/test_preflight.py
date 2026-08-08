"""The login shell's PATH is not this process's PATH.

Measured on the Pi: the unit's `path =` carried bubblewrap, `shutil.which`
found it, preflight passed -- and every sandboxed Bash call still died with
`zsh:1: command not found: bwrap`, because /etc/zshenv replaces PATH with the
system one before running anything.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from antares_agent.preflight import REQUIRED_BINARIES, check_shell_path


def _executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _shell(tmp_path: Path, path_line: str) -> str:
    """A stand-in for a login shell, with one rc line that sets PATH."""
    return str(
        _executable(
            tmp_path / "loginshell",
            f'#!/bin/sh\n{path_line}\nexec /bin/sh "$@"\n',
        )
    )


def test_a_shell_that_rewrites_path_is_caught(tmp_path: Path) -> None:
    report = check_shell_path(_shell(tmp_path, "export PATH=/nonexistent"))
    assert not report.ok
    assert all(b in report.problems[0] for b in REQUIRED_BINARIES)


def test_a_shell_that_keeps_them_passes(tmp_path: Path) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for binary in REQUIRED_BINARIES:
        _executable(binaries / binary, "#!/bin/sh\nexit 0\n")

    report = check_shell_path(_shell(tmp_path, f'export PATH="{binaries}:$PATH"'))
    assert report.ok, report.problems


def test_no_shell_is_not_a_failure(monkeypatch) -> None:
    # A container with no SHELL set is not evidence of anything; the check
    # only speaks about shells it can actually run.
    monkeypatch.delenv("SHELL", raising=False)
    assert check_shell_path().ok
    assert os.environ.get("SHELL") is None
