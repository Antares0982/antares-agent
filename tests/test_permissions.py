from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from antares_agent import permissions
from antares_agent.config import Settings
from antares_agent.permissions import Tier, classify
from antares_agent.shlex_split import matches, split_commands

SETTINGS = Settings()


def tier(command: str, **extra: Any) -> Tier:
    return classify("Bash", {"command": command, **extra}, SETTINGS).tier


# --- shell decomposition -------------------------------------------------
# F24 showed the CLI decomposes these for its own deny rules; our ask tier
# has to match or `true; git push` walks straight through.


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "true; git push origin main",
        "cd repo-a && git push",
        "bash -c 'git push origin main'",
        "GIT_SSH_COMMAND=ssh git push",
        "make build && bash -c \"cd api && git push\"",
        "sh -c 'true; git push'",
    ],
)
def test_ask_tier_survives_shell_structure(command: str) -> None:
    assert tier(command) is Tier.ASK


def test_flags_match_anywhere_in_the_command() -> None:
    assert matches(["git", "reset", "--hard", "HEAD~1"], ["git", "reset", "--hard"])
    assert matches(["git", "clean", "-xfd"], ["git", "clean", "-f"])  # bundled short flags
    assert not matches(["git", "reset", "--soft", "HEAD~1"], ["git", "reset", "--hard"])


def test_absolute_paths_are_matched_by_basename() -> None:
    assert matches(["/usr/bin/git", "push"], ["git", "push"])


def test_unparseable_input_does_not_vanish() -> None:
    # An unbalanced quote must not yield "no commands found" -- that would be a
    # clean bill of health for something we could not read.
    assert split_commands("git push 'unterminated") == [["git", "push", "'unterminated"]]


# --- tiers ---------------------------------------------------------------


def test_ordinary_commands_run_sandboxed_without_a_prompt() -> None:
    assert tier("pytest -q") is Tier.SANDBOXED
    assert tier("git status && git diff") is Tier.SANDBOXED
    assert tier("rg TODO src/") is Tier.SANDBOXED


def test_escaping_the_sandbox_always_asks() -> None:
    assert tier("echo hi", dangerouslyDisableSandbox=True) is Tier.ASK


def test_network_pseudo_tool_asks() -> None:
    d = classify(permissions.NETWORK_TOOL, {"host": "github.com"}, SETTINGS)
    assert d.tier is Tier.ASK and "github.com" in d.reason


def test_secret_paths_are_denied_not_asked() -> None:
    # F19: the sandbox blocks writes outside cwd but not reads.
    for command in ["cat ~/.ssh/id_rsa", "cat $HOME/.ssh/id_rsa", "grep -r . /run/secrets"]:
        d = classify("Bash", {"command": command}, SETTINGS)
        assert d.tier is Tier.DENY, command

    d = classify("Read", {"file_path": str(Path.home() / ".aws/credentials")}, SETTINGS)
    assert d.tier is Tier.DENY


def test_dot_dot_cannot_walk_back_into_a_secret_path() -> None:
    # F28: the CLI's own Bash path analysis misses this spelling -- it denied
    # `cat ~/.ssh/id_rsa` but allowed `cat ~/.ssh/../.ssh/id_rsa`. Our pass
    # normalises, so it must not have the same blind spot.
    home = Path.home()
    for command in [
        f"cat {home}/.ssh/../.ssh/id_rsa",
        f"cat {home}/Documents/../.ssh/id_rsa",
        "cat ~/.ssh/./id_rsa",
    ]:
        assert classify("Bash", {"command": command}, SETTINGS).tier is Tier.DENY, command


def test_secret_path_as_a_flag_value_is_caught() -> None:
    command = f"ssh -i {Path.home()}/.ssh/id_rsa host"
    assert classify("Bash", {"command": command}, SETTINGS).tier is Tier.DENY


def test_relative_paths_resolve_against_the_workspace(tmp_path: Path) -> None:
    secrets = tmp_path / "vault"
    secrets.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = Settings(workspace=workspace, secret_paths=(str(secrets),))

    assert classify("Bash", {"command": "cat ../vault/key"}, settings).tier is Tier.DENY
    assert classify("Bash", {"command": "cat ./notes.md"}, settings).tier is Tier.SANDBOXED


def test_symlink_into_a_secret_path_is_caught(tmp_path: Path) -> None:
    secrets = tmp_path / "vault"
    secrets.mkdir()
    (secrets / "key").write_text("x")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "shortcut").symlink_to(secrets)
    settings = Settings(workspace=workspace, secret_paths=(str(secrets),))

    # Planting the symlink is a write *inside* cwd, which the sandbox allows.
    assert classify("Bash", {"command": "cat shortcut/key"}, settings).tier is Tier.DENY


def test_writing_a_claude_settings_file_is_denied(tmp_path: Path) -> None:
    """The self-escalation path: allow rules are read before `can_use_tool`.

    Both the workspace root and a nested repo count -- a repo's `.claude/`
    appears wherever the repo does, so this cannot be a fixed deny rule.
    """
    workspace = tmp_path / "ws"
    (workspace / "api" / ".claude").mkdir(parents=True)
    settings = Settings(workspace=workspace)

    for target in (
        workspace / ".claude" / "settings.json",
        workspace / "api" / ".claude" / "settings.local.json",
        workspace / "api" / ".." / ".claude" / "settings.json",
    ):
        assert classify("Write", {"file_path": str(target)}, settings).tier is Tier.DENY, target

    # Skills and other project files under .claude stay writable.
    skill = workspace / "api" / ".claude" / "skills" / "build.md"
    assert classify("Write", {"file_path": str(skill)}, settings).tier is Tier.SANDBOXED


def test_writing_outside_the_workspace_is_escalated(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (workspace / "shortcut").symlink_to(outside)
    settings = Settings(workspace=workspace)

    assert classify("Write", {"file_path": "api/notes.md"}, settings).tier is Tier.SANDBOXED
    assert classify(
        "Edit", {"file_path": str(outside / "x.py")}, settings
    ).tier is Tier.ASK
    # Lexically inside, actually outside. The write follows the symlink.
    assert classify(
        "Write", {"file_path": "shortcut/x.py"}, settings
    ).tier is Tier.ASK


def test_path_rule_keeps_the_double_slash() -> None:
    # F27: `Read(/abs/**)` and `Read(**/.ssh/**)` both match nothing at all.
    assert permissions.path_rule("Read", Path("/home/x/.ssh")) == "Read(//home/x/.ssh)"


def test_read_of_a_normal_file_is_sandboxed() -> None:
    assert classify("Read", {"file_path": "api/routes.py"}, SETTINGS).tier is Tier.SANDBOXED


# --- wiring constraints --------------------------------------------------


def test_deny_list_covers_the_shapes_the_classifier_let_through() -> None:
    # F17: permission_mode="auto" silently allowed both of these.
    rules = permissions.denied_tools(SETTINGS)
    assert "Bash(git push --force:*)" in rules
    assert "Bash(chmod -R:*)" in rules
    assert any(".ssh" in r and r.startswith("Read(") for r in rules)


def test_will_sandbox_matches_classify() -> None:
    # The event translator labels tool.call from will_sandbox() without waiting
    # for the permission round-trip; the two must not disagree.
    cases: list[tuple[str, dict[str, Any]]] = [
        ("Bash", {"command": "pytest"}),
        ("Bash", {"command": "echo", "dangerouslyDisableSandbox": True}),
        ("Read", {"file_path": "a.py"}),
        (permissions.NETWORK_TOOL, {"host": "x"}),
    ]
    for tool, payload in cases:
        sandboxed = permissions.will_sandbox(tool, payload)
        assert sandboxed == (classify(tool, payload, SETTINGS).tier is Tier.SANDBOXED)


async def test_arbiter_allows_sandboxed_silently_and_escalates_the_rest() -> None:
    asked: list[str] = []

    async def ask(tool: str, payload: dict[str, Any], reason: str, ctx: Any) -> permissions.Verdict:
        asked.append(reason)
        return permissions.Verdict(allow=False, message="nope")

    arbiter = permissions.Arbiter(SETTINGS, ask)

    allow = await arbiter("Bash", {"command": "pytest -q"}, None)  # type: ignore[arg-type]
    assert allow.behavior == "allow" and asked == []

    deny = await arbiter("Bash", {"command": "git push"}, None)  # type: ignore[arg-type]
    assert deny.behavior == "deny" and len(asked) == 1

    hard = await arbiter("Bash", {"command": "cat ~/.ssh/id_rsa"}, None)  # type: ignore[arg-type]
    assert hard.behavior == "deny" and len(asked) == 1  # never reached the user
